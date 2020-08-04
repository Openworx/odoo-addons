# -*- coding: utf-8 -*-
# Copyright 2020 Openworx
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl.html).

{
    'name': 'Metro Style Portal Page',
    'summary': 'Metro Style Website User Portal Page',
    'version': '1.0',
    'category': 'Website',
    'summary': """
Give Odoo website portal a Metro style look
""",
    'author': "Openworx",
    'website': 'https://www.openworx.nl',
    'license': 'LGPL-3',
    'depends': [
	'website',
    ],
    'data': [
        'views/assets.xml',
        'views/portal.xml',
    ],
    'images': ['images/image.png'],
    'installable': True,
    'application': False,
}
