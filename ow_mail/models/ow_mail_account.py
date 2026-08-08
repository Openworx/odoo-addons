"""IMAP/SMTP account configuration."""
import email
import imaplib
import logging
import re
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import getaddresses

from odoo import _, api, fields, models

from .ow_mail_imap import imap_mbox_quote as _mbox
from odoo.exceptions import UserError
from odoo.tools import html_sanitize

_logger = logging.getLogger(__name__)

SPECIAL_USE_MAP = {
    "\\Inbox": "inbox",
    "\\Sent": "sent",
    "\\Drafts": "drafts",
    "\\Junk": "spam",
    "\\Trash": "trash",
    "\\Archive": "archive",
    "\\All": "archive",
}

NAME_SPECIAL_HINTS = [
    (re.compile(r"^inbox$", re.I), "inbox"),
    (re.compile(r"^sent( items| mail)?$", re.I), "sent"),
    (re.compile(r"^drafts?$", re.I), "drafts"),
    (re.compile(r"^(junk|spam)( mail)?$", re.I), "spam"),
    (re.compile(r"^(trash|deleted( items)?|bin)$", re.I), "trash"),
    (re.compile(r"^(archive|all mail)$", re.I), "archive"),
]

class OwMailAccount(models.Model):
    """Bridge between the Odoo ORM and an external IMAP/SMTP mailbox.

    Passwords are stored encrypted (Fernet) in ``*_password_enc`` fields that
    are restricted to ``base.group_no_one``. The UI-facing ``imap_password``
    and ``smtp_password`` fields are write-only virtual Chars: writes trigger
    ``_inverse_*`` which encrypts and persists the value; ``_compute_blank_password``
    always reads back an empty string so plain-text credentials are never returned
    via ORM reads or JSON-RPC. Decryption goes through ``_get_*_password()``
    which uses ``sudo()`` to bypass field-level ACL.

    Special-folder pointers (``sent_folder_id``, ``drafts_folder_id``, etc.) are
    populated by ``_map_special_folders()`` using IMAP SPECIAL-USE attributes with
    name-pattern heuristics as a fallback for servers that omit SPECIAL-USE.

    Each account is scoped to one user via ``user_id``; ``ir.rule`` on every
    dependent model (folder, tag, trusted sender) enforces that ownership so one
    user can never read or mutate another user's mail data.
    """

    _name = "ow.mail.account"
    _description = "OW Mail Account"
    _order = "sequence, id"

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    user_id = fields.Many2one("res.users", required=True,
                              default=lambda self: self.env.user, ondelete="cascade")
    email = fields.Char(required=True)
    color = fields.Integer(default=0)
    active = fields.Boolean(default=True)

    imap_host = fields.Char(string="IMAP Host", required=True)
    imap_port = fields.Integer(string="IMAP Port", required=True, default=993)
    imap_ssl = fields.Boolean(string="IMAP SSL", default=True)
    imap_login = fields.Char(string="IMAP Login", required=True)
    imap_password_enc = fields.Char(
        string="IMAP Password (encrypted)", groups="base.group_no_one")

    smtp_host = fields.Char(string="SMTP Host", required=True)
    smtp_port = fields.Integer(string="SMTP Port", required=True, default=465)
    smtp_encryption = fields.Selection(
        [("none", "None"), ("ssl", "SSL/TLS"), ("starttls", "STARTTLS")],
        string="SMTP Encryption", required=True, default="ssl",
    )
    smtp_login = fields.Char(string="SMTP Login")
    smtp_password_enc = fields.Char(
        string="SMTP Password (encrypted)", groups="base.group_no_one")
    smtp_same_as_imap = fields.Boolean(string="Login same as incoming", default=True)

    notify_new_mail = fields.Boolean(string="Notify on new mail", default=True)

    from_name_mode = fields.Selection(
        [("user", "Odoo user name"), ("custom", "Custom")],
        default="user", required=True, string="Sender name")
    from_name_custom = fields.Char(string="Custom sender name")

    signature_html = fields.Html(
        string="Signature", sanitize=True,
        compute="_compute_signature_html", inverse="_inverse_signature_html", store=True)

    signature_enabled = fields.Boolean(
        string="Include signature", default=True,
        help="If unchecked, no signature is inserted in new compose, reply or forward.")
    signature_odoo = fields.Boolean(
        string="Use Odoo user signature", default=True,
        help="If checked, the Odoo signature is used.")

    signature_placement = fields.Selection(
        [("below", "Below quoted message"),
         ("above", "Above quoted message")],
        default="below", required=True, string="Signature placement",
        help="Where the signature is inserted in replies and forwards "
             "relative to the quoted original.")

    state = fields.Selection(
        [("draft", "Draft"), ("confirmed", "Confirmed"), ("error", "Error")],
        default="draft", readonly=True,
    )
    error_message = fields.Text(readonly=True)
    last_sync = fields.Datetime(readonly=True)

    folder_ids = fields.One2many("ow.mail.folder", "account_id")

    inbox_folder_id = fields.Many2one("ow.mail.folder", domain="[('account_id','=',id)]")
    sent_folder_id = fields.Many2one("ow.mail.folder", domain="[('account_id','=',id)]")
    drafts_folder_id = fields.Many2one("ow.mail.folder", domain="[('account_id','=',id)]")
    archive_folder_id = fields.Many2one("ow.mail.folder", domain="[('account_id','=',id)]")
    spam_folder_id = fields.Many2one("ow.mail.folder", domain="[('account_id','=',id)]")
    trash_folder_id = fields.Many2one("ow.mail.folder", domain="[('account_id','=',id)]")

    imap_password = fields.Char(
        string="IMAP Password",
        inverse="_inverse_imap_password",
        compute="_compute_blank_password", store=False)
    smtp_password = fields.Char(
        string="SMTP Password",
        inverse="_inverse_smtp_password",
        compute="_compute_blank_password", store=False)

    @api.depends("signature_odoo", "user_id.signature")
    def _compute_signature_html(self):
        for rec in self:
            if rec.signature_odoo:
                rec.signature_html = rec.user_id.signature
            elif not rec.signature_html:
                rec.signature_html = False

    def _inverse_signature_html(self):
        for rec in self:
            if rec.signature_odoo:
                # If we are in "Odoo signature" mode, we don't allow manual override
                # to persist in signature_html if it's different from Odoo signature.
                # Actually, Odoo's inverse on a computed field usually means
                # we store the value. If signature_odoo is True, we just
                # re-compute it to be safe, but typically the UI should be readonly.
                continue
            # When not in Odoo mode, the value is just stored in the DB (standard behavior)

    def _compute_blank_password(self):
        """Always return empty so plain-text credentials are never exposed via ORM reads or JSON-RPC.

        The real values live only in the encrypted ``*_password_enc`` fields;
        a non-empty compute result here would leak them to any caller with
        read access on the record.
        """
        for rec in self:
            rec.imap_password = ""
            rec.smtp_password = ""

    def _inverse_imap_password(self):
        """Encrypt and persist the IMAP password written through the virtual field.

        The empty-string guard prevents a read-back of the blank compute value
        from accidentally overwriting a previously stored password with an
        empty ciphertext — the field is write-only, so any real update will
        always carry a non-empty value.
        """
        crypto = self.env["ow.mail.crypto"]
        for rec in self:
            if rec.imap_password:
                rec.sudo().imap_password_enc = crypto.encrypt(rec.imap_password)

    def _inverse_smtp_password(self):
        """Encrypt and persist the SMTP password — same pattern as ``_inverse_imap_password``."""
        crypto = self.env["ow.mail.crypto"]
        for rec in self:
            if rec.smtp_password:
                rec.sudo().smtp_password_enc = crypto.encrypt(rec.smtp_password)

    def _get_imap_password(self):
        """Decrypted IMAP password. Raises CipherKeyError if the Fernet key
        is unusable so callers see a clear "re-enter password" error rather
        than attempting login with an empty string (which risks lock-out)."""
        self.ensure_one()
        return self.env["ow.mail.crypto"].decrypt(
            self.sudo().imap_password_enc, strict=True)

    def _get_smtp_password(self):
        """Decrypted SMTP password — symmetric to ``_get_imap_password``.

        When ``smtp_same_as_imap`` is set the IMAP credentials are reused so
        there is no separate SMTP ciphertext to maintain. ``strict=True``
        raises ``CipherKeyError`` on key rotation so callers see a clear
        error rather than attempting login with garbage.
        """
        self.ensure_one()
        if self.smtp_same_as_imap:
            return self._get_imap_password()
        return self.env["ow.mail.crypto"].decrypt(
            self.sudo().smtp_password_enc, strict=True)

    # ---------------- IMAP/SMTP connect ----------------

    def _imap_connect(self):
        """Open and authenticate an IMAP connection.

        ``imap_ssl=True`` uses ``IMAP4_SSL`` (implicit TLS, typically port 993);
        ``False`` uses plain ``IMAP4``. STARTTLS is not supported — imaplib
        exposes ``starttls()`` but it must be called explicitly and this code
        does not do so; use port 993 with SSL for encrypted connections.
        No custom ``ssl_context`` is applied here so self-signed certificates
        used in dev (GreenMail) will be rejected — disable SSL in the account
        settings for those.
        """
        self.ensure_one()
        cls = imaplib.IMAP4_SSL if self.imap_ssl else imaplib.IMAP4
        conn = cls(self.imap_host, self.imap_port)
        conn.login(self.imap_login, self._get_imap_password())
        return conn

    def _smtp_connect(self):
        """Open and authenticate an SMTP connection.

        ``ssl`` wraps the socket immediately (``SMTP_SSL``, port 465);
        ``starttls`` connects plain then upgrades; ``none`` stays plain.
        ``login()`` is only called when both a login name and password are
        present — some internal relays accept unauthenticated submission and
        would reject a spurious AUTH attempt, so we skip it rather than
        treating missing credentials as an error.
        """
        self.ensure_one()
        ctx = ssl.create_default_context()
        if self.smtp_encryption == "ssl":
            conn = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, context=ctx, timeout=30)
        else:
            conn = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30)
            if self.smtp_encryption == "starttls":
                conn.starttls(context=ctx)
        login = self.smtp_login or self.imap_login
        pwd = self._get_smtp_password()
        if login and pwd:
            try:
                conn.login(login, pwd)
            except smtplib.SMTPNotSupportedError:
                pass
        return conn

    # ---------------- Actions ----------------

    def action_test_connection(self):
        """Test IMAP then SMTP in sequence; set account state to confirmed or error.

        Both protocols must succeed for the account to reach ``confirmed``
        state — a passing IMAP with a broken SMTP would still leave the
        account unusable for sending.
        """
        self.ensure_one()
        try:
            c = self._imap_connect()
            c.logout()
            s = self._smtp_connect()
            s.quit()
        except Exception as e:
            self.write({"state": "error", "error_message": str(e)})
            raise UserError(_("Connection failed: %s") % e)
        self.write({"state": "confirmed", "error_message": False})
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"title": _("OW Mail"), "message": _("Connection OK"), "type": "success"},
        }

    def action_test_incoming(self):
        """Test the IMAP connection only, leaving SMTP credentials unverified.

        On success, writes ``state='confirmed'`` — the same state set by the
        full ``action_test_connection``. This means a broken SMTP password will
        not prevent the account from appearing operational; use this only when
        debugging incoming mail specifically.
        """
        self.ensure_one()
        try:
            c = self._imap_connect()
            c.logout()
        except Exception as e:
            self.write({"state": "error", "error_message": str(e)})
            raise UserError(_("Incoming (IMAP) connection failed: %s") % e)
        self.write({"state": "confirmed", "error_message": False})
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"title": _("OW Mail"), "message": _("Incoming server OK"),
                       "type": "success"},
        }

    def action_test_outgoing(self):
        """Test the SMTP connection only, leaving IMAP credentials unverified.

        On success, writes ``state='confirmed'`` — the same state set by the
        full ``action_test_connection``. This means a broken IMAP password will
        not prevent the account from appearing operational; use this only when
        debugging outgoing mail specifically.
        """
        self.ensure_one()
        try:
            s = self._smtp_connect()
            s.quit()
        except Exception as e:
            self.write({"state": "error", "error_message": str(e)})
            raise UserError(_("Outgoing (SMTP) connection failed: %s") % e)
        self.write({"state": "confirmed", "error_message": False})
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"title": _("OW Mail"), "message": _("Outgoing server OK"),
                       "type": "success"},
        }

    def action_fetch_folders(self):
        """Re-fetch the folder tree and unread counts, then remap special folders.

        Calls ``_refresh_folders()`` which does a full IMAP LIST + per-folder
        STATUS and finishes with ``_map_special_folders()``. Intended for the
        "Fetch Folders" button in the account settings form.
        """
        self.ensure_one()
        try:
            self._refresh_folders()
            self.write({"state": "confirmed", "last_sync": fields.Datetime.now(),
                        "error_message": False})
        except Exception as e:
            _logger.exception("OW fetch folders failed for %s", self.name)
            self.write({"state": "error", "error_message": str(e)})
            raise UserError(_("Fetch folders failed: %s") % e)
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"title": _("OW Mail"), "message": _("Folders fetched"),
                       "type": "success"},
        }

    def action_sync(self):
        """Refresh the folder tree and unread counts for the manual "Sync" button.

        No message bodies are fetched — the model is live-read-only, so
        sync means reconstituting the ``ow.mail.folder`` cache from IMAP
        LIST/STATUS, not downloading any mail.
        """
        for rec in self:
            try:
                rec._refresh_folders()
                rec.write({"state": "confirmed", "last_sync": fields.Datetime.now(),
                           "error_message": False})
            except Exception as e:
                _logger.exception("OW refresh failed for %s", rec.name)
                rec.write({"state": "error", "error_message": str(e)})
        return True

    @api.model
    def _cron_refresh_counts(self):
        """Cheap periodic refresh of folder counts + new-mail notifications.

        Runs as the cron's user (OdooBot by default). We must sudo() the
        search so the per-user ir.rule on ow.mail.account doesn't hide every
        end-user account from the cron. Each account's own user_id still
        determines who receives the resulting bus notification.
        """
        from . import ow_mail_imap as imap_utils

        touched_partner_ids = set()
        accounts = self.sudo().search(
            [("active", "=", True), ("state", "=", "confirmed")]
        )
        for acc in accounts:
            try:
                conn = acc._imap_connect()
                try:
                    acc.folder_ids.refresh_counts(conn=conn)
                    # New-mail detection for inbox
                    if acc.notify_new_mail and acc.inbox_folder_id:
                        self._check_new_mail(acc, conn, imap_utils)
                finally:
                    conn.logout()
                self.env.cr.commit()
                touched_partner_ids.add(acc.user_id.partner_id.id)
            except Exception as e:
                _logger.debug("ow_mail refresh_counts failed for account %s: %s", acc.id, e)

        if touched_partner_ids:
            partners = self.env["res.partner"].browse(list(touched_partner_ids))
            for partner in partners:
                self.env["bus.bus"]._sendone(partner, "ow_mail/refresh", {})

    @api.model
    def _cron_gc_orphan_uploads(self):
        """Delete compose-attachment uploads older than 24h that never got sent.

        The /ow_mail/attachment/upload endpoint stores each file as an
        ir.attachment with ``res_model='ow.mail.compose', res_id=0``. Those
        records linger when the user closes the compose window without
        sending. This cron sweeps them to keep the filestore tidy.
        """
        cutoff = fields.Datetime.now() - timedelta(hours=24)
        stale = self.env["ir.attachment"].sudo().search([
            ("res_model", "=", "ow.mail.compose"),
            ("res_id", "=", 0),
            ("create_date", "<", cutoff),
        ])
        count = len(stale)
        if count:
            stale.unlink()
            _logger.info("ow_mail: gc-ed %s orphan compose attachments", count)
        return count

    def _check_new_mail(self, acc, conn, imap_utils):
        """Detect new unseen messages in the inbox and fire a bus notification.

        New-mail detection compares the UIDs of currently UNSEEN messages
        against ``inbox_folder_id.last_seen_uid``. UIDs are monotonically
        increasing on a given server, so any UID above the watermark must
        have arrived after the last check. This avoids a full EXAMINE/EXPUNGE
        round-trip and works even when the server does not support IDLE.
        As a side effect, ``last_seen_uid`` is advanced to the highest new UID
        so the same messages are not re-notified on the next cron tick.
        """
        inbox = acc.inbox_folder_id
        try:
            typ, _ = conn.select(imap_utils.imap_mbox_quote(inbox.full_path), readonly=True)
            if typ != "OK":
                return
            # Search for UNSEEN messages
            uids = imap_utils.search_uids(conn, "UNSEEN")
            if not uids:
                return
            last_seen = inbox.last_seen_uid or 0
            new_uids = [u for u in uids if u > last_seen]
            if not new_uids:
                return
            # Fetch sender info for the notification
            envs = imap_utils.fetch_envelopes(conn, new_uids[:5])
            count = len(new_uids)
            if envs:
                first = envs[0]
                if count == 1:
                    message = (f"New mail from {first['from_name'] or first['from_email']}"
                               f": {first['subject'] or '(no subject)'}")
                else:
                    message = (f"{count} new messages — latest from "
                               f"{first['from_name'] or first['from_email']}")
            else:
                message = f"{count} new message(s) in {acc.name}"
            # Update last_seen_uid
            inbox.sudo().write({"last_seen_uid": max(new_uids)})
            # Send bus notification to the user
            self.env["bus.bus"]._sendone(
                acc.user_id.partner_id, "ow_mail/new_mail",
                {"account_id": acc.id, "account_name": acc.name,
                 "count": count, "message": message})
        except Exception as e:
            _logger.debug("new-mail check failed for %s: %s", acc.name, e)

    # ---------------- Folder tree sync ----------------

    def _imap_delimiter(self, conn):
        """Return the server's hierarchy delimiter ('/', '.', …) — default '/'."""
        typ, data = conn.list()
        if typ == "OK":
            for line in data or []:
                decoded = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
                m = re.match(r'\([^)]*\) "(?P<delim>[^"]*)"', decoded)
                if m and m.group("delim"):
                    return m.group("delim")
        return "/"

    def imap_create_folder(self, parent_full_path, name):
        """Create a new IMAP folder and refresh the local folder tree.

        The full path is assembled using the server's own hierarchy delimiter
        so the result is correct on both slash-separated (Dovecot) and
        dot-separated (Courier) servers. The folder is also subscribed
        immediately so it appears in the client's folder list.
        """
        self.ensure_one()
        name = (name or "").strip()
        if not name:
            raise UserError(_("Folder name is required"))
        conn = self._imap_connect()
        try:
            delim = self._imap_delimiter(conn)
            full_path = f"{parent_full_path}{delim}{name}" if parent_full_path else name
            typ, data = conn.create(_mbox(full_path))
            if typ != "OK":
                raise UserError(_("IMAP CREATE failed: %s") % (data or b"").decode("utf-8", "replace"))
            try:
                conn.subscribe(_mbox(full_path))
            except Exception:
                pass
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        self._refresh_folders()
        return full_path

    def imap_rename_folder(self, old_full_path, new_full_path):
        """Rename a folder by its full server path (IMAP RENAME command).

        Callers that only need to change the leaf segment should use
        ``imap_rename_leaf`` instead, which derives ``new_full_path``
        automatically from the server delimiter.
        """
        self.ensure_one()
        conn = self._imap_connect()
        try:
            typ, data = conn.rename(_mbox(old_full_path), _mbox(new_full_path))
            if typ != "OK":
                raise UserError(_("IMAP RENAME failed: %s") % (data or b"").decode("utf-8", "replace"))
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        self._refresh_folders()

    def imap_rename_leaf(self, old_full_path, new_leaf):
        """Rename the last segment of a folder path in one IMAP session."""
        self.ensure_one()
        conn = self._imap_connect()
        try:
            delim = self._imap_delimiter(conn)
            parts = old_full_path.split(delim)
            parts[-1] = new_leaf
            new_full_path = delim.join(parts)
            if new_full_path == old_full_path:
                return new_full_path
            typ, data = conn.rename(_mbox(old_full_path), _mbox(new_full_path))
            if typ != "OK":
                raise UserError(_("IMAP RENAME failed: %s") % (data or b"").decode("utf-8", "replace"))
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        self._refresh_folders()
        return new_full_path

    def imap_move_folder(self, old_full_path, new_parent_path):
        """Move a folder under a new parent (or to the root if empty) in one session.

        Rejects moves into self or into own subfolder.
        """
        self.ensure_one()
        conn = self._imap_connect()
        try:
            delim = self._imap_delimiter(conn)
            if new_parent_path == old_full_path or (
                new_parent_path and new_parent_path.startswith(old_full_path + delim)
            ):
                raise UserError(_("Cannot move folder into itself or its own subfolder"))
            leaf = old_full_path.split(delim)[-1]
            new_full_path = f"{new_parent_path}{delim}{leaf}" if new_parent_path else leaf
            if new_full_path == old_full_path:
                return new_full_path
            typ, data = conn.rename(_mbox(old_full_path), _mbox(new_full_path))
            if typ != "OK":
                raise UserError(_("IMAP RENAME failed: %s") % (data or b"").decode("utf-8", "replace"))
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        self._refresh_folders()
        return new_full_path

    def imap_delete_folder(self, full_path):
        """Permanently delete a folder on the IMAP server.

        Unsubscribes first so the folder disappears from LSUB listings even if
        the server's DELETE fails — errors from UNSUBSCRIBE are intentionally
        swallowed. This operation is irreversible; messages inside the folder
        are lost.
        """
        self.ensure_one()
        conn = self._imap_connect()
        try:
            try:
                conn.unsubscribe(_mbox(full_path))
            except Exception:
                pass
            typ, data = conn.delete(_mbox(full_path))
            if typ != "OK":
                raise UserError(_("IMAP DELETE failed: %s") % (data or b"").decode("utf-8", "replace"))
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        self._refresh_folders()

    def imap_set_subscribed(self, full_path, subscribed):
        """SUBSCRIBE or UNSUBSCRIBE a folder so it appears in (or is hidden from) LSUB results.

        Subscription state controls which folders clients show by default
        without affecting the folder's existence or its messages.
        """
        self.ensure_one()
        conn = self._imap_connect()
        try:
            if subscribed:
                typ, data = conn.subscribe(_mbox(full_path))
            else:
                typ, data = conn.unsubscribe(_mbox(full_path))
            if typ != "OK":
                raise UserError(_("IMAP %s failed: %s") % (
                    "SUBSCRIBE" if subscribed else "UNSUBSCRIBE",
                    (data or b"").decode("utf-8", "replace")))
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        self._refresh_folders()

    def imap_quota(self):
        """Return {used, total, percent} in bytes. None-total if unsupported."""
        self.ensure_one()
        conn = self._imap_connect()
        try:
            try:
                typ, data = conn._simple_command("GETQUOTAROOT", "INBOX")
                typ, data = conn._untagged_response(typ, data, "QUOTA")
            except Exception:
                return {"used": 0, "total": 0, "percent": 0, "supported": False}
            used = total = 0
            for line in data or []:
                if not line:
                    continue
                decoded = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
                m = re.search(r"STORAGE (\d+) (\d+)", decoded)
                if m:
                    used = max(used, int(m.group(1)) * 1024)
                    total = max(total, int(m.group(2)) * 1024)
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        pct = (used * 100 // total) if total else 0
        return {"used": used, "total": total, "percent": pct, "supported": total > 0}

    def imap_empty_folder(self, full_path):
        """Flag every message \\Deleted then EXPUNGE to permanently purge the folder contents.

        Used by the settings UI "Empty" action. This is irreversible — messages
        are gone from the server immediately and cannot be recovered.
        """
        self.ensure_one()
        conn = self._imap_connect()
        try:
            typ, _ = conn.select(_mbox(full_path))
            if typ != "OK":
                raise UserError(_("Cannot select %s") % full_path)
            conn.store("1:*", "+FLAGS", r"(\Deleted)")
            conn.expunge()
            conn.close()
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        self._refresh_folders()

    def _refresh_folders(self):
        """Rebuild the ``ow.mail.folder`` cache from a live IMAP LIST + STATUS.

        Issues ``LIST "" "*"`` to discover all folders, then issues a
        ``STATUS (MESSAGES UNSEEN)`` for each to populate counts. Folder
        records are upserted (create or update) and any that no longer appear
        in the LIST response are deleted so stale records don't linger after
        server-side renames or deletes. Finishes by calling
        ``_map_special_folders()`` to refresh the account's special-folder
        pointers.
        """
        self.ensure_one()
        conn = self._imap_connect()
        try:
            typ, data = conn.list()
            if typ != "OK":
                return
            existing = {f.full_path: f for f in self.folder_ids}
            seen = set()
            # First pass: collect raw rows. Second pass upserts with
            # parent_id resolved using the server's own delimiter (the
            # previous code split on "/" even on Courier-style "." servers,
            # which is why INBOX.Foo never nested under INBOX).
            rows = []
            for line in data or []:
                if not line:
                    continue
                decoded = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
                m = re.match(r'\((?P<flags>[^)]*)\) "(?P<delim>[^"]*)" (?P<name>.+)$', decoded)
                if not m:
                    continue
                flags = m.group("flags")
                delim = m.group("delim") or "/"
                name = m.group("name").strip().strip('"')
                leaf = name.split(delim)[-1] if delim else name
                kind = "custom"
                for tag, k in SPECIAL_USE_MAP.items():
                    if tag in flags:
                        kind = k
                        break
                if kind == "custom":
                    for rx, k in NAME_SPECIAL_HINTS:
                        if rx.match(leaf):
                            kind = k
                            break
                rows.append({"full_path": name, "leaf": leaf, "kind": kind, "delim": delim})
                seen.add(name)
            # Sort by depth so parents get upserted before children, then
            # resolve parent_id by full_path lookup.
            rows.sort(key=lambda r: r["full_path"].count(r["delim"]))
            Folder = self.env["ow.mail.folder"]
            path_to_id = {f.full_path: f.id for f in self.folder_ids}
            for r in rows:
                delim = r["delim"]
                parent_path = r["full_path"].rsplit(delim, 1)[0] if delim and delim in r["full_path"] else ""
                parent_id = path_to_id.get(parent_path) if parent_path and parent_path != r["full_path"] else False
                values = {
                    "name": r["leaf"], "full_path": r["full_path"],
                    "kind": r["kind"], "parent_id": parent_id,
                }
                if r["full_path"] in existing:
                    existing[r["full_path"]].write(values)
                    path_to_id[r["full_path"]] = existing[r["full_path"]].id
                else:
                    values["account_id"] = self.id
                    rec = Folder.create(values)
                    path_to_id[r["full_path"]] = rec.id
            stale = [f for p, f in existing.items() if p not in seen]
            if stale:
                self.env["ow.mail.folder"].browse([f.id for f in stale]).unlink()
            # Subscribed set via LSUB
            subscribed = set()
            try:
                typ, lsub_data = conn.lsub()
                if typ == "OK":
                    for line in lsub_data or []:
                        if not line:
                            continue
                        decoded = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
                        m = re.match(r'\([^)]*\) "[^"]*" (.+)$', decoded)
                        if m:
                            subscribed.add(m.group(1).strip().strip('"'))
            except Exception:
                subscribed = None
            if subscribed is not None:
                for folder in self.folder_ids:
                    folder.subscribed = folder.full_path in subscribed
            # Counts
            for folder in self.folder_ids:
                try:
                    from . import ow_mail_imap as imap_utils
                    t, u = imap_utils.status_counts(conn, folder.full_path)
                    folder.write({"total_count": t, "unread_count": u})
                except Exception:
                    pass
            self._map_special_folders()
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def _map_special_folders(self):
        """Populate the account's special-folder Many2one pointers from the folder cache.

        ``kind`` values on ``ow.mail.folder`` records are set during
        ``_refresh_folders()`` using IMAP SPECIAL-USE attributes first, then
        name-pattern heuristics (``NAME_SPECIAL_HINTS``) as a fallback for
        servers that omit SPECIAL-USE. This method simply picks the first
        folder of each kind and writes it to the corresponding field if it
        has changed, avoiding spurious write-triggers.
        """
        for kind, field_name in [
            ("inbox", "inbox_folder_id"),
            ("sent", "sent_folder_id"),
            ("drafts", "drafts_folder_id"),
            ("archive", "archive_folder_id"),
            ("spam", "spam_folder_id"),
            ("trash", "trash_folder_id"),
        ]:
            folder = self.folder_ids.filtered(lambda f: f.kind == kind)[:1]
            if folder and getattr(self, field_name).id != folder.id:
                self[field_name] = folder.id

    # ---------------- Outgoing ----------------

    def _build_outgoing(self, to, cc, subject, body_html, attachments=None,
                       in_reply_to=None, references=None, bcc=None):
        """Assemble a multipart/alternative ``EmailMessage`` ready for SMTP submission.

        The HTML body is run through ``html_sanitize`` to strip any client-side
        XSS that crept in through the compose editor. Control characters are
        stripped from header values to prevent header injection. A plain-text
        fallback is generated by stripping HTML tags. The signature is expected
        to have been appended by the client before calling this method, so it
        is not re-injected here.
        """
        self.ensure_one()
        msg = EmailMessage()
        if self.from_name_mode == "custom" and self.from_name_custom:
            display = self.from_name_custom
        else:
            display = self.user_id.name
        _strip_ctl = lambda s: re.sub(r"[\r\n\x00-\x1f]", "", s or "")
        display = _strip_ctl(display)
        from_email = _strip_ctl(self.email)
        msg["From"] = f"{display} <{from_email}>"
        msg["To"] = _strip_ctl(to)
        if cc:
            msg["Cc"] = _strip_ctl(cc)
        if bcc:
            msg["Bcc"] = _strip_ctl(bcc)
        msg["Subject"] = _strip_ctl(subject)
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid(
            domain=(self.email.split("@")[-1] if "@" in (self.email or "") else "ow"))
        if in_reply_to:
            msg["In-Reply-To"] = _strip_ctl(in_reply_to)
        if references:
            msg["References"] = _strip_ctl(references)
        # Signature is appended client-side (openReply / defaultBody), so
        # don't re-append here. str()-coerce because fields.Html can return
        # Markup and `str + Markup` silently HTML-escapes the plain half.
        full_html = str(body_html or "")
        if full_html:
            full_html = str(html_sanitize(full_html, sanitize_style=True,
                                          strip_classes=False))
        plain = re.sub(r"<[^>]+>", "", full_html)
        msg.set_content(plain or "")
        msg.add_alternative(full_html or "<p></p>", subtype="html")
        for att in attachments or []:
            msg.add_attachment(att["content"],
                               maintype=att.get("maintype", "application"),
                               subtype=att.get("subtype", "octet-stream"),
                               filename=att.get("filename", "attachment"))
        return msg

    def send_mail(self, to=None, cc=None, bcc=None, subject="", body_html="", attachment_ids=None,
                  in_reply_to=None, references=None):
        """Submit via SMTP then IMAP-APPEND a copy to the Sent folder.

        SMTP delivery happens first; if it succeeds the message is considered
        sent regardless of what follows. The Bcc header is stripped before
        APPEND so recipients in the Sent copy cannot see who was blind-copied.
        APPEND failure is non-fatal — the mail is already out — but the
        warning is surfaced to the UI so the user knows their Sent folder
        won't contain a copy.
        """
        self.ensure_one()
        atts = []
        for att in self.env["ir.attachment"].browse(attachment_ids or []):
            atts.append({
                "content": att.raw,
                "filename": att.name or "attachment",
                "maintype": (att.mimetype or "application/octet-stream").split("/")[0],
                "subtype": (att.mimetype or "application/octet-stream").split("/")[-1],
            })
        msg = self._build_outgoing(to, cc, subject, body_html, attachments=atts,
                                   in_reply_to=in_reply_to, references=references, bcc=bcc)
        addr_fields = [str(h) for h in (msg["To"], msg.get("Cc"), msg.get("Bcc")) if h]
        recipients = [a for _n, a in getaddresses(addr_fields) if a]
        if not recipients:
            raise UserError(_(
                "No valid recipient address. Please fill in To, Cc, or Bcc."))
        smtp = self._smtp_connect()
        try:
            smtp.send_message(msg, from_addr=self.email, to_addrs=recipients)
        finally:
            smtp.quit()
        # Strip Bcc before storing in Sent folder
        if "Bcc" in msg:
            del msg["Bcc"]
        sent_warning = None
        if self.sent_folder_id:
            try:
                imap = self._imap_connect()
                try:
                    imap.append(_mbox(self.sent_folder_id.full_path), "(\\Seen)",
                                imaplib.Time2Internaldate(datetime.now(timezone.utc)), msg.as_bytes())
                finally:
                    imap.logout()
            except Exception as e:
                # SMTP already succeeded — the mail IS out. Surface the
                # APPEND failure so the UI can warn the user their Sent
                # folder won't show a copy, instead of silent.
                _logger.warning("Sent APPEND failed for account %s: %s",
                                self.name, e)
                sent_warning = str(e) or e.__class__.__name__
        return {"ok": True, "sent_append_failed": sent_warning} if sent_warning \
            else {"ok": True}

    def save_draft(self, to="", cc="", bcc="", subject="", body_html="", attachment_ids=None,
                   in_reply_to=None, references=None):
        """IMAP-APPEND the composed message to the Drafts folder with the \\Draft flag.

        No SMTP delivery occurs. The message is built via ``_build_outgoing``
        so it has the correct MIME structure and headers if the user later
        moves the draft to an external client.
        """
        self.ensure_one()
        atts = []
        for att in self.env["ir.attachment"].browse(attachment_ids or []):
            atts.append({
                "content": att.raw,
                "filename": att.name or "attachment",
                "maintype": (att.mimetype or "application/octet-stream").split("/")[0],
                "subtype": (att.mimetype or "application/octet-stream").split("/")[-1],
            })
        msg = self._build_outgoing(to, cc, subject, body_html, attachments=atts,
                                   in_reply_to=in_reply_to, references=references, bcc=bcc)
        imap = self._imap_connect()
        try:
            imap.append(_mbox(self.drafts_folder_id.full_path), "(\\Draft)",
                        imaplib.Time2Internaldate(datetime.now(timezone.utc)), msg.as_bytes())
        finally:
            imap.logout()
        return True

