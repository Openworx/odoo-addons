# -*- coding: utf-8 -*-
from odoo import models
from odoo.addons.mail.tools.discuss import Store


class ResUsers(models.Model):
    _inherit = 'res.users'

    def _store_init_global_fields(self, res: Store.FieldList):
        """Publish whether the user may use the assistant (the web client's
        AI tab and record actions read ``store.has_access_ow_ai``)."""
        super()._store_init_global_fields(res)
        res.attr('has_access_ow_ai', self.has_group('ow_ai.group_ai_user'))
