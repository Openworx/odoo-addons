# -*- coding: utf-8 -*-
# Copyright 2020 Openworx
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl.html).

{
    "name": "Openworx Notify",
    "summary": "Sound and push notifications on new messages.",
    "version": "13.0.0.1",
    'category': 'Tools',
    "website": "http://www.openworx.nl",
    "description": """
    Sound and push notifications on new messages.
    """,
    'images':['images/chat.png'],
    "author": "Openworx",
    "license": "LGPL-3",
    "installable": True,
    "depends": [
	'mail',
    ],
    "data": [
        'views/assets.xml',
    ],

}
