# -*- coding: utf-8 -*-
from odoo import Command, api, fields, models
from odoo.addons.mail.tools.discuss import Store
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import clean_context

from ..utils import params, websearch
from ..utils.access import check_model_access
from ..utils.prompts import build_instructions

_MODEL_OPTION_TYPES = {
    'temperature': (int, float),
    'max_tokens': int,
    'reasoning': dict,
}


class OwAiAgent(models.Model):
    _name = 'ow.ai.agent'
    _description = 'AI Agent'
    _order = 'sequence, id'
    _rec_name = 'name'

    name = fields.Char(related='partner_id.name', readonly=False, required=True, store=False)
    # Created by `create()` (flagged `ow_ai_agent_partner`); a duplicate gets
    # a contact of its own.
    partner_id = fields.Many2one('res.partner', required=True, ondelete='cascade', index=True, copy=False)
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    subtitle = fields.Char(translate=True)
    system_prompt = fields.Text()
    model = fields.Char(
        help="Model id at the configured endpoint, e.g. openai/gpt-6-luna. "
             "Empty = Default Model from Settings.")
    model_options = fields.Json(
        default=dict,
        help="Optional request options: temperature, max_tokens, reasoning; "
             "passed through as-is, the endpoint must support them")
    # `partner_id.image_128` is itself a related (readonly) field resized from
    # image_1920; relate to image_1920 directly (like EE's ai.agent does) so
    # this field is actually writable, sized down to 128x128 for display.
    # `copy=True`: a related field defaults to `copy=False` (Odoo's own
    # rule for related fields); without it, `copy_data()` would never put
    # `image_128` in the duplicate's vals, so `create()` (see below) would
    # never see it and a duplicated agent would start without its avatar.
    image_128 = fields.Image(
        related='partner_id.image_1920', max_width=128, max_height=128, readonly=False, copy=True)
    avatar_128 = fields.Image(related='partner_id.avatar_128')
    skill_ids = fields.Many2many('ow.ai.skill', 'ow_ai_agent_skill_rel', 'agent_id', 'skill_id')
    is_system_agent = fields.Boolean(readonly=True, default=False)
    usage_count = fields.Integer(compute='_compute_usage_count')
    session_count = fields.Integer(compute='_compute_session_count')

    @api.depends()
    def _compute_usage_count(self):
        counts = dict(self.env['ow.ai.usage']._read_group(
            [('agent_id', 'in', self.ids)], ['agent_id'], ['__count']))
        for agent in self:
            agent.usage_count = counts.get(agent, 0)

    @api.depends()
    def _compute_session_count(self):
        counts = dict(self.env['ow.ai.session'].sudo()._read_group(
            [('agent_id', 'in', self.ids)], ['agent_id'], ['__count']))
        for agent in self:
            agent.session_count = counts.get(agent, 0)

    @api.constrains('model_options')
    def _check_model_options(self):
        for agent in self:
            options = agent.model_options or {}
            if not isinstance(options, dict):
                raise ValidationError(self.env._("Model options must be a JSON object."))
            for key, value in options.items():
                expected = _MODEL_OPTION_TYPES.get(key)
                if expected is None:
                    raise ValidationError(self.env._(
                        "Unknown model option '%(key)s'.", key=key))
                if isinstance(value, bool) or not isinstance(value, expected):
                    raise ValidationError(self.env._(
                        "Model option '%(key)s' has the wrong type.", key=key))

    @api.model_create_multi
    def create(self, vals_list):
        """Create each agent with a contact of its own, flagged
        ``ow_ai_agent_partner`` (the one :meth:`unlink` deletes with it).

        Binding an agent to an existing contact is for the superuser only
        (data files, migrations): deleting that agent leaves the contact.
        """
        for vals in vals_list:
            if vals.get('partner_id'):
                if not self.env.su:
                    raise UserError(self.env._("An AI agent's contact is created by the assistant itself."))
                if 'name' in vals or 'image_128' in vals:
                    raise UserError(self.env._(
                        "An AI agent's contact is created by the assistant itself; "
                        "its name and image belong to the contact."))
                continue
            partner_vals = {
                'name': vals.pop('name', None),
                'active': False,
                'type': 'contact',
                'image_1920': vals.pop('image_128', False) or False,
                'ow_ai_agent_partner': True,
            }
            partner = self.env['res.partner'].sudo().with_context(
                mail_create_nolog=True, mail_notrack=True).create(partner_vals)
            vals['partner_id'] = partner.id
        return super().create(vals_list)

    def copy_data(self, default=None):
        vals_list = super().copy_data(default=default)
        for agent, vals in zip(self, vals_list, strict=True):
            vals.setdefault('name', self.env._("%s (copy)", agent.name))
        return vals_list

    def write(self, vals):
        if 'partner_id' in vals:
            for agent in self:
                if agent.partner_id.id != vals['partner_id']:
                    raise UserError(self.env._("The partner of an AI agent cannot be changed."))
        return super().write(vals)

    def unlink(self):
        """Delete the agents, and each contact of theirs that the assistant
        created (``ow_ai_agent_partner``) once no agent, archived or not,
        uses it any more."""
        for agent in self:
            if agent.is_system_agent:
                raise UserError(self.env._("System agents cannot be deleted."))
        partners = self.partner_id.sudo()
        result = super().unlink()
        agents = self.env['ow.ai.agent'].sudo().with_context(active_test=False)
        for partner in partners.filtered('ow_ai_agent_partner'):
            if not agents.search_count([('partner_id', '=', partner.id)]):
                partner.unlink()
        return result

    @api.model
    def _ow_ai_own_contacts(self, partners):
        """The ``partners`` shaped exactly like the contact :meth:`create` makes:
        archived, a plain contact (``type`` contact, not a company, no parent,
        no child contact, no user, archived ones included), without email,
        phone, mobile or tax id, and no external id of another module.

        Older versions left no trace of which contact they created: before
        19.0.2.1.0 an agent could also be bound to an existing contact. Only
        a contact of this shape is taken for the assistant's own; any other
        stays unflagged, so deleting its agent leaves it.
        """
        partners = partners.sudo().with_context(active_test=False)
        if not partners:
            return partners
        ids = partners.ids
        users = self.env['res.users'].sudo().with_context(active_test=False).search([('partner_id', 'in', ids)])
        children = partners.search([('parent_id', 'in', ids)])
        external_ids = self.env['ir.model.data'].sudo().search(
            [('model', '=', 'res.partner'), ('res_id', 'in', ids), ('module', '!=', 'ow_ai')])
        taken = set(users.partner_id.ids) | set(children.parent_id.ids) | set(external_ids.mapped('res_id'))
        contact_fields = [name for name in ('email', 'phone', 'mobile', 'vat') if name in partners._fields]
        return partners.filtered(lambda partner: (
            not partner.active and partner.type == 'contact' and not partner.is_company
            and not partner.parent_id and partner.id not in taken
            and not any(partner[name] for name in contact_fields)))

    @api.model
    def _ow_ai_flag_agent_partners(self):
        """Flag the contact of every agent, archived ones included, as created
        by the assistant (``res.partner.ow_ai_agent_partner``) -- the ones
        :meth:`_ow_ai_own_contacts` recognises, never any other.

        Run once by the ``19.0.2.1.0`` upgrade: the flag did not exist yet.
        """
        partners = self._ow_ai_own_contacts(self.sudo().with_context(active_test=False).search([]).partner_id)
        partners.filtered(lambda partner: not partner.ow_ai_agent_partner).write({'ow_ai_agent_partner': True})

    @api.model
    def _ow_ai_repair_agent_partner_flags(self):
        """Take the flag off every flagged contact :meth:`_ow_ai_own_contacts`
        does not recognise; only ever removes the flag, never sets it.

        Run once by the ``19.0.2.1.1`` upgrade: ``19.0.2.1.0`` flagged the
        contact of every agent, also an existing one an agent was bound to.
        """
        flagged = self.env['res.partner'].sudo().with_context(active_test=False).search(
            [('ow_ai_agent_partner', '=', True)])
        (flagged - self._ow_ai_own_contacts(flagged)).write({'ow_ai_agent_partner': False})

    def _get_model(self):
        self.ensure_one()
        return self.model or params.get_str(self.env, 'ow_ai.default_model')

    def _get_model_options(self):
        self.ensure_one()
        options = self.model_options or {}
        return {key: value for key, value in options.items() if key in _MODEL_OPTION_TYPES}

    def _get_instructions(self, extra_instructions=None, *, skills=None, loaded_skill_ids=(),
                           usage_context=None, record=None):
        """The system prompt; ``skills`` defaults to the agent's own (a chat
        passes its session's, :meth:`ow.ai.session._get_available_skills`)."""
        self.ensure_one()
        return build_instructions(
            self, extra=extra_instructions,
            skills=skills if skills is not None else self._get_available_skills(),
            loaded_skill_ids=loaded_skill_ids, usage_context=usage_context, record=record)

    @api.model
    def _get_default_builtin_keys(self):
        return ['interaction.load_skills', 'interaction.ask_user_question']

    def _get_default_tools(self):
        return self.env['ow.ai.tool'].search([('builtin_key', 'in', self._get_default_builtin_keys())])

    def _get_available_skills(self):
        self.ensure_one()
        return self.skill_ids.filtered('active')

    def action_launch_chat(self, *, interface_key='systray', res_model=None, res_id=None, channel_title=None):
        """Launch (create) a fresh AI chat for ``self.env.user``.

        Called on an empty recordset, the agent is resolved from
        ``interface_key``/``res_model`` via ``ow.ai.composer._get_for``;
        called on a record, that agent is used directly. Returns the data
        the JS client needs to open the chat window right away, without a
        second round-trip: the channel/session ids, their ``Store``
        payload, the composer's prompt buttons and a subtitle.
        """
        env = self.env
        if not env.user.has_group('ow_ai.group_ai_user'):
            raise AccessError(env._("You do not have access to the AI assistant."))

        composer = env['ow.ai.composer']._get_for(interface_key, res_model)
        if self:
            self.ensure_one()
            agent = self
        else:
            agent = composer.agent_id if composer else env.ref('ow_ai.agent_default')

        title = channel_title
        if res_model and res_id:
            check_model_access(env, res_model, 'read')
            record = env[res_model].browse(res_id)
            record.check_access('read')
            title = title or record.display_name

        channel = agent._create_chat_channel(
            env.user, title=title, res_model=res_model, res_id=res_id, composer=composer)
        session = channel.sudo().ow_ai_session_ids[:1]
        store = Store().add(channel)
        prompt_buttons = [
            {'name': button.name, 'prompt': button.prompt}
            for button in (composer.prompt_button_ids if composer else env['ow.ai.prompt.button'])
        ]
        return {
            'channel_id': channel.id,
            'session_id': session.id,
            'store_data': store.get_result(),
            'prompt_buttons': prompt_buttons,
            'subtitle': agent.subtitle,
        }

    def action_test_chat(self):
        self.ensure_one()
        data = self.action_launch_chat(interface_key='systray')
        return {'type': 'ir.actions.client', 'tag': 'ow_ai.open_chat', 'params': data}

    def _create_session(self, channel, *, composer=None, res_model=None, res_id=None):
        """Create the (sudo) root ``ow.ai.session`` of ``channel`` for this agent
        (the caller's ``default_*`` context keys are ignored). Its "Web search"
        switch starts on when web search is configured and on by default."""
        self.ensure_one()
        env = self.env
        return env['ow.ai.session'].sudo().with_context(clean_context(env.context)).create({
            'channel_id': channel.id,
            'agent_id': self.id,
            'composer_id': composer.id if composer else False,
            'res_model': res_model,
            'res_id': res_id,
            'web_search': websearch.is_configured(env) and params.get_bool(env, 'ow_ai.web_search_default', False),
        })

    def _create_chat_channel(self, user, *, title=None, res_model=None, res_id=None, composer=None):
        """Create a fresh ``ow_ai_chat`` channel (and its root session) for `user`.

        The channel ends up with exactly two members: `user` and this
        agent's partner. Creation happens ``sudo()`` with the
        ``ow_ai_channel_setup`` context flag so ``discuss.channel.member``
        allows it (it requires both: a client can send the flag, not
        ``sudo()``); any member ``discuss.channel.create()`` auto-adds on top
        of that (e.g. the current request user, when different from `user`)
        is stripped back out afterwards so exactly those two remain.
        """
        self.ensure_one()
        channel = self.env['discuss.channel'].sudo().with_context(
            clean_context(self.env.context),
            ow_ai_channel_setup=True, mail_create_nosubscribe=True, mail_create_nolog=True,
        ).create({
            'channel_type': 'ow_ai_chat',
            'name': title or self.name,
            'ow_ai_agent_id': self.id,
            'channel_member_ids': [
                Command.create({'partner_id': user.partner_id.id}),
                Command.create({'partner_id': self.partner_id.id}),
            ],
        })
        wanted_partners = user.partner_id | self.partner_id
        extra_members = channel.channel_member_ids.filtered(lambda member: member.partner_id not in wanted_partners)
        if extra_members:
            extra_members.sudo().unlink()
        if len(channel.channel_member_ids) != 2:
            raise UserError(self.env._("An AI chat channel must have exactly 2 members."))

        self._create_session(channel, composer=composer, res_model=res_model, res_id=res_id)
        # Drop the sudo/`ow_ai_channel_setup` context used for setup: callers
        # must get back a channel that behaves normally (member protection
        # enforced again) rather than one that silently keeps bypassing it.
        return channel.with_env(self.env)
