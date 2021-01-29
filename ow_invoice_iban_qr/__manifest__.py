# -*- coding: utf-8 -*-
# Copyright 2019, 2021 Openworx
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl.html).

{
    "name": "IBAN QR Code on Invoice",
    "summary": "Add IBAN QR Code on Invoice for scanning in mobile banking apps",
    "version": "0.1",
    "author": "Openworx",
    "website": "https://www.openworx.nl",
    'category': 'Accounting',
    "depends": ['account'],
    "data": [
        'views/invoice_iban_qr.xml',
		'views/res_company_view.xml',
    ],
    "license": "LGPL-3",
    'images': ['images/ibanqr.png'],
    "installable": True,
    "application": False,
}
