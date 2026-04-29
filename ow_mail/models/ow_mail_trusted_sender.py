from odoo import fields, models


class OwMailTrustedSender(models.Model):
    """Per-user safe-mode allow-list for remote content in emails.

    When a sender's full email address or bare domain appears in this list,
    the message viewer renders remote content (images, CSS background-images)
    without blocking. Trust is evaluated server-side on every message open
    and returned in the message payload as ``trusted=True``; the frontend
    then skips the ``SafeModeBanner`` and loads external resources directly.
    Records are user-scoped via ``ir.rule`` so each user maintains their own
    independent allow-list.
    """

    _name = "ow.mail.trusted.sender"
    _description = "OW Mail Trusted Sender"

    user_id = fields.Many2one("res.users", required=True,
                              default=lambda self: self.env.user, ondelete="cascade")
    email = fields.Char(required=True, help="Full email address or bare domain to trust.")
    note = fields.Char()

    _sql_constraints = [
        ("user_email_uniq", "unique(user_id, email)", "Already trusted."),
    ]
