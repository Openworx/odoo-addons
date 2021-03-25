# -*- coding: utf-8 -*-
# Copyright 2020, 2021 Openworx
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl.html).

{
    'name': "Invoice Payment received mail",
    'summary': "Send email when invoice payment is registered",
    'description': "Send email when invoice payment is registered",
    'version': "1.2",
    'category': "Accounting/Accounting",
    'author': "Openworx",
    'website': "https://www.openworx.nl",
    'license': 'LGPL-3',
    'depends': [
        'account'
    ],
    'data': [
        'data/email.xml',
        'views/res_config.xml',
    ],
    'images': ['images/notify-mail.png'],
    'application': False,
    'installable': True,
}
