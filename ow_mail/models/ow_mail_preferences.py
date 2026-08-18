"""Per-user client preferences for the OW Mail OWL client.

One record per user, created lazily by ``_get_for_user``. Kept as a
dedicated model (rather than fields on ``res.users``) so the standard
"own only" ``ir.rule`` pattern of this module applies and no
``SELF_READABLE_FIELDS`` plumbing is needed.
"""
from odoo import api, fields, models


class OwMailPreferences(models.Model):
    _name = "ow.mail.preferences"
    _description = "OW Mail Preferences"

    user_id = fields.Many2one(
        "res.users", required=True, ondelete="cascade",
        default=lambda self: self.env.user)
    # Seconds before an opened message is marked \Seen:
    # 0 = immediately (historic behaviour), -1 = only manually.
    mark_read_delay = fields.Integer(default=0)
    thread_view_default = fields.Boolean(default=False)
    # Gmail-style stacked conversation in the reading pane: when enabled the
    # client looks up the full thread (ancestors + descendants) on every
    # message open; disabled shows only the opened message.
    stacked_threads = fields.Boolean(default=True)

    _sql_constraints = [
        ("user_uniq", "unique(user_id)",
         "Each user has a single OW Mail preferences record."),
    ]

    @api.model
    def _get_for_user(self):
        """Return the current user's preferences record, creating defaults on first use."""
        prefs = self.search([("user_id", "=", self.env.user.id)], limit=1)
        if not prefs:
            prefs = self.create({"user_id": self.env.user.id})
        return prefs

    def _to_wire(self):
        self.ensure_one()
        return {
            "mark_read_delay": self.mark_read_delay,
            "thread_view_default": self.thread_view_default,
            "stacked_threads": self.stacked_threads,
        }
