# -*- coding: utf-8 -*-
# Copyright 2021 Openworx
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl.html).

{
    'name': 'Purchase Order Paid Ribbon',
    'summary': 'Paid Ribbon on Purchase Order Form',
    'version': '0.1',
    'category': 'Purchases',
    'summary': """
Paid Ribbon on Purchase Order Form
""",
    'author': "Openworx",
    'website': 'https://www.openworx.nl',
    'license': 'LGPL-3',
    'depends': [
	'purchase',
    ],
    'data': [
        'views/purchase_order_form.xml',
    ],
    'images': ['images/image.png'],
    'installable': True,
    'application': False,
}
