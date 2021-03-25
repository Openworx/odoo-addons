# -*- coding: utf-8 -*-
# Copyright 2020 Openworx
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl.html).

from odoo import models, fields
from odoo.http import request

class AccountPayment(models.Model):
    _inherit="account.payment"

    def action_post(self): 
        res = super(AccountPayment,self).action_post()
        template_id = self.env.ref('ow_payment_received.invoice_payment_received')
        if template_id:
            #if request.env['ir.config_parameter'].sudo().get_param('payment_mail_notify'):
                for payment in self:
                    if payment.payment_type == 'inbound' and payment.partner_type == 'customer':
                        try:
                            template_id.send_mail(payment.id)
                        except:
                            pass
        return res
