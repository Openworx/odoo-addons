"""- M2/H7: DCR off by default; when on, redirect_uris must be on a host
    allow-list; the consent page cannot be framed, flags dynamically
    registered clients as unverified, shows the redirect host, and does
    not pre-check "remember" (consent_remember_days defaults to 0);
  - H8/m5: the consent POST re-validates scope against the client's
    registered scopes and the PKCE challenge;
  - M4/H5: only internal users in "MCP User" may authorize or get tokens;
  - m6: /register and /token bodies are size-capped.
"""
import json
import re
from urllib.parse import urlencode

from odoo.tests import HttpCase, tagged

from . import _helpers as h

REDIR = 'http://127.0.0.1:9999/callback'


@tagged('post_install', '-at_install')
class TestAuthorizationServerHardening(HttpCase):
    def setUp(self):
        super().setUp()
        h.reset_throttles(self.env)
        self.cfg = self.env['ow.mcp.oauth.config'].sudo().get_singleton()
        self.g_mcp = self.env.ref('ow_mcp_server.group_mcp_user')
        self.g_int = self.env.ref('base.group_user')
        self.user = self._user('azuser', self.g_int | self.g_mcp)

    def _user(self, login, groups):
        return self.env['res.users'].create({
            'login': login, 'name': login, 'password': login + '-Pass-1',
            'groups_id': [(6, 0, groups.ids)]})

    def _register(self, redirect, name='Claude'):
        self.cfg.oauth_dcr_enabled = True
        self.env.flush_all()
        return self.url_open('/ow_mcp/oauth/register', data=json.dumps({
            'client_name': name, 'redirect_uris': [redirect],
            'token_endpoint_auth_method': 'none'}).encode(),
            headers={'Content-Type': 'application/json'})

    def _client(self, scope='mcp:read mcp:write', dynamic=True):
        return self.env['ow.mcp.oauth.client'].sudo().create({
            'client_name': 'Claude', 'is_dynamic': dynamic, 'scope': scope,
            'redirect_uris': json.dumps([REDIR])})

    def _authorize(self, client, login, scope='mcp:read mcp:write'):
        _v, challenge = h.make_pkce_pair()
        self.authenticate(login, login + '-Pass-1')
        q = urlencode({
            'response_type': 'code', 'client_id': client.client_id,
            'redirect_uri': REDIR, 'scope': scope, 'state': 'st',
            'code_challenge': challenge, 'code_challenge_method': 'S256'})
        return self.url_open('/ow_mcp/oauth/authorize?' + q,
                             allow_redirects=False)

    # ---- defaults ----
    def test_secure_defaults(self):
        d = self.env['ow.mcp.oauth.config'].default_get(
            ['oauth_dcr_enabled', 'consent_remember_days'])
        self.assertFalse(d['oauth_dcr_enabled'])
        self.assertEqual(d['consent_remember_days'], 0)

    # ---- DCR redirect allow-list ----
    def test_dcr_rejects_unlisted_redirect_host(self):
        r = self._register('https://evil.example/cb')
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()['error'], 'invalid_redirect_uri')

    def test_dcr_accepts_claude_and_loopback(self):
        for uri in ('https://claude.ai/api/mcp/auth_callback',
                    'http://localhost:33418/callback'):
            with self.subTest(uri=uri):
                self.assertEqual(self._register(uri).status_code, 201)

    # ---- consent page ----
    def test_consent_page_hardened(self):
        # Set explicitly: an upgraded DB keeps its previous value (30).
        self.cfg.consent_remember_days = 0
        self.env.flush_all()
        r = self._authorize(self._client(), 'azuser')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers.get('X-Frame-Options'), 'DENY')
        self.assertIn("frame-ancestors 'none'",
                      r.headers.get('Content-Security-Policy', ''))
        self.assertIn('Unverified', r.text)
        self.assertIn('127.0.0.1', r.text)
        # consent_remember_days=0: no "remember" box at all ...
        self.assertNotIn('name="remember"', r.text)
        # ... and when enabled it is never pre-checked.
        self.cfg.consent_remember_days = 30
        self.env.flush_all()
        r = self._authorize(self._client(), 'azuser')
        box = re.search(r'<input[^>]*name="remember"[^>]*>', r.text)
        self.assertTrue(box, r.text)
        self.assertNotIn('checked', box.group(0))

    # ---- consent POST re-validation (H8) ----
    def test_consent_post_cannot_widen_scope(self):
        client = self._client(scope='mcp:read')
        page = self._authorize(client, 'azuser', scope='mcp:read')
        form = dict(re.findall(r'name="(\w+)"\s+value="([^"]*)"', page.text))
        form.update(decision='allow', scope='mcp:read mcp:write')
        r = self.url_open('/ow_mcp/oauth/authorize', data=form,
                          allow_redirects=False)
        self.assertNotIn('code=', r.headers.get('Location', ''))
        self.assertFalse(self.env['ow.mcp.oauth.authorization_code'].sudo()
                         .search_count([('client_id', '=', client.id)]))

    def test_consent_post_requires_valid_challenge(self):
        client = self._client()
        page = self._authorize(client, 'azuser')
        form = dict(re.findall(r'name="(\w+)"\s+value="([^"]*)"', page.text))
        form.update(decision='allow', code_challenge='short')
        r = self.url_open('/ow_mcp/oauth/authorize', data=form,
                          allow_redirects=False)
        self.assertNotIn('code=', r.headers.get('Location', ''))

    # ---- who may authorize (M4/H5) ----
    def test_portal_user_cannot_authorize(self):
        self._user('azportal', self.env.ref('base.group_portal'))
        r = self._authorize(self._client(), 'azportal')
        self.assertEqual(r.status_code, 403, r.text)

    def test_internal_user_without_mcp_group_cannot_authorize(self):
        self._user('aznogroup', self.g_int)
        r = self._authorize(self._client(), 'aznogroup')
        self.assertEqual(r.status_code, 403, r.text)

    def test_token_refused_for_user_without_mcp_group(self):
        nog = self._user('aznogroup2', self.g_int)
        client = self._client(dynamic=False)
        verifier, challenge = h.make_pkce_pair()
        code = h.issue_code_directly(
            self, client=client, user=nog, redirect_uri=REDIR,
            code_challenge=challenge,
            resource=self.cfg.effective_canonical_resource_uri())
        r = h.post_token(self, grant_type='authorization_code', code=code,
                         redirect_uri=REDIR, client_id=client.client_id,
                         code_verifier=verifier)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()['error'], 'invalid_grant')

    # ---- body caps (m6) ----
    def test_register_and_token_bodies_capped(self):
        self.cfg.oauth_dcr_enabled = True
        self.env.flush_all()
        big = json.dumps({'redirect_uris': [REDIR], 'pad': 'x' * 70000})
        r = self.url_open('/ow_mcp/oauth/register', data=big.encode(),
                          headers={'Content-Type': 'application/json'})
        self.assertEqual(r.status_code, 413, r.text)
        r = self.url_open('/ow_mcp/oauth/token', data=big.encode(),
                          headers={'Content-Type': 'application/json'})
        self.assertEqual(r.status_code, 413, r.text)
