# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
import re
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import ValidationError

from odoo.addons.account_payment.tests.common import AccountPaymentCommon
from odoo.addons.payment.models.payment_provider import PaymentProvider as BaseProvider

SEPA_PROVIDER = "odoo.addons.ow_payment_mollie_sepa.models.payment_provider.PaymentProvider"


class FakeMollie:
    """ In-memory stand-in for the Mollie v2 API used by the module. """

    def __init__(self):
        self.calls = []
        self.payments = {}
        self.mandates = {}
        self.customers = 0
        self.next_status = "pending"
        self.fail_post = None

    def count(self, method, endpoint):
        return len([c for c in self.calls if c[0] == method and c[1] == endpoint])

    def __call__(self, provider, method, endpoint, *, params=None, data=None, json=None,
                 reference=None, **kwargs):
        self.calls.append((method, endpoint, json))
        if method == "POST" and endpoint == "/payments":
            if self.fail_post:
                raise ValidationError(self.fail_post)
            payment_id = f"tr_fake{len(self.payments) + 1}"
            payment = {
                "resource": "payment",
                "id": payment_id,
                "status": self.next_status,
                "amount": json["amount"],
                "description": json.get("description"),
                "metadata": json.get("metadata") or {},
                "customerId": json.get("customerId"),
                "mandateId": json.get("mandateId"),
                "sequenceType": json.get("sequenceType"),
                "method": "directdebit",
                "isCancelable": True,
                "details": {},
                "mode": "test",
                "_links": {"changePaymentState": {
                    "href": f"https://www.mollie.com/checkout/test-mode?token={payment_id}",
                }},
            }
            self.payments[payment_id] = payment
            return dict(payment)
        match = re.match(r"^/payments/(tr_\w+)$", endpoint)
        if match and method == "GET":
            return dict(self.payments[match[1]])
        if match and method == "DELETE":
            payment = self.payments[match[1]]
            payment["status"] = "canceled"
            return dict(payment)
        if method == "GET" and re.match(r"^/payments/tr_\w+/captures$", endpoint):
            return {"count": 0}
        if method == "POST" and endpoint == "/customers":
            self.customers += 1
            return {"resource": "customer", "id": f"cst_new{self.customers}"}
        match = re.match(r"^/customers/(\w+)/mandates/(\w+)$", endpoint)
        if match and method == "GET":
            if match[2] not in self.mandates:
                raise ValidationError("The payment provider rejected the request.\nNot found")
            return dict(self.mandates[match[2]])
        match = re.match(r"^/customers/(\w+)/mandates$", endpoint)
        if match and method == "GET":
            mandates = [dict(m) for m in self.mandates.values() if m["customerId"] == match[1]]
            return {"count": len(mandates), "_embedded": {"mandates": mandates}}
        match = re.match(r"^/customers/(\w+)/payments$", endpoint)
        if match and method == "GET":
            payments = [dict(p) for p in self.payments.values() if p["customerId"] == match[1]]
            return {"count": len(payments), "_embedded": {"payments": payments}}
        raise AssertionError(f"Unexpected Mollie call {method} {endpoint}")


class SepaCommon(AccountPaymentCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.mollie = cls._prepare_provider("mollie", update_values={
            "mollie_api_key": "live_dummy",
            "mollie_api_key_test": "test_dummy",
            "sepa_enabled": True,
            "allow_tokenization": True,
            "available_currency_ids": [Command.set(cls.currency_euro.ids)],
        })
        cls.provider = cls.mollie
        cls.currency = cls.currency_euro
        cls.mollie.journal_id.inbound_payment_method_line_ids.filtered(
            lambda l: l.payment_provider_id == cls.mollie
        ).payment_account_id = cls.inbound_payment_method_line.payment_account_id

        cls.company_partner = cls.env["res.partner"].create({
            "name": "Klant BV", "is_company": True, "email": "klant@example.com",
        })
        cls.contact = cls.env["res.partner"].create({
            "name": "Jan de Boekhouder", "parent_id": cls.company_partner.id, "type": "invoice",
        })

    def setUp(self):
        super().setUp()
        self.api = FakeMollie()
        self.startPatcher(patch.object(
            BaseProvider, "_send_api_request", autospec=True, side_effect=self.api,
        ))
        self.startPatcher(patch(
            f"{SEPA_PROVIDER}._mollie_validate_customer_id", return_value=True,
        ))

    def _mandate(self, mandate_id="mdt_1", customer_id="cst_1", status="valid"):
        mandate = {
            "resource": "mandate",
            "id": mandate_id,
            "customerId": customer_id,
            "method": "directdebit",
            "status": status,
            "mandateReference": f"REF-{mandate_id}",
            "signatureDate": "2026-01-01",
            "details": {"consumerName": "Klant BV", "consumerAccount": "NL55INGB0000000000"},
        }
        self.api.mandates[mandate_id] = mandate
        return mandate

    def _create_mandate_token(self, **kwargs):
        return self.mollie._sepa_get_or_create_token(self.company_partner, self._mandate(**kwargs))

    def _create_invoice(self, amount=100.0, partner=None, due_date=None):
        invoice = self.init_invoice(
            "out_invoice",
            partner=partner or self.contact,
            invoice_date=fields.Date.today(),
            amounts=[amount],
            taxes=[],
            currency=self.currency_euro,
        )
        if due_date:
            invoice.invoice_date_due = due_date
        invoice.action_post()
        return invoice

    def _new_batch(self, **vals):
        return self.env["mollie.sepa.batch"].create({"provider_id": self.mollie.id, **vals})

    def _collect(self, batch):
        batch.action_collect()
        self.env["mollie.sepa.batch"]._sepa_cron_process_collections()

    def _set_mollie_status(self, status, **extra):
        for payment in self.api.payments.values():
            payment["status"] = status
            payment.update(extra)
