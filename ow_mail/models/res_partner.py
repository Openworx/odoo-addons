from odoo import _, api, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    @api.model
    def _ow_find_by_email(self, email):
        """Resolve a sender address to a partner.

        Exact match on ``email_normalized`` first, then a case-insensitive
        fallback on the raw ``email`` column for legacy rows where the
        normalized value was never computed. Single home for the lookup —
        controllers and the record-link helper all funnel through here.
        """
        email = (email or "").strip().lower()
        if not email:
            return self.browse()
        return (self.search([("email_normalized", "=", email)], limit=1)
                or self.search([("email", "=ilike", email)], limit=1))

    def action_ow_mail_conversations(self):
        """Open the OW mail client pre-filtered to emails involving this partner.

        Acts as the smart button on the partner form that bridges Odoo
        CRM/contacts with the personal mail client. The search is seeded
        with the partner's normalized email so the message list opens
        directly on relevant conversations.
        """
        self.ensure_one()
        query = self.email_normalized or self.email or ""
        return {
            "type": "ir.actions.client",
            "tag": "ow_mail.mailclient",
            "name": _("Mail with %s") % self.display_name,
            "params": {"search": query},
        }
