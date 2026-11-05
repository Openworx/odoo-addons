# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
import logging
from datetime import datetime, time, timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .. import const

_logger = logging.getLogger(__name__)


class MollieSepaBatch(models.Model):
    _name = "mollie.sepa.batch"
    _description = "Mollie SEPA Collection Batch"
    _order = "date desc, id desc"
    _check_company_auto = True

    name = fields.Char(
        string="Batch Reference", required=True, readonly=True, copy=False,
        default=lambda self: _("New"),
    )
    date = fields.Date(
        string="Collection Date", required=True, default=fields.Date.context_today,
        help="Invoices due up to this date are fetched, and the direct debits "
        "are sent to Mollie from this date on.",
    )
    provider_id = fields.Many2one(
        "payment.provider",
        string="Provider",
        required=True,
        ondelete="restrict",
        domain="[('code', '=', 'mollie'), ('sepa_enabled', '=', True)]",
        default=lambda self: self._default_provider_id(),
    )
    company_id = fields.Many2one(
        related="provider_id.company_id", store=True, readonly=False
    )
    currency_id = fields.Many2one(
        "res.currency", string="Collection Currency", compute="_compute_currency_id",
        help="SEPA Direct Debit only exists in euro, whatever the company currency.",
    )
    state = fields.Selection(
        selection=[
            ("draft", "Draft"),
            ("collecting", "Collecting"),
            ("collected", "Collected"),
            ("done", "Completed"),
            ("cancel", "Canceled"),
        ],
        required=True,
        default="draft",
        copy=False,
        index=True,
    )
    line_ids = fields.One2many(
        "mollie.sepa.batch.line", "batch_id", string="Collection Lines",
        readonly=False, copy=False,
    )
    lines_count = fields.Integer(compute="_compute_totals")
    total_amount = fields.Monetary(
        string="Total to Collect", compute="_compute_totals",
        currency_field="currency_id",
    )
    collected_amount = fields.Monetary(
        compute="_compute_totals", currency_field="currency_id",
        string="Collected Amount",
    )
    failed_amount = fields.Monetary(
        compute="_compute_totals", currency_field="currency_id",
        string="Failed Amount",
    )

    @api.model
    def _default_provider_id(self):
        providers = self.env["payment.provider"]._sepa_get_enabled_providers(
            company=self.env.company
        ) or self.env["payment.provider"]._sepa_get_enabled_providers()
        return providers[:1].id

    def _compute_currency_id(self):
        euro = self.env.ref("base.EUR")
        for batch in self:
            batch.currency_id = euro

    @api.depends("line_ids", "line_ids.amount", "line_ids.state")
    def _compute_totals(self):
        for batch in self:
            batch.lines_count = len(batch.line_ids)
            batch.total_amount = sum(
                l.amount for l in batch.line_ids if l.state != "cancel"
            )
            batch.collected_amount = sum(l.amount for l in batch.line_ids if l.state == "done")
            batch.failed_amount = sum(
                l.amount for l in batch.line_ids if l.state in ("failed", "charged_back")
            )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", _("New")) == _("New"):
                vals["name"] = (
                    self.env["ir.sequence"].next_by_code("mollie.sepa.batch")
                    or _("New")
                )
        return super().create(vals_list)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_sent(self):
        if any(line.transaction_id for line in self.line_ids):
            raise UserError(_("A batch with direct debits sent to Mollie cannot be deleted."))

    def action_generate_lines(self):
        for batch in self:
            if batch.state != "draft":
                raise UserError(_(
                    "Batch %s is not a draft. Every collection runs in a new "
                    "batch: create a new batch instead.", batch.name
                ))
            if not batch._sepa_generate_lines():
                raise UserError(_(
                    "No open invoices can be collected through a SEPA direct "
                    "debit at this moment.\n\n"
                    "Requirements: posted invoices in %(currency)s due by %(date)s, for "
                    "customers with an active SEPA mandate and SEPA collection enabled, "
                    "no direct debit already running for those invoices, amount up to "
                    "%(max).2f, and the retry rules of the provider respected.",
                    currency=batch.currency_id.name,
                    date=batch.date,
                    max=batch.provider_id.sepa_max_amount or 0.0,
                ))
        return True

    def _sepa_generate_lines(self):
        """Replace the draft lines; return the number of lines created."""
        self.ensure_one()
        self.line_ids.filtered(lambda l: l.state == "draft" and not l.transaction_id).unlink()
        lines_vals = self._prepare_lines_vals()
        self.env["mollie.sepa.batch.line"].create(lines_vals)
        return len(lines_vals)

    def _prepare_lines_vals(self):
        self.ensure_one()
        provider = self.provider_id
        if provider.company_id not in self.env.companies and not self.env.su:
            raise UserError(_("The provider belongs to another company."))
        Line = self.env["mollie.sepa.batch.line"]
        lines_vals = []
        for move in self._search_collectable_invoices():
            if Line._sepa_get_blocking_reason(move):
                continue
            token = self.env["payment.token"]._get_sepa_token_for_partner(
                move.partner_id, provider
            )
            if not token or not token._sepa_is_chargeable():
                continue
            amount = move.amount_residual
            if amount > provider.sepa_max_amount:
                _logger.info(
                    "SEPA collection skipped %s: amount %.2f exceeds the "
                    "direct debit limit of %.2f.",
                    move.name, amount, provider.sepa_max_amount,
                )
                continue
            lines_vals.append({
                "batch_id": self.id,
                "partner_id": move.partner_id.id,
                "invoice_id": move.id,
                "amount": amount,
            })
        return lines_vals

    def _search_collectable_invoices(self):
        self.ensure_one()
        provider = self.provider_id
        token_partners = self.env["payment.token"].sudo().search([
            ("provider_id", "=", provider.id),
            ("provider_ref", "=like", "mdt_%"),
            ("active", "=", True),
            ("sepa_status", "!=", "invalid"),
        ]).filtered(lambda t: t._sepa_is_current_mode()).partner_id.commercial_partner_id
        token_partners = token_partners.filtered("sepa_collect_enabled")
        if not token_partners:
            return self.env["account.move"]
        moves = self.env["account.move"].search([
            ("move_type", "=", "out_invoice"),
            ("state", "=", "posted"),
            ("company_id", "=", provider.company_id.id),
            ("currency_id", "=", self.currency_id.id),
            ("amount_residual", ">", 0.0),
            ("commercial_partner_id", "in", token_partners.ids),
            "|", ("invoice_date_due", "=", False), ("invoice_date_due", "<=", self.date),
        ])
        if not moves:
            return moves
        # Retry rules: maximum attempts and delay between attempts.
        failed_lines = self.env["mollie.sepa.batch.line"].sudo().search([
            ("invoice_id", "in", moves.ids),
            ("state", "in", ["failed", "done", "charged_back"]),
            ("transaction_id", "!=", False),
        ])
        max_attempts = provider.sepa_retry_max_attempts
        retry_after = fields.Datetime.now() - timedelta(days=provider.sepa_retry_delay_days)
        excluded = self.env["account.move"]
        for move in moves:
            attempts = failed_lines.filtered(lambda l: l.invoice_id == move)
            if max_attempts and len(attempts) >= max_attempts:
                excluded |= move
            elif attempts.filtered(lambda l: l.state == "failed" and l.write_date > retry_after):
                excluded |= move
        return moves - excluded

    def action_collect(self):
        """Schedule the batch; the collection cron sends the direct debits."""
        self.check_access("write")
        for batch in self:
            if batch.state != "draft":
                raise UserError(_("Only a draft batch can be collected."))
            if not batch.line_ids.filtered(lambda l: l.state == "draft"):
                raise UserError(_("This batch has no lines to collect."))
            if batch.provider_id.state not in ("enabled", "test"):
                raise UserError(_("The provider of batch %s is disabled.", batch.name))
            batch.sudo()._sepa_schedule_collection()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "info",
                "message": _(
                    "The direct debits are being sent to Mollie in the background. "
                    "Refresh the batch in a moment to follow the progress."
                ),
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }

    def _sepa_schedule_collection(self):
        cron = self.env.ref("ow_payment_mollie_sepa.cron_sepa_process_collections")
        for batch in self:
            batch.state = "collecting"
            start = datetime.combine(batch.date, time.min)
            cron._trigger(at=max(start, fields.Datetime.now()))

    @api.model
    def _sepa_cron_process_collections(self):
        # Batches of a disabled provider wait until it is enabled again.
        batches = self.sudo().search([
            ("state", "=", "collecting"),
            ("date", "<=", fields.Date.context_today(self)),
            ("provider_id.state", "in", ["enabled", "test"]),
        ], order="date, id")
        Provider = self.env["payment.provider"]
        for batch in batches:
            for line in batch.line_ids.filtered(lambda l: l.state == "draft"):
                try:
                    line._sepa_charge()
                except Exception as e:  # noqa: B902
                    _logger.exception("SEPA collection of line %s failed: %s", line.id, e)
                    if not line.transaction_id.sepa_request_sent:
                        line.message = str(e)[:250]
                if Provider._sepa_commit() < const.CRON_TIME_MARGIN:
                    self.env.ref("ow_payment_mollie_sepa.cron_sepa_process_collections")._trigger()
                    return True
            if not batch.line_ids.filtered(lambda l: l.state == "draft"):
                batch.state = "collected"
                batch._check_auto_state()
            Provider._sepa_commit()
        return True

    def action_refresh(self):
        errors = []
        for batch in self:
            transactions = batch.line_ids.filtered(
                lambda l: l.state == "pending" and l.transaction_id
            ).transaction_id
            for tx in transactions.sudo():
                try:
                    with self.env.cr.savepoint():
                        tx._sepa_refresh()
                except UserError as e:
                    errors.append(f"{tx.reference}: {e}")
            batch._check_auto_state()
        return self._sepa_notify(errors, _("Status refreshed from Mollie."))

    def action_cancel(self):
        """Cancel the batch; direct debits Mollie can no longer cancel keep running."""
        errors = []
        for batch in self:
            if batch.state not in ("draft", "collecting", "collected"):
                raise UserError(_("Only an open batch can be canceled."))
            for line in batch.line_ids.sudo():
                if line.state == "draft" and not line.transaction_id:
                    line.write({"state": "cancel"})
                elif line.state in const.ACTIVE_LINE_STATES and line.transaction_id:
                    try:
                        with self.env.cr.savepoint():
                            line.transaction_id._sepa_cancel()
                            if line.transaction_id.state == "cancel":
                                line.write({"state": "cancel"})
                    except UserError as e:
                        errors.append(f"{line.invoice_id.name}: {e}")
            if any(l.state in const.ACTIVE_LINE_STATES for l in batch.line_ids):
                batch.state = "collected" if batch.state == "collecting" else batch.state
                batch._check_auto_state()
            else:
                batch.state = "cancel"
        return self._sepa_notify(errors, _("The batch was canceled."))

    def _sepa_notify(self, errors, success_message):
        if errors:
            params = {
                "type": "warning",
                "sticky": True,
                "message": "\n".join(errors),
            }
        else:
            params = {"type": "success", "message": success_message}
        params["next"] = {"type": "ir.actions.client", "tag": "soft_reload"}
        return {"type": "ir.actions.client", "tag": "display_notification", "params": params}

    def _check_auto_state(self):
        for batch in self:
            if batch.state != "collected":
                continue
            if any(l.state in const.ACTIVE_LINE_STATES for l in batch.line_ids):
                continue
            batch.write({"state": "done"})

    def action_view_transactions(self):
        self.ensure_one()
        return {
            "name": _("Direct Debit Transactions"),
            "type": "ir.actions.act_window",
            "res_model": "payment.transaction",
            "view_mode": "list,form",
            "domain": [("sepa_batch_line_id.batch_id", "=", self.id)],
        }
