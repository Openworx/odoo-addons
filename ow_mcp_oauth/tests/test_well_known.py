from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestWellKnown(HttpCase):
    def test_protected_resource_metadata(self):
        r = self.url_open('/.well-known/oauth-protected-resource')
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn('resource', body)
        self.assertIn('authorization_servers', body)
        self.assertEqual(body['bearer_methods_supported'], ['header'])
        self.assertIn('mcp:read', body['scopes_supported'])
        self.assertIn('mcp:write', body['scopes_supported'])

    def test_authorization_server_metadata(self):
        r = self.url_open('/.well-known/oauth-authorization-server')
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for key in (
            'issuer', 'authorization_endpoint', 'token_endpoint',
            'registration_endpoint', 'jwks_uri', 'revocation_endpoint',
            'response_types_supported', 'grant_types_supported',
            'code_challenge_methods_supported',
        ):
            self.assertIn(key, body, f'missing {key}')
        self.assertEqual(body['code_challenge_methods_supported'], ['S256'])
        self.assertIn('authorization_code', body['grant_types_supported'])
        self.assertIn('refresh_token', body['grant_types_supported'])

    def test_jwks(self):
        r = self.url_open('/ow_mcp/oauth/jwks.json')
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn('keys', body)
        self.assertGreaterEqual(len(body['keys']), 1)
        k = body['keys'][0]
        self.assertEqual(k['kty'], 'RSA')
        self.assertEqual(k['alg'], 'RS256')
        self.assertEqual(k['use'], 'sig')
        self.assertIn('n', k)
        self.assertIn('e', k)
        self.assertIn('kid', k)

    def test_disabled_returns_404(self):
        cfg = self.env['ow.mcp.oauth.config'].sudo().get_singleton()
        cfg.oauth_enabled = False
        try:
            r = self.url_open('/.well-known/oauth-protected-resource')
            self.assertEqual(r.status_code, 404)
        finally:
            cfg.oauth_enabled = True
