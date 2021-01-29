# -*- coding: utf-8 -*-
# Copyright 2019 Openworx
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl.html).

from odoo import models, fields

class ResCompany(models.Model):

    _inherit = 'res.company'

    iban_qr_number = fields.Many2one('res.partner.bank', domain="[('partner_id','=', partner_id)]")