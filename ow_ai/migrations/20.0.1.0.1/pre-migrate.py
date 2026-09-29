# -*- coding: utf-8 -*-
"""Native tools and skills became updatable data (``noupdate="0"``).

Odoo never clears the ``noupdate`` flag of an existing ``ir.model.data``
row when a data file stops being ``noupdate``: on a database installed
before this version the native tools/skills would stay frozen forever.
Clear the flag once, before the data files load, so this update (and
every later one) refreshes them from the module's data files.
"""


def migrate(cr, version):
    cr.execute("""
        UPDATE ir_model_data
           SET noupdate = false
         WHERE module = 'ow_ai'
           AND model IN ('ow.ai.tool', 'ow.ai.skill')
           AND noupdate
    """)
