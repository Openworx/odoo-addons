"""IMAP folder cache — tree + unread/total counts only."""
from odoo import api, fields, models

from . import ow_mail_imap as imap_utils


class OwMailFolder(models.Model):
    """Slim IMAP folder cache — metadata only, no message content.

    Each record mirrors one IMAP mailbox for a given account, storing the
    ``full_path`` (as reported by ``LIST``), the ``kind`` (inbox, sent,
    drafts, trash, spam, archive, or custom), and cached ``unread_count`` /
    ``total_count`` values. These counts are refreshed via cron and
    on-demand sync calls; they are never authoritative and must not be
    used for business logic. Message bodies and headers are never persisted
    — every open is a live IMAP round-trip.
    """

    _name = "ow.mail.folder"
    _description = "OW Mail Folder"
    _order = "kind, name"

    account_id = fields.Many2one("ow.mail.account", required=True, ondelete="cascade")
    user_id = fields.Many2one(related="account_id.user_id", store=True)
    name = fields.Char(required=True)
    full_path = fields.Char(string="Full Path", required=True)
    parent_id = fields.Many2one("ow.mail.folder", ondelete="set null")
    kind = fields.Selection([
        ("inbox", "Inbox"), ("sent", "Sent"), ("drafts", "Drafts"),
        ("archive", "Archive"), ("spam", "Spam"), ("trash", "Trash"),
        ("custom", "Custom"),
    ], default="custom")
    unread_count = fields.Integer(default=0)
    total_count = fields.Integer(default=0)
    subscribed = fields.Boolean(default=True)
    last_seen_uid = fields.Integer(default=0, help="Highest UID seen — used for new-mail detection")
    auto_link_uid = fields.Integer(
        default=0,
        help="Highest UID processed by reply auto-filing — independent of "
             "last_seen_uid and advanced only after successful processing")

    _sql_constraints = [
        ("account_path_uniq", "unique(account_id, full_path)",
         "Folder path must be unique per account."),
    ]

    def refresh_counts(self, conn=None):
        """Update unread/total via IMAP STATUS. `conn` reuses an existing session."""
        if not self:
            return
        close = False
        if conn is None:
            conn = self.account_id[:1]._imap_connect()
            close = True
        try:
            for folder in self:
                try:
                    total, unseen = imap_utils.status_counts(conn, folder.full_path)
                    folder.write({"total_count": total, "unread_count": unseen})
                except Exception:
                    pass
        finally:
            if close:
                try:
                    conn.logout()
                except Exception:
                    pass
