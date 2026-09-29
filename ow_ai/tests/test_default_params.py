from odoo.tests import TransactionCase
from odoo.tools.convert import convert_file

from ..models.ir_config_parameter import OW_AI_DEFAULT_PARAMS


class TestDefaultParams(TransactionCase):
    """The settings' defaults are set for missing keys only, on install and on every update."""

    def setUp(self):
        super().setUp()
        self.ICP = self.env['ir.config_parameter'].sudo()

    def _load_data_file(self):
        convert_file(self.env, 'ow_ai', 'data/ir_config_parameter.xml', {}, mode='update', noupdate=False)

    def test_update_after_a_parameter_was_deleted_and_created_again(self):
        # A parameter deleted in System Parameters and created again is a new row
        # without XML id. A module update used to create the data file's record
        # again and hit ``ir_config_parameter_key_uniq``.
        self.ICP.search([('key', '=', 'ow_ai.base_url')]).unlink()
        self.ICP.create({'key': 'ow_ai.base_url', 'value': 'http://ollama:11434/v1'})

        self._load_data_file()

        self.assertEqual(self.ICP.get_str('ow_ai.base_url'), 'http://ollama:11434/v1')

    def test_missing_keys_get_their_default_and_set_keys_keep_their_value(self):
        self.ICP.search([('key', 'in', ['ow_ai.default_model', 'ow_ai.max_rounds'])]).unlink()
        self.ICP.set_int('ow_ai.timeout', 45)

        self._load_data_file()

        self.assertEqual(self.ICP.get_str('ow_ai.default_model'), 'openai/gpt-6-luna')
        self.assertEqual(self.ICP.get_int('ow_ai.max_rounds'), 30)
        self.assertEqual(self.ICP.get_int('ow_ai.timeout'), 45)

    def test_every_default_is_stored(self):
        self.ICP.search([('key', 'in', list(OW_AI_DEFAULT_PARAMS))]).unlink()

        self.ICP._ow_ai_set_default_params()

        for key, value in OW_AI_DEFAULT_PARAMS.items():
            self.assertEqual(self.ICP.get_str(key), value, key)
