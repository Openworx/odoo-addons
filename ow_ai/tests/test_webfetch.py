# -*- coding: utf-8 -*-
"""Tests for ``utils/webfetch.py``: SSRF guard, fetch, HTML to text.

``odoo.tests.BaseCase`` (no transaction, no database — same pattern as
``test_schema.py``): plain unittest.TestCase subclasses aren't picked up by
Odoo's tag-based test loader (``BaseCase.__init_subclass__`` is what stamps
a test class with the ``standard``/``at_install`` tags the loader filters
on), so this module uses BaseCase to stay collectible while running with no
Odoo transaction. No real HTTP calls: the resolver and the
``requests.Session`` are always faked.
"""
from __future__ import annotations

import contextlib
import socket
import time
from types import SimpleNamespace
from unittest import mock

import requests
import urllib3
from odoo.tests import BaseCase
from odoo.tests import common as odoo_test_common

from ..utils import webfetch
from ..utils.webfetch import WebFetchError, guard_url, html_to_text


def resolver_for(*addresses):
    def resolve(host, port, *args, **kwargs):
        return [(None, None, None, None, (address, port)) for address in addresses]
    return resolve


def recording_resolver(address='93.184.216.34'):
    """A public-address resolver plus the list of host names it was asked for."""
    hosts = []

    def resolve(host, port, *args, **kwargs):
        hosts.append(host)
        return [(None, None, None, None, (address, port))]
    return hosts, resolve


def rebinding_resolver(first='93.184.216.34', then='127.0.0.1'):
    """DNS rebinding: a public answer to the first lookup, loopback to every later one.
    Returns the list of answers given and the resolver."""
    answers = []

    def resolve(host, port, *args, **kwargs):
        answers.append(then if answers else first)
        return [(None, None, None, None, (answers[-1], port))]
    return answers, resolve


class TestGuardUrl(BaseCase):

    def test_public_host_is_accepted_and_normalised(self):
        self.assertEqual(guard_url('HTTPS://Example.com/a b', resolver=resolver_for('93.184.216.34')),
                         'https://example.com/a%20b')

    def test_blocked_addresses(self):
        for address in ('127.0.0.1', '10.1.2.3', '172.16.5.5', '192.168.0.9', '169.254.169.254',
                        '::1', 'fd12::1', 'fe80::1', '::ffff:10.0.0.1', '224.0.0.1', '0.0.0.0',
                        '100.64.0.1', '100.127.255.254', '64:ff9b::a00:1'):
            with self.subTest(address=address), self.assertRaises(WebFetchError) as ctx:
                guard_url('http://host.example/', resolver=resolver_for(address))
            self.assertEqual(ctx.exception.code, 'blocked')

    def test_public_ipv4_and_ipv6_addresses_are_accepted(self):
        for address in ('93.184.216.34', '2606:2800:220:1:248:1893:25c8:1946'):
            with self.subTest(address=address):
                guard_url('http://host.example/', resolver=resolver_for(address))

    def test_nat64_address_of_a_public_ipv4_is_accepted(self):
        # 64:ff9b::5db8:d822 carries 93.184.216.34 (0x5db8d822) in its low 32 bits.
        guard_url('http://host.example/', resolver=resolver_for('64:ff9b::5db8:d822'))
        self.assertEqual(guard_url('http://[64:ff9b::5db8:d822]/', resolver=resolver_for()),
                         'http://[64:ff9b::5db8:d822]/')

    def test_one_private_address_among_public_ones_blocks(self):
        with self.assertRaises(WebFetchError):
            guard_url('http://host.example/', resolver=resolver_for('93.184.216.34', '10.0.0.1'))

    def test_bad_schemes_ports_and_hosts(self):
        for url in ('ftp://example.com/', 'file:///etc/passwd', 'javascript:alert(1)', 'http://example.com:22/',
                    'http://user:pw@example.com/', 'http:///nohost', 'http://exa mple.com/'):
            with self.subTest(url=url), self.assertRaises(WebFetchError):
                guard_url(url, resolver=resolver_for('93.184.216.34'))

    def test_idna_host_is_resolved_and_requested_as_its_ascii_form(self):
        # Python's own "idna" codec (IDNA 2003) maps ß to "ss"; requests/urllib3 (IDNA 2008) keep it:
        # the guard must look up exactly the name the request will connect to.
        hosts, resolve = recording_resolver()
        self.assertEqual(guard_url('http://faß.example/x', resolver=resolve), 'http://xn--fa-hia.example/x')
        self.assertEqual(hosts, ['xn--fa-hia.example'])
        self.assertEqual(webfetch.normalise_url('HTTP://FAß.Example/x'), 'http://xn--fa-hia.example/x')

    def test_hosts_a_request_would_read_differently_are_bad_urls(self):
        for url in ('http://a\\.b.example/', 'http://example.com\\@evil.example/', 'http://exa_mple.com/',
                    'http://exa%2Dmple.com/'):
            with self.subTest(url=url):
                with self.assertRaises(WebFetchError) as ctx:
                    guard_url(url, resolver=resolver_for('93.184.216.34'))
                self.assertEqual(ctx.exception.code, 'bad_url')

    def test_ip_literals_are_checked_themselves_and_keep_their_brackets(self):
        hosts, resolve = recording_resolver()
        self.assertEqual(guard_url('http://[2606:2800:220:1:248:1893:25c8:1946]/', resolver=resolve),
                         'http://[2606:2800:220:1:248:1893:25c8:1946]/')
        self.assertEqual(guard_url('http://93.184.216.34:8080/a', resolver=resolve), 'http://93.184.216.34:8080/a')
        for url in ('http://[::1]/', 'http://127.0.0.1/', 'http://[::ffff:10.0.0.1]/', 'http://[fe80::1%25eth0]/'):
            with self.subTest(url=url), self.assertRaises(WebFetchError):
                guard_url(url, resolver=resolve)
        self.assertEqual(hosts, [])

    def test_normalise_url_is_guard_url_without_dns(self):
        self.assertEqual(webfetch.normalise_url('HTTPS://Example.com:443/a b#top'), 'https://example.com/a%20b')
        self.assertEqual(webfetch.normalise_url('http://Example.com:8080/'), 'http://example.com:8080/')
        for url in ('ftp://example.com/', 'http://example.com:22/', 'http://user:pw@example.com/', 'not a url'):
            with self.subTest(url=url), self.assertRaises(WebFetchError):
                webfetch.normalise_url(url)

    def test_unresolvable_host_is_unreachable(self):
        def failing(host, port, *a, **k):
            raise OSError('no such host')
        for resolver in (failing, resolver_for()):
            with self.subTest(resolver=resolver), self.assertRaises(WebFetchError) as ctx:
                guard_url('http://nope.invalid/', resolver=resolver)
            self.assertEqual((ctx.exception.code, str(ctx.exception)),
                             ('unreachable', "The page could not be reached."))

    def test_blocked_ip_address_hosts_are_known_without_dns(self):
        for url in ('http://127.0.0.1/admin', 'http://169.254.169.254/latest/meta-data/', 'http://[::1]/',
                    'http://192.168.1.1/', 'http://2130706433/', 'http://[::ffff:10.0.0.1]:8080/x'):
            with self.subTest(url=url):
                self.assertTrue(webfetch.is_blocked_literal_host(url))
        for url in ('http://93.184.216.34/', 'https://example.org/', 'http://[2606:2800:220:1:248:1893:25c8:1946]/',
                    'http://localhost/', 'not a url'):
            with self.subTest(url=url):
                self.assertFalse(webfetch.is_blocked_literal_host(url))   # host names need DNS: the guard decides


class TestHtmlToText(BaseCase):

    def test_prefers_main_drops_chrome_keeps_headings(self):
        html = b"""<html><head><title>T &amp; Co</title><script>x()</script><style>p{}</style></head>
        <body><nav>Menu</nav><header>Top</header><main><h1>Head</h1><p>One  two\n three</p>
        <ul><li>a</li><li>b</li></ul></main><footer>F</footer><aside>Ad</aside></body></html>"""
        title, text = html_to_text(html)
        self.assertEqual(title, 'T & Co')
        self.assertNotIn('Menu', text); self.assertNotIn('x()', text); self.assertNotIn('Ad', text)  # noqa: E702
        self.assertIn('Head\n', text); self.assertIn('One two three', text); self.assertIn('- a\n- b', text)  # noqa: E702

    def test_charset_fallback_and_size_cap(self):
        body = '<html><head><meta charset="utf-8"></head><body><p>caf\xe9</p></body></html>'.encode('cp1252')
        title, text = html_to_text(body, charset='windows-1252')
        self.assertIn('café', text)
        _title, text2 = html_to_text(b'<p>' + b'x' * 10 + b'\xff\xfe</p>')   # broken bytes never raise
        self.assertIn('xxxxxxxxxx', text2)

    def test_plain_text_and_json_bodies(self):
        self.assertEqual(html_to_text(b'just text', charset='utf-8'), ('', 'just text'))

    def test_text_after_a_dropped_element_is_kept(self):
        # An inline icon, a <noscript> or a <script> goes; the text after it in the same parent stays.
        for html, text in ((b'<ul><li><svg><path/></svg>Fast setup</li></ul>', '- Fast setup'),
                           (b'<p>Price <svg></svg>10 EUR per user</p>', 'Price 10 EUR per user'),
                           (b'<p>Before <noscript>x</noscript>after noscript</p>', 'Before after noscript'),
                           (b'<p>Run <script>x()</script>this <style>p{}</style>now</p>', 'Run this now')):
            with self.subTest(html=html):
                self.assertEqual(html_to_text(html), ('', text))

    def test_content_type_decides_between_html_and_text(self):
        source = b'#include <stdio.h>\nint main(void) { return 0; }'
        self.assertEqual(html_to_text(source, charset='utf-8', content_type='text/plain'), ('', source.decode()))
        readme = b'<p align="center">Logo</p>\n# Project'
        self.assertEqual(html_to_text(readme, content_type='text/plain'), ('', readme.decode()))
        padded = (b' ' * 600 + b'<html><head><title>T</title><script>steal()</script></head>'
                  b'<body><p>Hello</p></body></html>')
        for content_type in ('text/html', 'application/xhtml+xml'):
            with self.subTest(content_type=content_type):
                self.assertEqual(html_to_text(padded, content_type=content_type), ('T', 'Hello'))
        self.assertEqual(html_to_text(b'<p>Hi</p>'), ('', 'Hi'))   # no content type: the first bytes decide

    def test_drops_html_comments(self):
        html = b'<html><body><main><p>Hello</p><!-- SECRET --><p>World</p></main></body></html>'
        _title, text = html_to_text(html)
        self.assertIn('Hello', text)
        self.assertIn('World', text)
        self.assertNotIn('SECRET', text)

        html_no_main = b'<html><body><!-- SECRET --><p>Hi there</p></body></html>'
        _title, text2 = html_to_text(html_no_main)
        self.assertIn('Hi there', text2)
        self.assertNotIn('SECRET', text2)

    def test_wrong_declared_charset_falls_back_to_lxml_sniffing(self):
        body = '<html><head><meta charset="iso-8859-1"></head><body><p>caf\xe9</p></body></html>'.encode(
            'iso-8859-1')
        _title, text = html_to_text(body, charset='utf-8')  # declared charset is wrong/lying
        self.assertIn('café', text)

        _title2, text2 = html_to_text(body, charset='x-bogus')  # unknown charset name: no exception
        self.assertIn('café', text2)


class TestFetch(BaseCase):

    def _session(self, responses):
        session = mock.Mock()
        session.get = mock.Mock(side_effect=responses)
        return session

    def _response(self, status, headers, chunks):
        response = mock.Mock(status_code=status, headers=headers)
        response.iter_content = lambda chunk_size: iter(chunks)
        response.close = mock.Mock()
        return response

    def test_follows_public_redirect_and_returns_final_url(self):
        session = self._session([
            self._response(302, {'Location': 'https://example.org/page'}, []),
            self._response(200, {'Content-Type': 'text/html; charset=utf-8'}, [b'<p>hi</p>']),
        ])
        result = webfetch.fetch('https://example.com/', timeout=5, user_agent='t', session=session,
                                resolver=resolver_for('93.184.216.34'))
        self.assertEqual(result.final_url, 'https://example.org/page')
        self.assertEqual(result.body, b'<p>hi</p>')
        self.assertEqual(session.get.call_args_list[0].kwargs['allow_redirects'], False)

    def _text_response(self, body=b'hi'):
        return self._response(200, {'Content-Type': 'text/plain'}, [body])

    def test_connects_to_the_checked_address_under_its_host_name(self):
        """The request goes to the address the guard checked (never to the
        host name, which a second DNS lookup could answer differently), with
        the host name as ``Host`` header; ``final_url`` keeps the host name."""
        ipv6 = '2606:2800:220:1:248:1893:25c8:1946'
        for url, address, connect_url, host, final_url in (
            ('https://Example.com/a b?q=1', '93.184.216.34', 'https://93.184.216.34/a%20b?q=1', 'example.com',
             'https://example.com/a%20b?q=1'),
            ('http://example.com:8080/x', '93.184.216.34', 'http://93.184.216.34:8080/x', 'example.com:8080',
             'http://example.com:8080/x'),
            ('https://example.com', ipv6, f'https://[{ipv6}]', 'example.com', 'https://example.com'),
        ):
            with self.subTest(url=url):
                session = self._session([self._text_response()])
                result = webfetch.fetch(url, timeout=5, user_agent='t', session=session,
                                        resolver=resolver_for(address))
                self.assertEqual(session.get.call_args.args, (connect_url,))
                self.assertEqual(session.get.call_args.kwargs['headers']['Host'], host)
                self.assertEqual(result.final_url, final_url)

    def test_ip_literal_urls_are_requested_as_they_are(self):
        for url, host in (('http://93.184.216.34:8080/a', '93.184.216.34:8080'),
                          ('https://[2606:2800:220:1:248:1893:25c8:1946]/', '[2606:2800:220:1:248:1893:25c8:1946]')):
            with self.subTest(url=url):
                hosts, resolve = recording_resolver()
                session = self._session([self._text_response()])
                result = webfetch.fetch(url, timeout=5, user_agent='t', session=session, resolver=resolve)
                self.assertEqual(session.get.call_args.args, (url,))
                self.assertEqual(session.get.call_args.kwargs['headers']['Host'], host)
                self.assertEqual((result.final_url, hosts), (url, []))

    def test_a_host_that_rebinds_after_the_check_is_never_reached(self):
        """The auditor's DNS rebinding: the lookup the guard checks answers a
        public address, every later one loopback. The request goes to the
        checked address; a redirect back to that host is checked again (and
        refused) before any request is made."""
        answers, resolve = rebinding_resolver()
        session = self._session([self._text_response(b'public')])
        result = webfetch.fetch('http://audit-rebind.example:8080/', timeout=5, user_agent='t', session=session,
                                resolver=resolve)
        self.assertEqual(result.body, b'public')
        self.assertEqual([call.args[0] for call in session.get.call_args_list], ['http://93.184.216.34:8080/'])
        self.assertEqual(answers, ['93.184.216.34'])

        answers, resolve = rebinding_resolver()
        session = self._session([self._response(302, {'Location': '/next'}, [])])
        with self.assertRaises(WebFetchError) as ctx:
            webfetch.fetch('http://audit-rebind.example:8080/', timeout=5, user_agent='t', session=session,
                           resolver=resolve)
        self.assertEqual(ctx.exception.code, 'blocked')
        self.assertEqual(answers, ['93.184.216.34', '127.0.0.1'])
        self.assertEqual([call.args[0] for call in session.get.call_args_list], ['http://93.184.216.34:8080/'])

    @contextlib.contextmanager
    def _fake_connections(self, answers=None):
        """The real ``requests``/``urllib3`` stack down to the socket connect,
        which is faked: nothing ever leaves the process. A connect to an
        address in ``answers`` gets one end of a socket pair whose other end
        has already sent ``answers[address]``; any other connect is refused.
        Yields ``(destinations, peers)``: the ``(host, port)`` of every
        connect, in order, and the other end of each accepted one by address
        (to read the request that reached it).

        Odoo's test case blocks ``requests.Session.send`` for non-local URLs;
        it is restored only for as long as every connect is faked."""
        answers = answers or {}
        destinations, peers = [], {}

        def connect(address, *args, **kwargs):
            destinations.append(address)
            if address[0] not in answers:
                raise ConnectionRefusedError('refused')
            ours, theirs = socket.socketpair()
            self.addCleanup(ours.close)
            self.addCleanup(theirs.close)
            theirs.settimeout(5)
            theirs.sendall(answers[address[0]])
            peers[address[0]] = theirs
            return ours

        with mock.patch.object(urllib3.util.connection, 'create_connection', connect), \
                mock.patch.object(requests.Session, 'send', odoo_test_common._super_send):
            yield destinations, peers

    def test_the_socket_connects_to_the_checked_address(self):
        """The auditor's DNS rebinding through the real stack: the connection
        is opened to the address the guard checked, never to the host name
        (a second lookup would now answer loopback)."""
        answers, resolve = rebinding_resolver()
        with self._fake_connections() as (destinations, _peers), self.assertRaises(WebFetchError) as ctx:
            webfetch.fetch('http://audit-rebind.example:8080/', timeout=5, user_agent='t', resolver=resolve)
        self.assertEqual(ctx.exception.code, 'unreachable')
        self.assertEqual(destinations, [('93.184.216.34', 8080)])
        self.assertEqual(answers, ['93.184.216.34'])

    def test_a_refused_address_falls_back_to_the_next_checked_one(self):
        """Like the HTTP library did before pinning, every checked address is
        tried in the resolver's order while the connection cannot be opened;
        the one lookup the guard checked is the only one made."""
        hosts = []

        def resolve(host, port, *args, **kwargs):
            hosts.append(host)
            return [(None, None, None, None, (address, port)) for address in ('93.184.216.34', '93.184.216.35')]

        page = (b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n'
                b'Connection: close\r\n\r\nhi')
        with self._fake_connections({'93.184.216.35': page}) as (destinations, peers):
            result = webfetch.fetch('http://host.example:8080/page', timeout=5, user_agent='t', resolver=resolve)
        self.assertEqual((result.body, result.final_url), (b'hi', 'http://host.example:8080/page'))
        self.assertEqual(destinations, [('93.184.216.34', 8080), ('93.184.216.35', 8080)])
        self.assertEqual(hosts, ['host.example'])
        request = peers['93.184.216.35'].recv(65536)
        self.assertTrue(request.startswith(b'GET /page HTTP/1.1\r\n'), request)
        self.assertIn(b'\r\nHost: host.example:8080\r\n', request)

        hosts.clear()
        with self._fake_connections() as (destinations, _peers), self.assertRaises(WebFetchError) as ctx:
            webfetch.fetch('http://host.example:8080/page', timeout=5, user_agent='t', resolver=resolve)
        self.assertEqual((ctx.exception.code, str(ctx.exception)), ('unreachable', "The page could not be reached."))
        self.assertEqual(destinations, [('93.184.216.34', 8080), ('93.184.216.35', 8080)])
        self.assertEqual(hosts, ['host.example'])

    def test_a_tls_failure_is_not_retried_on_the_next_address(self):
        """Only a connection that could not be opened moves on: a TLS failure
        on the first address (here: a server answering in plain HTTP) ends
        the read."""
        resolve = resolver_for('93.184.216.34', '93.184.216.35')
        with self._fake_connections({'93.184.216.34': b'HTTP/1.1 400 Bad Request\r\n\r\n'}) as (destinations, _p), \
                self.assertRaises(WebFetchError) as ctx:
            webfetch.fetch('https://example.com/', timeout=5, user_agent='t', resolver=resolve)
        self.assertIsInstance(ctx.exception.__cause__, requests.exceptions.SSLError)
        self.assertEqual(ctx.exception.code, 'unreachable')
        self.assertEqual(destinations, [('93.184.216.34', 443)])

    def test_https_verifies_the_certificate_of_the_host_name(self):
        """An https request to the checked address still sends the host name
        as TLS server name and checks the certificate against it (never
        against the address, never unverified)."""
        session = requests.Session()
        session.trust_env = False
        self.addCleanup(session.close)
        connections = []
        https_connect = urllib3.connection.HTTPSConnection.connect

        def connect(connection):
            connections.append(connection)
            return https_connect(connection)

        init_poolmanager = requests.adapters.HTTPAdapter.init_poolmanager
        with self._fake_connections() as (destinations, _peers), \
                mock.patch.object(requests.adapters.HTTPAdapter, 'init_poolmanager', autospec=True,
                                  side_effect=init_poolmanager) as init, \
                mock.patch.object(urllib3.connection.HTTPSConnection, 'connect', autospec=True, side_effect=connect), \
                self.assertRaises(WebFetchError) as ctx:
            webfetch.fetch('https://example.com/page', timeout=5, user_agent='t', session=session,
                           resolver=resolver_for('93.184.216.34'))
        self.assertEqual(ctx.exception.code, 'unreachable')
        self.assertEqual(destinations, [('93.184.216.34', 443)])
        self.assertEqual([(call.kwargs['server_hostname'], call.kwargs['assert_hostname'])
                          for call in init.call_args_list], [('example.com', 'example.com')])
        [connection] = connections
        self.assertEqual((connection.host, connection.port), ('93.184.216.34', 443))
        self.assertEqual((connection.server_hostname, connection.assert_hostname), ('example.com', 'example.com'))
        self.assertEqual(connection.cert_reqs, 'CERT_REQUIRED')

    def test_each_redirect_hop_is_pinned_to_its_own_host(self):
        def resolve(host, port, *args, **kwargs):
            address = {'example.com': '93.184.216.34', 'example.org': '93.184.216.35'}[host]
            return [(None, None, None, None, (address, port))]

        session = self._session([
            self._response(302, {'Location': 'https://example.org/page'}, []),
            self._response(301, {'Location': '/final?x=1'}, []),
            self._text_response(),
        ])
        result = webfetch.fetch('https://example.com/', timeout=5, user_agent='t', session=session, resolver=resolve)
        self.assertEqual(result.final_url, 'https://example.org/final?x=1')
        self.assertEqual([(call.args[0], call.kwargs['headers']['Host']) for call in session.get.call_args_list], [
            ('https://93.184.216.34/', 'example.com'),
            ('https://93.184.216.35/page', 'example.org'),
            ('https://93.184.216.35/final?x=1', 'example.org'),
        ])
        mounts = [(call.args[0], call.args[1]._server_hostname) for call in session.mount.call_args_list]
        self.assertEqual(mounts, [('https://93.184.216.34/', 'example.com'), ('https://93.184.216.35/', 'example.org'),
                                  ('https://93.184.216.35/', 'example.org')])

    def test_a_trailing_dot_is_not_part_of_the_host_name_sent(self):
        """``example.com.`` is looked up as written (a fully qualified name)
        but sent, as ``Host`` and TLS server name, as ``example.com`` (what
        certificates and virtual hosts name), like ``urllib3`` does."""
        for url, host_header in (('https://Example.com./a', 'example.com'),
                                 ('http://example.com.:8080/a', 'example.com:8080')):
            with self.subTest(url=url):
                hosts, resolve = recording_resolver()
                session = self._session([self._text_response()])
                result = webfetch.fetch(url, timeout=5, user_agent='t', session=session, resolver=resolve)
                self.assertEqual(hosts, ['example.com.'])
                self.assertEqual(session.get.call_args.kwargs['headers']['Host'], host_header)
                self.assertEqual(result.final_url, url.lower())
                if url.startswith('https'):
                    self.assertEqual([call.args[1]._server_hostname for call in session.mount.call_args_list],
                                     ['example.com'])

    def test_redirect_to_private_address_is_refused(self):
        calls = {'n': 0}
        def resolve(host, port, *a, **k):
            calls['n'] += 1
            return resolver_for('93.184.216.34' if host == 'example.com' else '10.0.0.5')(host, port)
        session = self._session([self._response(302, {'Location': 'http://internal.example/'}, [])])
        with self.assertRaises(WebFetchError) as ctx:
            webfetch.fetch('https://example.com/', timeout=5, user_agent='t', session=session, resolver=resolve)
        self.assertEqual(ctx.exception.code, 'blocked')
        self.assertEqual(session.get.call_count, 1)

    def test_too_many_redirects_size_cap_and_content_type(self):
        loop = [self._response(301, {'Location': 'https://example.com/'}, []) for _ in range(6)]
        with self.assertRaises(WebFetchError) as ctx:
            webfetch.fetch('https://example.com/', timeout=5, user_agent='t', session=self._session(loop),
                           resolver=resolver_for('93.184.216.34'))
        self.assertEqual(ctx.exception.code, 'too_many_redirects')
        big = self._response(200, {'Content-Type': 'text/html'}, [b'x' * 4096] * 3)
        result = webfetch.fetch('https://example.com/', timeout=5, max_bytes=5000, user_agent='t',
                                session=self._session([big]), resolver=resolver_for('93.184.216.34'))
        self.assertTrue(result.truncated); self.assertLessEqual(len(result.body), 8192)  # noqa: E702
        pdf = self._response(200, {'Content-Type': 'application/pdf'}, [b'%PDF'])
        with self.assertRaises(WebFetchError) as ctx:
            webfetch.fetch('https://example.com/x.pdf', timeout=5, user_agent='t', session=self._session([pdf]),
                           resolver=resolver_for('93.184.216.34'))
        self.assertEqual(ctx.exception.code, 'unsupported_content')

    def test_content_type_matching_is_exact(self):
        for bad_content_type in ('application/jsonld+json', 'application/jsonp'):
            response = self._response(200, {'Content-Type': bad_content_type}, [b'{}'])
            with self.subTest(content_type=bad_content_type), self.assertRaises(WebFetchError) as ctx:
                webfetch.fetch('https://example.com/', timeout=5, user_agent='t', session=self._session([response]),
                               resolver=resolver_for('93.184.216.34'))
            self.assertEqual(ctx.exception.code, 'unsupported_content')

        for good_content_type in ('text/html;charset=UTF-8', 'TEXT/HTML'):
            response = self._response(200, {'Content-Type': good_content_type}, [b'<p>hi</p>'])
            with self.subTest(content_type=good_content_type):
                result = webfetch.fetch('https://example.com/', timeout=5, user_agent='t',
                                        session=self._session([response]), resolver=resolver_for('93.184.216.34'))
                self.assertEqual(result.body, b'<p>hi</p>')

    def test_own_session_ignores_the_environment_and_is_closed(self):
        response = self._response(200, {'Content-Type': 'text/plain'}, [b'hi'])
        with mock.patch.object(webfetch.requests, 'Session') as session_cls:
            session_cls.return_value.get = mock.Mock(return_value=response)
            result = webfetch.fetch('https://example.com/', timeout=5, user_agent='t',
                                    resolver=resolver_for('93.184.216.34'))
        self.assertEqual(result.body, b'hi')
        session = session_cls.return_value
        self.assertIs(session.trust_env, False)   # no proxies or ~/.netrc credentials from the server's environment
        session.close.assert_called_once_with()

    def test_timeout_and_http_error(self):
        session = self._session([requests.Timeout('slow')])
        with self.assertRaises(WebFetchError) as ctx:
            webfetch.fetch('https://example.com/', timeout=1, user_agent='t', session=session,
                           resolver=resolver_for('93.184.216.34'))
        self.assertEqual(ctx.exception.code, 'timeout')
        session = self._session([self._response(404, {'Content-Type': 'text/html'}, [b''])])
        with self.assertRaises(WebFetchError) as ctx:
            webfetch.fetch('https://example.com/', timeout=1, user_agent='t', session=session,
                           resolver=resolver_for('93.184.216.34'))
        self.assertEqual((ctx.exception.code, ctx.exception.status), ('http_error', 404))

    def test_connection_failures_are_unreachable_and_timeouts_stay_timeouts(self):
        """Fixed texts: never the library's own message, which names the
        address connected to (``HTTPSConnectionPool(host='93.184.216.34', ...)``)."""
        pool = "HTTPSConnectionPool(host='93.184.216.34', port=443)"
        for exc, code, text in (
            (requests.ConnectionError(f'{pool}: Name or service not known'), 'unreachable',
             "The page could not be reached."),
            (requests.exceptions.SSLError(f'{pool}: certificate verify failed'), 'unreachable',
             "The page could not be reached."),
            (requests.ConnectTimeout(f'{pool}: connect timed out'), 'timeout', "The page did not answer in time."),
            (requests.ReadTimeout(f'{pool}: read timed out'), 'timeout', "The page did not answer in time."),
            (requests.exceptions.ChunkedEncodingError(f'{pool}: Connection broken'), 'http_error',
             "The page could not be fetched."),
        ):
            with self.subTest(exc=exc):
                with self.assertRaises(WebFetchError) as ctx:
                    webfetch.fetch('https://example.com/', timeout=5, user_agent='t', session=self._session([exc]),
                                   resolver=resolver_for('93.184.216.34'))
                self.assertEqual((ctx.exception.code, str(ctx.exception)), (code, text))

    def test_a_body_read_that_times_out_is_a_timeout(self):
        # requests reports a read timeout while streaming the body as a ConnectionError.
        def stalled(chunk_size):
            yield b'<p>start'
            raise requests.ConnectionError(urllib3.exceptions.ReadTimeoutError(None, None, 'Read timed out.'))
        response = self._response(200, {'Content-Type': 'text/html'}, [])
        response.iter_content = stalled
        with self.assertRaises(WebFetchError) as ctx:
            webfetch.fetch('https://example.com/', timeout=5, user_agent='t', session=self._session([response]),
                           resolver=resolver_for('93.184.216.34'))
        self.assertEqual((ctx.exception.code, str(ctx.exception)), ('timeout', "The page did not answer in time."))
        response.close.assert_called_with()

    def _fake_clock(self):
        """Freeze ``webfetch``'s clock at 1000 s; the test moves ``clock['now']``."""
        clock = {'now': 1000.0}
        patcher = mock.patch.object(webfetch, 'time', SimpleNamespace(monotonic=lambda: clock['now']))
        patcher.start()
        self.addCleanup(patcher.stop)
        return clock

    def test_a_page_that_trickles_in_is_stopped_at_the_timeout(self):
        """``timeout`` bounds the whole read, not each socket read: a body that
        keeps coming, one small chunk a second, is given up once it has passed."""
        clock = self._fake_clock()

        def trickle(chunk_size):
            for _second in range(60):
                clock['now'] += 1
                yield b'x' * 10

        response = self._response(200, {'Content-Type': 'text/plain'}, [])
        response.iter_content = trickle
        with self.assertRaises(WebFetchError) as ctx:
            webfetch.fetch('https://example.com/', timeout=5, user_agent='t', session=self._session([response]),
                           resolver=resolver_for('93.184.216.34'))
        self.assertEqual(ctx.exception.code, 'timeout')
        self.assertEqual(str(ctx.exception), "The page did not load within 5 seconds.")
        self.assertEqual(clock['now'], 1005.0)   # given up at the first chunk once the deadline is reached
        response.close.assert_called_with()

    def test_redirect_hops_share_the_timeout(self):
        """Every hop gets only the time left as its own timeout; no hop starts after the deadline."""
        clock = self._fake_clock()
        responses = [self._response(302, {'Location': f'https://example.com/{n}'}, []) for n in (1, 2, 3)]
        responses.append(self._response(200, {'Content-Type': 'text/plain'}, [b'hi']))

        def slow_get(url, **kwargs):
            clock['now'] += 2
            return responses.pop(0)

        session = mock.Mock()
        session.get = mock.Mock(side_effect=slow_get)
        with self.assertRaises(WebFetchError) as ctx:
            webfetch.fetch('https://example.com/', timeout=5, user_agent='t', session=session,
                           resolver=resolver_for('93.184.216.34'))
        self.assertEqual(ctx.exception.code, 'timeout')
        self.assertEqual([call.kwargs['timeout'] for call in session.get.call_args_list], [(5, 5), (3, 3), (1, 1)])

    def test_dns_time_counts_against_the_hop(self):
        clock = self._fake_clock()

        def slow_resolver(host, port, *args, **kwargs):
            clock['now'] += 2
            return resolver_for('93.184.216.34')(host, port)

        session = self._session([self._response(200, {'Content-Type': 'text/plain'}, [b'hi'])])
        webfetch.fetch('https://example.com/', timeout=5, user_agent='t', session=session, resolver=slow_resolver)
        self.assertEqual(session.get.call_args.kwargs['timeout'], (3, 3))

    def test_the_wait_for_a_connection_is_capped(self):
        """(connect, read) timeouts: an address that never answers the
        connect costs at most 10 s, so the next checked one still gets its turn."""
        self._fake_clock()
        session = self._session([self._response(200, {'Content-Type': 'text/plain'}, [b'hi'])])
        webfetch.fetch('https://example.com/', timeout=30, user_agent='t', session=session,
                       resolver=resolver_for('93.184.216.34'))
        self.assertEqual(session.get.call_args.kwargs['timeout'], (10, 30))

    def _socket_response(self, content_type, iter_content, via='connection'):
        """A response reading from one end of a socket pair (the other end stays
        silent), reachable where urllib3 keeps it (``via='connection'``) or,
        for a response that ends when the connection closes, where http.client
        hands it over to the response (``via='fp'``)."""
        ours, theirs = socket.socketpair()
        ours.settimeout(5)   # a missing watchdog fails the test after 5 s instead of hanging it
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        response = self._response(200, {'Content-Type': content_type}, [])
        if via == 'connection':
            response.raw._connection.sock = ours
        else:
            response.raw._connection.sock = None
            response.raw._fp.fp.raw._sock = ours
        response.iter_content = lambda chunk_size: iter_content(ours, chunk_size)
        return response

    def test_a_body_that_stalls_is_cut_at_the_deadline(self):
        """A server that stops sending mid-body (or sends a byte now and then)
        cannot hold the read past the deadline: a watchdog shuts the socket
        down, and whatever the read then does, the answer is ``timeout``."""
        def stalls_then_breaks(sock, chunk_size):
            yield b'<p>start'
            data = sock.recv(chunk_size)   # blocks: the server sends nothing more
            if data:
                yield data
            raise requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead")

        def stalls_then_ends(sock, chunk_size):
            yield b'<p>start'
            data = sock.recv(chunk_size)
            if data:
                yield data

        for iter_content, via in ((stalls_then_breaks, 'connection'), (stalls_then_ends, 'connection'),
                                  (stalls_then_ends, 'fp')):
            with self.subTest(iter_content=iter_content.__name__, via=via):
                response = self._socket_response('text/html', iter_content, via)
                started = time.monotonic()
                with self.assertRaises(WebFetchError) as ctx:
                    webfetch.fetch('https://example.com/', timeout=1, user_agent='t',
                                   session=self._session([response]), resolver=resolver_for('93.184.216.34'))
                elapsed = time.monotonic() - started
                self.assertEqual(ctx.exception.code, 'timeout')
                self.assertEqual(str(ctx.exception), "The page did not load within 1 seconds.")
                self.assertGreaterEqual(elapsed, 0.9)
                self.assertLess(elapsed, 1.5)
                response.close.assert_called_with()

    def test_the_watchdog_is_cancelled_after_a_complete_read(self):
        def one_chunk(sock, chunk_size):
            yield b'hi'

        response = self._socket_response('text/plain', one_chunk)
        timer_cls = mock.Mock()
        with mock.patch.object(webfetch, 'threading', SimpleNamespace(Timer=timer_cls)):
            result = webfetch.fetch('https://example.com/', timeout=5, user_agent='t',
                                    session=self._session([response]), resolver=resolver_for('93.184.216.34'))
        self.assertEqual(result.body, b'hi')
        timer_cls.assert_called_once()
        self.assertLessEqual(timer_cls.call_args.args[0], 5)
        timer_cls.return_value.start.assert_called_once_with()
        timer_cls.return_value.cancel.assert_called_once_with()

    def test_without_a_reachable_socket_no_watchdog_is_started(self):
        # The per-chunk check still applies (test_a_page_that_trickles_in_is_stopped_at_the_timeout).
        response = self._response(200, {'Content-Type': 'text/plain'}, [b'hi'])   # raw._connection.sock is a Mock
        timer_cls = mock.Mock()
        with mock.patch.object(webfetch, 'threading', SimpleNamespace(Timer=timer_cls)):
            result = webfetch.fetch('https://example.com/', timeout=5, user_agent='t',
                                    session=self._session([response]), resolver=resolver_for('93.184.216.34'))
        self.assertEqual(result.body, b'hi')
        timer_cls.assert_not_called()
