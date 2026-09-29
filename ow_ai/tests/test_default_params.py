from odoo.tests import TransactionCase
from odoo.tools.convert import convert_file

from ..models.ir_config_parameter import OW_AI_DEFAULT_PARAMS
from ..utils import params


class TestDefaultParams(TransactionCase):
    """The settings' defaults are set for missing keys only, on install and on every update."""

    def _load_data_file(self):
        convert_file(self.env, 'ow_ai', 'data/ir_config_parameter.xml', {}, mode='update', noupdate=False)

    def test_update_after_a_text_setting_was_emptied_and_filled_again(self):
        # Saving Settings with an empty SearXNG URL unsets the parameter (Odoo's
        # ``set_param`` unlinks it, XML id included); a URL saved later is a new
        # row without XML id. A module update used to create the data file's
        # record again and hit ``ir_config_parameter_key_uniq``.
        params.set_str(self.env, 'ow_ai.web_search_url', '')
        params.set_str(self.env, 'ow_ai.web_search_url', 'https://searxng.example.com')
        params.set_str(self.env, 'ow_ai.default_model', '')
        params.set_str(self.env, 'ow_ai.default_model', 'mistralai/mistral-large-2411')

        self._load_data_file()

        self.assertEqual(params.get_str(self.env, 'ow_ai.web_search_url'), 'https://searxng.example.com')
        self.assertEqual(params.get_str(self.env, 'ow_ai.default_model'), 'mistralai/mistral-large-2411')

    def test_missing_keys_get_their_default_and_set_keys_keep_their_value(self):
        params.set_str(self.env, 'ow_ai.base_url', '')
        params.set_int(self.env, 'ow_ai.max_rounds', None)
        params.set_int(self.env, 'ow_ai.timeout', 45)

        self._load_data_file()

        self.assertEqual(params.get_str(self.env, 'ow_ai.base_url'), 'https://openrouter.ai/api/v1')
        self.assertEqual(params.get_int(self.env, 'ow_ai.max_rounds'), 30)
        self.assertEqual(params.get_int(self.env, 'ow_ai.timeout'), 45)

    def test_every_default_is_stored_and_empty_ones_stay_unset(self):
        ICP = self.env['ir.config_parameter'].sudo()
        unset_by_default = ['ow_ai.web_search_url', 'ow_ai.web_search_language']
        ICP.search([('key', 'in', [*OW_AI_DEFAULT_PARAMS, *unset_by_default])]).unlink()

        ICP._ow_ai_set_default_params()

        for key, value in OW_AI_DEFAULT_PARAMS.items():
            self.assertEqual(ICP.get_param(key), value, key)
        self.assertFalse(ICP.search([('key', 'in', unset_by_default)]))
