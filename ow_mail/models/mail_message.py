from odoo import fields, models

class MailMessage(models.Model):
    _inherit = "mail.message"

    ow_mail_uid = fields.Integer(string="OW Mail UID", index=True)
    ow_mail_folder_id = fields.Many2one("ow.mail.folder", string="OW Mail Folder", index=True)
