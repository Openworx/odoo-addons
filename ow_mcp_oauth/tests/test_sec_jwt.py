"""- M3: a JWT signed with the server key is only valid if its jti is a
    live row of ow.mcp.oauth.access_token for the same user and an active
    client (a DB copy/backup holds the private key: forging must not work);
  - i3: header typ must be at+jwt (RFC 9068 §4); a missing token gets a
    401 challenge without error="invalid_token" (RFC 6750 §3.1);
"""
import json
import secrets
import time
from datetime import timedelta

import jwt

from odoo import fields
from odoo.tests import HttpCase, tagged

from odoo.addons.ow_mcp_server.protocol import authfail


@tagged('post_install', '-at_install')
class TestJwtMustBeRegistered(HttpCase):
    def setUp(self):
        super().setUp()
        authfail.reset()
        self.addCleanup(authfail.reset)
        self.user = self.env['res.users'].create({
            'login': 'jtiuser', 'name': 'JTI', 'password': 'jtiuser',
            'groups_id': [
                (4, self.env.ref('ow_mcp_server.group_mcp_user').id),
                (4, self.env.ref('base.group_user').id)],
        })
        self.cfg = self.env['ow.mcp.oauth.config'].sudo().get_singleton()
        self.client = self.env['ow.mcp.oauth.client'].sudo().create({
            'client_name': 'jti', 'is_dynamic': False,
            'redirect_uris': json.dumps(['http://127.0.0.1:9999/callback'])})

    def _forge(self, *, jti=None, sub=None, typ='at+jwt', register=True,
               user=None):
        now = int(time.time())
        jti = jti or secrets.token_urlsafe(16)
        payload = {
            'iss': self.cfg.issuer_url(),
            'aud': self.cfg.effective_canonical_resource_uri(),
            'sub': str(sub or self.user.id), 'client_id': self.client.client_id,
            'iat': now, 'exp': now + 600, 'jti': jti,
            'scope': 'mcp:read mcp:write'}
        tok = jwt.encode(payload, self.cfg.rsa_private_key_pem,
                         algorithm='RS256',
                         headers={'kid': self.cfg.rsa_private_key_kid,
                                  'typ': typ})
        if register:
            self.env['ow.mcp.oauth.access_token'].sudo().create({
                'jti': jti, 'client_id': self.client.id,
                'user_id': (user or self.user).id, 'scope': payload['scope'],
                'audience': payload['aud'], 'issued_at': now,
                'expires_at': fields.Datetime.now() + timedelta(minutes=10)})
        return tok

    def _ping(self, token):
        return self.url_open('/mcp', data=json.dumps(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}).encode(),
            headers={'Content-Type': 'application/json',
                     'Authorization': f'Bearer {token}'})

    def test_registered_token_accepted(self):
        self.assertEqual(self._ping(self._forge()).status_code, 200)

    def test_unknown_jti_rejected(self):
        r = self._ping(self._forge(register=False))
        self.assertEqual(r.status_code, 401, r.text)

    def test_jti_of_another_user_rejected(self):
        other = self.env['res.users'].create({
            'login': 'jtiother', 'name': 'Other', 'password': 'x',
            'groups_id': [(4, self.env.ref('base.group_user').id)]})
        r = self._ping(self._forge(user=other))
        self.assertEqual(r.status_code, 401, r.text)

    def test_inactive_client_rejected(self):
        tok = self._forge()
        self.client.active = False
        self.assertEqual(self._ping(tok).status_code, 401)

    def test_wrong_typ_rejected(self):
        self.assertEqual(self._ping(self._forge(typ='JWT')).status_code, 401)

    def test_missing_token_challenge_has_no_error_code(self):
        r = self.url_open('/mcp', data=b'{}', headers={
            'Content-Type': 'application/json'})
        self.assertEqual(r.status_code, 401)
        www = r.headers.get('WWW-Authenticate', '')
        self.assertIn('resource_metadata', www)
        self.assertNotIn('error=', www)
