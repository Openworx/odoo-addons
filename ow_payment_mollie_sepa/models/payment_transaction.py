# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
import logging

from markupsafe import Markup

from odoo import _, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import format_amount

from .. import const

_logger = logging.getLogger(__name__)


class PaymentTransaction(models.Model):
    _inherit = "payment.transaction"

    sepa_batch_line_id = fields.Many2one(
        string="SEPA Collection Line",
        comodel_name="mollie.sepa.batch.line",
        ondelete="set null",
        copy=False,
        index="btree_not_null",
    )
    sepa_request_sent = fields.Boolean(
        string="SEPA Request Sent",
        copy=False,
        readonly=True,
        help="Set (and committed) right before the direct debit is requested at "
        "Mollie. A transaction with this flag but without provider reference may "
        "exist at Mollie and must be recovered, never sent as a new payment.",
    )
    mollie_status = fields.Char(
        string="Mollie Status", copy=False, readonly=True,
        help="Raw payment status as last reported by Mollie.",
    )
    sepa_failure_reason = fields.Char(
        string="Bank Reason", copy=False, readonly=True,
        help="Reason code reported by the bank for a failed direct debit.",
    )
    mollie_test_url = fields.Char(
        string="Mollie Test Link", copy=False, readonly=True,
        help="Test mode only: Mollie page to set the final status of the payment.",
    )
    sepa_amount_charged_back = fields.Monetary(
        string="Charged Back Amount", currency_field="currency_id",
        copy=False, readonly=True,
    )

    def _mollie_prepare_payment_request_payload(self):
        """Request a SEPA mandate (sequenceType first) when the customer asked
        for it; the official module only does so for subscriptions.
        """
        payload = super()._mollie_prepare_payment_request_payload()
        if (
            self.provider_id.sepa_enabled
            and self.tokenize
            and not self.token_id
            and self.payment_method_code in const.SEPA_FIRST_PAYMENT_METHOD_CODES
            and not self.partner_id.is_public
        ):
            customer_id = self.provider_id._sepa_get_or_create_customer(
                self.partner_id
            )
            if customer_id:
                payload["customerId"] = customer_id
                payload["sequenceType"] = "first"
                # storeCredentials is card-only; captureMode makes no sense here.
                payload.pop("storeCredentials", None)
                payload.pop("captureMode", None)
        return payload

    def _extract_token_values(self, payment_data):
        """Fetch the mandate of a SEPA first payment to create the token."""
        token_values = super()._extract_token_values(payment_data)
        if (
            self.provider_code == "mollie"
            and (not token_values or not token_values.get("sepa_status"))
            and self.tokenize
            and payment_data.get("customerId")
            and payment_data.get("sequenceType") == "first"
            and payment_data.get("status") == "paid"
            and self.payment_method_code in const.SEPA_FIRST_PAYMENT_METHOD_CODES
        ):
            try:
                mandate = self._sepa_find_mandate_from_first_payment(payment_data)
            except ValidationError:
                # Never break the payment processing; `_sepa_backfill_mandate_token` retries.
                _logger.warning("SEPA mandate of %s could not be fetched yet", self.reference)
                return {}
            if mandate:
                iban = mandate.get("details", {}).get("consumerAccount") or ""
                token_values = {
                    **(token_values or {}),
                    # Mandates belong to the company, not to a contact person.
                    "partner_id": self.partner_id.commercial_partner_id.id,
                    "provider_ref": mandate["id"],
                    "mollie_customer_id": payment_data["customerId"],
                    "mollie_is_oneoff_token": False,
                    "payment_details": f"****{iban[-4:]}" if iban else mandate["id"],
                    "sepa_status": mandate.get("status"),
                    "sepa_mandate_reference": mandate.get("mandateReference"),
                    "sepa_consumer_name": mandate.get("details", {}).get("consumerName")
                    or self.partner_id.commercial_partner_id.name,
                    "sepa_consumer_account": iban,
                }
                if mandate.get("signatureDate"):
                    token_values["sepa_signature_date"] = mandate["signatureDate"]
        return token_values

    def _sepa_find_mandate_from_first_payment(self, payment_data):
        # Fetch by id: the mandate list may not be populated yet.
        mandate_id = payment_data.get("mandateId")
        if mandate_id:
            mandate = self.provider_id._api_sepa_get_mandate(
                payment_data["customerId"], mandate_id
            )
            if mandate and mandate.get("status") in ("valid", "pending"):
                return mandate
        mandates = self.provider_id._api_sepa_list_mandates(payment_data["customerId"])
        valid_mandates = [
            m for m in mandates
            if m.get("method") == const.SEPA_MOLLIE_METHOD_ID
            and m.get("status") in ("valid", "pending")
        ]
        return max(valid_mandates, key=lambda m: m.get("createdAt") or "", default=None)

    def _sepa_backfill_mandate_token(self):
        """Tokenize first payments whose mandate Mollie created after processing."""
        for tx in self:
            if (
                tx.provider_code != "mollie"
                or tx.token_id
                or not tx.tokenize
                or tx.state not in ("authorized", "done")
                or tx.payment_method_code not in const.SEPA_FIRST_PAYMENT_METHOD_CODES
                or not (tx.provider_reference or "").startswith("tr_")
            ):
                continue
            try:
                payment_data = tx.provider_id._api_mollie_get_payment_data(
                    tx.provider_reference, force_payment=True
                )
                if payment_data and payment_data.get("mandateId"):
                    tx._tokenize(payment_data)
            except ValidationError:
                _logger.exception(
                    "SEPA mandate backfill: could not fetch payment %s",
                    tx.provider_reference,
                )
        return True

    def _apply_updates(self, payment_data):
        """Keep the raw Mollie status and detect chargebacks: the official
        module ignores every update once a transaction is done.
        """
        if self.provider_code != "mollie":
            return super()._apply_updates(payment_data)
        status = payment_data.get("status")
        vals = {}
        if status and status != self.mollie_status:
            vals["mollie_status"] = status
        details = payment_data.get("details") or {}
        reason = " ".join(filter(None, [details.get("bankReasonCode"), details.get("bankReason")]))
        if reason and reason != self.sepa_failure_reason:
            vals["sepa_failure_reason"] = reason
        creditor_id = details.get("creditorIdentifier")
        if creditor_id and not self.provider_id.sepa_creditor_identifier:
            self.provider_id.sudo().sepa_creditor_identifier = creditor_id
        if vals:
            self.write(vals)
        res = super()._apply_updates(payment_data)
        self._sepa_store_test_url(payment_data)
        charged_back = float((payment_data.get("amountChargedBack") or {}).get("value") or 0.0)
        if (
            self.state == "done"
            and self.currency_id.compare_amounts(charged_back, self.sepa_amount_charged_back) > 0
        ):
            self._sepa_handle_chargeback(charged_back)
        return res

    def _sepa_store_test_url(self, payment_data):
        """Test mode: keep Mollie's link to set the final status of a direct
        debit, and show it once on the invoice."""
        url = ((payment_data.get("_links") or {}).get("changePaymentState") or {}).get("href")
        if payment_data.get("mode") != "test" or not url or self.mollie_test_url == url:
            return
        self.mollie_test_url = url
        if self.sepa_batch_line_id:
            body = Markup("%s <a href='%s' target='_blank'>%s</a>") % (
                _("Mollie test mode: set the result of direct debit %s here:", self.reference),
                url, url,
            )
            self.invoice_ids.sudo().message_post(body=body)

    def _sepa_handle_chargeback(self, charged_back):
        """Flag a chargeback. The reversal is booked manually (activity on the
        invoice): the payment may have no journal entry yet, or may already be
        matched with the Mollie settlement.
        """
        self.ensure_one()
        previous = self.sepa_amount_charged_back
        self.sepa_amount_charged_back = charged_back
        delta = charged_back - previous
        amount = format_amount(self.env, delta, self.currency_id)
        message = _(
            "Chargeback of %(amount)s received from Mollie for payment %(ref)s. The "
            "money was returned to the customer: reopen the invoice (book the "
            "reversal) and contact the customer.",
            amount=amount,
            ref=self.provider_reference or self.reference,
        )
        if self.sepa_batch_line_id:
            self.sepa_batch_line_id.sudo().write({"state": "charged_back", "message": message})
        invoices = self.invoice_ids.sudo()
        for invoice in invoices:
            invoice.message_post(body=message)
            invoice.activity_schedule(
                "mail.mail_activity_data_todo",
                summary=_("Process SEPA chargeback"),
                note=message,
                user_id=(invoice.invoice_user_id or invoice.create_uid).id,
            )
        if not invoices:
            self.partner_id.sudo().message_post(body=message)
        _logger.warning("Chargeback on Mollie payment %s (%s)", self.provider_reference, delta)

    def _post_process(self):
        res = super()._post_process()
        for tx in self:
            if tx.provider_code != "mollie":
                continue
            if tx.sepa_batch_line_id:
                tx._sepa_update_batch_line()
            elif (
                tx.tokenize
                and not tx.token_id
                and tx.state in ("done", "authorized")
            ):
                # First-payment mandate created asynchronously by Mollie.
                tx._sepa_backfill_mandate_token()
            if tx.operation == "validation" and tx.state == "done" and tx.token_id.is_sepa_mandate:
                tx._sepa_confirm_mandate()
        return res

    def _sepa_confirm_mandate(self):
        """E-mail the customer a confirmation of a mandate granted online, and
        archive the customer's older mandates: one active mandate per customer.
        """
        for tx in self:
            token = tx.token_id.sudo()
            partner = token.partner_id.commercial_partner_id
            older = self.env["payment.token"].sudo().search([
                ("partner_id", "child_of", partner.id),
                ("provider_id", "=", token.provider_id.id),
                ("provider_ref", "=like", "mdt_%"),
                ("id", "!=", token.id),
            ])
            if older:
                older.write({"sepa_superseded": True, "active": False})
                partner.message_post(body=_(
                    "SEPA mandates %(old)s archived: replaced by mandate %(new)s.",
                    old=", ".join(older.mapped("payment_details")),
                    new=token.payment_details,
                ))
            partner.message_post(
                body=_(
                    "You granted %(company)s a SEPA direct debit mandate for account "
                    "%(account)s (%(holder)s). Future invoices will be collected from this "
                    "account. Did you not authorize this? Please contact us right away.",
                    company=tx.company_id.name,
                    account=token.payment_details,
                    holder=token.sepa_consumer_name or partner.name,
                ),
                partner_ids=partner.ids,
                author_id=tx.company_id.partner_id.id,
                subtype_xmlid="mail.mt_comment",
            )

    def _sepa_update_batch_line(self):
        for tx in self:
            line = tx.sepa_batch_line_id.sudo()
            if not line or line.state == "charged_back":
                continue
            new_state = const.LINE_STATE_MAPPING.get(tx.state, line.state)
            if tx.state == "cancel" and tx.mollie_status == "failed":
                # A bank refusal (Mollie "failed") arrives as a canceled transaction.
                new_state = "failed"
            if line.state != new_state:
                message = tx.state_message or ""
                if tx.sepa_failure_reason:
                    message = f"{message} ({tx.sepa_failure_reason})".strip()
                line.write({"state": new_state, "message": message})
                if new_state == "failed":
                    tx._sepa_log_failure_note()
            if line.batch_id:
                line.batch_id._check_auto_state()

    def _sepa_charge_with_token(self):
        """Same as `_charge_with_token`, with an idempotency key."""
        for tx in self:
            tx._ensure_provider_is_not_disabled()
            tx._log_sent_message()
            try:
                tx.with_context(
                    mollie_idempotency_key=tx.reference
                )._send_payment_request()
            except ValidationError as e:
                tx._set_error(str(e))

    def _sepa_recover(self):
        """Link a payment whose response was lost, or resend the request with
        the same idempotency key.
        """
        for tx in self:
            if tx.state != "draft" or tx.provider_reference or not tx.sepa_request_sent:
                continue
            customer_id = tx.token_id.mollie_customer_id
            found = None
            if customer_id:
                for payment in tx.provider_id._api_sepa_list_customer_payments(customer_id):
                    if (payment.get("metadata") or {}).get("reference") == tx.reference:
                        found = payment
                        break
            if found:
                _logger.info("SEPA: recovered Mollie payment %s for %s", found["id"], tx.reference)
                tx.provider_reference = found["id"]
                tx._process("mollie", found)
            else:
                tx._sepa_charge_with_token()

    def _sepa_log_failure_note(self):
        for tx in self:
            if not tx.invoice_ids:
                continue
            tx.invoice_ids.sudo().message_post(body=_(
                "SEPA direct debit %(ref)s failed at Mollie (%(msg)s). The "
                "invoice remains open and can be collected again once the "
                "customer has sorted out their bank.",
                ref=tx.provider_reference or tx.reference,
                msg=tx.sepa_failure_reason or tx.state_message or "",
            ))

    def _sepa_cancel(self):
        """Cancel the payment at Mollie; raise UserError when no longer possible."""
        for tx in self:
            if tx.state not in ("draft", "pending"):
                continue
            if not tx.provider_reference:
                if tx.sepa_request_sent:
                    raise UserError(_(
                        "Direct debit %s may have been sent to Mollie; refresh "
                        "the batch status before canceling it.", tx.reference,
                    ))
                tx._set_canceled()
                continue
            payment_data = tx.provider_id._api_mollie_get_payment_data(
                tx.provider_reference, force_payment=True
            )
            if payment_data.get("isCancelable"):
                payment_data = tx.provider_id._api_sepa_cancel_payment(tx.provider_reference)
            if isinstance(payment_data, dict) and payment_data.get("id"):
                tx._process("mollie", payment_data)
            if tx.state not in ("cancel", "error"):
                raise UserError(_(
                    "Direct debit %(ref)s can no longer be canceled at Mollie "
                    "(status: %(status)s).",
                    ref=tx.reference, status=tx.mollie_status or tx.state,
                ))
            if not tx.is_post_processed:
                tx._post_process()
        return self

    def _sepa_refresh(self):
        for tx in self:
            if not (tx.provider_reference or "").startswith(("tr_", "ord_")):
                continue
            payment_data = tx.provider_id._api_mollie_get_payment_data(
                tx.provider_reference, force_payment=True
            )
            if payment_data:
                tx._process("mollie", payment_data)
            # Post-process once per state change: it logs messages and books payments.
            if tx.state in ("done", "cancel", "error") and not tx.is_post_processed:
                tx._post_process()
        return self
