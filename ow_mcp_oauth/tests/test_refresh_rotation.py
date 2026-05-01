from odoo.tests import HttpCase, tagged

from . import _helpers as h


@tagged('post_install', '-at_install')
class TestRefreshRotation(HttpCase):
    def setUp(self):
        super().setUp()
        self.user = self.env['res.users'].create({
            'login': 'refresher',
            'name': 'Refresher',
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
        self.canonical = self.cfg.effective_canonical_resource_uri()

    def _initial_token(self):
        verifier, challenge = h.make_pkce_pair()
        code = h.issue_code_directly(
            self, client=self.client, user=self.user,
            redirect_uri='http://127.0.0.1:9999/callback',
            code_challenge=challenge, resource=self.canonical,
        )
        r = h.post_token(self,
            grant_type='authorization_code',
            code=code,
            redirect_uri='http://127.0.0.1:9999/callback',
            client_id=self.client.client_id,
            code_verifier=verifier,
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_refresh_returns_new_pair(self):
        first = self._initial_token()
        old_refresh = first['refresh_token']

        r = h.post_token(self,
            grant_type='refresh_token',
            refresh_token=old_refresh,
            client_id=self.client.client_id,
        )
        self.assertEqual(r.status_code, 200, r.text)
        second = r.json()
        self.assertIn('access_token', second)
        self.assertIn('refresh_token', second)
        self.assertNotEqual(second['refresh_token'], old_refresh)

    def test_refresh_reuse_revokes_chain(self):
        first = self._initial_token()
        old_refresh = first['refresh_token']

        # First exchange consumes old_refresh, returns new_refresh.
        r1 = h.post_token(self,
            grant_type='refresh_token',
            refresh_token=old_refresh,
            client_id=self.client.client_id,
        )
        self.assertEqual(r1.status_code, 200)
        new_refresh = r1.json()['refresh_token']

        # Replay old_refresh → must fail and burn the chain.
        r2 = h.post_token(self,
            grant_type='refresh_token',
            refresh_token=old_refresh,
            client_id=self.client.client_id,
        )
        self.assertEqual(r2.status_code, 400)
        self.assertEqual(r2.json()['error'], 'invalid_grant')

        # The brand-new token from the previous exchange is now revoked.
        r3 = h.post_token(self,
            grant_type='refresh_token',
            refresh_token=new_refresh,
            client_id=self.client.client_id,
        )
        self.assertEqual(r3.status_code, 400)
        self.assertEqual(r3.json()['error'], 'invalid_grant')
