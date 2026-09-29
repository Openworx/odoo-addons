from odoo import api, models

# Defaults of the OW AI settings, stored for keys that are missing.
OW_AI_DEFAULT_PARAMS = {
    'ow_ai.base_url': 'https://openrouter.ai/api/v1',
    'ow_ai.default_model': 'openai/gpt-6-luna',
    'ow_ai.max_rounds': '30',
    'ow_ai.max_tool_calls_per_round': '20',
    'ow_ai.timeout': '120',
    'ow_ai.job_stale_minutes': '10',
    'ow_ai.max_tool_result_chars': '60000',
    'ow_ai.max_history_chars': '200000',
}


class IrConfigParameter(models.Model):
    _inherit = 'ir.config_parameter'

    @api.model
    def _ow_ai_set_default_params(self):
        """Store the default of every missing OW AI setting; a setting that exists keeps its value.

        Called by ``data/ir_config_parameter.xml`` on install and on every update. Not
        ``<record>`` data: a parameter deleted (Settings > Technical > System Parameters)
        and created again is a new row without XML id, and the next update's create of
        the record failed on ``ir_config_parameter_key_uniq`` (on 19.0, saving Settings
        with an empty text field does the same). A missing parameter gets its default
        back on update, as a deleted ``noupdate`` record did.
        """
        ICP = self.sudo()
        existing = set(ICP.search([('key', 'in', list(OW_AI_DEFAULT_PARAMS))]).mapped('key'))
        for key, value in OW_AI_DEFAULT_PARAMS.items():
            if key not in existing:
                ICP.set_str(key, value)
