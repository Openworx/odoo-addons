"""Tokens with the wrong `aud` claim must be rejected at /mcp.

This is the MCP spec's confused-deputy defense (RFC 8707). We mint a token
with the correct aud (happy path), then mutate the singleton's canonical
URI to simulate a server reconfigured to live under a different name —
the previously-issued token must stop validating.
"""
import json

from odoo.tests import HttpCase, tagged

from . import _helpers as h


@tagged('post_install', '-at_install')
class TestAudienceBinding(HttpCase):
    def setUp(self):
        super().setUp()
        self.user = self.env['res.users'].create({
            'login': 'auduser',
            'name': 'Audience User',
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
        })

    def _issue_token(self, canonical):
        verifier, challenge = h.make_pkce_pair()
        code = h.issue_code_directly(
            self, client=self.client, user=self.user,
            redirect_uri='http://127.0.0.1:9999/callback',
            code_challenge=challenge,
            resource=canonical,
        )
        r = h.post_token(self,
            grant_type='authorization_code',
            code=code,
            redirect_uri='http://127.0.0.1:9999/callback',
            client_id=self.client.client_id,
            code_verifier=verifier,
            resource=canonical,
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()['access_token']

    def test_correct_audience_accepted(self):
        canonical = self.cfg.effective_canonical_resource_uri()
        token = self._issue_token(canonical)
        r = self.url_open(
            '/mcp',
            data=json.dumps({
                'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
            }).encode(),
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {token}',
            },
        )
        self.assertEqual(r.status_code, 200, r.text)

    def test_mismatched_audience_rejected(self):
        canonical = self.cfg.effective_canonical_resource_uri()
        token = self._issue_token(canonical)
        # Move the goalposts: server now expects a different canonical URI.
        original = self.cfg.canonical_resource_uri
        self.cfg.canonical_resource_uri = 'https://other.example.com/mcp'
        try:
            r = self.url_open(
                '/mcp',
                data=json.dumps({
                    'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                }).encode(),
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {token}',
                },
            )
            self.assertEqual(r.status_code, 401)
            self.assertIn('resource_metadata',
                          r.headers.get('WWW-Authenticate', ''))
        finally:
            self.cfg.canonical_resource_uri = original
