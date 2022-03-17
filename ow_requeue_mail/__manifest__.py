# -*- coding: utf-8 -*-

{
    'name': 'Requeue Failed Mails',
    'summary': """Requeue failed mails""",
    'version': '1.0',
    'category': 'Extra Tools',
    'description': """Requeue failed mails""",
    'author': 'Openworx',
    'website': 'https://www.openworx.nl',
    'depends': ['mail'],
    'license': 'LGPL-3',
    'data': [
            'views/requeue_mail_cron.xml',
             ],
    'installable': True,
    'auto_install': False,
}

