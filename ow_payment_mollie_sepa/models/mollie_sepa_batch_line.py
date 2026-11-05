# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
import logging

from psycopg2.errors import LockNotAvailable

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import SQL

from .. import const

_logger = logging.getLogger(__name__)


class MollieSepaBatchLine(models.Model):
    _name = "mollie.sepa.batch.line"
    _description = "Mollie SEPA Collection Batch Line"
    _order = "id asc"
    _check_company_auto = True

    batch_id = fields.Many2one(
        "mollie.sepa.batch", string="Batch", required=True, index=True,
        ondelete="cascade",
    )
    company_id = fields.Many2one(
        related="batch_id.company_id", store=True, string="Company"
    )
    partner_id = fields.Many2one(
        "res.partner", string="Customer", required=True, readonly=True
    )
    invoice_id = fields.Many2one(
        "account.move",
        string="Invoice",
        required=True,
        readonly=True,
        index=True,
        check_company=True,
        domain="[('move_type', '=', 'out_invoice'), ('state', '=', 'posted')]",
    )
    amount = fields.Monetary(
        string="Amount", required=True, currency_field="currency_id"
    )
    currency_id = fields.Many2one(
        related="batch_id.currency_id", string="Currency"
    )
    state = fields.Selection(
        selection=[
            ("draft", "Draft"),
            ("pending", "Pending at Mollie"),
            ("done", "Collected"),
            ("failed", "Failed"),
            ("charged_back", "Charged Back"),
            ("cancel", "Canceled"),
        ],
        required=True,
        default="draft",
        index=True,
    )
    transaction_id = fields.Many2one(
        "payment.transaction", string="Payment Transaction", readonly=True, copy=False
    )
    message = fields.Char(readonly=True)
    mollie_test_url = fields.Char(related="transaction_id.mollie_test_url", string="Mollie Test Link")

    @api.constrains("amount", "invoice_id")
    def _check_line(self):
        for line in self:
            if line.amount <= 0:
                raise ValidationError(_("The collection amount must be positive."))
            if line.invoice_id.company_id != line.batch_id.company_id:
                raise ValidationError(_("The invoice belongs to another company."))

    @api.onchange("invoice_id")
    def _onchange_invoice_id(self):
        for line in self:
            if line.invoice_id:
                line.partner_id = line.invoice_id.partner_id
                line.amount = line.invoice_id.amount_residual

    @api.model
    def _sepa_get_blocking_reason(self, invoice, exclude_line=None):
        """Return why the invoice cannot be collected now, or False.

        Shared by the invoice button, the line generation and the charge.
        """
        invoice = invoice.sudo()
        invoice.invalidate_recordset(["amount_residual", "payment_state", "state"])
        if invoice.move_type != "out_invoice" or invoice.state != "posted":
            return _("Only posted customer invoices can be collected.")
        if invoice.currency_id.compare_amounts(invoice.amount_residual, 0) <= 0:
            return _("The invoice is already paid.")
        domain = [("invoice_id", "=", invoice.id), ("state", "in", ["draft", "pending", "done"])]
        if exclude_line:
            domain.append(("id", "!=", exclude_line.id))
        for other in self.sudo().search(domain):
            tx = other.transaction_id
            if other.state == "pending" or (tx and tx.state in ("draft", "pending", "authorized")):
                return _("A direct debit is already running in batch %s.", other.batch_id.name)
            if other.state == "done" and tx and not tx.is_post_processed:
                return _("A direct debit of batch %s is being booked.", other.batch_id.name)
            # Draft lines of draft batches do not block: the charge re-checks.
            if other.state == "draft" and not exclude_line and other.batch_id.state == "collecting":
                return _("The invoice is already scheduled in batch %s.", other.batch_id.name)
        own_tx = exclude_line.transaction_id if exclude_line else self.env["payment.transaction"]
        open_tx = self.env["payment.transaction"].sudo().search([
            ("invoice_ids", "in", invoice.ids),
            ("state", "in", ["pending", "authorized"]),
            ("id", "not in", own_tx.ids),
        ], limit=1)
        if open_tx:
            return _("Online payment %s is still being processed.", open_tx.reference)
        return False

    def _sepa_lock_invoice(self):
        """Lock the invoice row until commit; False if another transaction holds it."""
        self.ensure_one()
        try:
            with self.env.cr.savepoint(flush=False):
                self.env.cr.execute(SQL(
                    "SELECT id FROM account_move WHERE id = %s FOR UPDATE NOWAIT",
                    self.invoice_id.id,
                ))
        except LockNotAvailable:
            return False
        return True

    def _sepa_charge(self):
        """Send the direct debit to Mollie. Cron only: it commits.

        The transaction is committed before the request, so a payment created
        at Mollie always exists in Odoo; `_sepa_recover` handles interrupted runs.
        """
        self.ensure_one()
        Provider = self.env["payment.provider"]
        line = self.sudo()
        if line.state != "draft":
            return
        if line.transaction_id:
            # Interrupted earlier: recover, never send a new payment.
            with self.env.cr.savepoint():
                line.transaction_id._sepa_recover()
                line._sepa_sync_state_from_transaction()
            Provider._sepa_commit()
            return

        # 1. Prepare and commit.
        with self.env.cr.savepoint():
            if not line._sepa_lock_invoice():
                line.message = _("Invoice locked by another process; retried on the next run.")
                return
            reason = self._sepa_get_blocking_reason(line.invoice_id, exclude_line=line)
            if reason:
                line.write({"state": "cancel", "message": reason})
                return
            residual = line.invoice_id.amount_residual
            if line.currency_id.compare_amounts(line.amount, residual) > 0:
                line.amount = residual
            provider = line.batch_id.provider_id
            token = self.env["payment.token"]._get_sepa_token_for_partner(
                line.partner_id, provider
            )
            if not token or not token._sepa_is_chargeable():
                line.write({
                    "state": "failed",
                    "message": _("No valid SEPA mandate for this customer."),
                })
                line.invoice_id.message_post(body=_(
                    "SEPA direct debit skipped: no valid mandate registered for this "
                    "customer. The invoice remains open."
                ))
                return
            transaction = line._sepa_create_transaction(token)
            line.write({"transaction_id": transaction.id, "message": False})
        Provider._sepa_commit()

        # 2. Send and commit.
        with self.env.cr.savepoint():
            transaction._sepa_charge_with_token()
            line._sepa_sync_state_from_transaction()
        Provider._sepa_commit()

    def _sepa_sync_state_from_transaction(self):
        for line in self.sudo():
            tx = line.transaction_id
            if not tx or tx.state == "draft":
                continue
            if tx.state in ("done", "cancel", "error"):
                if not tx.is_post_processed:
                    tx._post_process()
            else:
                line.write({"state": const.LINE_STATE_MAPPING.get(tx.state, "pending")})

    def _sepa_create_transaction(self, token):
        self.ensure_one()
        batch = self.batch_id
        reference = self.env["payment.transaction"]._compute_reference(
            "mollie", prefix=f"{batch.name}-{self.invoice_id.name or self.partner_id.id}"
        )
        return self.env["payment.transaction"].sudo().create({
            "provider_id": batch.provider_id.id,
            "payment_method_id": token.payment_method_id.id,
            "reference": reference,
            "amount": self.amount,
            "currency_id": batch.currency_id.id,
            "partner_id": self.partner_id.id,
            "token_id": token.id,
            "operation": "offline",
            "invoice_ids": [(4, self.invoice_id.id)],
            "sepa_batch_line_id": self.id,
            "sepa_request_sent": True,
        })
