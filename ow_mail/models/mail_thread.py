from odoo import api, models, fields
from ..models import ow_mail_imap as imap_utils
import email
# from markupsafe import Markup
import logging

_logger = logging.getLogger(__name__)

class MailThread(models.AbstractModel):
    _inherit = 'mail.thread'

    @api.model_create_multi
    def create(self, vals_list):
        records = super(MailThread, self).create(vals_list)
        if self.env.context.get('ow_mail_create_record'):
            folder_id = self.env.context.get('ow_mail_folder_id')
            uid = self.env.context.get('ow_mail_uid')
            body_html = self.env.context.get('ow_mail_context_msg_body')
            
            if folder_id and uid:
                for record in records:
                    self._ow_mail_post_create(record, folder_id, uid, body_html)
        return records

    def _ow_mail_post_create(self, record, folder_id, uid, body_html):
        # Attach the mail to the record
        attach_wizard = self.env['ow.mail.attach.record'].with_context(active_test=False).create({
            'folder_id': folder_id,
            'uid': uid,
            'res_model_id': self.env['ir.model']._get_id(record._name),
            'res_id': record.id,
            'include_attachments': True,
        })
        # We need to bypass some checks or mock fields because action_confirm 
        # reads fields that might not be set in our dummy wizard instance
        # but let's see if we can just call it.
        # Actually, let's look at action_confirm in ow_mail_attach_record.py
        
        # We need to set subject, from_name, etc. for the header
        # Fetching them from the email again might be slow, but it's the safest.
        
        folder = self.env['ow.mail.folder'].browse(folder_id)

        try:
            with imap_utils.imap_session(folder.account_id, folder.full_path, readonly=True) as conn:
                _flags, raw = imap_utils.fetch_full(conn, uid)
                if raw:
                    msg = email.message_from_bytes(raw)
                    env = imap_utils.parse_envelope(msg)
                    
                    attach_wizard.write({
                        'subject': env.get('subject'),
                        'from_name': env.get('from_name'),
                        'from_email': env.get('from_email'),
                        'date': env.get('date'),
                        'body_html': body_html or env.get('body_html'), # use passed body or from imap
                        'has_attachments': any(kind == 'attachment' for _, _, kind in imap_utils.walk_parts(msg))
                    })
                    attach_wizard.action_confirm()

        except Exception as e:
            _logger.warning("Failed to perform post-create for OW Mail: %s", e)
