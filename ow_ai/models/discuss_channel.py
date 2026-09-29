# -*- coding: utf-8 -*-
"""``discuss.channel`` extension: the ``ow_ai_chat`` conversation type.

A chat channel bound to an ``ow.ai.agent``: exactly two members (the human
user and the agent's partner), no invitations, and garbage-collected once
stale. ``ow_ai_agent_id`` carries ``groups=fields.NO_ACCESS`` (the ORM's
"no user may read or write it, only sudo" marker: ``NO_ACCESS = '.'`` in
``odoo/orm/models.py``, re-exported by ``odoo.fields`` and used the same way
by ``auth_totp``'s ``totp_secret``) because letting a member write it
directly would let them repoint or hide the channel's agent binding.
"""
from __future__ import annotations

from datetime import timedelta

from odoo import api, fields, models
from odoo.addons.mail.tools.discuss import Store
from odoo.exceptions import AccessError, UserError, ValidationError


class DiscussChannel(models.Model):
    _inherit = 'discuss.channel'

    channel_type = fields.Selection(
        selection_add=[('ow_ai_chat', "AI Chat")],
        ondelete={'ow_ai_chat': 'cascade'})
    ow_ai_agent_id = fields.Many2one(
        'ow.ai.agent', groups=fields.NO_ACCESS, index='btree_not_null', ondelete='cascade')
    ow_ai_session_ids = fields.One2many('ow.ai.session', 'channel_id')

    @api.constrains('channel_type', 'ow_ai_agent_id')
    def _check_ow_ai_agent_channel_type(self):
        for channel in self:
            if channel.ow_ai_agent_id and channel.channel_type not in channel._ow_ai_agent_channel_types():
                raise ValidationError(self.env._(
                    "An AI agent can only be set on a channel of type: %(types)s.",
                    types=', '.join(channel._ow_ai_agent_channel_types())))

    def _ow_ai_agent_channel_types(self):
        """Channel types allowed to carry an ``ow_ai_agent_id``.

        Overridden by later modules (e.g. a livechat integration) to widen
        the set without touching this SQL-level concern again.
        """
        return ['ow_ai_chat']

    # -- naming / avatar ------------------------------------------------

    def _compute_display_name(self):
        super()._compute_display_name()
        for channel in self:
            if channel.channel_type == 'ow_ai_chat' and not channel.name:
                # sudo: ow_ai_agent_id has groups=fields.NO_ACCESS.
                channel.display_name = channel.sudo().ow_ai_agent_id.name

    @api.depends('channel_type', 'image_128', 'uuid', 'ow_ai_agent_id.partner_id.avatar_128')
    def _compute_avatar_128(self):
        ow_ai_channels = self.filtered(lambda channel: channel.channel_type == 'ow_ai_chat')
        for channel in ow_ai_channels:
            # sudo: ow_ai_agent_id has groups=fields.NO_ACCESS.
            agent = channel.sudo().ow_ai_agent_id
            channel.avatar_128 = channel.image_128 or (agent.partner_id.avatar_128 if agent else False)
        super(DiscussChannel, self - ow_ai_channels)._compute_avatar_128()

    def _types_allowing_seen_infos(self):
        return super()._types_allowing_seen_infos() + ['ow_ai_chat']

    # -- members ----------------------------------------------------------

    def _add_members(self, **kwargs):
        # sudo and the setup key: the assistant's own setup only (see
        # `discuss_channel_member.py`), never a client's forged context.
        setup = self.env.su and self.env.context.get('ow_ai_channel_setup')
        if not setup and any(channel.channel_type == 'ow_ai_chat' for channel in self):
            raise UserError(self.env._("AI chats cannot have additional members."))
        return super()._add_members(**kwargs)

    def ow_ai_delete_chat(self):
        """Delete this AI chat (public RPC).

        Only an ``ow_ai_chat`` channel qualifies, and the caller must be one
        of its members *and* pass the ``unlink`` access check as themselves
        (the ``ir.model.access`` row granting ``unlink`` to AI users, scoped
        to their own AI chats by ``ow_ai.rule_ow_ai_chat_unlink_own``);
        only then is the channel removed with ``sudo()`` (its sessions,
        events and jobs cascade with it).
        """
        self.ensure_one()
        if self.channel_type != 'ow_ai_chat':
            raise AccessError(self.env._("Only AI chats can be deleted this way."))
        if self.env.user.partner_id not in self.channel_member_ids.partner_id:
            raise AccessError(self.env._("You are not a member of this AI chat."))
        self.check_access('unlink')
        self.sudo().unlink()

    # -- Store / bus ------------------------------------------------------

    def _to_store_defaults(self, target: Store.Target):
        return super()._to_store_defaults(target) + self._store_ow_ai_fields()

    def _sync_field_names(self):
        field_names = super()._sync_field_names()
        field_names[None].extend(self._store_ow_ai_fields())
        return field_names

    def _store_ow_ai_fields(self):
        def is_ow_ai_agent_channel(channel):
            return channel.channel_type in channel._ow_ai_agent_channel_types()

        def has_ow_ai_session(channel):
            # mail 19's `create` sends a new channel on the bus with its
            # payload built right away, before `ow.ai.agent._create_chat_channel`
            # adds the root session: an empty list delivered after the
            # launch's answer would unlink that session in the client. No
            # session yet: the key is left out.
            return is_ow_ai_agent_channel(channel) and bool(channel.sudo().ow_ai_session_ids)

        return [
            Store.One(
                'ow_ai_agent_id', ['name', 'subtitle', 'partner_id'],
                predicate=is_ow_ai_agent_channel, sudo=True),
            # All sessions of the channel for now; once sub-sessions exist this
            # should be restricted to the root session (no parent).
            Store.Many(
                'ow_ai_session_ids', self.env['ow.ai.session']._store_session_fields(),
                predicate=has_ow_ai_session, sudo=True),
        ]

    # -- garbage collection -------------------------------------------------

    @api.autovacuum
    def _gc_ow_ai_chats(self):
        now = fields.Datetime.now()
        domain = [
            ('channel_type', '=', 'ow_ai_chat'),
            '|',
            ('last_interest_dt', '<', now - timedelta(days=30)),
            '&', ('last_interest_dt', '<', now - timedelta(days=1)), ('has_message', '=', False),
        ]
        self.sudo().search(domain).unlink()
