# -*- encoding: utf-8 -*-

from odoo import api, models

class RequeueMails(models.Model):
    _inherit = 'mail.mail'
    
    def requeue_failed_mails(self):
        failed_mail = self.env['mail.mail'].search([('state', '=', 'exception')])
        for requeue_mail in failed_mail:
            requeue_mail.state = 'outgoing'
