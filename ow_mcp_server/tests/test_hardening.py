"""Tests for the transport-layer hardening features:
max body size, require_https, auth-failure throttle.
"""
import json
from datetime import datetime, timedelta

from odoo.tests import HttpCase, tagged
from odoo.tests.common import TransactionCase

from odoo.addons.ow_mcp_server.protocol import authfail


@tagged('post_install', '-at_install')
class TestAuthFailUnit(TransactionCase):
    def setUp(self):
        super().setUp()
        authfail.reset()
        self.addCleanup(authfail.reset)

    def test_empty_ip_never_throttles(self):
        self.assertFalse(authfail.is_throttled(''))
        self.assertFalse(authfail.is_throttled(None))
        authfail.register_failure('')  # no-op
        self.assertFalse(authfail.is_throttled(''))

    def test_threshold_triggers_throttle(self):
        ip = '203.0.113.7'
        for _ in range(authfail.THRESHOLD - 1):
            authfail.register_failure(ip)
        self.assertFalse(authfail.is_throttled(ip))
        authfail.register_failure(ip)
        self.assertTrue(authfail.is_throttled(ip))

    def test_different_ips_are_isolated(self):
        for _ in range(authfail.THRESHOLD):
            authfail.register_failure('10.0.0.1')
        self.assertTrue(authfail.is_throttled('10.0.0.1'))
        self.assertFalse(authfail.is_throttled('10.0.0.2'))

    def test_reset_clears_state(self):
        for _ in range(authfail.THRESHOLD):
            authfail.register_failure('10.0.0.1')
        authfail.reset()
        self.assertFalse(authfail.is_throttled('10.0.0.1'))


@tagged('post_install', '-at_install')
class TestHardeningController(HttpCase):
    def setUp(self):
        super().setUp()
        authfail.reset()
        self.cfg = self.env['ow.mcp.config'].get_singleton()
        self.cfg.write({
            'enable_http_transport': True,
            'require_https': False,
            'max_body_bytes': 1048576,
            'rate_limit_per_minute': 0,
            'audit_log': False,
            'ip_whitelist': False,
        })
        self.user = self.env['res.users'].create({
            'login': 'mcphard',
            'name': 'MCP Hard',
            'password': 'x',
            'groups_id': [
                (4, self.env.ref('ow_mcp_server.group_mcp_user').id),
                (4, self.env.ref('base.group_user').id),
            ],
        })
        self.key = (
            self.env['res.users.apikeys'].with_user(self.user)
            ._generate('mcp', 'hard', datetime.now() + timedelta(days=1))
        )
        self.env.flush_all()

    def _post(self, body, headers=None, auth=True):
        h = {'Content-Type': 'application/json'}
        if auth:
            h['Authorization'] = f'Bearer {self.key}'
        if headers:
            h.update(headers)
        return self.url_open(
            '/mcp', data=json.dumps(body).encode(), headers=h,
        )

    # ---- max_body_bytes ----

    def test_body_over_limit_returns_413(self):
        self.cfg.max_body_bytes = 256
        self.env.flush_all()
        big = {'jsonrpc': '2.0', 'id': 1, 'method': 'ping',
               'params': {'x': 'a' * 1024}}
        r = self._post(big)
        self.assertEqual(r.status_code, 413)

    def test_body_under_limit_passes(self):
        self.cfg.max_body_bytes = 1048576
        self.env.flush_all()
        r = self._post({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
        self.assertEqual(r.status_code, 200)

    def test_body_limit_zero_disables_check(self):
        self.cfg.max_body_bytes = 0
        self.env.flush_all()
        body = {'jsonrpc': '2.0', 'id': 1, 'method': 'ping',
                'params': {'x': 'a' * 10000}}
        r = self._post(body)
        self.assertEqual(r.status_code, 200)

    # ---- require_https ----

    def test_https_required_rejects_http(self):
        self.cfg.require_https = True
        self.env.flush_all()
        r = self._post({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
        self.assertEqual(r.status_code, 400)
        body = json.loads(r.text)
        self.assertIn('HTTPS', body['error']['message'])

    def test_https_off_by_default_allows_http(self):
        self.cfg.require_https = False
        self.env.flush_all()
        r = self._post({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
        self.assertEqual(r.status_code, 200)

    # ---- auth throttle ----

    def test_throttle_kicks_in_after_threshold_bad_keys(self):
        body = {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}
        bad = {'Content-Type': 'application/json',
               'Authorization': 'Bearer invalid-key'}
        # First THRESHOLD requests fail with 401.
        for _ in range(authfail.THRESHOLD):
            r = self.url_open(
                '/mcp', data=json.dumps(body).encode(), headers=bad,
            )
            self.assertEqual(r.status_code, 401)
        # The next one is throttled with 429 before auth runs.
        r = self.url_open(
            '/mcp', data=json.dumps(body).encode(), headers=bad,
        )
        self.assertEqual(r.status_code, 429)
        self.assertIn('Retry-After', r.headers)

    def test_throttle_does_not_block_valid_key_from_other_ip(self):
        # Burn the bucket for the test client's IP, then verify that a
        # throttled IP blocks even valid credentials from the *same* IP.
        bad = {'Content-Type': 'application/json',
               'Authorization': 'Bearer bogus'}
        body = {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}
        for _ in range(authfail.THRESHOLD):
            self.url_open(
                '/mcp', data=json.dumps(body).encode(), headers=bad,
            )
        # Valid key now blocked because the client IP is throttled.
        r = self._post(body)
        self.assertEqual(r.status_code, 429)
