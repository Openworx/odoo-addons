# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
import logging
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import requests

from odoo import _, api, Command, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.modules import module as odoo_module

from .. import const

_logger = logging.getLogger(__name__)


class PaymentProvider(models.Model):
    _inherit = "payment.provider"

    sepa_enabled = fields.Boolean(
        string="SEPA Direct Debit",
        help="Enable SEPA Direct Debit mandates and automatic collection "
        "(incasso) of open invoices through this provider.",
    )
    sepa_mandate_ref_prefix = fields.Char(
        string="Mandate Reference Prefix",
        default="SEPA-DD",
        help="Prefix used to build the unique mandate reference sent to Mollie. "
        "Some banks decline direct debits when the reference is not unique.",
    )
    sepa_max_amount = fields.Monetary(
        string="Max Amount per Direct Debit",
        currency_field="main_currency_id",
        default=const.MOLLIE_SEPA_DEFAULT_MAX_AMOUNT,
        help="Maximum amount collected per invoice with a single direct debit. "
        "Mollie's default limit is 1000.00 EUR per transaction.",
    )
    sepa_creditor_identifier = fields.Char(
        string="Creditor Identifier",
        help="SEPA creditor identifier used by Mollie for the direct debits, printed "
        "on mandate forms. Filled automatically from the first Mollie direct debit.",
    )
    sepa_auto_collect = fields.Boolean(
        string="Automatic Daily Collection",
        help="When enabled, a cron creates and runs a new collection batch for "
        "every open invoice covered by a valid mandate, once per day.",
    )
    sepa_retry_max_attempts = fields.Integer(
        string="Max Collection Attempts",
        default=2,
        help="Maximum number of direct debits sent for the same invoice by the "
        "line generation (failed attempts included). Set to 0 for no limit.",
    )
    sepa_retry_delay_days = fields.Integer(
        string="Days Between Attempts",
        default=7,
        help="Minimum number of days between a failed direct debit and the next "
        "attempt for the same invoice.",
    )

    def _sepa_mode(self):
        self.ensure_one()
        return "test" if self.state == "test" else "live"

    @api.model
    def _sepa_commit(self, processed=1):
        """Commit progress, except in tests: once a request reached Mollie the
        Odoo side must never be rolled back. Return the remaining cron time.
        """
        if odoo_module.current_test:
            return float("inf")
        return self.env["ir.cron"]._commit_progress(processed)

    @api.model
    def _sepa_get_enabled_providers(self, company=None):
        domain = [
            ("code", "=", "mollie"),
            ("state", "in", ["enabled", "test"]),
            ("sepa_enabled", "=", True),
        ]
        if company:
            domain.append(("company_id", "=", company.id))
        return self.search(domain)

    def _sepa_get_payment_method(self):
        self.ensure_one()
        method = self.with_context(active_test=False).payment_method_ids.filtered(
            lambda m: m.code == const.SEPA_ODOO_METHOD_CODE
        )[:1]
        if method:
            return method
        PaymentMethod = self.env["payment.method"].sudo().with_context(active_test=False)
        method = PaymentMethod.search([
            ("code", "=", const.SEPA_ODOO_METHOD_CODE),
            ("is_primary", "=", True),
        ], limit=1)
        if not method:
            method = PaymentMethod.create({
                "name": "SEPA Direct Debit",
                "code": const.SEPA_ODOO_METHOD_CODE,
            })
        self.sudo().write({"payment_method_ids": [Command.link(method.id)]})
        return method

    def _sepa_next_mandate_reference(self):
        prefix = (self[:1].sepa_mandate_ref_prefix or "SEPA-DD").strip()
        number = self.env["ir.sequence"].sudo().next_by_code("mollie.sepa.mandate.reference")
        return f"{prefix}/{number}"

    def _sepa_mandate_methods(self):
        """Active bank methods of this provider whose first payment creates a mandate."""
        self.ensure_one()
        return self.payment_method_ids.filtered(
            lambda m: m.active and m.code in const.SEPA_FIRST_PAYMENT_METHOD_CODES
        )

    def _sepa_create_mandate_transaction(self, partner, payment_method, landing_route):
        """Create the validation transaction of a standalone mandate request: a
        symbolic first payment that creates the mandate, without booking.
        """
        self.ensure_one()
        partner = partner.commercial_partner_id.sudo()
        if not self.sepa_enabled or self.state not in ("enabled", "test"):
            raise UserError(_("SEPA Direct Debit is not available."))
        if payment_method not in self._sepa_mandate_methods():
            raise UserError(_("This payment method cannot be used to grant a mandate."))
        if partner.is_public:
            raise UserError(_("A mandate needs a known customer."))
        Transaction = self.env["payment.transaction"].sudo()
        amount = const.SEPA_MANDATE_AMOUNTS.get(
            payment_method.code, const.SEPA_MANDATE_DEFAULT_AMOUNT
        )
        return Transaction.create({
            "provider_id": self.id,
            "payment_method_id": payment_method.id,
            "reference": Transaction._compute_reference("mollie", prefix=f"MANDATE-{partner.id}"),
            "amount": amount,
            "currency_id": self.env.ref("base.EUR").id,
            "partner_id": partner.id,
            "operation": "validation",
            "tokenize": True,
            "landing_route": landing_route,
        })

    def _mollie_validate_customer_id(self, customer_id):
        """Return False for an unknown customer (404/410) instead of raising."""
        self.ensure_one()
        if self.code != "mollie":
            return super()._mollie_validate_customer_id(customer_id)
        endpoint = f"/customers/{customer_id}"
        try:
            response = requests.get(
                self._build_request_url(endpoint),
                headers=self._build_request_headers("GET", endpoint, None),
                timeout=10,
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            raise ValidationError(
                _("Could not establish the connection to the payment provider.")
            ) from e
        if response.status_code in (404, 410):
            return False
        if not response.ok:
            raise ValidationError(_(
                "The payment provider rejected the request.\n%s", response.text
            ))
        return True

    def _mollie_get_customer_id(self, partner):
        """Reuse the partner's SEPA customer instead of creating one per payment."""
        self.ensure_one()
        if self.code == "mollie" and self.sepa_enabled and partner and not partner.is_public:
            return self._sepa_get_or_create_customer(partner)
        return super()._mollie_get_customer_id(partner)

    def _sepa_get_customer(self, partner):
        """Return the stored customer id for this provider and mode (no API call)."""
        self.ensure_one()
        record = self.env["mollie.sepa.customer"].sudo().search([
            ("partner_id", "=", partner.commercial_partner_id.id),
            ("provider_id", "=", self.id),
            ("mode", "=", self._sepa_mode()),
        ], limit=1)
        return record.customer_id or False

    def _sepa_store_customer(self, partner, customer_id):
        self.ensure_one()
        Customer = self.env["mollie.sepa.customer"].sudo()
        partner = partner.commercial_partner_id
        record = Customer.search([
            ("partner_id", "=", partner.id),
            ("provider_id", "=", self.id),
            ("mode", "=", self._sepa_mode()),
        ], limit=1)
        if record:
            if record.customer_id != customer_id:
                record.customer_id = customer_id
        else:
            Customer.create({
                "partner_id": partner.id,
                "provider_id": self.id,
                "mode": self._sepa_mode(),
                "customer_id": customer_id,
            })

    def _sepa_get_or_create_customer(self, partner):
        self.ensure_one()
        partner = partner.commercial_partner_id.sudo()

        customer_id = self._sepa_get_customer(partner)
        if customer_id and self._mollie_validate_customer_id(customer_id):
            return customer_id

        # Reuse a customer id of the current mode from an existing token.
        tokens = self.env["payment.token"].sudo().with_context(active_test=False).search([
            ("partner_id", "child_of", partner.id),
            ("provider_id", "=", self.id),
            ("mollie_customer_id", "!=", False),
            ("sepa_mode", "in", [self._sepa_mode(), False]),
        ], order="create_date desc", limit=5)
        for candidate in dict.fromkeys(tokens.mapped("mollie_customer_id")):
            if candidate != customer_id and self._mollie_validate_customer_id(candidate):
                self._sepa_store_customer(partner, candidate)
                return candidate

        customer_data = {
            "name": partner.display_name[:250],
            "metadata": {"odoo_partner_id": partner.id},
        }
        if partner.email:
            customer_data["email"] = partner.email
        created = self._send_api_request("POST", "/customers", json=customer_data)
        if created and created.get("id"):
            self._sepa_store_customer(partner, created["id"])
            return created["id"]
        return False

    def _build_request_headers(self, *args, **kwargs):
        """Add an Idempotency-Key to POST requests when set in the context.

        https://docs.mollie.com/reference/api-idempotency
        """
        headers = super()._build_request_headers(*args, **kwargs)
        idempotency_key = self.env.context.get("mollie_idempotency_key")
        method = args[0] if args else kwargs.get("method")
        if self.code == "mollie" and idempotency_key and method == "POST":
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _sepa_list_resources(self, endpoint, embedded_key):
        items = []
        params = {"limit": const.MOLLIE_LIST_LIMIT}
        for _page in range(100):  # safety limit
            data = self._send_api_request("GET", endpoint, params=params) or {}
            items += data.get("_embedded", {}).get(embedded_key, [])
            next_href = ((data.get("_links") or {}).get("next") or {}).get("href")
            next_from = parse_qs(urlparse(next_href).query).get("from") if next_href else None
            if not next_from:
                break
            params = {"limit": const.MOLLIE_LIST_LIMIT, "from": next_from[0]}
        return items

    def _api_sepa_create_mandate(
        self, customer_id, consumer_name, iban, signature_date, mandate_reference, bic=None
    ):
        payload = {
            "method": const.SEPA_MOLLIE_METHOD_ID,
            "consumerName": consumer_name,
            "consumerAccount": iban,
            "signatureDate": fields.Date.to_string(signature_date),
            "mandateReference": mandate_reference,
        }
        if bic:
            payload["consumerBic"] = bic
        return self._send_api_request(
            "POST", f"/customers/{customer_id}/mandates", json=payload
        )

    def _api_sepa_get_mandate(self, customer_id, mandate_id):
        return self._send_api_request(
            "GET", f"/customers/{customer_id}/mandates/{mandate_id}"
        )

    def _api_sepa_list_mandates(self, customer_id):
        """All pages; the API does not filter on method."""
        return self._sepa_list_resources(f"/customers/{customer_id}/mandates", "mandates")

    def _api_sepa_list_customer_payments(self, customer_id):
        """Most recent page only."""
        data = self._send_api_request(
            "GET", f"/customers/{customer_id}/payments",
            params={"limit": const.MOLLIE_LIST_LIMIT},
        ) or {}
        return data.get("_embedded", {}).get("payments", [])

    def _api_sepa_cancel_payment(self, provider_reference):
        return self._send_api_request("DELETE", f"/payments/{provider_reference}")

    def _sepa_cron_refresh_pending_transactions(self):
        """Fallback for webhooks (Mollie rejects localhost webhook URLs)."""
        cutoff = fields.Datetime.now() - timedelta(days=90)
        transactions = self.env["payment.transaction"].sudo().search([
            ("provider_code", "=", "mollie"),
            ("state", "=", "pending"),
            ("provider_reference", "=like", "tr_%"),
            ("create_date", ">=", cutoff),
        ])
        for tx in transactions:
            try:
                with self.env.cr.savepoint():
                    tx._sepa_refresh()
            except Exception as e:  # noqa: B902
                _logger.exception(
                    "SEPA refresh of transaction %s failed: %s", tx.reference, e
                )
            if self._sepa_commit() < const.CRON_TIME_MARGIN:
                break
        return True

    def _sepa_cron_sync_mandates(self):
        tokens = self.env["payment.token"].sudo().search([
            ("provider_code", "=", "mollie"),
            ("provider_ref", "=like", "mdt_%"),
            ("active", "=", True),
        ]).filtered(lambda t: t._sepa_is_current_mode())
        for token in tokens:
            try:
                with self.env.cr.savepoint():
                    token._sepa_sync()
            except Exception as e:  # noqa: B902
                _logger.exception(
                    "SEPA mandate sync failed for token %s: %s", token.id, e
                )
            self._sepa_commit()

        # First payments whose mandate Mollie created after processing.
        orphans = self.env["payment.transaction"].sudo().search([
            ("provider_code", "=", "mollie"),
            ("tokenize", "=", True),
            ("token_id", "=", False),
            ("state", "in", ("authorized", "done")),
            ("payment_method_code", "in", const.SEPA_FIRST_PAYMENT_METHOD_CODES),
            ("create_date", ">=", fields.Datetime.now() - timedelta(days=30)),
        ], limit=50)
        orphans._sepa_backfill_mandate_token()
        self._sepa_commit()

        # Chargebacks can follow up to 8 weeks later.
        collected = self.env["payment.transaction"].sudo().search([
            ("provider_code", "=", "mollie"),
            ("state", "=", "done"),
            ("sepa_batch_line_id", "!=", False),
            ("provider_reference", "=like", "tr_%"),
            ("last_state_change", ">=", fields.Datetime.now() - timedelta(days=60)),
        ])
        for tx in collected:
            try:
                with self.env.cr.savepoint():
                    tx._sepa_refresh()
            except Exception as e:  # noqa: B902
                _logger.exception("SEPA chargeback check of %s failed: %s", tx.reference, e)
            if self._sepa_commit() < const.CRON_TIME_MARGIN:
                break
        return True

    def _sepa_cron_auto_collect(self):
        providers = self._sepa_get_enabled_providers()
        Batch = self.env["mollie.sepa.batch"].sudo()
        for provider in providers.filtered("sepa_auto_collect"):
            try:
                with self.env.cr.savepoint():
                    batch = Batch.create({"provider_id": provider.id})
                    if batch._sepa_generate_lines():
                        batch._sepa_schedule_collection()
                    else:
                        batch.unlink()
            except Exception as e:  # noqa: B902
                _logger.exception("SEPA auto collection failed for %s: %s", provider.name, e)
            self._sepa_commit()
        return True
