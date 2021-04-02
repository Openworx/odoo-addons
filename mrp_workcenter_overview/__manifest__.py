# -*- coding: utf-8 -*-
# Copyright 2021 Openworx
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl.html).

{
    'name': 'MRP Workcenter Overview',
    'summary': 'MRP Workcenter Overview',
    'version': '1.0',
    'category': 'Manufacturing/Manufacturing',
    'summary': """
MRP Workcenter Overview backport from Odoo 13 CE
""",
    'author': "Openworx",
    'website': 'https://www.openworx.nl',
    'license': 'LGPL-3',
    'images': ['images/screen.png'],
    'depends': [
	'mrp',
    ],
    'data': [
        'views/mrp_workcenter_views.xml',
    ],
    'installable': True,
    'application': False,
}
