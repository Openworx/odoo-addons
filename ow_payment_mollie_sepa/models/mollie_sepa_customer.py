# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
from odoo import fields, models


class MollieSepaCustomer(models.Model):
    """Mollie customer per partner, provider and mode (customers are scoped
    to an API key: test and live never share them).
    """
    _name = "mollie.sepa.customer"
    _description = "Mollie Customer"
    _rec_name = "customer_id"

    partner_id = fields.Many2one(
        "res.partner", string="Partner", required=True, index=True, ondelete="cascade",
    )
    provider_id = fields.Many2one(
        "payment.provider", string="Provider", required=True, ondelete="cascade",
    )
    mode = fields.Selection(
        selection=[("test", "Test"), ("live", "Live")], required=True,
    )
    customer_id = fields.Char(string="Mollie Customer ID", required=True)

    _partner_provider_mode_uniq = models.Constraint(
        "UNIQUE(partner_id, provider_id, mode)",
        "A partner can only have one Mollie customer per provider and mode.",
    )
