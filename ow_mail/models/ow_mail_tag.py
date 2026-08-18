import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .ow_mail_imap import imap_mbox_quote, search_uids

_logger = logging.getLogger(__name__)

# Chunk size for keyword-migration STORE commands — mirrors the controller's
# _MAX_ACTION_UIDS so a single IMAP command never carries an unbounded uid set.
_MIGRATE_CHUNK = 1000


# IMAP-atom-safe alphabet for keywords. RFC 3501 atom-specials disallow
# space, parens, brackets, braces, quotes, backslash, controls, and the
# wildcards % *. The narrower [A-Za-z0-9_-] set is well within the legal
# range and matches the OwTag_<slug> shape emitted by _slug() below.
_KEYWORD_RX = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Default tag set seeded per user on first account creation:
# (display name, palette color index).
DEFAULT_TAGS = [
    ("Urgent", 1),       # red
    ("Follow-up", 10),   # green
]


def _slug(name):
    """Convert a display name to an IMAP-safe atom fragment.

    Strips every character that is not alphanumeric — IMAP keyword atoms
    cannot contain spaces or special characters (RFC 3501 §9 atom definition).
    Case is preserved. The result is truncated to 24 characters and falls
    back to ``"Tag"`` for empty input.
    """
    return re.sub(r"[^A-Za-z0-9]+", "", name or "")[:24] or "Tag"


class OwMailTag(models.Model):
    """Local Odoo record that maps a display label to an IMAP custom keyword.

    The ``imap_keyword`` field (e.g. ``OwTag_InboxReview``) is the actual
    flag stored on the IMAP server via ``UID STORE +FLAGS (<keyword>)``.
    Filtering by a tag issues ``UID SEARCH KEYWORD <keyword>``. The
    ``OwTag_`` prefix reserves the namespace to avoid clashing with IMAP
    system flags (``\\Seen``, ``\\Flagged``, etc.). Tags are user-scoped
    via ``ir.rule`` so two users may have tags with the same display name
    without conflict.
    """

    _name = "ow.mail.tag"
    _description = "OW Mail Tag"
    _order = "sequence, name"

    name = fields.Char(required=True)
    color = fields.Integer(default=0)
    sequence = fields.Integer(default=10)
    user_id = fields.Many2one("res.users", required=True,
                              default=lambda self: self.env.user, ondelete="cascade")
    imap_keyword = fields.Char(help="IMAP keyword flag used for this tag.")

    _sql_constraints = [
        ("user_name_uniq", "unique(user_id, name)", "Tag name must be unique per user."),
    ]

    @api.constrains("imap_keyword")
    def _check_imap_keyword(self):
        """Validate that ``imap_keyword`` is a legal IMAP atom.

        An invalid keyword would silently break ``STORE`` / ``SEARCH`` on the
        server (IMAP atom rules), leaving the user with a tag that doesn't
        round-trip. Rejected at write-time so the error surfaces immediately.
        The ``OwTag_`` prefix is enforced by ``create``; this constraint
        catches manual overwrites or direct ORM writes that bypass the default.
        """
        for rec in self:
            kw = rec.imap_keyword or ""
            if kw and not _KEYWORD_RX.match(kw):
                raise ValidationError(_(
                    "IMAP keyword %r must match [A-Za-z0-9_-] (1–64 chars)."
                ) % kw)

    @api.model
    def _ensure_default_tags(self, user):
        """Seed ``DEFAULT_TAGS`` for *user* if they have no tags yet.

        Called when a user's first mail account is created, so every user
        starts with the same basic label set. Runs as sudo with an explicit
        ``user_id`` because an admin may create the account on behalf of
        another user. Users who later delete these tags are not re-seeded —
        the guard is "has no tags", checked only at account creation.
        """
        Tag = self.sudo()
        if Tag.search_count([("user_id", "=", user.id)]):
            return
        Tag.create([
            {"name": name, "color": color, "user_id": user.id}
            for name, color in DEFAULT_TAGS
        ])

    @api.model_create_multi
    def create(self, vals_list):
        """Auto-generate ``imap_keyword`` from the display name when not provided.

        Renaming a tag later regenerates the keyword too — ``write`` migrates
        the flag on the IMAP server (best-effort) so messages stay associated
        with the tag under its new keyword.
        """
        for v in vals_list:
            if not v.get("imap_keyword"):
                v["imap_keyword"] = f"OwTag_{_slug(v.get('name'))}"
        return super().create(vals_list)

    def _unique_keyword(self, base_kw):
        """Return *base_kw*, suffixed ``-2``/``-3``… if another of the user's
        tags already claims it. Stays within the 64-char keyword limit."""
        self.ensure_one()
        kw = base_kw
        n = 2
        while self.search_count([
                ("user_id", "=", self.user_id.id),
                ("imap_keyword", "=", kw),
                ("id", "!=", self.id)]):
            suffix = f"-{n}"
            kw = base_kw[: 64 - len(suffix)] + suffix
            n += 1
        return kw

    def _sync_keyword_on_server(self, old_kw, new_kw):
        """Rewrite (or, when ``new_kw`` is None, remove) *old_kw* on every
        folder of the owner's confirmed accounts.

        Fully best-effort: IMAP failures are logged and never block the ORM
        operation that triggered the migration. Cost is O(folders) SEARCH +
        STORE round-trips per account, which is acceptable for a rename but
        noticeably slow on very large mailboxes.
        """
        self.ensure_one()
        if not old_kw or old_kw == new_kw:
            return
        accounts = self.env["ow.mail.account"].sudo().search([
            ("user_id", "=", self.user_id.id),
            ("state", "=", "confirmed"),
        ])
        for acc in accounts:
            try:
                conn = acc._imap_connect()
                try:
                    for folder in acc.folder_ids:
                        try:
                            typ, _sel = conn.select(
                                imap_mbox_quote(folder.full_path))
                            if typ != "OK":
                                continue
                            uids = search_uids(conn, f"KEYWORD {old_kw}")
                            for i in range(0, len(uids), _MIGRATE_CHUNK):
                                uid_set = ",".join(
                                    str(u) for u in uids[i:i + _MIGRATE_CHUNK])
                                if new_kw:
                                    conn.uid("STORE", uid_set, "+FLAGS",
                                             f"({new_kw})")
                                conn.uid("STORE", uid_set, "-FLAGS",
                                         f"({old_kw})")
                        except Exception:
                            continue
                finally:
                    try:
                        conn.logout()
                    except Exception:
                        pass
            except Exception as e:
                _logger.warning(
                    "ow_mail: keyword migration %s -> %s failed for %s: %s",
                    old_kw, new_kw, acc.email, e)

    def write(self, vals):
        """Regenerate + migrate the IMAP keyword when a tag is renamed.

        Only auto-generated keywords (``OwTag_`` prefix) follow the name; a
        manually assigned keyword is left alone. An explicit ``imap_keyword``
        in *vals* also disables the auto-regeneration for that write.
        """
        if "name" not in vals or "imap_keyword" in vals:
            return super().write(vals)
        for rec in self:
            old_kw = rec.imap_keyword or ""
            rec_vals = dict(vals)
            if old_kw.startswith("OwTag_"):
                new_kw = rec._unique_keyword(f"OwTag_{_slug(vals['name'])}")
                if new_kw != old_kw:
                    rec._sync_keyword_on_server(old_kw, new_kw)
                    rec_vals["imap_keyword"] = new_kw
            super(OwMailTag, rec).write(rec_vals)
        return True

    def unlink(self):
        """Remove each tag's keyword from the mail server before deleting.

        Best-effort: without this, deleted tags leave orphan ``OwTag_*``
        keywords on every flagged message forever.
        """
        for rec in self:
            if rec.imap_keyword:
                rec._sync_keyword_on_server(rec.imap_keyword, None)
        return super().unlink()
