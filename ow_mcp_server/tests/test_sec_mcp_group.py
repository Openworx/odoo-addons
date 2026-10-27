"""Only active internal users in
"MCP User" may use /mcp. A valid API key alone (e.g. a global key created
for another integration, or a portal user's key) is not enough.
"""
import json
from datetime import datetime, timedelta

from odoo.tests import HttpCase, tagged

from odoo.addons.ow_mcp_server.protocol import authfail


@tagged('post_install', '-at_install')
class TestMcpUserGroupRequired(HttpCase):
    def setUp(self):
        super().setUp()
        authfail.reset()
        self.addCleanup(authfail.reset)
        self.g_mcp = self.env.ref('ow_mcp_server.group_mcp_user')
        self.g_internal = self.env.ref('base.group_user')
        self.g_portal = self.env.ref('base.group_portal')
        partner = self.env.ref('base.model_res_partner').id
        self.env['ow.mcp.model.access'].sudo().search(
            [('model_id', '=', partner)]
        ).unlink()
        self.env['ow.mcp.model.access'].create({
            'model_id': partner, 'allow_read': True,
        })

    def _key_for(self, login, groups):
        user = self.env['res.users'].create({
            'login': login, 'name': login, 'password': login,
            'group_ids': [(6, 0, groups.ids)],
        })
        key = self.env['res.users.apikeys'].with_user(user).sudo()._generate(
            'mcp', 'k', datetime.now() + timedelta(days=1),
        )
        return user, key

    def _call(self, key, method='GET'):
        body = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'search_count', 'arguments': {'model': 'res.partner'}}}
        return self.url_open('/mcp', data=json.dumps(body).encode(), headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {key}',
        })

    def test_internal_user_with_group_allowed(self):
        _u, key = self._key_for('grp_ok', self.g_internal | self.g_mcp)
        r = self._call(key)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()['result'].get('isError'), r.text)

    def test_internal_user_without_group_forbidden(self):
        _u, key = self._key_for('grp_none', self.g_internal)
        r = self._call(key)
        self.assertEqual(r.status_code, 403, r.text)

    def test_portal_user_forbidden_even_with_group(self):
        _u, key = self._key_for('grp_portal', self.g_portal | self.g_mcp)
        r = self._call(key)
        self.assertEqual(r.status_code, 403, r.text)
