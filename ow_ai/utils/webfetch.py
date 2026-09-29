# -*- coding: utf-8 -*-
"""SSRF-safe URL fetching for the web-search tools.

Odoo-free by design: no ORM, no ``env``, so it can be unit tested without a
database and reused by any tool that needs to fetch a URL. Three pieces:

- ``guard_url``: validates and normalises a URL, resolving its host and
  rejecting anything that points at a private/loopback/link-local/multicast/
  reserved/unspecified address (including the cloud metadata address and
  IPv4-mapped IPv6). This is the SSRF guard; it is re-run on every redirect
  hop inside ``fetch`` so a public URL can never redirect us into a private
  network. The URL is parsed the way ``requests`` parses it (urllib3) and
  rebuilt around the ASCII host that was checked, so the guard, the DNS
  lookup and the ``Host`` header all see one identical host name.
- ``fetch``: connects each request to an address the guard checked (never
  lets the HTTP library resolve the host name again, which a DNS rebinding
  could answer with an internal address; the next checked address only
  when no connection could be opened), with the host name as ``Host``
  header and, for https, as TLS server name and certificate name;
  follows redirects manually (never trusts ``requests``' own redirect
  handling, since that would skip the guard on each hop), streams the body
  up to a byte cap within one overall deadline (a watchdog shuts the
  connection down when it passes), and maps transport failures onto a
  small set of stable error codes.
- ``html_to_text``: turns a fetched HTML (or plain text/JSON) body into a
  ``(title, text)`` pair for the LLM, stripping chrome (nav/header/footer/…)
  and keeping a light block structure (headings, list bullets, paragraph
  breaks).
"""
from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import requests
from lxml import html as lxml_html
from requests.adapters import HTTPAdapter
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError, ReadTimeoutError
from urllib3.util import parse_url

PUBLIC_PORTS = (80, 443, 8080, 8443)

_DEFAULT_PORTS = {'http': 80, 'https': 443}
_MAX_REDIRECTS = 5
_CONNECT_TIMEOUT = 10   # seconds one checked address gets to accept the connection
_CHUNK_SIZE = 8192
_METADATA_ADDRESS = '169.254.169.254'
_CGNAT_NETWORK = ipaddress.ip_network('100.64.0.0/10')
_NAT64_NETWORK = ipaddress.ip_network('64:ff9b::/96')
_ASCII_HOST_RE = re.compile(r'[a-z0-9.-]+')
_HTML_CONTENT_TYPES = frozenset({'text/html', 'application/xhtml+xml'})
_ALLOWED_CONTENT_TYPES = _HTML_CONTENT_TYPES | {'text/plain', 'application/json'}
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_PATH_SAFE_CHARS = "/%:@&=+$,;~!*'()"
_BLOCK_TAGS = ('script', 'style', 'noscript', 'iframe', 'svg', 'nav', 'header', 'footer', 'aside', 'form',
               'template')
_NEWLINE_AFTER_TAGS = ('p', 'div', 'section', 'li', 'tr', 'br', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'pre',
                        'blockquote')
_HEADING_TAGS = ('h1', 'h2', 'h3', 'h4', 'h5', 'h6')


class WebFetchError(Exception):
    """Raised by ``guard_url``/``fetch``/``html_to_text`` for any failure.

    ``code`` is one of ``bad_url``, ``blocked``, ``too_many_redirects``,
    ``timeout``, ``unreachable`` (no connection: host not resolved,
    refused, TLS error), ``http_error``, ``unsupported_content``. A body larger than
    the size cap is cut (``FetchResult.truncated``), never an error.
    ``status`` carries the HTTP status for ``http_error``, ``None``
    otherwise. The message is always user-readable and never echoes
    response headers (which may carry sensitive proxy/internal data) nor
    the HTTP library's own error text (which names the address connected to).
    """

    def __init__(self, code: str, message: str, *, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    content_type: str
    charset: str | None
    body: bytes
    truncated: bool


def _bad_url(message: str) -> WebFetchError:
    return WebFetchError('bad_url', message)


def _blocked(message: str) -> WebFetchError:
    return WebFetchError('blocked', message)


def _unreachable() -> WebFetchError:
    return WebFetchError('unreachable', "The page could not be reached.")


def _deadline_passed(timeout: float) -> WebFetchError:
    return WebFetchError('timeout', f"The page did not load within {timeout:g} seconds.")


def _is_blocked_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(address, 'ipv4_mapped', None)
    if mapped is not None:
        address = mapped

    if isinstance(address, ipaddress.IPv6Address) and address in _NAT64_NETWORK:
        # NAT64 (64:ff9b::/96): the embedded IPv4 is the address's low 32 bits.
        embedded = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
        return _is_blocked_address(embedded)

    if str(address) == _METADATA_ADDRESS:
        return True
    if isinstance(address, ipaddress.IPv4Address) and address in _CGNAT_NETWORK:
        return True

    return bool(address.is_private or address.is_loopback or address.is_link_local
                or address.is_multicast or address.is_reserved or address.is_unspecified
                or not address.is_global)


def _ascii_host(host: str) -> str:
    """``host`` exactly as the request will carry it: lower-case ASCII, or a bracketed
    IPv6 literal; anything else is a ``bad_url``.

    urllib3's ``parse_url`` has already IDNA-encoded an internationalised
    http(s) host (IDNA 2008, like ``requests``), so a host that is still not
    ASCII here fails the character check below.
    """
    if host.startswith('[') and host.endswith(']'):
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError as exc:
            raise _bad_url(f"Invalid IPv6 address: {host!r}") from exc
        if '%' in host:
            raise _bad_url("IPv6 zone identifiers are not allowed")
        return host.lower()
    host = host.lower()
    if not _ASCII_HOST_RE.fullmatch(host):
        raise _bad_url(f"Invalid host: {host!r}")
    return host


def _parse_url(url: str):
    """The syntax half of ``guard_url``: ``(scheme, host, port, path, query)`` or ``WebFetchError('bad_url')``.

    Parsed with urllib3's ``parse_url``, the parser ``requests`` itself uses;
    ``host`` is the ASCII form from ``_ascii_host``. A backslash is refused
    outright: parsers disagree on whether it ends the host.
    """
    if '\\' in url:
        raise _bad_url("URLs with a backslash are not allowed")
    try:
        parsed = parse_url(url)
    except ValueError as exc:   # urllib3's LocationParseError is a ValueError
        raise _bad_url(f"Could not parse URL: {exc}") from exc

    scheme = (parsed.scheme or '').lower()
    if scheme not in ('http', 'https'):
        raise _bad_url(f"Unsupported URL scheme: {parsed.scheme!r}")
    if parsed.auth is not None:
        raise _bad_url("URLs with embedded credentials are not allowed")
    if not parsed.host:
        raise _bad_url("URL has no host")
    host = _ascii_host(parsed.host)

    port = parsed.port if parsed.port is not None else _DEFAULT_PORTS[scheme]
    if port not in PUBLIC_PORTS:
        raise _bad_url(f"Port {port} is not allowed")

    return scheme, host, port, parsed.path or '', parsed.query or ''


def _unsplit(scheme: str, host: str, port: int, path: str, query: str) -> str:
    netloc = host if port == _DEFAULT_PORTS[scheme] else f'{host}:{port}'
    return urlunsplit((scheme, netloc, quote(path, safe=_PATH_SAFE_CHARS), query, ''))


def _ip_literal(host: str) -> str | None:
    """The address of an IP-literal ``host`` (IPv6 without its brackets), else None."""
    if host.startswith('['):
        return host[1:-1]
    try:
        return str(ipaddress.IPv4Address(host))
    except ValueError:
        return None


def _numeric_address(host: str):
    """The address a numeric ``host`` stands for without DNS: an IP literal, or a
    legacy IPv4 form the system resolver reads as one (``2130706433``,
    ``127.1``); ``None`` for a host name."""
    literal = _ip_literal(host)
    if literal is not None:
        try:
            return ipaddress.ip_address(literal)
        except ValueError:
            return None
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except OSError:
        return None


def is_blocked_literal_host(url: str) -> bool:
    """True when ``url``'s host is an IP address ``guard_url`` refuses, checked without DNS.

    A host name is never blocked here (only ``guard_url`` resolves it), nor
    is a URL ``guard_url`` would reject for its syntax: those are ``False``.
    """
    try:
        host = _parse_url(url)[1]
    except WebFetchError:
        return False
    address = _numeric_address(host)
    return address is not None and _is_blocked_address(address)


def normalise_url(url: str) -> str:
    """``url`` in ``guard_url``'s normalised form, with the same syntax checks but no DNS lookup.

    For comparing URLs (e.g. an allow-list), never as a substitute for
    ``guard_url`` before a request.
    """
    return _unsplit(*_parse_url(url))


def guard_url(url: str, *, resolver=socket.getaddrinfo) -> str:
    """Validate ``url`` and return it normalised, or raise ``WebFetchError``.

    Rejects anything but ``http``/``https``, userinfo, missing/invalid
    hosts, non-standard ports, and hosts that resolve (via ``resolver``) to
    any private/loopback/link-local/multicast/reserved/unspecified address
    or the cloud metadata address — checking *every* resolved address, so a
    host that resolves to a mix of public and private addresses is blocked.
    An IP-literal host is checked itself, without a lookup; a host that does
    not resolve is ``unreachable``. The returned URL carries the very host
    name that was resolved (``fetch`` connects to the checked addresses
    themselves, see ``_guarded_get``).
    """
    return _guard(url, resolver=resolver)[0]


def _guard(url: str, *, resolver=socket.getaddrinfo) -> tuple[str, str, int, tuple[str, ...]]:
    """``guard_url``'s check, returning ``(normalised URL, host, port, addresses)``.

    ``addresses`` are the addresses the host resolved to, every one checked,
    in the resolver's order without repeats (an IP-literal host's own
    address, without brackets): the only addresses a request to ``url``
    may connect to.
    """
    scheme, host, port, path, query = _parse_url(url)
    literal = _ip_literal(host)
    if literal is not None:
        raw_addresses = [literal]
    else:
        try:
            addrinfo = resolver(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise _unreachable() from exc
        if not addrinfo:
            raise _unreachable()
        raw_addresses = [info[4][0] for info in addrinfo]

    addresses = []
    for raw_address in raw_addresses:
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as exc:
            raise _bad_url(f"Host resolved to an invalid address: {raw_address!r}") from exc
        if _is_blocked_address(address):
            raise _blocked(f"Host {host!r} resolves to a disallowed address")
        addresses.append(address)

    pinned = (literal,) if literal is not None else tuple(dict.fromkeys(str(address) for address in addresses))
    return _unsplit(scheme, host, port, path, query), host, port, pinned


class _PinnedHttpsAdapter(HTTPAdapter):
    """An https adapter whose connections send ``server_hostname`` as TLS
    server name (SNI) and check the certificate against it, whatever
    address the request URL names (``_guarded_get`` requests the checked
    address itself). Certificate verification stays ``requests``' own
    (``verify=True``: CA bundle, ``CERT_REQUIRED``)."""

    __attrs__ = [*HTTPAdapter.__attrs__, '_server_hostname']

    def __init__(self, server_hostname: str, **kwargs):
        self._server_hostname = server_hostname   # before super(): its __init__ calls init_poolmanager
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        super().init_poolmanager(connections, maxsize, block=block, server_hostname=self._server_hostname,
                                 assert_hostname=self._server_hostname, **pool_kwargs)


def _never_connected(exc: requests.ConnectionError) -> bool:
    """True when ``exc`` means no connection was ever opened (refused, host
    or network unreachable, no route, connect timeout), so nothing was sent;
    never for a TLS, read or HTTP failure."""
    error = exc.args[0] if exc.args else None
    return isinstance(getattr(error, 'reason', error), (NewConnectionError, ConnectTimeoutError))


def _guarded_get(session, url: str, *, deadline: float, timeout: float, user_agent: str, resolver):
    """Guard ``url``, then request it from the addresses the guard checked.

    Connecting by host name would let the HTTP library resolve it once
    more, and a DNS rebinding could answer that lookup with an internal
    address: the request URL names a checked address instead (IPv6 in
    brackets), the host name goes in the ``Host`` header and, for https, a
    ``_PinnedHttpsAdapter`` mounted for exactly that address and port sends
    it as TLS server name and checks the certificate against it. The
    checked addresses are tried in turn, like the HTTP library itself would,
    but only while no connection could be opened (``_never_connected``);
    each gets at most ``_CONNECT_TIMEOUT`` seconds to accept it, all within
    the deadline. When none does, the page is ``unreachable``. Returns the
    normalised URL (with the host name, for redirects and ``final_url``)
    and the response.
    """
    guarded_url, host, port, addresses = (
        _guard(url, resolver=resolver) if resolver is not None else _guard(url))
    parts = urlsplit(guarded_url)
    # As urllib3 sends it: `example.com.` is looked up as written, but certificates and
    # virtual hosts name `example.com`.
    sent_host = host.rstrip('.')
    headers = {
        'Host': sent_host if port == _DEFAULT_PORTS[parts.scheme] else f'{sent_host}:{port}',
        'User-Agent': user_agent,
        'Accept': 'text/html,application/xhtml+xml,text/plain,application/json;q=0.9,*/*;q=0.1',
    }
    failure = None
    for address in addresses:
        left = _time_left(deadline, timeout)   # after the DNS lookup and any address tried: their time counts too
        pinned_host = f'[{address}]' if ':' in address else address
        pinned_netloc = pinned_host if port == _DEFAULT_PORTS[parts.scheme] else f'{pinned_host}:{port}'
        if parts.scheme == 'https':
            # One adapter per checked address and port (the longest prefix wins in `requests`), mounted
            # anew on every attempt: a redirect may reach the same address under another name. One it
            # replaces holds no open connection (every response is closed before the next hop).
            session.mount(f'https://{pinned_netloc}/', _PinnedHttpsAdapter(sent_host.strip('[]')))
        try:
            response = session.get(
                urlunsplit(parts._replace(netloc=pinned_netloc)),
                headers=headers,
                timeout=(min(left, _CONNECT_TIMEOUT), left),
                stream=True,
                allow_redirects=False,
            )
        except requests.ConnectionError as exc:
            if not _never_connected(exc):
                raise
            failure = exc   # nothing was sent: the next checked address may still answer
            continue
        return guarded_url, response
    raise _unreachable() from failure


def _time_left(deadline: float, timeout: float) -> float:
    """Seconds until ``deadline`` (never more than ``timeout``); ``WebFetchError('timeout')`` once it is reached."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise _deadline_passed(timeout)
    return left


def _response_socket(response):
    """The socket a streamed ``requests`` response reads its body from, or ``None``
    where it is not reachable. urllib3 2.x keeps it as ``raw._connection.sock``;
    for a response that ends when the connection closes (``Connection: close``,
    HTTP/1.0) ``http.client`` hands the socket over to the response itself,
    reachable as ``raw._fp.fp.raw._sock``."""
    raw = getattr(response, 'raw', None)
    candidates = (
        getattr(getattr(raw, '_connection', None), 'sock', None),
        getattr(getattr(getattr(getattr(raw, '_fp', None), 'fp', None), 'raw', None), '_sock', None),
    )
    return next((sock for sock in candidates if isinstance(sock, socket.socket)), None)


class _Watchdog:
    """Shuts ``sock`` down once ``seconds`` have passed, so a read blocked on it
    returns at once (``fired`` tells what ended it). Without a socket it does
    nothing: the caller's per-chunk deadline check still applies."""

    def __init__(self, sock, seconds: float):
        self.fired = False
        self._sock = sock
        self._timer = None
        if sock is not None:
            self._timer = threading.Timer(seconds, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self):
        self.fired = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:   # already closed
            pass

    def cancel(self):
        if self._timer is not None:
            self._timer.cancel()


def _request_error(exc: requests.RequestException) -> WebFetchError:
    """``exc`` as a ``WebFetchError``: ``timeout``, ``unreachable`` or ``http_error``.

    With a fixed text: the library's own message names the address connected to.
    """
    # requests reports a read timeout while streaming the body as a ConnectionError around urllib3's error.
    if isinstance(exc, requests.Timeout) or (exc.args and isinstance(exc.args[0], ReadTimeoutError)):
        return WebFetchError('timeout', "The page did not answer in time.")
    if isinstance(exc, requests.ConnectionError):   # DNS failure, connection refused, TLS error
        return _unreachable()
    return WebFetchError('http_error', "The page could not be fetched.")


def fetch(url: str, *, timeout: float, max_bytes: int = 5_000_000, user_agent: str, session=None,
          resolver=None) -> FetchResult:
    """Fetch ``url``, following up to 5 redirects, each hop guarded against SSRF.

    ``timeout`` (seconds) is one deadline for the whole read, DNS lookups,
    every hop and the body included: each request gets only the time left
    as its connect/read timeout (a wait for data, not a total), and a
    watchdog shuts the connection down when the deadline passes while the
    body is read, so the body is cut there (``timeout``). A read thus ends
    at the deadline plus at most one socket wait; only a server that sends
    its response headers a byte at a time can stretch it further. Where the
    response's socket is not reachable, the deadline is checked at every
    body chunk instead. Streams the body and stops at ``max_bytes``
    (``FetchResult.truncated`` is set rather than raising). Only HTML/XHTML/plain-text/JSON content types are
    accepted; anything else raises ``unsupported_content``. Without a
    ``session``, a fresh one is used and closed afterwards; it ignores the
    server's environment (no proxies, no ``~/.netrc`` credentials).
    """
    if session is not None:
        return _fetch(session, url, timeout=timeout, max_bytes=max_bytes, user_agent=user_agent, resolver=resolver)
    own_session = requests.Session()
    own_session.trust_env = False
    try:
        return _fetch(own_session, url, timeout=timeout, max_bytes=max_bytes, user_agent=user_agent,
                      resolver=resolver)
    finally:
        own_session.close()


def _fetch(session, url: str, *, timeout: float, max_bytes: int, user_agent: str, resolver) -> FetchResult:
    deadline = time.monotonic() + timeout
    current_url = url
    response = None
    try:
        for hop in range(_MAX_REDIRECTS + 1):
            current_url, response = _guarded_get(
                session, current_url, deadline=deadline, timeout=timeout, user_agent=user_agent,
                resolver=resolver)

            if response.status_code in _REDIRECT_STATUSES:
                location = response.headers.get('Location')
                response.close()
                if not location:
                    raise WebFetchError('http_error', "Redirect response has no Location header",
                                         status=response.status_code)
                if hop >= _MAX_REDIRECTS:
                    raise WebFetchError('too_many_redirects', "Too many redirects")
                current_url = urljoin(current_url, location)
                continue

            break
        else:  # pragma: no cover - loop always breaks or raises
            raise WebFetchError('too_many_redirects', "Too many redirects")

        if not (200 <= response.status_code < 300):
            status = response.status_code
            response.close()
            raise WebFetchError('http_error', f"HTTP {status}", status=status)

        content_type_header = response.headers.get('Content-Type', '')
        content_type = content_type_header.split(';', 1)[0].strip().lower()
        charset = None
        for param in content_type_header.split(';')[1:]:
            key, _, value = param.strip().partition('=')
            if key.lower() == 'charset':
                charset = value.strip().strip('"\'') or None

        if content_type not in _ALLOWED_CONTENT_TYPES:
            response.close()
            raise WebFetchError('unsupported_content', f"Unsupported content type: {content_type!r}")

        chunks = []
        total = 0
        truncated = False
        watchdog = None
        try:
            watchdog = _Watchdog(_response_socket(response), _time_left(deadline, timeout))
            for chunk in response.iter_content(_CHUNK_SIZE):
                _time_left(deadline, timeout)
                if not chunk:
                    continue
                chunks.append(chunk)
                total += len(chunk)
                if total >= max_bytes:
                    truncated = True
                    break
        except Exception as exc:
            if watchdog is not None and watchdog.fired:   # the read broke off because the watchdog shut it down
                raise _deadline_passed(timeout) from exc
            raise
        finally:
            if watchdog is not None:
                watchdog.cancel()
            response.close()
        if watchdog.fired:   # a shut-down connection can also look like the end of the body
            raise _deadline_passed(timeout)

        body = b''.join(chunks)
        if truncated:
            body = body[:max_bytes]

        return FetchResult(
            url=url,
            final_url=current_url,
            status=response.status_code,
            content_type=content_type,
            charset=charset,
            body=body,
            truncated=truncated,
        )
    except requests.RequestException as exc:
        raise _request_error(exc) from exc


def _looks_like_html(body: bytes) -> bool:
    return b'<' in body[:512]


def _decode(body: bytes, charset: str | None) -> str:
    if charset:
        try:
            return body.decode(charset, errors='strict')
        except (LookupError, UnicodeDecodeError):
            pass
    return body.decode('utf-8', errors='replace')


def _try_strict_decode(body: bytes, charset: str | None) -> str | None:
    """Return ``body`` decoded with ``charset``, or ``None`` if that charset is unknown or
    does not decode ``body`` cleanly (a ``UnicodeDecodeError`` in strict mode) — i.e. it is
    trusted only when it is demonstrably the right charset, never used to paper over mismatches
    with ``errors='replace'`` (that would silently corrupt the accented/multi-byte text it was
    supposed to decode; letting lxml sniff the real charset from bytes does better)."""
    if not charset:
        return None
    try:
        body.decode(charset, errors='strict')
    except (LookupError, UnicodeDecodeError):
        return None
    return body.decode(charset, errors='replace')


def _norm_text(text: str) -> str:
    """Collapse a raw text/tail node's internal whitespace (incl. newlines) to single spaces.

    Applied to every text/tail fragment as it is appended, so that source
    formatting (indentation, line wraps inside a paragraph) never produces
    spurious newlines in the output — only the ``\\n`` markers ``_walk``
    inserts for block boundaries do.
    """
    return re.sub(r'\s+', ' ', text)


def _collapse_whitespace(text: str) -> str:
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _extract_title(tree) -> str:
    title_elements = tree.xpath('//title')
    if not title_elements:
        return ''
    return (title_elements[0].text_content() or '').strip()


def _select_root(tree):
    body = tree.find('.//body')
    if body is None:
        body = tree
    for tag in ('main', 'article'):
        candidate = body.find(f'.//{tag}')
        if candidate is not None and len(candidate.text_content().strip()) > 200:
            return candidate
    return body


def _walk(element, parts: list) -> None:
    tag = element.tag if isinstance(element.tag, str) else None

    if tag in ('li',):
        parts.append('- ')
    if tag in _HEADING_TAGS:
        parts.append('\n')

    if element.text:
        parts.append(_norm_text(element.text))

    for child in element:
        if not isinstance(child.tag, str):
            # lxml Comment/ProcessingInstruction: its "text" is markup content
            # (e.g. the comment body), never real page text — skip it without
            # descending, but keep its tail, which is real sibling text.
            if child.tail:
                parts.append(_norm_text(child.tail))
            continue
        _walk(child, parts)
        if child.tail:
            parts.append(_norm_text(child.tail))

    if tag in _NEWLINE_AFTER_TAGS:
        parts.append('\n')
    if tag in _HEADING_TAGS:
        parts.append('\n')


def html_to_text(body: bytes, *, charset: str | None = None, content_type: str | None = None) -> tuple[str, str]:
    """Turn a fetched body into ``(title, text)``.

    ``content_type`` (the response's) decides: ``text/html`` and
    ``application/xhtml+xml`` are parsed as HTML, any other type (plain
    text, JSON, …) is returned as-is with an empty title. Without one, a
    ``<`` in the first 512 bytes means HTML. HTML bodies are decoded with
    ``charset`` (falling back to lxml's own byte-level parsing when that
    charset does not apply), stripped of non-content chrome (scripts,
    nav/header/footer/aside/forms/…, keeping the text that follows them),
    and walked to produce a lightly structured plain-text rendering
    (heading/paragraph/list breaks).
    """
    if content_type:
        is_html = content_type.split(';', 1)[0].strip().lower() in _HTML_CONTENT_TYPES
    else:
        is_html = _looks_like_html(body)
    if not is_html:
        return '', _decode(body, charset).strip()

    tree = None
    text_str = _try_strict_decode(body, charset)
    if text_str is not None:
        try:
            tree = lxml_html.fromstring(text_str)
        except Exception:  # noqa: BLE001 - malformed markup: fall back to byte-level parsing below
            tree = None

    if tree is None:
        # No charset given, an unknown charset name, or the declared charset didn't actually
        # decode the body (wrong/lying Content-Type charset): let lxml parse the raw bytes and
        # sniff the real charset itself (<meta charset>/BOM), rather than trust a bad guess.
        try:
            tree = lxml_html.fromstring(body)
        except Exception:  # noqa: BLE001 - truly broken markup: no title, decoded text
            return '', _decode(body, charset).strip()

    title = _extract_title(tree)

    for element in tree.xpath('//' + '|//'.join(_BLOCK_TAGS)):
        if element.getparent() is not None:
            element.drop_tree()   # unlike ``parent.remove``, keeps the text after the element (its tail)

    root = _select_root(tree)
    parts: list = []
    _walk(root, parts)
    text = ''.join(parts)
    text = _collapse_whitespace(text)
    return title, text
