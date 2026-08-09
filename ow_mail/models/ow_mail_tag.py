import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


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

        The keyword is intentionally set only at creation time. Renaming a
        tag's display name later does NOT update the keyword, which keeps the
        IMAP flag data intact — all messages already flagged with the old
        keyword remain correctly associated with the tag.
        """
        for v in vals_list:
            if not v.get("imap_keyword"):
                v["imap_keyword"] = f"OwTag_{_slug(v.get('name'))}"
        return super().create(vals_list)
