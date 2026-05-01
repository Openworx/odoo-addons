"""Tests for PKCE enforcement at the token endpoint.

The /authorize browser flow is exercised end-to-end in test_mcp_with_oauth.
Here we focus on the PKCE check at /token by issuing a code directly with
a known challenge and trying various verifiers.
"""
from odoo.tests import HttpCase, tagged

from . import _helpers as h


@tagged('post_install', '-at_install')
class TestAuthorizePKCE(HttpCase):
    def setUp(self):
        super().setUp()
        self.user = self.env['res.users'].create({
            'login': 'oauthuser',
            'name': 'OAuth User',
            'password': 'oauthpass',
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

    def test_correct_verifier_accepted(self):
        verifier, challenge = h.make_pkce_pair()
        code = h.issue_code_directly(
            self, client=self.client, user=self.user,
            redirect_uri='http://127.0.0.1:9999/callback',
            code_challenge=challenge,
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
        body = r.json()
        self.assertIn('access_token', body)
        self.assertEqual(body['token_type'], 'Bearer')

    def test_wrong_verifier_rejected(self):
        _verifier, challenge = h.make_pkce_pair()
        code = h.issue_code_directly(
            self, client=self.client, user=self.user,
            redirect_uri='http://127.0.0.1:9999/callback',
            code_challenge=challenge,
            resource=self.cfg.effective_canonical_resource_uri(),
        )
        r = h.post_token(self,
            grant_type='authorization_code',
            code=code,
            redirect_uri='http://127.0.0.1:9999/callback',
            client_id=self.client.client_id,
            code_verifier='not-the-right-verifier-12345678901234567890123',
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()['error'], 'invalid_grant')

    def test_redirect_uri_must_match(self):
        verifier, challenge = h.make_pkce_pair()
        code = h.issue_code_directly(
            self, client=self.client, user=self.user,
            redirect_uri='http://127.0.0.1:9999/callback',
            code_challenge=challenge,
            resource=self.cfg.effective_canonical_resource_uri(),
        )
        r = h.post_token(self,
            grant_type='authorization_code',
            code=code,
            redirect_uri='http://127.0.0.1:9999/other',
            client_id=self.client.client_id,
            code_verifier=verifier,
        )
        self.assertEqual(r.status_code, 400)
