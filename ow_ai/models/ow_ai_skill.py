# -*- coding: utf-8 -*-
from odoo import Command, api, fields, models
from odoo.exceptions import UserError

_LOCKED_FIELDS = {'name', 'tool_ids', 'is_native'}


class OwAiSkill(models.Model):
    _name = 'ow.ai.skill'
    _description = 'AI Skill'
    _order = 'sequence, id'

    name = fields.Char(required=True, translate=True)
    description = fields.Text(
        required=True, translate=True,
        help="Shown to the model so it can decide when to load this skill")
    instructions = fields.Text(
        translate=True,
        help="Injected into the conversation when the skill is loaded")
    # No field-level `groups`: `load_skills` (Task 1.11) must read a loaded
    # skill's tools as the requesting (non-manager) user, never sudo. Only
    # managers can *write* it anyway (``ow.ai.skill``'s own CRUD ACL); the
    # form view still hides it from non-managers (`groups=` on the view
    # field) for editing.
    tool_ids = fields.Many2many('ow.ai.tool', 'ow_ai_skill_tool_rel', 'skill_id', 'tool_id')
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    is_native = fields.Boolean(readonly=True, default=False)
    skill_type = fields.Selection(
        [('executable', "Executable"), ('guidance', "Guidance")],
        compute='_compute_skill_type', store=True)
    agent_ids = fields.Many2many('ow.ai.agent', 'ow_ai_agent_skill_rel', 'skill_id', 'agent_id')

    @api.depends('tool_ids')
    def _compute_skill_type(self):
        for skill in self:
            skill.skill_type = 'executable' if skill.tool_ids else 'guidance'

    def _check_native_lock(self, vals):
        if self.env.context.get('install_mode') or self.env.context.get('module'):
            return
        if any(skill.is_native for skill in self) and (set(vals) & _LOCKED_FIELDS):
            raise UserError(self.env._("Native skills cannot have this field changed."))

    def write(self, vals):
        self._check_native_lock(vals)
        return super().write(vals)

    def unlink(self):
        if not (self.env.context.get('install_mode') or self.env.context.get('module')):
            for skill in self:
                if skill.is_native:
                    raise UserError(self.env._("Native skills cannot be deleted."))
        return super().unlink()

    @api.model
    def _sync_default_agent_skills(self):
        """Link every active native skill to ``ow_ai.agent_default`` when missing.

        Called from ``data/ow_ai_skills.xml`` on every module install/update:
        the default agent is ``noupdate`` data (its own edits survive an
        update), so a native skill added by a newer version of the module
        would otherwise never reach it. Only adds links, never removes one.
        No-op while the agent does not exist yet (first install: the skills
        file loads before the agent's, which links them itself).
        """
        agent = self.env.ref('ow_ai.agent_default', raise_if_not_found=False)
        if not agent:
            return
        agent = agent.sudo()
        missing = self.sudo().search([('is_native', '=', True)]) - agent.skill_ids
        if missing:
            agent.write({'skill_ids': [Command.link(skill.id) for skill in missing]})
