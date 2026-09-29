# -*- coding: utf-8 -*-
"""19.0.2.1.1 repairs two upgrade assumptions of 19.0.2.1.0.

- It flagged the contact of every agent as the assistant's own
  (``res.partner.ow_ai_agent_partner``), also an existing contact an agent
  had been bound to, which deleting the agent would then delete. The flag
  stays only on contacts shaped exactly like the one the assistant creates
  (``ow.ai.agent._ow_ai_own_contacts``).
- A round job queued before jobs carried a turn id cannot prove which turn
  it belongs to: each pending or failed one that can no longer run is
  failed as superseded (``ow.ai.job._ow_ai_supersede_turnless_rounds``).
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['ow.ai.agent']._ow_ai_repair_agent_partner_flags()
    env['ow.ai.job']._ow_ai_supersede_turnless_rounds()
