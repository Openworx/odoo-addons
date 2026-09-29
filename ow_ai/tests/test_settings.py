import os
from unittest.mock import patch

from odoo.tests import TransactionCase, new_test_user
from odoo.tools import config


class TestOwAiSettings(TransactionCase):

    def test_get_values_never_exposes_api_key(self):
        settings = self.env['res.config.settings'].create({})
        values = settings.get_values()
        self.assertTrue(
            'ow_ai_api_key' not in values or not values['ow_ai_api_key'])

    def test_set_api_key_and_clear(self):
        with patch.dict(os.environ, {}, clear=False), patch.dict(config.options, {'ow_ai_api_key': ''}):
            os.environ.pop('OW_AI_API_KEY', None)
            os.environ.pop('OPENROUTER_API_KEY', None)

            settings = self.env['res.config.settings'].create({
                'ow_ai_api_key': 'sk-or-test-1234',
            })
            settings.set_values()

            self.assertEqual(
                self.env['ir.config_parameter'].sudo().get_str('ow_ai.api_key'),
                'sk-or-test-1234',
            )
            self.assertEqual(settings.ow_ai_api_key_state, 'param')
            self.assertEqual(settings.ow_ai_api_key_hint, '…1234')

            # Setting with an empty key leaves the stored value unchanged.
            settings_empty = self.env['res.config.settings'].create({
                'ow_ai_api_key': '',
            })
            settings_empty.set_values()
            self.assertEqual(
                self.env['ir.config_parameter'].sudo().get_str('ow_ai.api_key'),
                'sk-or-test-1234',
            )

            settings.action_ow_ai_clear_api_key()
            settings._compute_ow_ai_api_key_state()
            self.assertEqual(
                self.env['ir.config_parameter'].sudo().get_str('ow_ai.api_key'),
                '',
            )
            self.assertEqual(settings.ow_ai_api_key_state, 'missing')

    def test_saved_key_does_not_linger_on_the_settings_row(self):
        settings = self.env['res.config.settings'].create({'ow_ai_api_key': 'sk-or-lingering-5678'})
        settings.set_values()

        self.assertEqual(self.env['ir.config_parameter'].sudo().get_str('ow_ai.api_key'), 'sk-or-lingering-5678')
        self.assertFalse(settings.ow_ai_api_key)
        self.env.flush_all()
        self.env.cr.execute("SELECT ow_ai_api_key FROM res_config_settings WHERE id = %s", (settings.id,))
        self.assertIsNone(self.env.cr.fetchone()[0])

    def test_api_key_env_fallback(self):
        self.env['ir.config_parameter'].sudo().set_str('ow_ai.api_key', '')
        with patch.dict(os.environ, {'OW_AI_API_KEY': '', 'OPENROUTER_API_KEY': 'sk-or-env-9999'}):
            settings = self.env['res.config.settings'].create({})
            settings._compute_ow_ai_api_key_state()
            self.assertEqual(settings.ow_ai_api_key_state, 'env')
            self.assertEqual(settings.ow_ai_api_key_hint, '…9999')

    def test_api_key_env_fallback_prefers_ow_ai_api_key(self):
        self.env['ir.config_parameter'].sudo().set_str('ow_ai.api_key', '')
        with patch.dict(os.environ, {'OW_AI_API_KEY': 'k-new-1111', 'OPENROUTER_API_KEY': 'k-old-2222'}):
            settings = self.env['res.config.settings'].create({})
            settings._compute_ow_ai_api_key_state()
            self.assertEqual(settings.ow_ai_api_key_state, 'env')
            self.assertEqual(settings.ow_ai_api_key_hint, '…1111')

    def test_api_key_conf_fallback(self):
        """The client falls back to odoo.conf's ow_ai_api_key: the badge must say so."""
        self.env['ir.config_parameter'].sudo().set_str('ow_ai.api_key', '')
        with patch.dict(os.environ, {'OW_AI_API_KEY': '', 'OPENROUTER_API_KEY': ''}), \
             patch.dict(config.options, {'ow_ai_api_key': 'sk-conf-7777'}):
            settings = self.env['res.config.settings'].create({})
            settings._compute_ow_ai_api_key_state()
            self.assertEqual(settings.ow_ai_api_key_state, 'conf')
            self.assertEqual(settings.ow_ai_api_key_hint, '…7777')
        self.assertEqual(
            dict(settings._fields['ow_ai_api_key_state'].selection)['conf'], "Set via odoo.conf")

    def test_defaults_round_trip(self):
        settings = self.env['res.config.settings'].create({})
        self.assertEqual(settings.ow_ai_max_rounds, 30)
        self.assertEqual(settings.ow_ai_default_model, 'openai/gpt-6-luna')

    def test_groups_assigned(self):
        user = new_test_user(self.env, login='ow_ai_test_user', groups='base.group_user')
        self.assertIn(self.env.ref('ow_ai.group_ai_user'), user.all_group_ids)

        admin = self.env.ref('base.user_admin')
        self.assertIn(self.env.ref('ow_ai.group_ai_manager'), admin.all_group_ids)
