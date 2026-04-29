from odoo import api, fields, models


def _norm_email(email):
    """Normalize an email address to lowercase for case-insensitive deduplication.

    RFC 5321 technically allows case-sensitive local-parts, but virtually all
    mail systems treat them as case-insensitive in practice. Normalizing on
    write prevents duplicate contacts arising from case variations such as
    ``Alice@Example.com`` vs ``alice@example.com``.
    """
    return (email or "").strip().lower()


class OwMailContact(models.Model):
    """Local address book for the OW mail client, distinct from ``res.partner``.

    Records are stored per-user (enforced via ``ir.rule``) and carry a
    ``last_used`` timestamp that is updated each time a message is sent to
    that address. The default ordering (``last_used desc``) means the compose
    autocomplete ranks recently-used addresses highest.
    """

    _name = "ow.mail.contact"
    _description = "OW Mail Contact"
    _order = "last_used desc, name"

    name = fields.Char(required=True)
    email = fields.Char(required=True)
    company = fields.Char()
    phone = fields.Char()
    note = fields.Text()
    user_id = fields.Many2one(
        "res.users", required=True,
        default=lambda self: self.env.user, ondelete="cascade",
    )
    partner_id = fields.Many2one("res.partner", ondelete="set null")
    last_used = fields.Datetime()

    _sql_constraints = [
        ("user_email_uniq", "unique(user_id, email)",
         "This email is already in your contacts."),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        """Normalize email to lowercase before inserting to prevent duplicates."""
        for v in vals_list:
            if v.get("email"):
                v["email"] = _norm_email(v["email"])
        return super().create(vals_list)

    def write(self, vals):
        """Normalize email to lowercase on update to prevent case-variant duplicates."""
        if "email" in vals:
            vals["email"] = _norm_email(vals["email"])
        return super().write(vals)

    def touch(self):
        """Update ``last_used`` to now.

        Called after successfully sending a message to this contact so that
        the compose autocomplete ranks recently-used addresses higher.
        """
        self.write({"last_used": fields.Datetime.now()})
