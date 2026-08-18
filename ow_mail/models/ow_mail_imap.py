"""Shared IMAP helpers: MIME parsing, BODYSTRUCTURE walk, live FETCH.

All live-read paths go through here so controllers stay thin.
"""
import email
import html as _stdlib_html
import imaplib
import logging
import re
import pytz
from contextlib import contextmanager
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

_logger = logging.getLogger(__name__)

# Tags dropped entirely — they can execute, navigate, or load remote content
# that the iframe sandbox does not fully neutralise (iframe/form nav, link/meta
# remote fetch, object/embed plugin handlers).
_DROP_TAGS = frozenset({
    "script", "iframe", "object", "embed", "meta", "link",
    "form", "base", "frame", "frameset",
})

# Attributes that fetch or navigate to a URL; each is sanitised against
# _DANGER_SCHEME_RX and inspected for remote-content detection.
_URL_ATTRS = (
    "href", "src", "action", "formaction", "data", "poster",
    "background", "ping", "{http://www.w3.org/1999/xlink}href",
)

_DANGER_SCHEME_RX = re.compile(r"^\s*(?:javascript|vbscript|file):", re.I)
# data: is only allowed for image payloads (inline pixels). Block text/html,
# application/*, etc. which can carry script in Content-Type.
_DATA_NONIMAGE_RX = re.compile(r"^\s*data:(?!image/)", re.I)
_REMOTE_URL_RX = re.compile(r"^\s*(?:https?:)?//", re.I)


def imap_mbox_quote(name):
    """Return an IMAP-quoted mailbox name (RFC 3501 quoted-string).

    Escapes backslashes and double quotes before wrapping, so a server that
    returns a mailbox named ``evil"/INBOX`` via LIST can't be used to break
    out of the quoted form in a subsequent SELECT/MOVE/COPY/APPEND. Control
    characters (CR/LF/NUL) are forbidden in IMAP atoms and mailbox names;
    we reject rather than silently strip so a malformed name surfaces.
    """
    if name is None:
        raise ValueError("IMAP mailbox name is None")
    s = str(name)
    if any(ch in s for ch in ("\r", "\n", "\x00")):
        raise ValueError("IMAP mailbox name contains control characters")
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

# CSS inside <style> blocks and style="" attrs. The iframe sandbox blocks JS
# execution but cannot stop @import / url() from phoning home, so we still
# need to scan for remote loading and strip the IE-era XSS vectors.
_CSS_EXPRESSION_RX = re.compile(r"expression\s*\([^)]*\)", re.I)
_CSS_MOZ_BINDING_RX = re.compile(r"-moz-binding\s*:[^;]*;?", re.I)
_CSS_BEHAVIOR_RX = re.compile(r"behavior\s*:\s*url[^;]*;?", re.I)
_CSS_URL_JS_RX = re.compile(
    r"""url\s*\(\s*['"]?\s*(?:javascript|vbscript):[^)]*\)""", re.I,
)
_CSS_IMPORT_REMOTE_RX = re.compile(
    r"""@import\s+(?:url\()?\s*['"]?(?:https?:)?//""", re.I,
)
_CSS_URL_REMOTE_RX = re.compile(r"""url\s*\(\s*['"]?\s*(?:https?:)?//""", re.I)


def decode_header_value(value):
    """Decode an RFC 2047 MIME-encoded header value to a plain string.

    Headers like Subject, From, and To can contain encoded words of the form
    ``=?charset?encoding?text?=`` (RFC 2047).  Python's ``decode_header``
    returns a list of ``(bytes, charset)`` pairs; ``make_header`` reassembles
    them into a unicode string.  ``errors='replace'`` (the default via
    ``make_header``) tolerates malformed encodings emitted by broken mailers
    rather than raising an exception and discarding the whole header.
    """
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _addrs(header_value):
    """Parse a comma-separated RFC 2822 address header into a display string.

    ``email.utils.getaddresses`` correctly handles display names that contain
    commas (e.g. ``"Smith, John" <john@example.com>``); a naïve ``split(',')``
    would split inside the quoted name and produce garbage.  Entries with an
    empty address component are filtered out to avoid trailing delimiters when
    only display names are present.
    """
    return ", ".join(
        a for _n, a in getaddresses([decode_header_value(header_value or "")]) if a
    )


def parse_envelope(msg):
    """Return headers dict from an email.message.Message."""
    from_name, from_email = parseaddr(decode_header_value(msg.get("From")))
    try:
        dt = parsedate_to_datetime(msg.get("Date")) if msg.get("Date") else None
        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
    except Exception:
        dt = None
    return {
        "subject": decode_header_value(msg.get("Subject")),
        "from_name": from_name or from_email,
        "from_email": (from_email or "").lower(),
        "to": _addrs(msg.get("To")),
        "cc": _addrs(msg.get("Cc")),
        "reply_to": decode_header_value(msg.get("Reply-To") or ""),
        "message_id": (msg.get("Message-ID") or "").strip(),
        "in_reply_to": (msg.get("In-Reply-To") or "").strip(),
        "references": (msg.get("References") or "").strip(),
        "date": dt,
    }


_MAX_MIME_DEPTH = 32  # Bound recursion on adversarial / pathological multipart trees.


def walk_parts(msg):
    """Yield (part, section_path, kind) where kind in (text, html, inline, attachment).

    `section_path` is IMAP BODY[<section>] notation (e.g. "1.2"). For non-
    multipart messages the single part is numbered "1".

    Recursion is capped at ``_MAX_MIME_DEPTH`` so a malicious sender cannot
    DoS a worker with a deeply nested multipart payload (default Python
    recursion limit ~1000 → uncaught RecursionError aborts the request).
    """
    def _walk(part, prefix, depth):
        if depth > _MAX_MIME_DEPTH:
            _logger.warning(
                "ow_mail: MIME tree exceeded depth %s; truncating walk",
                _MAX_MIME_DEPTH,
            )
            return
        if part.is_multipart():
            for i, child in enumerate(part.get_payload(), start=1):
                yield from _walk(child, prefix + [str(i)], depth + 1)
            return
        section = ".".join(prefix) if prefix else "1"
        ctype = part.get_content_type()
        disp = str(part.get("Content-Disposition", "")).lower()
        filename = decode_header_value(part.get_filename()) or ""
        cid = (part.get("Content-ID") or "").strip("<>").strip()
        if ctype == "text/plain" and "attachment" not in disp and not filename:
            kind = "text"
        elif ctype == "text/html" and "attachment" not in disp and not filename:
            kind = "html"
        elif cid and ("inline" in disp or not filename):
            kind = "inline"
        else:
            kind = "attachment"
        yield part, section, kind

    if msg.is_multipart():
        for i, child in enumerate(msg.get_payload(), start=1):
            yield from _walk(child, [str(i)], 1)
    else:
        yield from _walk(msg, [], 1)


def extract_parts(msg):
    """Return (text, html, inline[{cid,section,mimetype,filename}], atts[...])"""
    text = html = ""
    inline = []
    atts = []
    for part, section, kind in walk_parts(msg):
        ctype = part.get_content_type()
        filename = decode_header_value(part.get_filename()) or ""
        cid = (part.get("Content-ID") or "").strip("<>").strip()
        if kind == "text" and not text:
            try:
                text = part.get_content()
            except Exception:
                payload = part.get_payload(decode=True) or b""
                text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        elif kind == "html" and not html:
            try:
                html = part.get_content()
            except Exception:
                payload = part.get_payload(decode=True) or b""
                html = payload.decode(part.get_content_charset() or "utf-8", "replace")
        elif kind == "inline":
            inline.append({"cid": cid, "section": section, "mimetype": ctype,
                           "filename": filename or cid or "inline"})
        elif kind == "attachment":
            atts.append({"section": section, "mimetype": ctype,
                         "filename": filename or "attachment",
                         "size": len(part.get_payload(decode=True) or b"")})
    return text, html, inline, atts


def _ics_unfold(text):
    """Unfold RFC 5545 continuation lines (lines starting with space/tab)."""
    out = []
    for line in text.splitlines():
        if line and line[0] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _ics_parse_dt(value, params, default_tz=None):
    """Parse an RFC 5545 DTSTART/DTEND value into an ISO 8601 string, or None.

    ICS date/time values come in three forms: bare date (``YYYYMMDD``), local
    date-time (``YYYYMMDDTHHmmss``), and UTC date-time (``YYYYMMDDTHHmmssZ``).
    A ``TZID`` parameter names a timezone for local date-times. If a
    ``default_tz`` is provided, it is used for local date-times without a
    ``TZID`` (floating time). All date-times are converted to UTC.
    Returns ``None`` or the raw value string on parse failure rather than
    raising, so a malformed calendar part does not abort rendering the rest
    of the message.
    """
    if not value:
        return None
    v = value.strip()
    # Date-only (VALUE=DATE): 20260418
    if params.get("VALUE", "").upper() == "DATE" or (len(v) == 8 and v.isdigit()):
        if len(v) == 8 and v.isdigit():
            return f"{v[0:4]}-{v[4:6]}-{v[6:8]}"
    # 20260418T140000 or 20260418T140000Z
    m = re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z)?$", v)
    if not m:
        return v

    y, mo, d, hh, mm, ss, z = m.groups()
    dt = datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss))

    tz_name = params.get("TZID")
    if z:
        dt = pytz.utc.localize(dt)
    elif tz_name:
        try:
            dt = pytz.timezone(tz_name).localize(dt).astimezone(pytz.utc)
        except Exception:
            if default_tz:
                try:
                    dt = pytz.timezone(default_tz).localize(dt).astimezone(pytz.utc)
                except Exception:
                    pass
    elif default_tz:
        try:
            dt = pytz.timezone(default_tz).localize(dt).astimezone(pytz.utc)
        except Exception:
            pass

    return dt.isoformat()


def _ics_unescape(value):
    """Reverse RFC 5545 text-value escaping (``\\,``, ``\\;``, ``\\n``, ``\\\\``)."""
    return (value or "").replace(r"\n", "\n").replace(r"\N", "\n") \
        .replace(r"\,", ",").replace(r"\;", ";").replace(r"\\", "\\")


def _ics_split_property(line):
    """Split one RFC 5545 property line into ``(name, params_dict, value)``.

    ICS property lines have the form ``NAME;PARAM=VAL:value``.  The first
    colon separates the name+parameter block from the value; semicolons within
    the name block separate individual parameters; each parameter is split on
    the first ``=``.  Example: ``DTSTART;TZID=America/New_York:20240101T120000``
    yields ``("DTSTART", {"TZID": "America/New_York"}, "20240101T120000")``.
    """
    if ":" not in line:
        return None, {}, ""
    head, value = line.split(":", 1)
    parts = head.split(";")
    name = parts[0].upper()
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip('"')
    return name, params, value


def extract_invite(msg, default_tz=None):
    """Parse the first text/calendar part into a compact invite dict.

    Returns None if the message has no calendar part.
    """
    ics_part = None
    ics_section = None
    for part, section, _kind in walk_parts(msg):
        if part.get_content_type() == "text/calendar":
            ics_part = part
            ics_section = section
            break
    if not ics_part:
        return None
    try:
        payload = ics_part.get_payload(decode=True) or b""
        text = payload.decode(ics_part.get_content_charset() or "utf-8", "replace")
    except Exception:
        return None

    lines = _ics_unfold(text)
    invite = {
        "method": "",
        "summary": "",
        "dtstart_iso": None,
        "dtend_iso": None,
        "location": "",
        "description": "",
        "organizer_name": "",
        "organizer_email": "",
        "attendees": [],
        "uid": "",
        "section": ics_section,
    }
    in_vevent = False
    for line in lines:
        name, params, value = _ics_split_property(line)
        if name == "BEGIN" and value.upper() == "VEVENT":
            in_vevent = True
        elif name == "END" and value.upper() == "VEVENT":
            break
        elif name == "METHOD" and not in_vevent:
            invite["method"] = value.strip().upper()
        elif in_vevent:
            if name == "SUMMARY":
                invite["summary"] = _ics_unescape(value)
            elif name == "DTSTART":
                invite["dtstart_iso"] = _ics_parse_dt(value, params, default_tz=default_tz)
            elif name == "DTEND":
                invite["dtend_iso"] = _ics_parse_dt(value, params, default_tz=default_tz)
            elif name == "LOCATION":
                invite["location"] = _ics_unescape(value)
            elif name == "DESCRIPTION":
                invite["description"] = _ics_unescape(value)
            elif name == "UID":
                invite["uid"] = value.strip()
            elif name == "ORGANIZER":
                addr = value.strip()
                if addr.lower().startswith("mailto:"):
                    addr = addr[7:]
                invite["organizer_email"] = addr
                invite["organizer_name"] = params.get("CN", "")
            elif name == "ATTENDEE":
                addr = value.strip()
                if addr.lower().startswith("mailto:"):
                    addr = addr[7:]
                invite["attendees"].append({
                    "email": addr,
                    "name": params.get("CN", ""),
                })
    return invite


def _sanitize_css(css):
    """Strip XSS vectors from CSS and detect remote fetches.

    Returns ``(clean_css, has_remote_fetch)``.  Three categories of vector are
    removed: ``url(...)`` pointing to remote origins (loads external resources,
    enables tracking pixels and CSRF probes); ``expression(...)`` (legacy IE
    CSS expression execution); and ``@import`` of remote stylesheets (loads
    arbitrary external CSS including further expressions or url() calls).
    ``-moz-binding`` and ``behavior:url(...)`` are also stripped as historical
    XSS vectors in Gecko/Trident.  Called on both inline ``style=""`` attributes
    and ``<style>`` blocks during ``sanitize_and_detect``.
    """
    if not css:
        return "", False
    css = _CSS_EXPRESSION_RX.sub("", css)
    css = _CSS_MOZ_BINDING_RX.sub("", css)
    css = _CSS_BEHAVIOR_RX.sub("", css)
    css = _CSS_URL_JS_RX.sub("url()", css)
    has_remote = bool(
        _CSS_IMPORT_REMOTE_RX.search(css) or _CSS_URL_REMOTE_RX.search(css)
    )
    return css, has_remote


def sanitize_and_detect(html_body):
    """Sanitise an email HTML body and report whether it pulls remote content.

    Uses ``lxml.html.fromstring`` with ``recover=True`` + ``huge_tree=True``
    (NOT ``lxml.html.clean.Cleaner``, which silently returns an empty string
    on Zimbra-wrapped emails with 200+ nested divs — see commit a0eb413).
    A single DOM walk:

      * drops dangerous tags (script/iframe/object/embed/meta/link/form/base)
      * strips every ``on*`` event attribute
      * rewrites ``javascript:``/``vbscript:``/non-image ``data:`` URLs
      * scrubs CSS ``expression()``, ``-moz-binding``, ``behavior:url``,
        ``url(javascript:…)`` from both ``style=""`` and ``<style>`` blocks
      * detects remote loading in any URL attr, inline CSS, or ``<style>``
        (so the safe-mode banner can trigger)

    Inline ``style=""`` attrs and ``<style>`` blocks are preserved otherwise
    — marketing emails (Zimbra/table-based layouts) rely on them.
    """
    if not html_body:
        return "", False
    try:
        from lxml import etree
        from lxml import html as lh
    except ImportError:
        # Fail-secure: lxml is the sanitiser; without it we cannot strip
        # script/iframe/on*-handlers safely. Returning the body verbatim
        # would be an XSS escalation, so fall back to HTML-escaped text
        # wrapped in <pre> — readable but inert. lxml is part of Odoo's
        # baseline, so this branch should never fire in practice; the
        # error log makes the regression visible if it ever does.
        _logger.error("lxml unavailable; falling back to text-escaped rendering")
        return f"<pre>{_stdlib_html.escape(html_body)}</pre>", False

    parser = lh.HTMLParser(recover=True, huge_tree=True, remove_comments=True,
                           remove_pis=True)
    # Always wrap in a known container so we can walk uniformly and serialise
    # inner HTML without an injected <html><body>. fragment_fromstring drops
    # <head> content when the input is a full document, so we strip outer
    # <html>/<body>/<head> tags first and keep their children verbatim —
    # styles in <head> still apply because the iframe template provides the
    # outer document structure.
    source = html_body
    for _ in range(4):
        stripped = source.strip()
        lower = stripped[:6].lower()
        if lower.startswith("<!doct"):
            # Strip leading doctype
            gt = stripped.find(">")
            source = stripped[gt + 1:] if gt != -1 else stripped
            continue
        if lower.startswith("<html"):
            # Unwrap <html>…</html>
            open_end = stripped.find(">")
            close_start = stripped.lower().rfind("</html>")
            if open_end != -1 and close_start != -1:
                source = stripped[open_end + 1:close_start]
                continue
        break

    try:
        root = lh.fragment_fromstring(
            source, create_parent="div", parser=parser,
        )
    except (etree.ParserError, etree.XMLSyntaxError, ValueError):
        # Truly unparseable — safer to drop than to pass through.
        return "", False

    has_remote = False
    to_drop = []
    for el in root.iter():
        if not isinstance(el.tag, str):  # comments/PIs slipped through
            to_drop.append(el)
            continue
        local_tag = el.tag.rsplit("}", 1)[-1].lower()
        if local_tag in _DROP_TAGS and local_tag != "style":
            to_drop.append(el)
            continue
        # Event handler attrs
        for attr_name in list(el.attrib):
            if attr_name.lower().startswith("on"):
                del el.attrib[attr_name]
        # URL-bearing attrs
        for attr_name in _URL_ATTRS:
            if attr_name not in el.attrib:
                continue
            val = el.attrib[attr_name]
            if not val:
                continue
            if _DATA_NONIMAGE_RX.match(val) or _DANGER_SCHEME_RX.match(val):
                del el.attrib[attr_name]
                continue
            # A plain hyperlink is user-initiated navigation, not
            # auto-fetched remote content — it must not trigger the
            # safe-mode banner (nor be neutered by it client-side).
            if _REMOTE_URL_RX.match(val) and not (
                    local_tag == "a" and attr_name == "href"):
                has_remote = True
        # Inline style
        style = el.attrib.get("style")
        if style:
            new_style, style_remote = _sanitize_css(style)
            has_remote = has_remote or style_remote
            if new_style.strip():
                el.attrib["style"] = new_style
            else:
                del el.attrib["style"]
        # <style> block content
        if local_tag == "style" and el.text:
            new_css, css_remote = _sanitize_css(el.text)
            has_remote = has_remote or css_remote
            el.text = new_css

    for el in to_drop:
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)

    # Serialise the wrapper's inner HTML only.
    out = root.text or ""
    for child in root:
        out += lh.tostring(child, encoding="unicode")
    return out, has_remote


# ---------------- IMAP session helper ----------------

@contextmanager
def imap_session(account, folder_path=None, readonly=False):
    """Context manager for a one-shot IMAP connection.

    No connection pooling: every request opens a fresh connection and logs out
    on exit.  This avoids stale-state bugs (e.g. a previous SELECT leaving the
    wrong mailbox open) and matches the stateless HTTP model — simplicity wins
    over the marginal cost of an extra TCP handshake per request.

    ``folder_path`` is quoted with ``imap_mbox_quote`` before being passed to
    SELECT/EXAMINE so mailbox names containing spaces or special characters are
    handled safely.  ``readonly=True`` issues EXAMINE instead of SELECT, which
    does not advance the Recent counter and prevents implicitly marking messages
    as Seen on read-only operations such as BODYSTRUCTURE inspection.

    ``LOGOUT`` is called in the ``finally`` block so the connection is always
    released, even if the body raises.
    """
    conn = account._imap_connect()
    try:
        if folder_path is not None:
            typ, _ = conn.select(imap_mbox_quote(folder_path), readonly=readonly)
            if typ != "OK":
                raise RuntimeError(
                    f"IMAP {'EXAMINE' if readonly else 'SELECT'} failed: {folder_path}"
                )
        yield conn
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def fetch_full(conn, uid):
    """UID FETCH the full body without touching \\Seen.

    Uses ``BODY.PEEK[]`` rather than ``RFC822`` so opening an inline image
    or attachment preview never flips the read-state as a side-effect.
    The ``/ow_mail/message`` endpoint still sets \\Seen explicitly after
    rendering — there, the read-state change is the intended behaviour.

    Returns ``(flags_str, raw_bytes)`` or ``(None, None)`` on error.
    """
    typ, data = conn.uid("FETCH", str(uid), "(FLAGS BODY.PEEK[])")
    if typ != "OK" or not data:
        return None, None
    flags = b""
    raw = b""
    for item in data:
        if isinstance(item, tuple) and len(item) == 2:
            flags = item[0] or b""
            raw = item[1] or b""
            break
    return flags.decode(errors="replace"), raw


def fetch_section(conn, uid, section):
    """Fetch a single BODYSTRUCTURE section via ``BODY.PEEK[<section>]``.

    ``section`` is a MIME part path such as ``"1.2"`` or ``"TEXT"``.  Using a
    section fetch instead of ``fetch_full`` is critical for efficiency: a 20 MB
    email with a 1 KB inline image should transfer only ~1 KB, not the entire
    message.  ``BODY.PEEK`` is used (rather than ``BODY``) so the fetch never
    flips ``\\Seen`` as a side effect.  Returns raw bytes of the part, or
    ``b""`` on error.
    """
    typ, data = conn.uid("FETCH", str(uid), f"(BODY.PEEK[{section}])")
    if typ != "OK" or not data:
        return b""
    for item in data:
        if isinstance(item, tuple) and len(item) == 2:
            return item[1] or b""
    return b""


def fetch_envelopes(conn, uids):
    """UID FETCH ENVELOPE FLAGS BODYSTRUCTURE RFC822.SIZE for list view.

    Returns list of dicts: {uid, subject, from_name, from_email, date, flags, size,
                            has_attachments, keywords[]}.
    """
    if not uids:
        return []
    uid_set = ",".join(str(u) for u in uids)
    results = []
    typ, data = conn.uid(
        "FETCH", uid_set,
        "(UID FLAGS RFC822.SIZE BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS "
        "(SUBJECT FROM TO DATE MESSAGE-ID IN-REPLY-TO REFERENCES)])"
    )
    if typ != "OK" or not data:
        return []
    for item in data:
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        meta = item[0].decode(errors="replace") if isinstance(item[0], bytes) else str(item[0])
        header_bytes = item[1] or b""
        m_uid = re.search(r"UID (\d+)", meta)
        m_flags = re.search(r"FLAGS \(([^)]*)\)", meta)
        m_size = re.search(r"RFC822\.SIZE (\d+)", meta)
        if not m_uid:
            continue
        flags_str = m_flags.group(1) if m_flags else ""
        keywords = [f for f in flags_str.split() if f and not f.startswith("\\")]
        hdrs = email.message_from_bytes(header_bytes)
        env = parse_envelope(hdrs)
        has_att = bool(re.search(r'"attachment"', meta, re.I))
        results.append({
            "uid": int(m_uid.group(1)),
            "subject": env["subject"] or "(no subject)",
            "from_name": env["from_name"],
            "from_email": env["from_email"],
            "to": env["to"],
            "date": env["date"].isoformat() if env["date"] else None,
            "message_id": env.get("message_id", ""),
            "in_reply_to": env.get("in_reply_to", ""),
            "references": env.get("references", ""),
            "flags_seen": "\\Seen" in flags_str,
            "flags_flagged": "\\Flagged" in flags_str,
            "flags_answered": "\\Answered" in flags_str,
            "keywords": keywords,
            "size": int(m_size.group(1)) if m_size else 0,
            "has_attachments": has_att,
        })
    return results


# ---------------- List-view preview snippets ----------------

_PREVIEW_FETCH_BYTES = 2048   # partial-fetch size per message body part
_PREVIEW_MAX_LEN = 140        # characters kept in the snippet

_SNIPPET_DROP_BLOCK_RX = re.compile(
    r"<(script|style|head|title)[^>]*>.*?</\1\s*>", re.I | re.S)
_SNIPPET_TAG_RX = re.compile(r"<[^>]*>?")


def snippet_from_bytes(payload, ctype, cte, charset, max_len=_PREVIEW_MAX_LEN):
    """Turn a *partial* raw MIME part payload into a short plain-text snippet.

    The payload comes from a truncated ``BODY.PEEK[n]<0.N>`` fetch, so the
    transfer decoding must tolerate mid-stream truncation: base64 is cut back
    to a multiple of 4 chars, quoted-printable and charset decoding both use
    ``errors="replace"``. HTML parts are tag-stripped. Never raises — a
    malformed part yields ``""``.
    """
    if not payload:
        return ""
    try:
        cte = (cte or "").strip().lower()
        if cte == "base64":
            import base64
            compact = re.sub(rb"\s+", b"", payload)
            compact = compact[: len(compact) // 4 * 4]
            try:
                payload = base64.b64decode(compact)
            except Exception:
                return ""
        elif cte == "quoted-printable":
            import quopri
            # Drop a truncated trailing escape ("=" or "=X") before decoding.
            payload = re.sub(rb"=[0-9A-Fa-f]?$", b"", payload)
            payload = quopri.decodestring(payload)
        for cs in (charset or "utf-8", "utf-8", "latin-1"):
            try:
                text = payload.decode(cs, "replace")
                break
            except LookupError:
                continue
        else:
            return ""
        base = (ctype or "").split(";", 1)[0].strip().lower()
        if base == "text/html":
            text = _SNIPPET_DROP_BLOCK_RX.sub(" ", text)
            text = _SNIPPET_TAG_RX.sub(" ", text)
            text = _stdlib_html.unescape(text)
        elif base and not base.startswith("text/"):
            return ""
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_len]
    except Exception:
        return ""


# Closed set of MIME transfer encodings — used to spot the CTE token inside a
# raw BODYSTRUCTURE line without writing a full parenthesis parser.
_BS_CTE_RX = re.compile(r'"(7bit|8bit|binary|base64|quoted-printable)"', re.I)
_BS_CHARSET_RX = re.compile(r'"charset"\s+"([^"]+)"', re.I)
_BS_FIRST_TEXT_RX = re.compile(r'^\(+\s*"text"\s+"(plain|html)"', re.I)


def preview_plan_from_bodystructure(line):
    """Derive the preview fetch plan from one raw BODYSTRUCTURE response line.

    Returns ``(section, ctype, charset, cte)`` or ``None`` when the message's
    first leaf part is not text (image-only mail, exotic nesting).

    The section is derived from the nesting depth of the *first* part:
    ``("text" …)`` → single-part → ``1``; ``(("text" …) … "alternative")`` →
    part 1 is the text leaf → ``1``; ``((("text" …) …) "mixed")`` → the leaf
    sits one level deeper → ``1.1``; and so on. This deliberately avoids
    fetching ``BODY[n.MIME]``: GreenMail answers that with a hard ``NO`` for
    single-part messages, which used to fail the whole batched FETCH and blank
    every preview on the page.
    """
    m_bs = re.search(r"BODYSTRUCTURE \(", line)
    if not m_bs:
        return None
    rest = line[m_bs.end() - 1:]
    depth = len(rest) - len(rest.lstrip("("))
    if depth < 1:
        return None
    # Only trust the shape when the first leaf really is a text part —
    # otherwise the section we compute would point at e.g. an image.
    m_txt = _BS_FIRST_TEXT_RX.match(rest)
    if not m_txt:
        return None
    section = "1" + ".1" * max(0, depth - 2)
    m_cs = _BS_CHARSET_RX.search(rest)
    m_cte = _BS_CTE_RX.search(rest)
    return (
        section,
        f"text/{m_txt.group(1).lower()}",
        m_cs.group(1) if m_cs else None,
        m_cte.group(1).lower() if m_cte else "",
    )


def _parse_preview_fetch(data):
    """Group a multi-literal FETCH response into ``{uid: {section: bytes}}``.

    A FETCH requesting two body sections returns *two* ``(meta, literal)``
    tuples per message in imaplib's response list, with the UID present only
    in the first tuple's metadata on most servers. Item order differs between
    Dovecot and GreenMail (same caveat as ``set_tags`` in the controller), so
    UID and section are matched independently per tuple.
    """
    out = {}
    cur_uid = None
    for item in data or []:
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        meta = item[0].decode(errors="replace") if isinstance(item[0], bytes) else str(item[0])
        m_uid = re.search(r"UID (\d+)", meta)
        if m_uid:
            cur_uid = int(m_uid.group(1))
        m_sec = re.search(r"BODY\[([0-9.]+(?:\.MIME)?)\]", meta, re.I)
        if cur_uid is None or not m_sec:
            continue
        out.setdefault(cur_uid, {})[m_sec.group(1).upper()] = item[1] or b""
    return out


def fetch_previews(conn, uids, max_len=_PREVIEW_MAX_LEN):
    """Return ``{uid: snippet}`` for the given UIDs using bounded partial fetches.

    Two phases, both batched: one ``UID FETCH (UID BODYSTRUCTURE)`` to derive
    each message's text-part section + charset/encoding (see
    ``preview_plan_from_bodystructure``), then one truncated
    ``BODY.PEEK[<section>]<0.N>`` fetch per distinct section (in practice 1–2
    commands per page, ~2 KB per message). ``.MIME`` fetches are deliberately
    avoided — GreenMail rejects them for single-part messages and fails the
    whole batch. Fully best-effort — any failure yields ``{}`` or a missing
    uid, never an exception.
    """
    if not uids:
        return {}
    previews = {}
    try:
        uid_set = ",".join(str(u) for u in uids)
        typ, data = conn.uid("FETCH", uid_set, "(UID BODYSTRUCTURE)")
        if typ != "OK":
            return {}
        plan = {}
        for item in data or []:
            if isinstance(item, tuple):
                line = b" ".join(
                    p for p in item if isinstance(p, (bytes, bytearray))
                ).decode(errors="replace")
            elif isinstance(item, (bytes, bytearray)):
                line = item.decode(errors="replace")
            else:
                continue
            m_uid = re.search(r"UID (\d+)", line)
            if not m_uid:
                continue
            entry = preview_plan_from_bodystructure(line)
            if entry:
                plan[int(m_uid.group(1))] = entry
        by_section = {}
        for uid, (section, _ct, _cs, _cte) in plan.items():
            by_section.setdefault(section, []).append(uid)
        for section, sec_uids in by_section.items():
            typ, data = conn.uid(
                "FETCH", ",".join(str(u) for u in sec_uids),
                f"(UID BODY.PEEK[{section}]<0.{_PREVIEW_FETCH_BYTES}>)")
            if typ != "OK":
                continue
            for uid, parts in _parse_preview_fetch(data).items():
                if uid not in plan:
                    continue
                _sec, ctype, charset, cte = plan[uid]
                previews[uid] = snippet_from_bytes(
                    parts.get(section.upper(), b""), ctype, cte, charset,
                    max_len)
    except Exception as e:
        _logger.debug("ow_mail preview fetch failed: %s", e)
    return previews


def status_counts(conn, folder_path):
    """Return (messages, unseen) via IMAP STATUS."""
    typ, data = conn.status(imap_mbox_quote(folder_path), "(MESSAGES UNSEEN)")
    if typ != "OK" or not data or not data[0]:
        return 0, 0
    line = data[0].decode(errors="replace") if isinstance(data[0], bytes) else data[0]
    m_tot = re.search(r"MESSAGES\s+(\d+)", line)
    m_uns = re.search(r"UNSEEN\s+(\d+)", line)
    return int(m_tot.group(1)) if m_tot else 0, int(m_uns.group(1)) if m_uns else 0


def search_uids(conn, criteria):
    """Execute UID SEARCH criteria and return a list of ints (ascending)."""
    # Try UTF-8 charset first (better substring matching on encoded headers),
    # fall back to no charset (US-ASCII) if the server rejects it.
    for charset in ("UTF-8", None):
        try:
            typ, data = conn.uid("SEARCH", charset, criteria)
            if typ == "OK":
                if not data or not data[0]:
                    return []
                return sorted(int(x) for x in data[0].split() if x.isdigit())
        except Exception:
            continue
    return []


# IMAP SORT field mapping
_SORT_FIELDS = {
    "date": "DATE",
    "from": "FROM",
    "subject": "SUBJECT",
    "size": "SIZE",
    "to": "TO",
}


_CLIENT_SORT_CAP = 2000  # envelope-fetch cap for client-side sort fallback


def sort_uids(conn, criteria, sort_by="date", sort_order="desc"):
    """Return UIDs matching `criteria` ordered by `sort_by`/`sort_order`.

    Tries IMAP SORT (RFC 5256); if unavailable, sorts client-side by
    fetching envelopes (capped at _CLIENT_SORT_CAP). Servers without SORT
    would otherwise silently return UID-order regardless of sort_by.
    """
    imap_field = _SORT_FIELDS.get(sort_by, "DATE")
    caps = conn.capabilities or ()
    caps_str = " ".join(c.decode() if isinstance(c, bytes) else c for c in caps).upper()
    if "SORT" in caps_str:
        try:
            order = f"REVERSE {imap_field}" if sort_order == "desc" else imap_field
            typ, data = conn.uid("SORT", f"({order})", "UTF-8", criteria or "ALL")
            if typ == "OK" and data and data[0]:
                return [int(x) for x in data[0].split() if x.isdigit()]
        except Exception:
            pass  # fall through to client-side sort
    uids = search_uids(conn, criteria)
    if not uids:
        return uids
    # DATE on a server without SORT: UID order approximates date order.
    if sort_by == "date":
        if sort_order == "desc":
            uids.reverse()
        return uids
    # Client-side sort for FROM/SUBJECT/SIZE. Cap to bound IMAP work;
    # pick the newest slice (tail = highest UIDs) so recent mail wins.
    if len(uids) > _CLIENT_SORT_CAP:
        uids = uids[-_CLIENT_SORT_CAP:]
    envs = fetch_envelopes(conn, uids)
    key_map = {
        "from": lambda e: (e.get("from_email") or "").lower(),
        "subject": lambda e: (e.get("subject") or "").lower(),
        "size": lambda e: e.get("size") or 0,
    }
    keyfn = key_map.get(sort_by, lambda e: e.get("date") or "")
    envs.sort(key=keyfn, reverse=(sort_order == "desc"))
    return [e["uid"] for e in envs]


def search_thread_uids(conn, message_ids):
    """Find every member of a conversation given some of its Message-IDs.

    Two match directions per id, folded into one nested OR query:

    * ``HEADER Message-ID "<id>"`` — the messages themselves (ancestors of
      the opened message, whose ids appear in its References chain).
    * ``HEADER References "<id>"`` — **descendants**: every later reply
      carries the earlier ids (including the thread root) in its own
      References header. Without this leg, opening the root or a middle
      message of a thread only ever showed the ancestors, so the stacked
      conversation view seemed to "miss" replies.

    Returns list of UIDs (ascending).
    """
    if not message_ids:
        return []
    # Clean IDs — strip angle brackets for the search
    clean = []
    for mid in message_ids:
        mid = mid.strip().strip("<>")
        if mid:
            clean.append(mid)
    if not clean:
        return []
    # Two criteria per id — halve the id cap to keep the command bounded.
    clean = clean[-10:]
    atoms = [f'HEADER Message-ID "{mid}"' for mid in clean]
    atoms += [f'HEADER References "{mid}"' for mid in clean]
    # Build nested OR: OR (a) (OR (b) (c))
    criteria = atoms[-1]
    for atom in reversed(atoms[:-1]):
        criteria = f"OR {atom} {criteria}"
    return search_uids(conn, criteria)
