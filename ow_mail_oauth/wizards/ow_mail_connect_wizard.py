"""Provider presets for the Connect Mailbox wizard.

Gmail / Microsoft 365 skip the password probe entirely: the wizard creates
a draft account with the provider's fixed servers and immediately sends the
browser to the OAuth consent screen; the callback then tests the connection
and syncs the folder tree (see ``controllers/main.py``).
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError

_PRESETS = {
    "gmail": {
        "auth_type": "gmail",
        "imap_host": "imap.gmail.com", "imap_port": 993, "imap_ssl": True,
        "smtp_host": "smtp.gmail.com", "smtp_port": 465,
        "smtp_encryption": "ssl",
    },
    "outlook": {
        "auth_type": "outlook",
        "imap_host": "outlook.office365.com", "imap_port": 993,
        "imap_ssl": True,
        "smtp_host": "smtp.office365.com", "smtp_port": 587,
        "smtp_encryption": "starttls",
    },
}


class OwMailConnectWizard(models.TransientModel):
    _inherit = "ow.mail.connect.wizard"

    provider = fields.Selection(
        [("other", "Other (IMAP/SMTP)"),
         ("gmail", "Gmail"),
         ("outlook", "Microsoft 365 / Outlook")],
        default="other", required=True, string="Provider")
    # Password is only meaningful for the classic IMAP/SMTP flow; the model
    # no longer enforces it so the OAuth presets can leave it empty. The
    # password path re-validates in action_connect below.
    password = fields.Char(required=False)

    @api.onchange("provider")
    def _onchange_provider(self):
        preset = _PRESETS.get(self.provider)
        if preset:
            self.imap_host = preset["imap_host"]
            self.imap_port = preset["imap_port"]
            self.imap_ssl = preset["imap_ssl"]
            self.smtp_host = preset["smtp_host"]
            self.smtp_port = preset["smtp_port"]
            self.smtp_encryption = preset["smtp_encryption"]

    def action_connect(self):
        self.ensure_one()
        preset = _PRESETS.get(self.provider)
        if not preset:
            if not self.password:
                raise UserError(_("Please fill in the password."))
            return super().action_connect()
        account = self.env["ow.mail.account"].create({
            "name": self.name,
            "email": self.email,
            "imap_login": self.email,
            **preset,
        })
        # Straight to the provider's consent screen; the OAuth callback
        # finishes with a connection test + folder sync.
        return {
            "type": "ir.actions.act_url",
            "url": account._ow_oauth_authorize_uri(),
            "target": "self",
        }
