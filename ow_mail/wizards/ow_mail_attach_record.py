from odoo import _, api, fields, models
from odoo.exceptions import UserError
from markupsafe import Markup
import logging
import base64
import email
from ..models import ow_mail_imap as imap_utils

_logger = logging.getLogger(__name__)

class OwMailAttachRecord(models.TransientModel):
    _name = "ow.mail.attach.record"
    _description = "Attach Mail to Record"

    folder_id = fields.Integer(required=True)
    uid = fields.Integer(required=True)
    subject = fields.Char(readonly=True)
    from_name = fields.Char(readonly=True)
    from_email = fields.Char(readonly=True)
    date = fields.Char(readonly=True)
    body_html = fields.Html(readonly=True)
    has_attachments = fields.Boolean(readonly=True)
    include_attachments = fields.Boolean(string="Include Attachments", default=True)
    
    res_model_id = fields.Many2one("ir.model", string="Model", required=True, domain=[("model", "!=", "ow.mail.attach.record")])
    res_id = fields.Integer(string="Record ID", required=True)
    
    # For selection of record
    record_ref = fields.Reference(selection="_selection_target_model", string="Target Record")

    @api.model
    def _selection_target_model(self):
        models = self.env['ir.model'].search([('model', '!=', 'ow.mail.attach.record')])
        return [(model.model, model.name) for model in models]

    @api.onchange('record_ref')
    def _onchange_record_ref(self):
        if self.record_ref:
            self.res_model_id = self.env['ir.model'].search([('model', '=', self.record_ref._name)], limit=1)
            self.res_id = self.record_ref.id

    def action_confirm(self):
        self.ensure_one()
        if not self.res_model_id or not self.res_id:
            raise UserError(_("Please select a target record."))

        TargetModel = self.env[self.res_model_id.model]
        record = TargetModel.browse(self.res_id)
        if not record.exists():
            raise UserError(_("The selected record does not exist."))

        # Check if already attached
        existing_msg = self.env['mail.message'].search([
            ('model', '=', self.res_model_id.model),
            ('res_id', '=', self.res_id),
            ('ow_mail_uid', '=', self.uid),
            ('ow_mail_folder_id', '=', self.folder_id),
        ], limit=1)
        if existing_msg:
            raise UserError(_("This message has already been attached to this record."))

        # Find partners for author and recipients
        author_partner = self.env['res.partner'].search([('email_normalized', '=', self.from_email)], limit=1)
        if not author_partner:
            author_partner = self.env['res.partner'].search([('email', '=ilike', self.from_email)], limit=1)

        _logger.debug(
            "Author: %s (email: %s) (id: %s)",
            author_partner.name if author_partner else self.from_name,
            author_partner.email if author_partner else self.from_email,
            author_partner.id if author_partner else False,
        )

        email_from = (
            author_partner.email_formatted
            if author_partner and author_partner.email_formatted
            else f"{self.from_name} <{self.from_email}>"
            if self.from_name and self.from_email
            else self.from_email
        )

        # Add chatter message
        header_html = Markup("<b>From:</b> %s &lt;%s&gt;<br/><b>Date:</b> %s<br/><b>Subject:</b> %s<br/><br/>") % (
            self.from_name, self.from_email, self.date, self.subject
        )
        msg_body = header_html + Markup(self.body_html or "")

        attachment_ids = []
        if self.has_attachments and self.include_attachments:
            folder = self.env['ow.mail.folder'].browse(self.folder_id)
            try:
                with imap_utils.imap_session(folder.account_id, folder.full_path, readonly=True) as conn:
                    _flags, raw = imap_utils.fetch_full(conn, self.uid)
                    if raw:
                        msg = email.message_from_bytes(raw)
                        for part, section, kind in imap_utils.walk_parts(msg):
                            if kind == 'attachment':
                                payload = part.get_payload(decode=True)
                                if not payload:
                                    continue
                                filename = imap_utils.decode_header_value(part.get_filename()) or "attachment"
                                attachment = self.env['ir.attachment'].create({
                                    'name': filename,
                                    'datas': base64.b64encode(payload),
                                    'res_model': self.res_model_id.model,
                                    'res_id': self.res_id,
                                })
                                attachment_ids.append(attachment.id)
            except Exception as e:
                _logger.warning("Failed to fetch attachments for message UID %s: %s", self.uid, e)

        message = record.message_post(
            body=msg_body,
            message_type='email',
            author_id=author_partner.id if author_partner else self.env.user.partner_id.id,
            email_from=email_from,
            attachment_ids=attachment_ids,
        )
        
        # Update message with custom fields to prevent duplicates and mark origin
        message.write({
            'ow_mail_uid': self.uid,
            'ow_mail_folder_id': self.folder_id,
        })

        # Add followers
        partner_emails = [self.from_email]
        # In a real scenario we'd parse To and Cc here too if passed.
        _logger.info(f"partner_emails: {partner_emails}")

        followers = self.env['res.partner'].search([('email_normalized', 'in', partner_emails)])
        if followers:
            record.message_subscribe(partner_ids=followers.ids)

        return {'type': 'ir.actions.act_window_close'}
