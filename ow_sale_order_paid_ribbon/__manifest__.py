# -*- coding: utf-8 -*-
# Copyright 2021 Openworx
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl.html).

{
    'name': 'Sales Order Paid Ribbon',
    'summary': 'Paid Ribbon on Sales Order Form',
    'version': '0.1',
    'category': 'Sales',
    'summary': """
Paid Ribbon on Sales Order Form
""",
    'author': "Openworx",
    'website': 'https://www.openworx.nl',
    'license': 'LGPL-3',
    'depends': [
	'sale',
    ],
    'data': [
        'views/sale_order_form.xml',
    ],
    'images': ['images/image.png'],
    'installable': True,
    'application': False,
}
