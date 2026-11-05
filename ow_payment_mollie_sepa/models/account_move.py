# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class AccountMove(models.Model):
    _inherit = "account.move"

    sepa_line_state = fields.Selection(
        selection=[
            ("none", "None"),
            ("draft", "Scheduled in a batch"),
            ("pending", "Pending at Mollie"),
            ("done", "Collected"),
            ("failed", "Failed"),
            ("charged_back", "Charged Back"),
        ],
        string="SEPA Direct Debit",
        compute="_compute_sepa_line_state",
    )

    sepa_mandate_available = fields.Boolean(
        string="SEPA Mandate", compute="_compute_sepa_mandate_available",
    )

    @api.depends("partner_id", "company_id")
    def _compute_sepa_mandate_available(self):
        partners = self.commercial_partner_id
        tokens = self.env["payment.token"]
        if partners:
            tokens = tokens.sudo().search([
                ("partner_id", "child_of", partners.ids),
                ("provider_id.code", "=", "mollie"),
                ("provider_id.sepa_enabled", "=", True),
                ("provider_id.state", "in", ["enabled", "test"]),
                ("provider_ref", "=like", "mdt_%"),
                ("active", "=", True),
                ("sepa_status", "!=", "invalid"),
            ]).filtered(lambda t: t._sepa_is_current_mode())
        covered = {
            (t.partner_id.commercial_partner_id.id, t.provider_id.company_id.id) for t in tokens
        }
        for move in self:
            move.sepa_mandate_available = (
                (move.commercial_partner_id.id, move.company_id.id) in covered
            )

    @api.depends("partner_id")
    def _compute_sepa_line_state(self):
        lines = self.env["mollie.sepa.batch.line"].sudo().search([
            ("invoice_id", "in", self.ids),
            ("state", "!=", "cancel"),
        ], order="id desc")
        latest = {}
        for line in lines:
            latest.setdefault(line.invoice_id.id, line.state)
        for move in self:
            move.sepa_line_state = latest.get(move.id, "none")

    def action_sepa_collect(self):
        self.ensure_one()
        if not self.sepa_mandate_available:
            raise UserError(_("This customer has no valid SEPA mandate."))
        reason = self.env["mollie.sepa.batch.line"]._sepa_get_blocking_reason(self)
        if reason:
            raise UserError(reason)
        Provider = self.env["payment.provider"]
        provider = Provider._sepa_get_enabled_providers(company=self.company_id)[:1]
        if not provider:
            raise UserError(_(
                "No Mollie provider with SEPA Direct Debit is enabled for company %s.",
                self.company_id.name,
            ))
        batch = self.env["mollie.sepa.batch"].create({
            "provider_id": provider.id,
            "line_ids": [(0, 0, {
                "partner_id": self.partner_id.id,
                "invoice_id": self.id,
                "amount": self.amount_residual,
            })],
        })
        return {
            "name": _("SEPA Collection %s", batch.name),
            "type": "ir.actions.act_window",
            "res_model": "mollie.sepa.batch",
            "view_mode": "form",
            "res_id": batch.id,
        }
