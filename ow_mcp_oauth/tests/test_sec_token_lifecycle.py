"""Token lifecycle.
  - revoking a refresh chain (reuse detection or /revoke) also revokes the
    access tokens already issued to that client+user;
  - a refresh chain has an absolute lifetime (no endless rotation);
  - changing the password, deactivating the user or removing them from
    "MCP User" revokes / refuses their tokens.
"""
import json
import time

from odoo.tests import HttpCase, tagged

from odoo.addons.ow_mcp_server.protocol import authfail

from . import _helpers as h

REDIR = 'http://127.0.0.1:9999/callback'


@tagged('post_install', '-at_install')
class TestTokenLifecycle(HttpCase):
    def setUp(self):
        super().setUp()
        authfail.reset()
        self.addCleanup(authfail.reset)
        self.user = self.env['res.users'].create({
            'login': 'lifeuser', 'name': 'Life', 'password': 'lifeuser-Pass-1',
            'group_ids': [
                (4, self.env.ref('ow_mcp_server.group_mcp_user').id),
                (4, self.env.ref('base.group_user').id)]})
        self.cfg = self.env['ow.mcp.oauth.config'].sudo().get_singleton()
        self.client = self.env['ow.mcp.oauth.client'].sudo().create({
            'client_name': 'life', 'is_dynamic': False,
            'redirect_uris': json.dumps([REDIR])})

    def _pair(self):
        verifier, challenge = h.make_pkce_pair()
        code = h.issue_code_directly(
            self, client=self.client, user=self.user, redirect_uri=REDIR,
            code_challenge=challenge,
            resource=self.cfg.effective_canonical_resource_uri())
        r = h.post_token(self, grant_type='authorization_code', code=code,
                         redirect_uri=REDIR, client_id=self.client.client_id,
                         code_verifier=verifier)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _refresh(self, rt):
        return h.post_token(self, grant_type='refresh_token', refresh_token=rt,
                            client_id=self.client.client_id)

    def _ping(self, at):
        return self.url_open('/mcp', data=json.dumps(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}).encode(),
            headers={'Content-Type': 'application/json',
                     'Authorization': f'Bearer {at}'}).status_code

    def test_reuse_detection_revokes_issued_access_tokens(self):
        first = self._pair()
        second = self._refresh(first['refresh_token']).json()
        self.assertEqual(self._ping(second['access_token']), 200)
        r = self._refresh(first['refresh_token'])  # replay
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._ping(second['access_token']), 401)

    def test_revoking_refresh_revokes_access_token(self):
        pair = self._pair()
        r = self.url_open('/ow_mcp/oauth/revoke', data={
            'token': pair['refresh_token'],
            'client_id': self.client.client_id})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._ping(pair['access_token']), 401)

    def test_refresh_chain_has_absolute_lifetime(self):
        pair = self._pair()
        rt = self.env['ow.mcp.oauth.refresh_token'].sudo().search(
            [('client_id', '=', self.client.id)], limit=1)
        rt.chain_issued_at = int(time.time()) - (
            self.cfg.refresh_token_absolute_ttl_seconds + 60)
        self.env.flush_all()
        r = self._refresh(pair['refresh_token'])
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()['error'], 'invalid_grant')

    def test_rotation_keeps_chain_start(self):
        pair = self._pair()
        RT = self.env['ow.mcp.oauth.refresh_token'].sudo()
        root = RT.search([('client_id', '=', self.client.id)], limit=1)
        self._refresh(pair['refresh_token'])
        child = RT.search([('parent_id', '=', root.id)], limit=1)
        self.assertTrue(child)
        self.assertEqual(child.chain_issued_at, root.chain_issued_at)

    def test_password_change_revokes_tokens(self):
        pair = self._pair()
        self.user.password = 'lifeuser-Pass-2'
        self.env.flush_all()
        self.assertEqual(self._ping(pair['access_token']), 401)
        self.assertEqual(self._refresh(pair['refresh_token']).status_code, 400)

    def test_deactivated_user_cannot_refresh(self):
        pair = self._pair()
        self.user.active = False
        self.env.flush_all()
        self.assertEqual(self._refresh(pair['refresh_token']).status_code, 400)

    def test_user_removed_from_mcp_group_cannot_refresh(self):
        pair = self._pair()
        self.user.group_ids = [
            (3, self.env.ref('ow_mcp_server.group_mcp_user').id)]
        self.env.flush_all()
        self.assertEqual(self._refresh(pair['refresh_token']).status_code, 400)
