from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.tests import tagged


@tagged('post_install', '-at_install')
class TestInvoiceEmailContact(AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Partner = cls.env['res.partner']

        # A: company with a dedicated invoice address -- the happy path.
        cls.company_a = Partner.create({
            'name': "Acme Holding BV",
            'is_company': True,
            'email': "ceo@acme.test",
        })
        cls.invoice_contact_a = Partner.create({
            'name': "Acme Facturatie",
            'type': 'invoice',
            'parent_id': cls.company_a.id,
            'email': "facturen@acme.test",
        })

        # B: only a regular contact child -- must NOT be picked up.
        cls.company_b = Partner.create({
            'name': "Beta BV", 'is_company': True, 'email': "info@beta.test",
        })
        cls.contact_b = Partner.create({
            'name': "Bob Beta", 'type': 'contact',
            'parent_id': cls.company_b.id, 'email': "bob@beta.test",
        })

        # C: invoice address child without an email.
        cls.company_c = Partner.create({
            'name': "Gamma BV", 'is_company': True, 'email': "info@gamma.test",
        })
        cls.invoice_contact_c = Partner.create({
            'name': "Gamma Facturatie", 'type': 'invoice',
            'parent_id': cls.company_c.id, 'email': False,
        })

        # D: invoice address, but the company itself has no email. super()
        # drops the company before our override runs, so this is the case a
        # naive "filter super()'s output" implementation gets wrong.
        cls.company_d = Partner.create({
            'name': "Delta BV", 'is_company': True, 'email': False,
        })
        cls.invoice_contact_d = Partner.create({
            'name': "Delta Facturatie", 'type': 'invoice',
            'parent_id': cls.company_d.id, 'email': "ar@delta.test",
        })

        # E: plain individual, no parent, no children.
        cls.individual_e = Partner.create({
            'name': "Eva Solo", 'email': "eva@example.test",
        })

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _make_invoice(self, partner, move_type='out_invoice'):
        return self.init_invoice(
            move_type, partner=partner, products=self.product_a, post=True,
        )

    def _recipients(self, invoice):
        return self.env['account.move.send']._get_default_mail_partner_ids(
            invoice, invoice._get_mail_template(), 'en_US',
        )

    def _wizard(self, invoice):
        return self.env['account.move.send.wizard'].create({'move_id': invoice.id})

    # ------------------------------------------------------------------
    # res.partner._get_invoice_email_partner
    # ------------------------------------------------------------------
    def test_helper_returns_invoice_child(self):
        self.assertEqual(
            self.company_a._get_invoice_email_partner(), self.invoice_contact_a)

    def test_helper_ignores_contact_child(self):
        """address_get() falls back to a 'contact' child; the type guard rejects it."""
        self.assertEqual(
            self.company_b._get_invoice_email_partner(), self.company_b)

    def test_helper_ignores_invoice_child_without_email(self):
        self.assertEqual(
            self.company_c._get_invoice_email_partner(), self.company_c)

    def test_helper_is_idempotent_on_the_invoice_contact_itself(self):
        self.assertEqual(
            self.invoice_contact_a._get_invoice_email_partner(),
            self.invoice_contact_a)

    def test_helper_noop_on_individual(self):
        self.assertEqual(
            self.individual_e._get_invoice_email_partner(), self.individual_e)

    # ------------------------------------------------------------------
    # account.move.send._get_default_mail_partner_ids
    # ------------------------------------------------------------------
    def test_recipients_swapped_for_invoice_contact(self):
        recipients = self._recipients(self._make_invoice(self.company_a))
        self.assertEqual(recipients, self.invoice_contact_a)
        self.assertNotIn(self.company_a, recipients)

    def test_recipients_unchanged_without_invoice_address(self):
        recipients = self._recipients(self._make_invoice(self.company_b))
        self.assertEqual(recipients, self.company_b)

    def test_recipients_unchanged_when_invoice_contact_has_no_email(self):
        recipients = self._recipients(self._make_invoice(self.company_c))
        self.assertEqual(recipients, self.company_c)

    def test_recipients_when_company_has_no_email(self):
        """Regression: super() applies .filtered('email') before we run."""
        recipients = self._recipients(self._make_invoice(self.company_d))
        self.assertEqual(recipients, self.invoice_contact_d)

    def test_recipients_idempotent_when_invoiced_to_the_invoice_contact(self):
        recipients = self._recipients(self._make_invoice(self.invoice_contact_a))
        self.assertEqual(recipients, self.invoice_contact_a)

    def test_recipients_on_credit_note(self):
        recipients = self._recipients(
            self._make_invoice(self.company_a, move_type='out_refund'))
        self.assertEqual(recipients, self.invoice_contact_a)

    def test_move_partner_id_is_untouched(self):
        """Scope guard: only the mail recipient changes."""
        invoice = self._make_invoice(self.company_a)
        self._recipients(invoice)
        self.assertEqual(invoice.partner_id, self.company_a)
        self.assertEqual(invoice.commercial_partner_id, self.company_a)

    # ------------------------------------------------------------------
    # account.move.send.wizard
    # ------------------------------------------------------------------
    def test_wizard_to_field_shows_invoice_contact(self):
        wizard = self._wizard(self._make_invoice(self.company_a))
        self.assertTrue(wizard.mail_template_id)
        self.assertEqual(wizard.mail_partner_ids, self.invoice_contact_a)

    def test_wizard_to_field_unchanged_without_invoice_address(self):
        wizard = self._wizard(self._make_invoice(self.company_b))
        self.assertEqual(wizard.mail_partner_ids, self.company_b)

    def test_wizard_without_template_uses_invoice_contact(self):
        """Covers the else-branch that hardcodes commercial_partner_id."""
        wizard = self._wizard(self._make_invoice(self.company_a))
        wizard.mail_template_id = False
        self.assertEqual(wizard.mail_partner_ids, self.invoice_contact_a)

    def test_wizard_without_template_unchanged_without_invoice_address(self):
        wizard = self._wizard(self._make_invoice(self.company_b))
        wizard.mail_template_id = False
        self.assertEqual(wizard.mail_partner_ids, self.company_b)

    # ------------------------------------------------------------------
    # follower notifications
    # ------------------------------------------------------------------
    def _sent_recipients(self, invoice):
        """Actually run the send flow and return every notified partner."""
        # mail_notify_force_send=False queues the mail.mail instead of handing
        # it to SMTP; the mail.notification rows we assert on are written either
        # way.
        wizard = self._wizard(invoice).with_context(mail_notify_force_send=False)
        wizard.action_send_and_print()
        message = invoice.message_ids.filtered(
            lambda m: m.message_type == 'comment')[:1]
        notifications = self.env['mail.notification'].search([
            ('mail_message_id', '=', message.id),
        ])
        return notifications.res_partner_id

    def test_sent_mail_skips_superseded_company(self):
        """The company is an auto-subscribed follower of its own invoice, so it
        would receive a second copy unless the follower notification is dropped
        as well."""
        invoice = self._make_invoice(self.company_a)
        self.assertIn(
            self.company_a, invoice.message_follower_ids.partner_id,
            "precondition: the customer follows its own invoice")
        recipients = self._sent_recipients(invoice)
        self.assertIn(self.invoice_contact_a, recipients)
        self.assertNotIn(self.company_a, recipients)

    def test_sent_mail_unchanged_without_invoice_address(self):
        invoice = self._make_invoice(self.company_b)
        recipients = self._sent_recipients(invoice)
        self.assertIn(self.company_b, recipients)

    # ------------------------------------------------------------------
    # batch sending
    # ------------------------------------------------------------------
    def _batch_send(self, invoices):
        """Run the batch wizard synchronously and return notified partners per move."""
        wizard = self.env['account.move.send.batch.wizard'].with_context(
            active_ids=invoices.ids, mail_notify_force_send=False,
        ).create({'move_ids': [(6, 0, invoices.ids)]})
        wizard.action_send_and_print(force_synchronous=True)
        result = {}
        for invoice in invoices:
            messages = invoice.message_ids.filtered(
                lambda m: m.message_type == 'comment')
            result[invoice] = self.env['mail.notification'].search([
                ('mail_message_id', 'in', messages.ids),
            ]).res_partner_id
        return result

    def test_batch_send_uses_invoice_contact(self):
        """The batch wizard reaches _get_default_mail_partner_ids through
        _get_default_sending_settings, so it must behave like the single send."""
        with_addr = self._make_invoice(self.company_a)
        without_addr = self._make_invoice(self.company_b)
        sent = self._batch_send(with_addr | without_addr)

        self.assertIn(self.invoice_contact_a, sent[with_addr])
        self.assertNotIn(self.company_a, sent[with_addr])
        self.assertIn(self.company_b, sent[without_addr])

    def test_batch_send_via_cron(self):
        """The UI's default batch path defers to ir_cron_account_move_send.

        `mail_partner_ids` is the one setting `_get_default_sending_settings`
        does NOT read back from `move.sending_data` (it passes no from_cron),
        so the cron recomputes it through our override just like the wizard.
        """
        invoice = self._make_invoice(self.company_a)
        self.env.ref('account.ir_cron_account_move_send').sudo().active = True
        self.env['account.move.send.batch.wizard'].with_context(
            active_ids=invoice.ids, mail_notify_force_send=False,
        ).create({'move_ids': [(6, 0, invoice.ids)]}).action_send_and_print()
        self.assertTrue(invoice.sending_data, "the cron job was queued")

        self.env['account.move'].sudo()._cron_account_move_send()

        messages = invoice.message_ids.filtered(
            lambda m: m.message_type == 'comment')
        recipients = self.env['mail.notification'].search([
            ('mail_message_id', 'in', messages.ids),
        ]).res_partner_id
        self.assertIn(self.invoice_contact_a, recipients)
        self.assertNotIn(self.company_a, recipients)

    # ------------------------------------------------------------------
    # credit notes
    # ------------------------------------------------------------------
    def _reverse(self, invoice, reason="Retour"):
        """Credit the invoice the way the "Credit Note" button does."""
        wizard = self.env['account.move.reversal'].with_context(
            active_model='account.move', active_ids=invoice.ids,
        ).create({'journal_id': invoice.journal_id.id, 'reason': reason})
        credit_note = self.env['account.move'].browse(
            wizard.reverse_moves()['res_id'])
        credit_note.action_post()
        return credit_note

    def test_credit_note_uses_its_own_template(self):
        """Credit notes render account.email_template_edi_credit_note, a
        different record from the invoice template -- but with the same
        hardcoded partner_to, so the same fix has to cover it."""
        credit_note = self._reverse(self._make_invoice(self.company_a))
        self.assertEqual(credit_note.move_type, 'out_refund')
        self.assertEqual(
            credit_note._get_mail_template(),
            self.env.ref('account.email_template_edi_credit_note'))
        self.assertEqual(self._recipients(credit_note), self.invoice_contact_a)

    def test_credit_note_send_notifies_only_invoice_contact(self):
        credit_note = self._reverse(self._make_invoice(self.company_a))
        self.assertIn(
            self.company_a, credit_note.message_follower_ids.partner_id,
            "precondition: the customer follows its own credit note")
        recipients = self._sent_recipients(credit_note)
        self.assertIn(self.invoice_contact_a, recipients)
        self.assertNotIn(self.company_a, recipients)

    def test_credit_note_unchanged_without_invoice_address(self):
        credit_note = self._reverse(self._make_invoice(self.company_b))
        self.assertIn(self.company_b, self._sent_recipients(credit_note))
