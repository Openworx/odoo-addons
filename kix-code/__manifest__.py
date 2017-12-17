# -*- coding: utf-8 -*-

{
    'name': 'PostNL KIX-Code',
    'version': '10.0.1',
    'author': 'Openworx',
    'category': 'Base',
    'license': 'LGPL-3',
    'summary': 'PostNL Address barcode on invoice',
    'description': """
PostNL maakt bij de sortering van post gebruik van een streepjescode: de KIX (KlantIndeX). Deze code voegt u toe aan de adresgegevens. De KIX bevat de gegevens die wij nodig hebben om uw post met de modernste sorteermachines te verwerken. Door de KIX bij het adres af te drukken helpt u ons om de kwaliteit van de dienstverlening te verbeteren.
    """,
    'website': 'http://www.openworx.nl',
    'images':[
        'images/kixcode.png'
    ],
    'depends': ['report','partner_street_number'],
    'data': [
        'kixcode_invoice.xml',
    ],
    'installable': True,
    'auto_install': False,
}
