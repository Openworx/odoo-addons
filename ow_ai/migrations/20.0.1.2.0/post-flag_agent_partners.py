# -*- coding: utf-8 -*-
"""20.0.1.2.0: an agent deletes only the contact the assistant created for
it (``res.partner.ow_ai_agent_partner``). Older versions left no trace of
which contact that was (an agent could also be bound to an existing one):
flag the contacts of existing agents shaped exactly like the one the
assistant creates (``ow.ai.agent._ow_ai_own_contacts``), no other.

Run once, on the upgrade to this version only: a contact the superuser binds
an agent to later on is not the assistant's and must stay unflagged.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['ow.ai.agent']._ow_ai_flag_agent_partners()
