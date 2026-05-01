from odoo.tests import HttpCase, tagged

from . import _helpers as h


@tagged('post_install', '-at_install')
class TestTokenExchange(HttpCase):
    def setUp(self):
        super().setUp()
        self.user = self.env['res.users'].create({
            'login': 'tokenuser',
            'name': 'Token User',
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

    def test_code_is_single_use(self):
        verifier, challenge = h.make_pkce_pair()
        code = h.issue_code_directly(
            self, client=self.client, user=self.user,
            redirect_uri='http://127.0.0.1:9999/callback',
            code_challenge=challenge,
            resource=self.cfg.effective_canonical_resource_uri(),
        )
        first = h.post_token(self,
            grant_type='authorization_code',
            code=code,
            redirect_uri='http://127.0.0.1:9999/callback',
            client_id=self.client.client_id,
            code_verifier=verifier,
        )
        self.assertEqual(first.status_code, 200)

        second = h.post_token(self,
            grant_type='authorization_code',
            code=code,
            redirect_uri='http://127.0.0.1:9999/callback',
            client_id=self.client.client_id,
            code_verifier=verifier,
        )
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.json()['error'], 'invalid_grant')

    def test_unknown_code_rejected(self):
        r = h.post_token(self,
            grant_type='authorization_code',
            code='no-such-code',
            redirect_uri='http://127.0.0.1:9999/callback',
            client_id=self.client.client_id,
            code_verifier='whatever',
        )
        self.assertEqual(r.status_code, 400)

    def test_unknown_grant_rejected(self):
        r = h.post_token(self,
            grant_type='client_credentials',
            client_id=self.client.client_id,
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()['error'], 'unsupported_grant_type')

    def test_jwt_aud_matches_canonical(self):
        verifier, challenge = h.make_pkce_pair()
        canonical = self.cfg.effective_canonical_resource_uri()
        code = h.issue_code_directly(
            self, client=self.client, user=self.user,
            redirect_uri='http://127.0.0.1:9999/callback',
            code_challenge=challenge, resource=canonical,
        )
        r = h.post_token(self,
            grant_type='authorization_code',
            code=code,
            redirect_uri='http://127.0.0.1:9999/callback',
            client_id=self.client.client_id,
            code_verifier=verifier,
            resource=canonical,
        )
        self.assertEqual(r.status_code, 200)
        token = r.json()['access_token']
        # Decode without verifying — we just want the claims.
        import jwt as _jwt
        payload = _jwt.decode(
            token, options={'verify_signature': False, 'verify_aud': False},
        )
        self.assertEqual(payload['aud'], canonical)
        self.assertEqual(payload['sub'], str(self.user.id))
