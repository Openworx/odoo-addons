# -*- coding: utf-8 -*-
# Copyright 2021 Openworx
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl.html).

from odoo import models,fields

class Maintenance_Config(models.TransientModel):
    _inherit = "res.config.settings"

    ow_maintenance = fields.Boolean(
        related="website_id.ow_maintenance",
        string="Maintenance",
        readonly=False
    )
    
    ow_maintenance_title = fields.Char(
        related="website_id.ow_maintenance_title",
        string="Title",
        readonly=False
    )
    
    ow_maintenance_message = fields.Text(
        related="website_id.ow_maintenance_message",
        string="Message",
        readonly=False
    )

class Website(models.Model):
    _inherit = "website"

    ow_maintenance = fields.Boolean(string="Maintenance")
    ow_maintenance_title = fields.Char(string="Title")
    ow_maintenance_message = fields.Text(string="Message")
