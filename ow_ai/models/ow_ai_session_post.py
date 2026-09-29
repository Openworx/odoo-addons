# -*- coding: utf-8 -*-
"""Posting/publishing helpers for ``ow.ai.session``.

Split out of ``ow_ai_session.py`` (kept < 300 lines) as a second
``_inherit='ow.ai.session'`` class in a separate module. Every helper here
posts through the channel with ``sudo()`` (the agent is never a real
member-with-write-access from the ORM's point of view -- membership is
locked down by ``discuss_channel_member.py``), except ``_post_user_choice``
which posts as the requesting user so the message is genuinely theirs.
"""
from __future__ import annotations

from odoo import models
from odoo.addons.mail.tools.discuss import Store

from ..engine.html_output import agent_step_markup, notification_markup, sanitize_ai_html, tool_summary_markup

# Loop states in which the session is waiting on the user/client for an
# interaction; `resume_token` is only meaningful (and only published to the
# Store) while in one of these -- publishing it otherwise would expose a
# stale token nothing can still resume against.
_INTERACTION_STATES = ('waiting_confirmation', 'waiting_answer', 'waiting_client_result')
_CONFIG_KEYS = ('auto_confirm', 'show_agent_steps', 'web_search')
AUTO_CONFIRM_ENABLED_NOTE = "Auto-approval enabled for this chat"
AUTO_CONFIRM_DISABLED_NOTE = "Auto-approval disabled for this chat"


class OwAiSessionPost(models.Model):
    _inherit = 'ow.ai.session'

    # -- posting ------------------------------------------------------------

    def _ow_ai_post(self, *, body, message_type, subtype_xmlid, author_partner=None, is_internal=False):
        self.ensure_one()
        channel = self.channel_id.sudo().with_context(mail_create_nosubscribe=True, mail_post_autofollow=False)
        return channel.message_post(
            body=body,
            author_id=(author_partner or self.agent_id.partner_id).id,
            message_type=message_type,
            subtype_xmlid=subtype_xmlid,
            is_internal=is_internal,
            silent=True,
        )

    def _post_answer(self, html):
        """Post the agent's final answer for this turn."""
        self.ensure_one()
        return self._ow_ai_post(body=html, message_type='comment', subtype_xmlid='mail.mt_comment')

    def _post_agent_step(self, html, event_id):
        """Post a step the agent explains it is taking (internal note)."""
        self.ensure_one()
        body = agent_step_markup(html, event_id)
        return self._ow_ai_post(
            body=body, message_type='comment', subtype_xmlid='mail.mt_note', is_internal=True)

    def _post_tool_summary(self, icon, text, call_id, event_id=None):
        """Post a one-line tool-call summary (internal note).

        ``event_id`` is the assistant event holding the call, so the UI can
        fetch the call's arguments (``ow.ai.session.event.get_tool_params``).
        """
        self.ensure_one()
        body = tool_summary_markup(icon, text, call_id, event_id)
        return self._ow_ai_post(
            body=body, message_type='notification', subtype_xmlid='mail.mt_note', is_internal=True)

    def _post_notification(self, kind, html):
        """Post a UI notification (e.g. a preview or config-change note).

        ``kind`` is ``note`` or ``preview``; it becomes the ``data-oe-type``
        marker (``ow_ai_note``/``ow_ai_preview``).
        """
        self.ensure_one()
        if kind not in ('note', 'preview'):
            raise ValueError(f"Invalid notification kind: {kind!r}")
        body = notification_markup(f'ow_ai_{kind}', html)
        return self._ow_ai_post(body=body, message_type='notification', subtype_xmlid='mail.mt_comment')

    def _update_config(self, config):
        """Update ``auto_confirm``/``show_agent_steps``/``web_search`` from ``config`` (other keys ignored).

        Toggling ``auto_confirm`` posts a short note; the session's new
        state is always republished. Returns the resulting config dict.
        """
        self.ensure_one()
        config = config or {}
        vals = {key: bool(config[key]) for key in _CONFIG_KEYS if key in config}
        if 'auto_confirm' in vals and vals['auto_confirm'] != self.auto_confirm:
            note = AUTO_CONFIRM_ENABLED_NOTE if vals['auto_confirm'] else AUTO_CONFIRM_DISABLED_NOTE
            self.write(vals)
            self._post_notification('note', note)
        elif vals:
            self.write(vals)
        self._publish_state()
        return self._config_values()

    def _config_values(self):
        """The chat settings as the client reads them (Store ``config``)."""
        self.ensure_one()
        return {key: self[key] for key in _CONFIG_KEYS}

    def _post_user_choice(self, text):
        """Post the user's own choice/answer, as themselves (not sudo)."""
        self.ensure_one()
        if not self.request_user_id:
            return self.env['mail.message']
        channel = self.channel_id.with_user(self.request_user_id)
        # `silent` is a `discuss.channel` notification parameter (see its
        # `_get_notify_valid_parameters`): it flags the channel's bus
        # `new_message` payload so no client alerts on it.
        return channel.message_post(
            body=text, message_type='comment', subtype_xmlid='mail.mt_comment', silent=True)

    # -- typing / state -------------------------------------------------------

    def _notify_typing(self, is_typing: bool):
        self.ensure_one()
        if not self.channel_id:
            return
        member = self.channel_id.sudo().channel_member_ids._ow_ai_agent_member()
        member._notify_typing(is_typing)

    def _publish_state(self):
        self.ensure_one()
        if not self.channel_id:
            return
        Store(bus_channel=self.channel_id).add(self, self._store_session_fields()).bus_send()

    # -- Store ----------------------------------------------------------------

    def _store_session_fields(self):
        return [
            Store.One('channel_id', []),
            Store.One('agent_id', ['name', 'partner_id']),
            'res_model',
            'res_id',
            Store.One('composer_id', ['interface_key']),
            Store.Attr('config', value=lambda session: session._config_values()),
            'loop_state',
            Store.Attr('resume_token', predicate=lambda session: session.loop_state in _INTERACTION_STATES),
            Store.Attr('userInputRequest', value=lambda session: session._ow_ai_user_input_request()),
            Store.Attr('clientToolRequest', value=lambda session: session._ow_ai_client_tool_request()),
            Store.Attr('toolStatus', value=lambda session: session._ow_ai_tool_status()),
        ]

    def _ow_ai_user_input_request(self):
        self.ensure_one()
        if self.loop_state not in ('waiting_confirmation', 'waiting_answer'):
            return False
        request = (self.pending_tool_call or {}).get('user_input_request') or {}
        return {
            'type': request.get('type'),
            # nested in an attr: tag it ourselves (the Store only tags top-level
            # Markup values) so the client's `fields.Html` keeps it as HTML
            'body': ['markup', str(sanitize_ai_html(request.get('body') or '', base_url=self.get_base_url()))],
            'choices': request.get('choices') or [],
            'labels': request.get('labels') or {},
            'multiSelect': request.get('multi_select', False),
            'allowFreeText': request.get('allow_free_text', False),
            'resumeToken': self.resume_token,
        }

    def _ow_ai_client_tool_request(self):
        self.ensure_one()
        if self.loop_state != 'waiting_client_result':
            return False
        client_tool = (self.pending_tool_call or {}).get('client_tool') or {}
        return {
            'name': client_tool.get('name'),
            'params': client_tool.get('params') or {},
            'resumeToken': self.resume_token,
        }

    def _ow_ai_tool_status(self):
        self.ensure_one()
        if self.loop_state == 'ready':
            return False
        events = self.event_ids.sorted(key=lambda event: (event.sequence, event.id))
        assistant_events = [event for event in events if event.role == 'assistant']
        if not assistant_events:
            return False
        tool_calls = [
            part for part in (assistant_events[-1].metadata or {}).get('content', [])
            if part.get('type') == 'tool_call'
        ]
        if not tool_calls:
            return False
        index = min((self.pending_tool_call or {}).get('index') or 0, len(tool_calls) - 1)
        return tool_calls[index].get('args', {}).get('tool_status') or False
