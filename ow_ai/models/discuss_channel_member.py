# -*- coding: utf-8 -*-
"""``discuss.channel.member`` extension: block invitations to ``ow_ai_chat``.

Members are only ever created by ``ow.ai.agent._create_chat_channel``, as
the superuser with the ``ow_ai_channel_setup`` context key; any other
attempt -- direct create, or through ``discuss.channel._add_members`` -- is
rejected. The key alone is not enough: an RPC client can send any context
key, not ``sudo()``.
"""
from __future__ import annotations

from odoo import api, models
from odoo.exceptions import UserError


class DiscussChannelMember(models.Model):
    _inherit = 'discuss.channel.member'

    @api.model_create_multi
    def create(self, vals_list):
        if not (self.env.su and self.env.context.get('ow_ai_channel_setup')):
            channel_ids = {vals['channel_id'] for vals in vals_list if vals.get('channel_id')}
            if channel_ids:
                channels = self.env['discuss.channel'].sudo().browse(channel_ids)
                if any(channel.channel_type == 'ow_ai_chat' for channel in channels):
                    raise UserError(self.env._("AI chats cannot have additional members."))
        return super().create(vals_list)

    def _ow_ai_agent_member(self):
        """Return the member(s) of ``self`` whose partner is their channel's agent."""
        return self.filtered(
            lambda member: member.partner_id
            # sudo: ow_ai_agent_id has groups=fields.NO_ACCESS.
            and member.partner_id == member.channel_id.sudo().ow_ai_agent_id.partner_id)
