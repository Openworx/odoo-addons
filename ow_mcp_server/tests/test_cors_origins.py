"""M7: the `cors_origins` allowlist restricts which Origin headers are
echoed back. Empty list keeps the legacy wildcard behaviour (backward
compatible with existing deployments).
"""
import json
from datetime import datetime, timedelta

from odoo.tests import HttpCase, tagged

from odoo.addons.ow_mcp_server.protocol import authfail, ratelimit


@tagged('post_install', '-at_install')
class TestCorsOrigins(HttpCase):
    def setUp(self):
        super().setUp()
        ratelimit.reset()
        authfail.reset()
        self.cfg = self.env['ow.mcp.config'].get_singleton()
        self.cfg.write({
            'enable_http_transport': True,
            'enable_sse': False,
            'enable_cors': True,
            'cors_origins': False,
            'rate_limit_per_minute': 0,
            'audit_log': False,
            'ip_whitelist': False,
        })
        self.user = self.env['res.users'].create({
            'login': 'mcpcors',
            'name': 'MCP Cors',
            'password': 'x',
            'groups_id': [
                (4, self.env.ref('ow_mcp_server.group_mcp_user').id),
                (4, self.env.ref('base.group_user').id),
            ],
        })
        self.key = (
            self.env['res.users.apikeys'].with_user(self.user)
            ._generate('mcp', 'cors', datetime.now() + timedelta(days=1))
        )
        self.env.flush_all()

    def _post(self, body, origin=None):
        h = {'Content-Type': 'application/json',
             'Authorization': f'Bearer {self.key}'}
        if origin is not None:
            h['Origin'] = origin
        return self.url_open(
            '/mcp', data=json.dumps(body).encode(), headers=h,
        )

    def test_empty_allowlist_falls_back_to_wildcard(self):
        self.cfg.cors_origins = False
        self.env.flush_all()
        r = self._post(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
            origin='https://example.org',
        )
        self.assertEqual(r.headers.get('Access-Control-Allow-Origin'), '*')

    def test_allowlisted_origin_is_echoed(self):
        self.cfg.cors_origins = 'https://chat.example.com\nhttps://cursor.sh'
        self.env.flush_all()
        r = self._post(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
            origin='https://chat.example.com',
        )
        self.assertEqual(
            r.headers.get('Access-Control-Allow-Origin'),
            'https://chat.example.com',
        )
        # Vary: Origin must be set whenever the response depends on the
        # Origin header so caches don't cross-contaminate.
        self.assertIn('Origin', r.headers.get('Vary', ''))

    def test_non_allowlisted_origin_gets_no_allow_origin(self):
        self.cfg.cors_origins = 'https://chat.example.com'
        self.env.flush_all()
        r = self._post(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
            origin='https://attacker.example',
        )
        self.assertNotIn('Access-Control-Allow-Origin', r.headers)

    def test_missing_origin_with_allowlist_set(self):
        self.cfg.cors_origins = 'https://chat.example.com'
        self.env.flush_all()
        r = self._post({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
        self.assertNotIn('Access-Control-Allow-Origin', r.headers)

    def test_commented_and_blank_lines_ignored(self):
        self.cfg.cors_origins = (
            '# trusted clients\n'
            '\n'
            'https://chat.example.com\n'
        )
        self.env.flush_all()
        r = self._post(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
            origin='https://chat.example.com',
        )
        self.assertEqual(
            r.headers.get('Access-Control-Allow-Origin'),
            'https://chat.example.com',
        )
