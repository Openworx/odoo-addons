from odoo import fields, models

class MailMessage(models.Model):
    _inherit = "mail.message"

    ow_mail_uid = fields.Integer(string="OW Mail UID", index=True)
    ow_mail_folder_id = fields.Many2one("ow.mail.folder", string="OW Mail Folder", index=True)
    # RFC 5322 Message-ID of the source email. Unlike (folder, uid) this
    # survives IMAP moves (archive assigns a fresh UID in the target
    # folder), so the linked-records lookup keys on it first.
    ow_mail_message_id = fields.Char(
        string="OW Mail Message-ID", index="btree_not_null")
