"""Transient model backing the floating compose window."""
from odoo import _, api, fields, models


class OwMailCompose(models.TransientModel):
    _name = "ow.mail.compose"
    _description = "OW Mail Compose"

    account_id = fields.Many2one("ow.mail.account", required=True,
                                 default=lambda s: s._default_account())
    to = fields.Char(string="To", required=True)
    cc = fields.Char(string="Cc")
    subject = fields.Char(string="Subject")
    body_html = fields.Html(string="Body", sanitize=True)
    in_reply_to = fields.Char(string="In-Reply-To")
    references = fields.Text(string="References")
    attachment_ids = fields.Many2many("ir.attachment", string="Attachments")

    @api.model
    def _default_account(self):
        """Return the first confirmed account for the current user.

        Used as the default ``account_id`` when a new compose window is opened
        so the user does not have to select an account manually.  Confirmed
        accounts are preferred to avoid presenting accounts whose credentials
        are broken or not yet tested.
        """
        return self.env["ow.mail.account"].search(
            [("user_id", "=", self.env.uid), ("state", "=", "confirmed")], limit=1
        ).id

    @api.onchange("account_id")
    def _onchange_account_signature(self):
        """Auto-insert the account's HTML signature when ``account_id`` changes.

        Only runs when the body is still empty so that switching accounts on a
        message already being drafted does not overwrite the user's text.  The
        signature is appended after a blank paragraph; placement above or below
        quoted text is determined by ``account_id.signature_placement``
        (``'above'`` = above the quoted text, ``'below'`` = below it, or at
        the end of the body for new messages with no quote block).
        """
        if self.account_id and not self.body_html:
            self.body_html = "<p><br/></p>" + (self.account_id.signature_html or "")

    def action_send(self):
        """Send the composed message via the account's SMTP connection.

        Delegates to ``account_id.send_mail``, which handles SMTP delivery and
        appends a copy to the account's Sent folder via IMAP APPEND.  On
        success, returns an ``ir.actions.act_window_close`` client action so
        the Odoo form-view dialog closes automatically.

        Note: this is the Odoo form-view code path.  The floating compose
        window used in the main mail client calls ``/ow_mail/send`` directly
        via RPC in ``ow_mail_service.js`` and never invokes this method.
        """
        self.ensure_one()
        self.account_id.send_mail(
            to=self.to,
            cc=self.cc,
            subject=self.subject or "",
            body_html=self.body_html or "",
            attachment_ids=self.attachment_ids.ids,
            in_reply_to=self.in_reply_to or None,
            references=self.references or None,
        )
        return {"type": "ir.actions.act_window_close"}
