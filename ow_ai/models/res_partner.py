# -*- coding: utf-8 -*-
from odoo import fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    _ow_ai_extra_fields = ['email', 'phone', 'mobile', 'vat', 'street', 'city', 'country_id', 'category_id']

    # Set on the contact `ow.ai.agent.create` makes for a new agent: deleting
    # an agent deletes its contact only when it carries this flag, never a
    # contact the agent was bound to otherwise (only the superuser can).
    ow_ai_agent_partner = fields.Boolean(string="AI Agent Contact", copy=False, groups='base.group_system')
