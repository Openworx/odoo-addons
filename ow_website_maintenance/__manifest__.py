# -*- coding: utf-8 -*-
# Copyright 2021 Openworx
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl.html).


{
    'name': 'Website Maintenance Mode',
    'summary': 'Website Maintenance/Coming Soon Mode',
    'version': '1.0',
    'category': 'Website',
    'summary': """
Website Maintenance/Coming Soon Mode
""",
    'author': "Openworx",
    'website': 'https://www.openworx.nl',
    'license': 'LGPL-3',
    'images': ['images/screen.png'],
    'depends': [
	'website',
	'portal',
    ],
    'data': [
        'views/res_config.xml',
        'views/website.xml',
    ],
    'installable': True,
    'application': False,

}
