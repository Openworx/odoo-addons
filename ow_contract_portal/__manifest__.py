# -*- coding: utf-8 -*-
# Copyright 2019 Openworx
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl.html).

{
    'name': 'Contracts Portal',
    'category': 'Contract Management',
    'summary': 'View contracts in user portal',
    'version': '1.0',
	'license': 'LGPL-3',
    'author': 'Openworx',
    'description': """
        View contracts in user portal
    """,
	'images':['images/screenshot.png'],
    'website': 'http://www.openworx.nl',
    'depends': [
        'portal',
        'contract',
    ],
    'data': [
        'views/templates.xml',
        'security/portal_security.xml',
		'security/ir.model.access.csv'
    ],
    'installable': True,
}
