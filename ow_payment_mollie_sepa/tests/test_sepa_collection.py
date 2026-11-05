# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
from datetime import timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.ow_payment_mollie_sepa.models.payment_provider import (
    PaymentProvider as SepaProvider,
)

from .common import SepaCommon

ORIGINAL_VALIDATE = SepaProvider._mollie_validate_customer_id


@tagged("post_install", "-at_install")
class TestSepaCollection(SepaCommon):

    def test_collect_success_books_invoice(self):
        """ A contact-person invoice is collected with the company's mandate
        and booked once Mollie reports it paid. """
        token = self._create_mandate_token()
        invoice = self._create_invoice(100.0)
        batch = self._new_batch()
        batch.action_generate_lines()
        self.assertEqual(batch.line_ids.invoice_id, invoice)

        self._collect(batch)
        line = batch.line_ids
        self.assertEqual(line.state, "pending")
        self.assertEqual(batch.state, "collected")
        post = [c for c in self.api.calls if c[:2] == ("POST", "/payments")]
        self.assertEqual(len(post), 1)
        payload = post[0][2]
        self.assertEqual(payload["mandateId"], token.provider_ref)
        self.assertEqual(payload["customerId"], "cst_1")
        self.assertEqual(payload["sequenceType"], "recurring")
        self.assertNotIn("method", payload)
        self.assertEqual(payload["amount"], {"currency": "EUR", "value": "100.00"})

        self._set_mollie_status("paid")
        batch.action_refresh()
        self.assertEqual(line.state, "done")
        self.assertEqual(batch.state, "done")
        self.assertIn(invoice.payment_state, ("paid", "in_payment"))

    def test_idempotency_key_header(self):
        headers = self.mollie.with_context(mollie_idempotency_key="REF-1")._build_request_headers(
            "POST", "/payments", {}
        )
        self.assertEqual(headers.get("Idempotency-Key"), "REF-1")
        headers = self.mollie.with_context(mollie_idempotency_key="REF-1")._build_request_headers(
            "GET", "/payments/tr_1", None
        )
        self.assertNotIn("Idempotency-Key", headers)

    def test_failed_debit_respects_retry_policy(self):
        self._create_mandate_token()
        invoice = self._create_invoice(100.0)
        self.mollie.write({"sepa_retry_max_attempts": 2, "sepa_retry_delay_days": 7})
        batch = self._new_batch()
        batch.action_generate_lines()
        self._collect(batch)
        self._set_mollie_status("failed", details={"bankReasonCode": "AM04", "bankReason": "Insufficient funds"})
        batch.action_refresh()
        line = batch.line_ids
        self.assertEqual(line.state, "failed")
        self.assertIn("AM04", line.message)
        self.assertEqual(invoice.payment_state, "not_paid")

        # Not retried within the waiting period.
        self.assertFalse(self._new_batch()._sepa_generate_lines())
        # Retried once the waiting period is over...
        self.mollie.sepa_retry_delay_days = 0
        line.write_date = fields.Datetime.now() - timedelta(days=1)
        retry_batch = self._new_batch()
        self.assertEqual(retry_batch._sepa_generate_lines(), 1)
        self._collect(retry_batch)
        self._set_mollie_status("failed")
        retry_batch.action_refresh()
        # ...but never more than the maximum number of attempts.
        self.assertFalse(self._new_batch()._sepa_generate_lines())

    def test_invoice_button_blocks_double_collection(self):
        self._create_mandate_token()
        invoice = self._create_invoice(50.0)
        action = invoice.action_sepa_collect()
        self.env["mollie.sepa.batch"].browse(action["res_id"]).action_collect()
        with self.assertRaises(UserError):
            invoice.action_sepa_collect()

    def test_abandoned_draft_batch_does_not_block(self):
        self._create_mandate_token()
        invoice = self._create_invoice(50.0)
        invoice.action_sepa_collect()  # draft batch left behind
        batch = self._new_batch()
        batch.action_generate_lines()
        self.assertEqual(batch.line_ids.invoice_id, invoice)

    def test_refresh_done_transaction_posts_once(self):
        self._create_mandate_token()
        invoice = self._create_invoice(100.0)
        batch = self._new_batch()
        batch.action_generate_lines()
        self._collect(batch)
        self._set_mollie_status("paid")
        batch.action_refresh()
        messages = len(invoice.message_ids)
        tx = batch.line_ids.transaction_id
        tx._sepa_refresh()
        tx._sepa_refresh()
        self.assertEqual(len(invoice.message_ids), messages)

    def test_mandate_fetch_failure_keeps_payment(self):
        """A first payment is processed even when its mandate cannot be fetched."""
        ideal = self.env.ref("payment.payment_method_ideal")
        tx = self._create_transaction(
            "redirect", payment_method_id=ideal.id, tokenize=True,
            partner_id=self.contact.id, reference="FIRST-2", amount=10.0,
            provider_reference="tr_first2",
        )
        tx._process("mollie", {
            "resource": "payment", "id": "tr_first2", "status": "paid",
            "amount": {"currency": "EUR", "value": "10.00"}, "method": "ideal",
            "customerId": "cst_1", "mandateId": "mdt_unknown", "sequenceType": "first",
        })
        self.assertEqual(tx.state, "done")
        self.assertFalse(tx.token_id)
        self.assertTrue(tx.tokenize)  # left for the backfill

    def test_two_batches_same_invoice_debited_once(self):
        """ Two draft batches containing the same invoice: only one debit. """
        self._create_mandate_token()
        invoice = self._create_invoice(50.0)
        batch_1 = self._new_batch()
        batch_1.action_generate_lines()
        batch_2 = self._new_batch(line_ids=[(0, 0, {
            "partner_id": invoice.partner_id.id, "invoice_id": invoice.id, "amount": 50.0,
        })])
        batch_1.action_collect()
        batch_2.action_collect()
        self.env["mollie.sepa.batch"]._sepa_cron_process_collections()
        self.assertEqual(self.api.count("POST", "/payments"), 1)
        self.assertEqual(batch_2.line_ids.state, "cancel")

    def test_invoice_paid_meanwhile_is_not_debited(self):
        self._create_mandate_token()
        invoice = self._create_invoice(80.0)
        batch = self._new_batch()
        batch.action_generate_lines()
        self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=invoice.ids,
        ).create({})._create_payments()
        self._collect(batch)
        self.assertEqual(self.api.count("POST", "/payments"), 0)
        self.assertEqual(batch.line_ids.state, "cancel")

    def test_partial_payment_lowers_amount(self):
        self._create_mandate_token()
        invoice = self._create_invoice(80.0)
        batch = self._new_batch()
        batch.action_generate_lines()
        self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=invoice.ids,
        ).create({"amount": 30.0})._create_payments()
        self._collect(batch)
        post = [c for c in self.api.calls if c[:2] == ("POST", "/payments")]
        self.assertEqual(post[0][2]["amount"]["value"], "50.00")

    def test_recover_sent_request_without_response(self):
        """ A request that reached Mollie but whose response was lost is
        linked again, never sent as a second payment. """
        token = self._create_mandate_token()
        self._create_invoice(60.0)
        batch = self._new_batch()
        batch.action_generate_lines()
        line = batch.line_ids
        tx = line._sepa_create_transaction(token)
        line.transaction_id = tx
        # Mollie created the payment, Odoo never got the answer.
        self.api.payments["tr_lost"] = {
            "resource": "payment", "id": "tr_lost", "status": "pending",
            "amount": {"currency": "EUR", "value": "60.00"},
            "metadata": {"reference": tx.reference}, "customerId": "cst_1",
            "mandateId": token.provider_ref, "sequenceType": "recurring",
            "method": "directdebit", "details": {},
        }
        self._collect(batch)
        self.assertEqual(self.api.count("POST", "/payments"), 0)
        self.assertEqual(tx.provider_reference, "tr_lost")
        self.assertEqual(line.state, "pending")

    def test_resend_unsent_request(self):
        token = self._create_mandate_token()
        self._create_invoice(60.0)
        batch = self._new_batch()
        batch.action_generate_lines()
        line = batch.line_ids
        line.transaction_id = line._sepa_create_transaction(token)
        self._collect(batch)
        self.assertEqual(self.api.count("POST", "/payments"), 1)
        self.assertEqual(line.state, "pending")

    def test_rejected_request_marks_line_failed(self):
        self._create_mandate_token()
        self._create_invoice(60.0)
        batch = self._new_batch()
        batch.action_generate_lines()
        self.api.fail_post = "The payment provider rejected the request.\nMandate invalid"
        self._collect(batch)
        self.assertEqual(batch.line_ids.state, "failed")
        self.assertEqual(batch.state, "done")

    def test_chargeback_flags_invoice(self):
        self._create_mandate_token()
        invoice = self._create_invoice(100.0)
        batch = self._new_batch()
        batch.action_generate_lines()
        self._collect(batch)
        self._set_mollie_status("paid")
        batch.action_refresh()
        tx = batch.line_ids.transaction_id
        self.assertEqual(tx.state, "done")

        self._set_mollie_status("paid", amountChargedBack={"currency": "EUR", "value": "100.00"})
        tx._sepa_refresh()
        self.assertEqual(batch.line_ids.state, "charged_back")
        self.assertEqual(tx.sepa_amount_charged_back, 100.0)
        self.assertTrue(invoice.activity_ids)
        # Processing the same data again does not duplicate the activity.
        tx._sepa_refresh()
        self.assertEqual(len(invoice.activity_ids), 1)

    def test_cancel_not_cancelable_keeps_batch_open(self):
        self._create_mandate_token()
        self._create_invoice(100.0)
        batch = self._new_batch()
        batch.action_generate_lines()
        self._collect(batch)
        self._set_mollie_status("pending", isCancelable=False)
        action = batch.action_cancel()
        self.assertEqual(action["params"]["type"], "warning")
        self.assertEqual(batch.line_ids.state, "pending")
        self.assertEqual(batch.state, "collected")

    def test_cancel_cancelable(self):
        self._create_mandate_token()
        self._create_invoice(100.0)
        batch = self._new_batch()
        batch.action_generate_lines()
        self._collect(batch)
        batch.action_cancel()
        self.assertEqual(batch.line_ids.state, "cancel")
        self.assertEqual(batch.state, "cancel")
        self.assertEqual(self.api.count("DELETE", f"/payments/{batch.line_ids.transaction_id.provider_reference}"), 1)

    def test_valid_mandate_preferred_over_pending(self):
        valid = self._create_mandate_token(mandate_id="mdt_valid")
        self._create_mandate_token(mandate_id="mdt_pending", status="pending")
        token = self.env["payment.token"]._get_sepa_token_for_partner(self.contact, self.mollie)
        self.assertEqual(token, valid)

    def test_due_date_and_opt_out(self):
        self._create_mandate_token()
        future = self._create_invoice(10.0, due_date=fields.Date.today() + timedelta(days=10))
        due = self._create_invoice(20.0)
        batch = self._new_batch()
        batch.action_generate_lines()
        self.assertEqual(batch.line_ids.invoice_id, due)
        self.assertNotIn(future, batch.line_ids.invoice_id)

        self.company_partner.sepa_collect_enabled = False
        with self.assertRaises(UserError):
            self._new_batch().action_generate_lines()

    def test_customer_per_mode(self):
        """ A test-mode customer id is never used in live mode. """
        self.mollie._sepa_store_customer(self.company_partner, "cst_testmode")
        self.assertEqual(self.mollie._sepa_get_or_create_customer(self.contact), "cst_testmode")
        self.mollie.state = "enabled"
        customer_id = self.mollie._sepa_get_or_create_customer(self.contact)
        self.assertEqual(customer_id, "cst_new1")
        self.mollie.state = "test"
        self.assertEqual(self.mollie._sepa_get_customer(self.company_partner), "cst_testmode")

    def test_test_mode_token_not_chargeable_live(self):
        token = self._create_mandate_token()
        self.assertEqual(token.sepa_mode, "test")
        self.assertTrue(token._sepa_is_chargeable())
        self.mollie.state = "enabled"
        self.assertFalse(token._sepa_is_chargeable())

    def test_validate_customer_unknown_returns_false(self):
        """ An unknown customer (other mode, deleted) is not an error. """
        class Response:
            def __init__(self, status_code):
                self.status_code = status_code
                self.ok = status_code < 400
                self.text = "error"
        target = "odoo.addons.ow_payment_mollie_sepa.models.payment_provider.requests.get"
        with patch(target, return_value=Response(404)):
            self.assertFalse(ORIGINAL_VALIDATE(self.mollie, "cst_gone"))
        with patch(target, return_value=Response(200)):
            self.assertTrue(ORIGINAL_VALIDATE(self.mollie, "cst_ok"))
        with patch(target, return_value=Response(500)), self.assertRaises(UserError):
            ORIGINAL_VALIDATE(self.mollie, "cst_x")

    def test_accountant_can_collect(self):
        """ An accountant (not a system admin) can run a collection from the
        invoice; the requests themselves are sent by the cron. """
        self._create_mandate_token()
        invoice = self._create_invoice(40.0)
        user = self.env["res.users"].create({
            "name": "Accountant", "login": "accountant_sepa",
            "group_ids": [(6, 0, [self.env.ref("account.group_account_manager").id])],
        })
        action = invoice.with_user(user).action_sepa_collect()
        batch = self.env["mollie.sepa.batch"].with_user(user).browse(action["res_id"])
        batch.action_collect()
        self.assertEqual(batch.state, "collecting")
        self.assertEqual(self.api.count("POST", "/payments"), 0)
        self.env["mollie.sepa.batch"]._sepa_cron_process_collections()
        self.assertEqual(self.api.count("POST", "/payments"), 1)

    def test_first_payment_payload_single_customer(self):
        """ A first payment requesting a mandate creates one Mollie customer and
        asks for sequenceType "first". """
        ideal = self.env.ref("payment.payment_method_ideal")
        tx = self._create_transaction(
            "redirect", payment_method_id=ideal.id, tokenize=True,
            partner_id=self.contact.id, reference="FIRST-1", amount=10.0,
        )
        payload = tx._mollie_prepare_payment_request_payload()
        self.assertEqual(payload["sequenceType"], "first")
        self.assertEqual(payload["customerId"], "cst_new1")
        self.assertNotIn("storeCredentials", payload)
        self.assertEqual(self.api.count("POST", "/customers"), 1)
        self.assertEqual(self.mollie._sepa_get_customer(self.company_partner), "cst_new1")

    def test_disabled_provider_pauses_collection(self):
        self._create_mandate_token()
        self._create_invoice(70.0)
        batch = self._new_batch()
        batch.action_generate_lines()
        batch.action_collect()
        self.mollie.state = "disabled"
        self.env["mollie.sepa.batch"]._sepa_cron_process_collections()
        self.assertEqual(self.api.count("POST", "/payments"), 0)
        self.assertEqual(batch.state, "collecting")
        # Odoo archived the token; syncing from Mollie restores the valid mandate.
        self.mollie.state = "test"
        self.company_partner.action_sync_sepa_mandates()
        self.env["mollie.sepa.batch"]._sepa_cron_process_collections()
        self.assertEqual(self.api.count("POST", "/payments"), 1)
        self.assertEqual(batch.state, "collected")

    def test_iban_restricted_to_invoicing_users(self):
        token = self._create_mandate_token()
        self.assertFalse(self.internal_user.has_group("account.group_account_invoice"))
        with self.assertRaises(AccessError):
            token.with_user(self.internal_user).read(["sepa_consumer_account"])
        self.assertEqual(token.sepa_consumer_account, "NL55INGB0000000000")

    def test_test_mode_link_posted_once(self):
        self._create_mandate_token()
        invoice = self._create_invoice(30.0)
        batch = self._new_batch()
        batch.action_generate_lines()
        self._collect(batch)
        line = batch.line_ids
        self.assertIn("test-mode?token=tr_fake1", line.mollie_test_url)
        posted = invoice.message_ids.filtered(lambda m: "test-mode?token=tr_fake1" in (m.body or ""))
        self.assertEqual(len(posted), 1)
        batch.action_refresh()
        posted = invoice.message_ids.filtered(lambda m: "test-mode?token=tr_fake1" in (m.body or ""))
        self.assertEqual(len(posted), 1)

    def test_fallback_to_unknown_valid_mandate(self):
        """A valid mandate created outside Odoo is used when Odoo has none."""
        self.mollie._sepa_store_customer(self.company_partner, "cst_1")
        self._mandate(mandate_id="mdt_dashboard")
        token = self.env["payment.token"]._get_sepa_token_for_partner(self.contact, self.mollie)
        self.assertEqual(token.provider_ref, "mdt_dashboard")

    def test_archived_mandate_not_revived_by_fallback(self):
        token = self._create_mandate_token()
        token.active = False
        found = self.env["payment.token"]._get_sepa_token_for_partner(self.contact, self.mollie)
        self.assertFalse(found)
        self.assertFalse(token.active)

    def test_empty_consumer_name_uses_partner_name(self):
        mandate = self._mandate(mandate_id="mdt_bc")
        mandate["details"]["consumerName"] = ""
        token = self.mollie._sepa_get_or_create_token(self.contact, mandate)
        self.assertEqual(token.sepa_consumer_name, "Klant BV")

    def _ideal(self):
        ideal = self.env.ref("payment.payment_method_ideal")
        ideal.active = True
        self.mollie.payment_method_ids = [Command.link(ideal.id)]
        return ideal

    def test_mandate_request_creates_validated_mandate(self):
        """A standalone mandate request: EUR 0.01 validation, token, no booking."""
        ideal = self._ideal()
        tx = self.mollie._sepa_create_mandate_transaction(self.contact, ideal, "/my/sepa_mandate")
        self.assertEqual(tx.operation, "validation")
        self.assertEqual(tx.amount, 0.01)
        self.assertEqual(tx.currency_id.name, "EUR")
        self.assertEqual(tx.partner_id, self.company_partner)
        self.assertTrue(tx.tokenize)
        payload = tx._mollie_prepare_payment_request_payload()
        self.assertEqual(payload["sequenceType"], "first")
        self.assertEqual(payload["customerId"], "cst_new1")

        self._mandate(mandate_id="mdt_req", customer_id="cst_new1")
        tx.provider_reference = "tr_req"
        tx._process("mollie", {
            "resource": "payment", "id": "tr_req", "status": "paid",
            "amount": {"currency": "EUR", "value": "0.01"}, "method": "ideal",
            "customerId": "cst_new1", "mandateId": "mdt_req", "sequenceType": "first",
        })
        self.assertEqual(tx.state, "done")
        self.assertEqual(tx.token_id.provider_ref, "mdt_req")
        self.assertEqual(tx.token_id.sepa_status, "valid")
        self.assertEqual(tx.token_id.partner_id, self.company_partner)
        tx._post_process()
        self.assertFalse(tx.payment_id)
        confirmation = self.company_partner.message_ids.filtered(
            lambda m: "****0000" in (m.body or "") and self.company_partner in m.partner_ids
        )
        self.assertEqual(len(confirmation), 1)
        self.assertTrue(self.company_partner.sepa_collect_enabled)

    def test_mandate_request_refuses_other_methods(self):
        card = self.env.ref("payment.payment_method_card")
        with self.assertRaises(UserError):
            self.mollie._sepa_create_mandate_transaction(self.contact, card, "/my/sepa_mandate")

    def test_mandate_link_token_is_per_partner(self):
        other = self.env["res.partner"].create({"name": "Andere BV", "is_company": True})
        token = self.company_partner._sepa_mandate_access_token()
        self.assertTrue(self.company_partner._sepa_check_mandate_access_token(token))
        self.assertFalse(other._sepa_check_mandate_access_token(token))
        self.assertFalse(self.company_partner._sepa_check_mandate_access_token(""))

    def test_mandate_link_only_for_accountants(self):
        with self.assertRaises(AccessError):
            self.company_partner.with_user(self.internal_user).get_sepa_mandate_url()
        accountant = self.env["res.users"].create({
            "name": "Accountant 2", "login": "accountant_sepa_2",
            "group_ids": [(6, 0, [self.env.ref("account.group_account_manager").id])],
        })
        url = self.contact.with_user(accountant).get_sepa_mandate_url()
        self.assertIn(f"/sepa/mandate/{self.company_partner.id}/", url)

    def test_new_online_mandate_replaces_older(self):
        old = self._create_mandate_token(mandate_id="mdt_old")
        ideal = self._ideal()
        tx = self.mollie._sepa_create_mandate_transaction(self.contact, ideal, "/my/sepa_mandate")
        tx._mollie_prepare_payment_request_payload()
        self._mandate(mandate_id="mdt_new", customer_id="cst_1")
        tx.provider_reference = "tr_new"
        tx._process("mollie", {
            "resource": "payment", "id": "tr_new", "status": "paid",
            "amount": {"currency": "EUR", "value": "0.01"}, "method": "ideal",
            "customerId": "cst_1", "mandateId": "mdt_new", "sequenceType": "first",
        })
        tx._post_process()
        self.assertFalse(old.active)
        self.assertTrue(old.sepa_superseded)
        self.assertTrue(tx.token_id.active)
        # Syncing from Mollie (old mandate still valid there) does not restore it.
        self.company_partner.action_sync_sepa_mandates()
        self.assertFalse(old.active)
        token = self.env["payment.token"]._get_sepa_token_for_partner(self.contact, self.mollie)
        self.assertEqual(token.provider_ref, "mdt_new")

    def test_invoice_button_needs_mandate(self):
        invoice = self._create_invoice(25.0)
        self.assertFalse(invoice.sepa_mandate_available)
        with self.assertRaises(UserError):
            invoice.action_sepa_collect()
        self._create_mandate_token()
        invoice.invalidate_recordset(["sepa_mandate_available"])
        self.assertTrue(invoice.sepa_mandate_available)
        invoice.action_sepa_collect()

    def test_print_mandate_form_reserves_reference(self):
        self.mollie.sepa_creditor_identifier = "NL08ZZZ502057730000"
        self.contact.action_print_sepa_mandate_form()
        reference = self.company_partner.sepa_form_reference
        self.assertTrue(reference.startswith("SEPA-DD/"))
        self.contact.action_print_sepa_mandate_form()
        self.assertEqual(self.company_partner.sepa_form_reference, reference)
        html = str(self.env["ir.actions.report"]._render_qweb_html(
            "ow_payment_mollie_sepa.action_report_sepa_mandate", self.company_partner.ids,
        )[0])
        for text in (reference, "NL08ZZZ502057730000", "Klant BV", self.env.company.name):
            self.assertIn(text, html)
        # The wizard proposes the printed reference...
        wizard_defaults = self.env["mollie.sepa.mandate.wizard"].with_context(
            default_partner_id=self.company_partner.id,
        ).default_get(["mandate_reference"])
        self.assertEqual(wizard_defaults["mandate_reference"], reference)
        # ...until a mandate uses it; then a new form gets a new reference.
        token = self._create_mandate_token()
        token.sepa_mandate_reference = reference
        self.contact.action_print_sepa_mandate_form()
        self.assertNotEqual(self.company_partner.sepa_form_reference, reference)

    def test_creditor_identifier_from_payment(self):
        self._create_mandate_token()
        self._create_invoice(10.0)
        batch = self._new_batch()
        batch.action_generate_lines()
        self._collect(batch)
        self._set_mollie_status("pending", details={"creditorIdentifier": "NL08ZZZ502057730000"})
        batch.action_refresh()
        self.assertEqual(self.mollie.sepa_creditor_identifier, "NL08ZZZ502057730000")
        self._set_mollie_status("pending", details={"creditorIdentifier": "XX00ZZZ0"})
        batch.action_refresh()
        self.assertEqual(self.mollie.sepa_creditor_identifier, "NL08ZZZ502057730000")
