# -*- coding: utf-8 -*-
"""HTTP entry points driving an ``ow_ai_chat`` session's turn.

Every route is ``type='jsonrpc', auth='user'`` and starts by checking
``ow_ai.group_ai_user`` (portal users are ``auth='user'`` too, but never
carry that group -- see ``security/ow_ai_groups.xml``) and loading the
channel *as the requesting user*: ``discuss.channel``'s own ``ir.access``
domain row already restricts a plain search to channels the user is a
member of (or otherwise allowed), so a non-member/non-existent id comes
back as an empty recordset here, surfaced as ``NotFound``. Only after that
access check does anything switch to ``sudo()`` for the session/channel
plumbing.
"""
from __future__ import annotations

from odoo import http
from odoo.exceptions import AccessError
from odoo.http import request
from werkzeug.exceptions import NotFound

from ..engine import loop

# The web client's tab id is a 16-character hex string (`uuid()` of @web/core/utils/strings).
_CLIENT_IDENTIFIER_MAX_LENGTH = 64


def _clean_client_identifier(client_identifier):
    """The browser tab id sent by the client, or None unless it is a short string."""
    if isinstance(client_identifier, str) and 0 < len(client_identifier) <= _CLIENT_IDENTIFIER_MAX_LENGTH:
        return client_identifier
    return None


class OwAiSessionController(http.Controller):

    # -- access helpers ---------------------------------------------------

    def _get_channel(self, channel_id):
        env = request.env
        if not env.user.has_group('ow_ai.group_ai_user'):
            raise AccessError(env._("You do not have access to the AI assistant."))
        channel = env['discuss.channel'].search([
            ('id', '=', channel_id),
            ('channel_type', '=', 'ow_ai_chat'),
        ])
        if not channel:
            raise NotFound()
        return channel

    def _get_root_session(self, channel, session_id=None):
        sessions = channel.sudo().ow_ai_session_ids
        if session_id is not None:
            sessions = sessions.filtered(lambda session: session.id == session_id)
        session = sessions[:1]
        if not session:
            raise NotFound()
        return session

    # -- routes -------------------------------------------------------------

    @http.route('/ow_ai/session/advance', type='jsonrpc', auth='user', methods=['POST'])
    def ow_ai_session_advance(self, channel_id, message_id, session_config=None,
                               current_view_info=None, client_identifier=None):
        env = request.env
        channel = self._get_channel(channel_id)
        message = env['mail.message'].browse(message_id)
        if not (message.model == 'discuss.channel' and message.res_id == channel.id
                and message.author_id == env.user.partner_id):
            raise AccessError(env._("You may only advance the chat on your own message."))

        session = channel.sudo().ow_ai_session_ids[:1]
        if not session:
            session = channel.sudo().ow_ai_agent_id._create_session(channel)
        parts = message._ow_ai_to_parts()
        loop.submit_user_message(
            session, env, parts, session_config=session_config,
            extra_context={
                'current_view_info': current_view_info,
                'client_identifier': _clean_client_identifier(client_identifier),
            })
        return {'loop_state': session.loop_state, 'session_id': session.id}

    @http.route('/ow_ai/session/resume', type='jsonrpc', auth='user', methods=['POST'])
    def ow_ai_session_resume(self, channel_id, session_id, resume_token, response, client_identifier=None):
        channel = self._get_channel(channel_id)
        session = self._get_root_session(channel, session_id)
        return loop.resume_pending(
            request.env, session, response, resume_token=resume_token,
            client_identifier=_clean_client_identifier(client_identifier))

    @http.route('/ow_ai/session/config', type='jsonrpc', auth='user', methods=['POST'])
    def ow_ai_session_config(self, channel_id, config):
        channel = self._get_channel(channel_id)
        session = self._get_root_session(channel)
        return session.sudo()._update_config(config)

    @http.route('/ow_ai/session/delete_chat', type='jsonrpc', auth='user', methods=['POST'])
    def ow_ai_session_delete_chat(self, channel_id):
        channel = self._get_channel(channel_id)
        channel.ow_ai_delete_chat()
        return True
