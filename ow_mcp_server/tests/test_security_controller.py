"""End-to-end tests for transport-level security features:
IP whitelist, rate limiting, CORS, transport/SSE toggles, audit log.
"""
import json
from datetime import datetime, timedelta

from odoo.tests import HttpCase, tagged

from odoo.addons.ow_mcp_server.protocol import authfail, ratelimit


@tagged('post_install', '-at_install')
class TestMcpSecurityController(HttpCase):
    def setUp(self):
        super().setUp()
        ratelimit.reset()
        authfail.reset()
        self.cfg = self.env['ow.mcp.config'].get_singleton()
        # Clean, off-by-default security posture for each test.
        self.cfg.write({
            'enable_http_transport': True,
            'enable_sse': False,
            'enable_cors': False,
            'rate_limit_per_minute': 0,
            'audit_log': False,
            'ip_whitelist': False,
        })
        self.user = self.env['res.users'].create({
            'login': 'mcpsec',
            'name': 'MCP Sec',
            'password': 'x',
            'groups_id': [
                (4, self.env.ref('ow_mcp_server.group_mcp_user').id),
                (4, self.env.ref('base.group_user').id),
            ],
        })
        self.key = (
            self.env['res.users.apikeys'].with_user(self.user)
            ._generate('mcp', 'sec', datetime.now() + timedelta(days=1))
        )
        partner = self.env.ref('base.model_res_partner').id
        self.env['ow.mcp.model.access'].sudo().search(
            [('model_id', '=', partner)]
        ).unlink()
        self.env['ow.mcp.model.access'].create({
            'model_id': partner,
            'allow_read': True,
        })
        self.env['ow.mcp.audit.log'].sudo().search([]).unlink()
        self.env.flush_all()

    def _flush(self):
        self.env.flush_all()

    def _post(self, body, headers=None):
        h = {'Content-Type': 'application/json',
             'Authorization': f'Bearer {self.key}'}
        if headers:
            h.update(headers)
        return self.url_open(
            '/mcp', data=json.dumps(body).encode(), headers=h,
        )

    # ---- enable_http_transport ----

    def test_transport_off_returns_503(self):
        self.cfg.enable_http_transport = False
        self._flush()
        r = self._post({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
        self.assertEqual(r.status_code, 503)

    # ---- enable_sse ----

    def _request(self, method, path='/mcp', headers=None, data=None):
        h = {'Authorization': f'Bearer {self.key}'}
        if headers:
            h.update(headers)
        return self.opener.request(
            method, self.base_url() + path, headers=h, data=data, timeout=10,
        )

    def test_sse_off_returns_405(self):
        self.cfg.enable_sse = False
        self._flush()
        r = self._request('GET')
        self.assertEqual(r.status_code, 405)

    def test_sse_on_streams_ready_event(self):
        self.cfg.enable_sse = True
        self._flush()
        r = self._request('GET')
        self.assertEqual(r.status_code, 200)
        self.assertIn('text/event-stream', r.headers.get('Content-Type', ''))

    # ---- ip_whitelist ----

    def test_ip_whitelist_blocks_non_matching(self):
        self.cfg.ip_whitelist = '10.0.0.0/8\n# only office'
        self._flush()
        r = self._post({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
        # 127.0.0.1 is not in 10/8 → 403.
        self.assertEqual(r.status_code, 403)

    def test_ip_whitelist_allows_matching(self):
        self.cfg.ip_whitelist = '127.0.0.0/8\n# local'
        self._flush()
        r = self._post({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
        self.assertEqual(r.status_code, 200)

    def test_ip_whitelist_empty_means_no_restriction(self):
        self.cfg.ip_whitelist = False
        self._flush()
        r = self._post({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
        self.assertEqual(r.status_code, 200)

    # ---- reverse proxy: X-Forwarded-For handling ----

    def test_xforwarded_ignored_without_proxy_mode(self):
        # proxy_mode is off by default; X-Forwarded-For MUST be ignored,
        # otherwise any caller could spoof their client IP.
        from odoo.tools import config
        self.assertFalse(config.get('proxy_mode'))
        self.cfg.ip_whitelist = '10.0.0.0/8'
        self._flush()
        r = self._post(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
            headers={
                'X-Forwarded-For': '10.0.0.5',
                'X-Forwarded-Host': 'example.org',
            },
        )
        # Real socket peer is 127.0.0.1, whitelist is 10/8 → 403.
        self.assertEqual(r.status_code, 403)

    def test_xforwarded_honored_with_proxy_mode(self):
        # With proxy_mode on, Odoo's ProxyFix rewrites remote_addr from
        # X-Forwarded-For, so the whitelist sees the real client IP.
        from odoo.tools import config
        config['proxy_mode'] = True
        try:
            self.cfg.ip_whitelist = '10.0.0.0/8'
            self._flush()
            r = self._post(
                {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
                headers={
                    'X-Forwarded-For': '10.0.0.5',
                    'X-Forwarded-Host': 'example.org',
                },
            )
            self.assertEqual(r.status_code, 200)
        finally:
            config['proxy_mode'] = False

    def test_proxy_mode_warning_shows_when_whitelist_set(self):
        from odoo.tools import config
        config['proxy_mode'] = False
        self.cfg.ip_whitelist = '10.0.0.0/8'
        self.cfg.invalidate_recordset(['proxy_mode_warning'])
        self.assertIn('proxy_mode', self.cfg.proxy_mode_warning or '')

    def test_proxy_mode_warning_hidden_when_proxy_on(self):
        from odoo.tools import config
        config['proxy_mode'] = True
        try:
            self.cfg.ip_whitelist = '10.0.0.0/8'
            self.cfg.invalidate_recordset(['proxy_mode_warning'])
            self.assertFalse(self.cfg.proxy_mode_warning)
        finally:
            config['proxy_mode'] = False

    def test_proxy_mode_warning_hidden_when_no_whitelist(self):
        self.cfg.ip_whitelist = False
        self.cfg.invalidate_recordset(['proxy_mode_warning'])
        self.assertFalse(self.cfg.proxy_mode_warning)

    # ---- rate_limit_per_minute ----

    def test_rate_limit_returns_429_after_quota(self):
        self.cfg.rate_limit_per_minute = 3
        self._flush()
        for _ in range(3):
            r = self._post({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
            self.assertEqual(r.status_code, 200)
        r = self._post({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.headers.get('Retry-After'), '60')

    def test_rate_limit_zero_disables(self):
        self.cfg.rate_limit_per_minute = 0
        self._flush()
        for _ in range(10):
            r = self._post({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
            self.assertEqual(r.status_code, 200)

    # ---- enable_cors ----

    def test_cors_off_no_headers(self):
        self.cfg.enable_cors = False
        self._flush()
        r = self._post({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
        self.assertNotIn('Access-Control-Allow-Origin', r.headers)

    def test_cors_on_emits_headers(self):
        self.cfg.enable_cors = True
        self._flush()
        r = self._post({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
        self.assertEqual(r.headers.get('Access-Control-Allow-Origin'), '*')

    def test_cors_preflight_options(self):
        self.cfg.enable_cors = True
        self._flush()
        r = self._request('OPTIONS', headers={
            'Origin': 'https://example.org',
            'Access-Control-Request-Method': 'POST',
        })
        self.assertEqual(r.status_code, 204)
        self.assertEqual(r.headers.get('Access-Control-Allow-Origin'), '*')
        self.assertIn('POST', r.headers.get('Access-Control-Allow-Methods', ''))

    def test_options_405_when_cors_off(self):
        self.cfg.enable_cors = False
        self._flush()
        r = self._request('OPTIONS')
        self.assertEqual(r.status_code, 405)

    # ---- audit_log ----

    def test_audit_log_records_successful_call(self):
        self.cfg.audit_log = True
        self._flush()
        r = self._post({
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'list_models', 'arguments': {}},
        })
        self.assertEqual(r.status_code, 200)
        self.env['ow.mcp.audit.log'].invalidate_model()
        rows = self.env['ow.mcp.audit.log'].sudo().search([
            ('login', '=', 'mcpsec'),
        ])
        self.assertTrue(rows)
        row = rows[0]
        self.assertEqual(row.method, 'tools/call')
        self.assertEqual(row.tool_name, 'list_models')
        self.assertTrue(row.success)
        self.assertEqual(row.ip_address, '127.0.0.1')

    def test_audit_log_records_error(self):
        self.cfg.audit_log = True
        self._flush()
        r = self._post({
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'search_records',
                       'arguments': {'model': 'ir.config_parameter'}},
        })
        self.assertEqual(r.status_code, 200)  # JSON-RPC error inside 200
        self.env['ow.mcp.audit.log'].invalidate_model()
        rows = self.env['ow.mcp.audit.log'].sudo().search([
            ('login', '=', 'mcpsec'),
            ('success', '=', False),
        ])
        self.assertTrue(rows)
        self.assertEqual(rows[0].tool_name, 'search_records')
        self.assertEqual(rows[0].model_name, 'ir.config_parameter')

    def test_audit_log_off_no_rows(self):
        self.cfg.audit_log = False
        self._flush()
        before = self.env['ow.mcp.audit.log'].sudo().search_count([])
        self._post({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
        after = self.env['ow.mcp.audit.log'].sudo().search_count([])
        self.assertEqual(before, after)


class TestIpAllowedHelper(HttpCase):
    """Unit tests for the _ip_allowed helper."""

    def test_helper(self):
        from odoo.addons.ow_mcp_server.controllers.mcp_controller import _ip_allowed
        self.assertTrue(_ip_allowed('1.2.3.4', None))
        self.assertTrue(_ip_allowed('1.2.3.4', ''))
        self.assertTrue(_ip_allowed('1.2.3.4', '   \n  '))
        self.assertTrue(_ip_allowed('10.0.0.5', '10.0.0.0/8'))
        self.assertTrue(_ip_allowed('127.0.0.1', '# comment\n127.0.0.1'))
        self.assertFalse(_ip_allowed('8.8.8.8', '10.0.0.0/8\n127.0.0.0/8'))
        self.assertFalse(_ip_allowed(None, '10.0.0.0/8'))
        self.assertFalse(_ip_allowed('not-an-ip', '10.0.0.0/8'))
        self.assertTrue(_ip_allowed('1.2.3.4', 'garbage\n1.2.3.4'))

    def test_helper_ipv6(self):
        from odoo.addons.ow_mcp_server.controllers.mcp_controller import _ip_allowed
        # Literal IPv6 match.
        self.assertTrue(_ip_allowed('::1', '::1'))
        self.assertTrue(_ip_allowed('2001:db8::1', '2001:db8::/32'))
        self.assertFalse(_ip_allowed('2001:dead::1', '2001:db8::/32'))
        # Mixed v4 + v6 whitelist works for both.
        mixed = '10.0.0.0/8\n::1\n2001:db8::/32'
        self.assertTrue(_ip_allowed('10.1.2.3', mixed))
        self.assertTrue(_ip_allowed('::1', mixed))
        self.assertTrue(_ip_allowed('2001:db8::5', mixed))
        self.assertFalse(_ip_allowed('8.8.8.8', mixed))
        self.assertFalse(_ip_allowed('fe80::1', mixed))
        # Zone-id is stripped before parsing.
        self.assertTrue(_ip_allowed('fe80::1%eth0', 'fe80::/10'))
        # IPv4-mapped IPv6 client matches an IPv4 whitelist entry.
        self.assertTrue(_ip_allowed('::ffff:10.0.0.5', '10.0.0.0/8'))
        self.assertFalse(_ip_allowed('::ffff:8.8.8.8', '10.0.0.0/8'))
        # v4 client must not match a v6-only whitelist and vice versa.
        self.assertFalse(_ip_allowed('10.0.0.1', '::/0'))
        self.assertFalse(_ip_allowed('::1', '0.0.0.0/0'))
        # Garbage v6 entries are skipped, valid ones still match.
        self.assertTrue(_ip_allowed('::1', 'not-an-addr\n::1'))
