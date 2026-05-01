import json

from odoo.tests import HttpCase, tagged

from . import _helpers as h


@tagged('post_install', '-at_install')
class TestDCR(HttpCase):
    def test_register_happy_path_public_client(self):
        body = h.register_client(self)
        self.assertIn('client_id', body)
        # Public client (auth_method=none) MUST NOT receive a secret.
        self.assertNotIn('client_secret', body)
        self.assertEqual(body['token_endpoint_auth_method'], 'none')
        self.assertIn('registration_access_token', body)

    def test_register_confidential_client_returns_secret(self):
        body = h.register_client(self, auth_method='client_secret_basic')
        self.assertIn('client_secret', body)
        self.assertGreater(len(body['client_secret']), 16)

    def test_register_disabled(self):
        cfg = self.env['ow.mcp.oauth.config'].sudo().get_singleton()
        cfg.oauth_dcr_enabled = False
        try:
            r = self.url_open(
                '/ow_mcp/oauth/register',
                data=json.dumps({
                    'redirect_uris': ['http://127.0.0.1:9999/cb'],
                }).encode(),
                headers={'Content-Type': 'application/json'},
            )
            self.assertEqual(r.status_code, 403)
        finally:
            cfg.oauth_dcr_enabled = True

    def test_register_rejects_bad_redirect(self):
        r = self.url_open(
            '/ow_mcp/oauth/register',
            data=json.dumps({
                'redirect_uris': ['http://example.com/cb'],  # http non-loopback
            }).encode(),
            headers={'Content-Type': 'application/json'},
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()['error'], 'invalid_redirect_uri')

    def test_register_rejects_unsupported_grant(self):
        r = self.url_open(
            '/ow_mcp/oauth/register',
            data=json.dumps({
                'redirect_uris': ['https://example.com/cb'],
                'grant_types': ['client_credentials'],
            }).encode(),
            headers={'Content-Type': 'application/json'},
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()['error'], 'invalid_client_metadata')
