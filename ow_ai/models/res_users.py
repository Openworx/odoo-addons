# -*- coding: utf-8 -*-
from odoo import api, models
from odoo.addons.mail.tools.discuss import Store

from ..utils import websearch


class ResUsers(models.Model):
    _inherit = 'res.users'

    @api.model
    def _init_store_data(self, store: Store):
        """Publish whether the user may use the assistant (the web client's
        AI tab and record actions read ``store.has_access_ow_ai``) and
        whether web search is configured (the composer's web search switch
        reads ``store.has_ow_ai_web_search``)."""
        super()._init_store_data(store)
        store.add_global_values(has_access_ow_ai=self.env.user.has_group('ow_ai.group_ai_user'))
        store.add_global_values(has_ow_ai_web_search=websearch.is_configured(self.env))
