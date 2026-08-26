{
    'name': "Invoice Email to Invoice Address",
    'summary': "Send invoice emails to the customer's Invoice Address contact",
    'description': """
Odoo mails an invoice to the partner set on the move, which for manually created
invoices is the customer company itself. When that company has a child contact of
address type "Invoice Address" carrying its own email, that address is ignored.

This module resolves the invoice-address contact at send time and uses it as the
recipient instead of the company. Customers without such a contact are untouched.
""",
    'author': "Openworx <info@openworx.nl>",
    'website': "https://www.openworx.nl",
    'version': '18.0.1.0.0',
    'category': 'Accounting/Accounting',
    'license': 'LGPL-3',
    'depends': ['account'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
