# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

{
    'name': 'Budget Management',
    'author': 'Odoo SA, Openworx',
    'category': 'Accounting',
    'description': """
Use budgets to compare actual with expected revenues and costs
--------------------------------------------------------------

This module is backported from Odoo version 20180927. For Odoo 12.0 Community only.
""",
    'summary': 'Budget Management backported to Odoo 12.0 CE',
    'depends': ['account'],
    'data': [
        'security/ir.model.access.csv',
        'security/account_budget_security.xml',
        'views/account_analytic_account_views.xml',
        'views/account_budget_views.xml',
        'views/res_config_settings_views.xml',
    ],
    'demo': ['data/account_budget_demo.xml'],
}
