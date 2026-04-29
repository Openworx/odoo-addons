from odoo import _, models


class ResPartner(models.Model):
    _inherit = "res.partner"

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
