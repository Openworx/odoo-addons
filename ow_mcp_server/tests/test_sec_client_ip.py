"""A client-supplied X-Forwarded-For must
not move the effective IP used by ip_whitelist, the auth-failure throttle
and the audit log.
"""
import json
from datetime import datetime, timedelta
from unittest.mock import patch

from odoo.tests import HttpCase, tagged

from odoo.addons.ow_mcp_server.controllers import mcp_controller as ctrl
from odoo.addons.ow_mcp_server.protocol import authfail


@tagged('post_install', '-at_install')
class TestSpoofedForwardedFor(HttpCase):
    def setUp(self):
        super().setUp()
        authfail.reset()
        self.addCleanup(authfail.reset)
        self.cfg = self.env['ow.mcp.config'].get_singleton()
        user = self.env['res.users'].create({
            'login': 'mcpip', 'name': 'MCP IP', 'password': 'mcpip',
            'groups_id': [
                (4, self.env.ref('ow_mcp_server.group_mcp_user').id),
                (4, self.env.ref('base.group_user').id),
            ],
        })
        self.key = self.env['res.users.apikeys'].with_user(user)._generate(
            'mcp', 'ip', datetime.now() + timedelta(days=1),
        )
        # Behave as in production (proxy_mode = True in odoo.conf).
        patcher = patch.object(ctrl, 'odoo_config', {'proxy_mode': True})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _post(self, key, xff):
        return self.url_open('/mcp', data=json.dumps(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
        ).encode(), headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {key}',
            'X-Forwarded-For': xff,
        })

    def test_whitelist_not_bypassed_by_spoofed_xff(self):
        self.cfg.ip_whitelist = '203.0.113.7/32'
        self.env.flush_all()
        r = self._post(self.key, '203.0.113.7')
        self.assertEqual(r.status_code, 403, r.text)

    def test_throttle_not_evaded_by_rotating_xff(self):
        for i in range(authfail.THRESHOLD):
            self._post('bad-key', f'198.51.100.{i}')
        r = self._post('bad-key', '198.51.100.250')
        self.assertEqual(r.status_code, 429, r.text)
