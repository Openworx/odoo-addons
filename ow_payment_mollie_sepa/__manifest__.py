# Copyright 2026 Openworx (https://www.openworx.nl)
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
{
    "name": "Mollie SEPA Direct Debit",
    "summary": "SEPA Direct Debit collection via Mollie for Odoo 19 Community",
    "version": "19.0.1.1.0",
    "category": "Accounting/Payments",
    "license": "LGPL-3",
    "author": "Openworx",
    "maintainer": "Openworx",
    "support": "info@openworx.nl",
    "website": "https://www.openworx.nl",
    # 'sale': payment_mollie_official reads payment.transaction.sale_order_ids.
    "depends": ["payment_mollie_official", "sale"],
    # python-stdnum is optional (strict IBAN validation when importable).
    "data": [
        "views/payment_templates.xml",
        "security/ir.model.access.csv",
        "security/ir_rules.xml",
        "data/ir_sequence_data.xml",
        "data/ir_cron_data.xml",
        "data/mail_template_data.xml",
        "report/sepa_mandate_report.xml",
        "views/payment_provider_views.xml",
        "views/payment_token_views.xml",
        "views/res_partner_views.xml",
        "views/account_move_views.xml",
        "views/mollie_sepa_batch_views.xml",
        "views/mollie_sepa_menus.xml",
        "wizards/mollie_sepa_mandate_wizard_views.xml",
    ],
    "installable": True,
    "application": False,
}
