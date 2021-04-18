# -*- coding: utf-8 -*-
# Copyright 2021 Openworx
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl.html).


{
    'name': 'Invoice Treeview state color',
    'summary': 'Invoice Treeview state color',
    'version': '1.1',
    'category': 'Accounting',
    'summary': """
Show colors on Invoice Treeview depending on state.
""",
    'author': "Openworx",
    'website': 'https://www.openworx.nl',
    'license': 'LGPL-3',
    'images': ['images/screen.png'],
    'depends': [
	'account',
    ],
    'data': [
        'views/invoice_tree.xml',
    ],
    'installable': True,
    'application': False,
}
