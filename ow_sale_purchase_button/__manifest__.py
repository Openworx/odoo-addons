# -*- coding: utf-8 -*-
# Copyright 2020 Openworx
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl.html).

{
    'name': 'Purchase Smartbutton on Sales Order',
    'summary': 'Purchase Smartbutton on Sales Order',
    'version': '1.0',
    'category': 'Sales',
    'website': 'https://www.openworx.nl',
    'description': """
    Add purchase order smartbutton on the sales order form
    """,
    'images': ['images/purchase_button.png'],
    'author': 'Openworx',
    'license': 'LGPL-3',
    'installable': True,
    'depends': [
    'sale_management','purchase',
    ],
    'data': [
        'views/sale_order_view.xml',
    ],
}
