# -*- coding: utf-8 -*-
from odoo import fields, models


class OwAiComposer(models.Model):
    _name = 'ow.ai.composer'
    _description = 'AI Composer Configuration'

    _unique_interface_model = models.UniqueIndex(
        '(interface_key, model_id) NULLS NOT DISTINCT WHERE active',
        "There is already an active composer for this interface and model.",
    )

    name = fields.Char(required=True)
    interface_key = fields.Selection(
        [('systray', 'Systray "Ask AI"'), ('record_chat', "Chat about a record")],
        required=True)
    model_id = fields.Many2one('ir.model', ondelete='cascade')
    model_name = fields.Char(related='model_id.model', store=True, string="Model Name")
    agent_id = fields.Many2one('ow.ai.agent', required=True, ondelete='restrict')
    default_prompt = fields.Text(translate=True, help="Extra instructions for this interface")
    prompt_button_ids = fields.One2many('ow.ai.prompt.button', 'composer_id')
    is_system_default = fields.Boolean(readonly=True)
    active = fields.Boolean(default=True)

    def _get_for(self, interface_key, res_model=None):
        domain = [('interface_key', '=', interface_key), ('active', '=', True)]
        specific = self.search(domain + [('model_name', '=', res_model)], limit=1) if res_model else self.browse()
        if specific:
            return specific
        return self.search(domain + [('model_id', '=', False)], limit=1)


class OwAiPromptButton(models.Model):
    _name = 'ow.ai.prompt.button'
    _description = 'AI Composer Prompt Button'
    _order = 'sequence, id'

    name = fields.Char(required=True, translate=True)
    prompt = fields.Text(required=True, translate=True)
    sequence = fields.Integer(default=10)
    composer_id = fields.Many2one('ow.ai.composer', required=True, ondelete='cascade')
