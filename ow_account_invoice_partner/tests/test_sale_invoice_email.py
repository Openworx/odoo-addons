from unittest import SkipTest

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.tests import tagged


@tagged('post_install', '-at_install')
class TestSaleInvoiceEmailContact(AccountTestInvoicingCommon):
    """Sale orders already resolve the invoice address themselves, via
    ``sale.order._compute_partner_invoice_id`` -> ``address_get(['invoice'])``.
    Invoices generated from a sale order therefore arrive with the invoice
    contact already on ``partner_id``, and this module must leave them alone.

    These tests skip when ``sale`` is not installed: the module only depends on
    ``account``.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not cls.env['ir.module.module'].sudo().search([
            ('name', '=', 'sale'), ('state', '=', 'installed'),
        ]):
            raise SkipTest("sale is not installed")

        # AccountTestInvoicingCommon's user carries accounting rights only, so
        # in Odoo 19 creating a sale.order raises AccessError. Grant the group
        # rather than reaching for sudo(), so the flow still runs through the
        # ACLs a real salesperson would hit.
        cls.env.user.group_ids |= cls.env.ref(
            'sales_team.group_sale_salesman_all_leads')

        Partner = cls.env['res.partner']
        cls.company_a = Partner.create({
            'name': "Acme Holding BV", 'is_company': True, 'email': "ceo@acme.test",
        })
        cls.invoice_contact_a = Partner.create({
            'name': "Acme Facturatie", 'type': 'invoice',
            'parent_id': cls.company_a.id, 'email': "facturen@acme.test",
        })
        cls.company_b = Partner.create({
            'name': "Beta BV", 'is_company': True, 'email': "info@beta.test",
        })
        Partner.create({
            'name': "Bob Beta", 'type': 'contact',
            'parent_id': cls.company_b.id, 'email': "bob@beta.test",
        })

    def _invoice_from_sale_order(self, partner):
        order = self.env['sale.order'].create({
            'partner_id': partner.id,
            'order_line': [(0, 0, {
                'product_id': self.product_a.id, 'product_uom_qty': 1,
            })],
        })
        order.action_confirm()
        order.order_line.qty_delivered = 1
        invoice = order._create_invoices()
        invoice.action_post()
        return order, invoice

    def _recipients(self, invoice):
        return self.env['account.move.send']._get_default_mail_partner_ids(
            invoice, invoice._get_mail_template(), 'en_US',
        )

    def test_sale_order_invoice_goes_to_invoice_contact(self):
        order, invoice = self._invoice_from_sale_order(self.company_a)
        # Odoo itself already redirects here; the module must not disturb it.
        self.assertEqual(order.partner_invoice_id, self.invoice_contact_a)
        self.assertEqual(invoice.partner_id, self.invoice_contact_a)
        self.assertEqual(self._recipients(invoice), self.invoice_contact_a)

    def test_sale_order_invoice_without_invoice_address(self):
        order, invoice = self._invoice_from_sale_order(self.company_b)
        self.assertEqual(order.partner_invoice_id, self.company_b)
        self.assertEqual(self._recipients(invoice), self.company_b)

    def test_sale_order_invoice_send_notifies_only_invoice_contact(self):
        _order, invoice = self._invoice_from_sale_order(self.company_a)
        wizard = self.env['account.move.send.wizard'].with_context(
            mail_notify_force_send=False).create({'move_id': invoice.id})
        wizard.action_send_and_print()
        messages = invoice.message_ids.filtered(
            lambda m: m.message_type == 'comment')
        recipients = self.env['mail.notification'].search([
            ('mail_message_id', 'in', messages.ids),
        ]).res_partner_id
        self.assertIn(self.invoice_contact_a, recipients)
        self.assertNotIn(self.company_a, recipients)
