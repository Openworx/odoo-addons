from odoo.exceptions import ValidationError
from odoo.tests.common import HttpCase, TransactionCase, tagged


class TestSecurityTxtRendering(TransactionCase):

    def _set(self, key, value):
        self.env['ir.config_parameter'].sudo().set_param(key, value)

    def _render(self):
        return self.env['res.config.settings']._render_ow_security_txt()

    def test_no_contact_returns_false(self):
        self._set('ow_security_txt.contact', '')
        self.assertFalse(self._render())

    def test_bare_email_normalized_to_mailto(self):
        self._set('ow_security_txt.contact', 'security@example.com')
        self.assertIn('Contact: mailto:security@example.com', self._render())

    def test_multiple_contacts_one_line_each(self):
        self._set('ow_security_txt.contact',
                  'security@example.com\nhttps://example.com/report')
        content = self._render()
        self.assertIn('Contact: mailto:security@example.com', content)
        self.assertIn('Contact: https://example.com/report', content)

    def test_expires_is_rfc3339_utc(self):
        self._set('ow_security_txt.contact', 'security@example.com')
        self.assertRegex(
            self._render(),
            r'Expires: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\n')

    def test_optional_fields_omitted_when_empty(self):
        self._set('ow_security_txt.contact', 'security@example.com')
        self._set('ow_security_txt.policy', '')
        content = self._render()
        self.assertNotIn('Policy:', content)
        self.assertNotIn('Encryption:', content)

    def test_optional_fields_rendered_when_set(self):
        self._set('ow_security_txt.contact', 'security@example.com')
        self._set('ow_security_txt.policy', 'https://example.com/security-policy')
        self._set('ow_security_txt.preferred_languages', 'en, nl')
        content = self._render()
        self.assertIn('Policy: https://example.com/security-policy', content)
        self.assertIn('Preferred-Languages: en, nl', content)

    def test_contact_persists_via_settings(self):
        # Text fields do not support config_parameter=; the manual
        # get_values/set_values wiring must round-trip through settings.
        Settings = self.env['res.config.settings']
        settings = Settings.create({
            'ow_security_txt_contact': 'security@example.com',
        })
        settings.set_values()
        self.assertEqual(
            self.env['ir.config_parameter'].sudo()
                .get_param('ow_security_txt.contact'),
            'security@example.com')
        self.assertEqual(
            Settings.default_get(['ow_security_txt_contact'])
                    ['ow_security_txt_contact'],
            'security@example.com')

    def test_invalid_contact_scheme_raises(self):
        with self.assertRaises(ValidationError):
            self.env['res.config.settings'].create({
                'ow_security_txt_contact': 'ftp://example.com/contact',
            })

    def test_expires_months_out_of_range_raises(self):
        with self.assertRaises(ValidationError):
            self.env['res.config.settings'].create({
                'ow_security_txt_expires_months': 13,
            })


@tagged('post_install', '-at_install')
class TestSecurityTxtHttp(HttpCase):

    def test_404_when_unconfigured(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'ow_security_txt.contact', '')
        response = self.url_open('/.well-known/security.txt')
        self.assertEqual(response.status_code, 404)

    def test_served_when_configured(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'ow_security_txt.contact', 'security@example.com')
        response = self.url_open('/.well-known/security.txt')
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/plain', response.headers['Content-Type'])
        self.assertIn('Contact: mailto:security@example.com', response.text)
        self.assertIn('Expires: ', response.text)

    def test_legacy_redirect(self):
        response = self.url_open('/security.txt', allow_redirects=False)
        self.assertEqual(response.status_code, 301)
        self.assertTrue(response.headers['Location']
                        .endswith('/.well-known/security.txt'))
