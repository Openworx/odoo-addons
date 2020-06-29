# -*- coding: utf-8 -*-
# Copyright 2020 Openworx
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl.html).

from odoo import fields, models, api


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    payment_mail_notify = fields.Boolean("Notify invoice payment via email", default=False)
    
    @api.model
    def get_values(self):
        res = super(ResConfigSettings, self).get_values()
        ir_config = self.env['ir.config_parameter'].sudo()
        payment_mail_notify = ir_config.get_param('payment_mail_notify', default=False)

        res.update(
            payment_mail_notify=payment_mail_notify,
        )
        return res

    def set_values(self):
        super(ResConfigSettings, self).set_values()
        ir_config = self.env['ir.config_parameter'].sudo()
        ir_config.set_param("payment_mail_notify", self.payment_mail_notify or '')

