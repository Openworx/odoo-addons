"""Quick 'Connect to Mailbox' wizard — email + password."""
import imaplib
import logging
import smtplib
import ssl as ssl_mod

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class OwMailConnectWizard(models.TransientModel):
    _name = "ow.mail.connect.wizard"
    _description = "OW Mail Connect Wizard"

    name = fields.Char(required=True)
    email = fields.Char(required=True)
    password = fields.Char(required=True)

    imap_host = fields.Char(string="IMAP Host")
    imap_port = fields.Integer(string="IMAP Port", default=993)
    imap_ssl = fields.Boolean(string="IMAP SSL", default=True)
    smtp_host = fields.Char(string="SMTP Host")
    smtp_port = fields.Integer(string="SMTP Port", default=465)
    smtp_encryption = fields.Selection(
        [("none", "None"), ("ssl", "SSL/TLS"), ("starttls", "STARTTLS")],
        string="SMTP Encryption", default="ssl"
    )

    @api.onchange("email")
    def _onchange_email(self):
        """Pre-fill ``name`` from the email address when the field is empty.

        The wizard is designed for quick setup, so deriving the display name
        from the email saves the user an extra keystroke.  The email may also
        be used by ``action_connect`` to pre-populate IMAP and SMTP host names
        via domain heuristics when the user does not supply them explicitly.
        """
        if self.email and not self.name:
            self.name = self.email

    def _probe_imap(self, login):
        """Attempt IMAP LOGIN + LOGOUT; raise UserError on failure."""
        cls = imaplib.IMAP4_SSL if self.imap_ssl else imaplib.IMAP4
        try:
            conn = cls(self.imap_host, self.imap_port)
        except (OSError, imaplib.IMAP4.error) as e:
            raise UserError(_("Cannot reach IMAP server %s:%s — %s")
                            % (self.imap_host, self.imap_port, e))
        try:
            try:
                conn.login(login, self.password)
            except imaplib.IMAP4.error as e:
                raise UserError(_("IMAP login failed for %s — %s")
                                % (login, e))
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def _probe_smtp(self, login):
        """Attempt SMTP connect + (auth if supported); raise UserError on failure."""
        ctx = ssl_mod.create_default_context()
        try:
            if self.smtp_encryption == "ssl":
                smtp = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port,
                                        context=ctx, timeout=15)
            else:
                smtp = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15)
                if self.smtp_encryption == "starttls":
                    smtp.starttls(context=ctx)
        except (OSError, smtplib.SMTPException) as e:
            raise UserError(_("Cannot reach SMTP server %s:%s — %s")
                            % (self.smtp_host, self.smtp_port, e))
        try:
            try:
                smtp.login(login, self.password)
            except smtplib.SMTPNotSupportedError:
                # Some relays accept unauthenticated submission on LAN.
                pass
            except smtplib.SMTPException as e:
                raise UserError(_("SMTP login failed for %s — %s")
                                % (login, e))
        finally:
            try:
                smtp.quit()
            except Exception:
                pass

    def action_connect(self):
        self.ensure_one()
        if not self.imap_host or not self.smtp_host:
            raise UserError(_("Please fill in IMAP and SMTP hosts."))

        # Probe BEFORE creating the account record. If credentials are wrong
        # or the server is unreachable the user sees a clear error and no
        # broken row is left in ow.mail.account (previous behaviour: record
        # was created, encrypted password stored, then action_test_connection
        # flipped state to 'error' — requiring manual cleanup).
        imap_login = self.email  # keep existing convention; users can
        # override after creation (login may differ from the address,
        # e.g. GreenMail's 'demo' user)
        self._probe_imap(imap_login)
        self._probe_smtp(imap_login)

        acc = self.env["ow.mail.account"].create({
            "name": self.name,
            "email": self.email,
            "imap_host": self.imap_host,
            "imap_port": self.imap_port,
            "imap_ssl": self.imap_ssl,
            "imap_login": imap_login,
            "imap_password": self.password,
            "smtp_host": self.smtp_host,
            "smtp_port": self.smtp_port,
            "smtp_encryption": self.smtp_encryption,
            "smtp_same_as_imap": True,
        })
        try:
            acc.action_test_connection()
            acc.action_sync()
        except Exception:
            # Probes passed but setup still failed (e.g. folder refresh
            # barfed on an unusual server response). Clean up so the user
            # can retry without a half-configured account lingering.
            _logger.exception("Post-create setup failed; rolling back account %s",
                              acc.id)
            acc.sudo().unlink()
            raise
        return {
            "type": "ir.actions.client",
            "tag": "ow_mail.mailclient",
        }
