"""End-to-end: register → code exchange → /mcp call. Also verifies scope
gating rejects write tools when the token only carries mcp:read.
"""
import json

from odoo.tests import HttpCase, tagged

from . import _helpers as h


@tagged('post_install', '-at_install')
class TestMcpWithOauth(HttpCase):
    def setUp(self):
        super().setUp()
        self.user = self.env['res.users'].create({
            'login': 'e2euser',
            'name': 'E2E User',
            'password': 'pass',
            'groups_id': [
                (4, self.env.ref('ow_mcp_server.group_mcp_user').id),
                (4, self.env.ref('base.group_user').id),
            ],
        })
        self.dcr = h.register_client(self)
        self.client = self.env['ow.mcp.oauth.client'].sudo().search(
            [('client_id', '=', self.dcr['client_id'])], limit=1,
        )
        self.cfg = self.env['ow.mcp.oauth.config'].sudo().get_singleton()
        partner = self.env.ref('base.model_res_partner').id
        self.env['ow.mcp.model.access'].sudo().search(
            [('model_id', '=', partner)]
        ).unlink()
        self.env['ow.mcp.model.access'].create({
            'model_id': partner,
            'allow_read': True,
            'allow_create': True,
            'allow_write': True,
            'allow_delete': True,
        })

    def _mint(self, scope='mcp:read mcp:write'):
        verifier, challenge = h.make_pkce_pair()
        code = h.issue_code_directly(
            self, client=self.client, user=self.user,
            redirect_uri='http://127.0.0.1:9999/callback',
            code_challenge=challenge, scope=scope,
            resource=self.cfg.effective_canonical_resource_uri(),
        )
        r = h.post_token(self,
            grant_type='authorization_code',
            code=code,
            redirect_uri='http://127.0.0.1:9999/callback',
            client_id=self.client.client_id,
            code_verifier=verifier,
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()['access_token']

    def _mcp(self, body, token):
        return self.url_open(
            '/mcp',
            data=json.dumps(body).encode(),
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {token}',
            },
        )

    def test_initialize_with_full_scope(self):
        token = self._mint('mcp:read mcp:write')
        r = self._mcp(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'}, token,
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['result']['protocolVersion'], '2025-06-18')

    def test_tools_list_with_read_only_scope(self):
        token = self._mint('mcp:read')
        r = self._mcp(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, token,
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn('tools', r.json()['result'])

    def test_write_tool_blocked_without_write_scope(self):
        token = self._mint('mcp:read')
        r = self._mcp({
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {
                'name': 'create_record',
                'arguments': {
                    'model': 'res.partner',
                    'values': {'name': 'Hacker'},
                },
            },
        }, token)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        # MCP idiom: tools/call returns isError=True in result, not a
        # JSON-RPC error.
        result = body.get('result', {})
        self.assertTrue(result.get('isError'),
                        f'expected isError=True, got {result}')
        text = (result.get('content') or [{}])[0].get('text', '')
        self.assertIn('mcp:write', text)
