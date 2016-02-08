# -*- coding: utf-8 -*-

{
    'name': 'Packing Slip',
    'version': '0.0.1',
    'author': 'Openworx',
    'category': 'Sales',
    'summary': 'Packing Slip Report',
    'description': """
    This module adds a print option in the order form for a packing slip.
    """,
    'website': 'http://www.openworx.nl',
    'images': [],
    'depends': ['base', 'sale'],
    'data': [
        'report_packing_slip.xml',
    ],
    'installable': True,
    'auto_install': False,
}
