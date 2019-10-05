# -*- encoding: utf-8 -*-

{
    'name': 'Website Portal Login Page',
    'summary': 'Make Odoo Website loginpage like fullscreen portal',
    'version': '13.0.1.0',
    'category': 'Website',
    'summary': """
Make Odoo Website loginpage like fullscreen portal
""",
    'author': "Openworx",
    'website': 'http://www.openworx.nl',
    'license': 'LGPL-3',
    'images': ['images/loginpage.png'],
    'depends': [
	'website',
    ],
    'data': [
        'templates/portal_login_page.xml',
    ],
    'installable': True,
    'application': False,
}
