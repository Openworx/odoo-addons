# -*- coding: utf-8 -*-
# Copyright 2019 Openworx
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

{
    'name': 'Contracts Portal',
    'category': 'Contract Management',
    'summary': 'View contracts in user portal',
    'version': '1.0',
	'license': 'AGPL-3',
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
