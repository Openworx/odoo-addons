# -*- coding: utf-8 -*-
"""``ow.ai.session`` (a conversation's persistent state) and
``ow.ai.session.event`` (its ordered, immutable message log).

The stateless request/tool-call loop lives in
``engine/direct.py::run_direct``/``run_structured``, the stateful chat loop
in ``engine/loop.py``/``engine/tool_batch.py`` (run by ``ow.ai.job``); the
methods here are thin wrappers plus the persistence/history-building
helpers both loops build on.
"""
from __future__ import annotations

import copy

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from ..engine import direct, history, loop, tool_batch
from ..engine.tools_registry import ToolContext
from ..utils import websearch

_LOOP_STATES = [
    ('ready', "Ready"),
    ('waiting_model', "Waiting for model"),
    ('waiting_confirmation', "Waiting for confirmation"),
    ('waiting_answer', "Waiting for answer"),
    ('waiting_client_result', "Waiting for client result"),
    ('waiting_child', "Waiting for child session"),
]


def _is_web_tool(tool):
    """The web search/page tools (``tools/web.py``), switched per chat."""
    return (tool.builtin_key or '').startswith('web.')


class OwAiSession(models.Model):
    _name = 'ow.ai.session'
    _description = 'AI Session'
    _order = 'id desc'

    channel_id = fields.Many2one('discuss.channel', ondelete='cascade', index='btree_not_null')
    agent_id = fields.Many2one('ow.ai.agent', required=True, ondelete='cascade')
    composer_id = fields.Many2one('ow.ai.composer', ondelete='set null')
    res_model = fields.Char()
    res_id = fields.Many2oneReference(model_field='res_model')
    event_ids = fields.One2many('ow.ai.session.event', 'session_id')
    # The conversation's content and what resumes it (the tool state, the
    # request context, a paused batch's tool arguments and results, its
    # resume token, the notification buffers, the last error) is
    # system-only, like the events' `metadata`: AI managers get the
    # operational fields (Reports > Sessions). The engine and the
    # controllers read the session through `sudo()`; the chat's member gets
    # what the client needs through the Store (`_store_session_fields`).
    state = fields.Json(default=dict, groups='base.group_system')
    loop_state = fields.Selection(_LOOP_STATES, required=True, default='ready', index=True)
    request_round = fields.Integer(default=0)
    request_round_limit = fields.Integer(default=0)
    request_user_id = fields.Many2one('res.users', ondelete='restrict')
    # A random id set for the whole turn (``secrets.token_hex(8)``, next to
    # ``request_round=1``) and cleared with it: an ``agent_round`` job
    # records it in its payload (``ow.ai.job._enqueue``) so a job left over
    # from an earlier turn of the same user can never be mistaken for the
    # current one, even though every turn's round numbering restarts at 1
    # (see ``ow.ai.job._is_current_round``).
    request_turn = fields.Char(groups='base.group_system')
    request_context = fields.Json(default=dict, groups='base.group_system')
    pending_tool_call = fields.Json(groups='base.group_system')
    resume_token = fields.Char(groups='base.group_system')
    auto_confirm = fields.Boolean()
    show_agent_steps = fields.Boolean(default=False)
    # The chat's "Web search" switch: set when a chat starts
    # (`ow.ai.agent._create_session`), off for every other session.
    web_search = fields.Boolean(default=False)
    turn_notifications = fields.Json(default=list, groups='base.group_system')
    turn_client_notifications = fields.Json(default=list, groups='base.group_system')
    last_error = fields.Char(groups='base.group_system')
    active = fields.Boolean(default=True)
    usage_ids = fields.One2many('ow.ai.usage', 'session_id')
    job_ids = fields.One2many('ow.ai.job', 'session_id')

    _loop_state_round_consistency = models.Constraint(
        "CHECK (loop_state = 'ready' OR "
        "(request_round >= 1 AND request_round_limit >= request_round AND request_user_id IS NOT NULL))",
        "A session that is not ready must have a positive round, a round "
        "limit at least as high, and a requesting user.",
    )
    _loop_state_waiting_consistency = models.Constraint(
        "CHECK (loop_state NOT IN ('waiting_confirmation', 'waiting_answer', 'waiting_client_result') OR "
        "(resume_token IS NOT NULL AND pending_tool_call IS NOT NULL))",
        "A session waiting on the user must have a resume token and a pending tool call.",
    )

    # -- history / events -----------------------------------------------

    def _get_history_messages(self) -> list:
        """Return this session's events, in order, as neutral messages."""
        self.ensure_one()
        events = self.event_ids.sorted(key=lambda event: (event.sequence, event.id))
        return history.events_to_messages(events)

    def _append_event(self, role, message, usage=None):
        """Append ``message`` (a neutral message dict) as the next event."""
        self.ensure_one()
        sequence = max(self.event_ids.mapped('sequence'), default=0) + 1
        return self.env['ow.ai.session.event'].create({
            'session_id': self.id,
            'sequence': sequence,
            'role': role,
            'metadata': message,
            'usage_id': usage.id if usage else False,
        })

    # -- context / tools ---------------------------------------------------

    @api.model
    def _get_request_context_snapshot(self) -> dict:
        env = self.env
        return {
            'allowed_company_ids': env.companies.ids,
            'company_id': env.company.id,
            'lang': env.user.lang,
            'tz': env.user.tz or 'UTC',
            'uid': env.uid,
        }

    def _get_bound_record(self, env=None):
        """The session's record in ``env`` (default: ``self.env``), or None when unset/deleted."""
        self.ensure_one()
        env = env if env is not None else self.env
        if not self.res_model or not self.res_id or self.res_model not in env:
            return None
        return env[self.res_model].browse(self.res_id).exists() or None

    def _get_context_input(self, message_parts=None) -> str:
        """Return the ``<odoo_context>`` block for this session's current state."""
        self.ensure_one()
        return history.context_input(self.env, record=self._get_bound_record(), session=self)

    def _build_tools_context(self, tool_call_id='', confirmed=False, *, env=None) -> ToolContext:
        """The ``ToolContext`` for one tool call, bound to ``env``.

        The chat loop always passes the requesting user's own environment:
        the session itself is usually ``sudo()``, and neither it nor any
        record in its environment may reach a tool. ``state`` is a copy the
        engine persists back only when the call succeeds (after a failed
        call, only its web call counts: ``tool_batch.advance_tool_batch``).
        """
        self.ensure_one()
        env = env if env is not None else self.env
        return ToolContext(
            env=env,
            session_id=self.id,
            agent_id=self.agent_id.id,
            tool_call_id=tool_call_id,
            state=copy.deepcopy(self.state or {}),
            tool_request_confirmed=confirmed,
            auto_confirm=self.auto_confirm,
            record=self._get_bound_record(env),
        )

    def _has_web_search(self):
        """Whether the web tools are on: the chat's switch, and a configured search service."""
        self.ensure_one()
        return self.web_search and websearch.is_configured(self.env)

    def _get_tools(self):
        """The ``ow.ai.tool`` records available for this session's next round.

        The union of ``state['available_tools']`` (by id) and the agent's
        default tools, deduplicated, limited by :meth:`_offered_tools`.
        """
        self.ensure_one()
        tool_ids = (self.state or {}).get('available_tools') or []
        tools = self.env['ow.ai.tool'].browse(tool_ids).exists() | self.agent_id._get_default_tools()
        return self._offered_tools(tools)

    def _offered_tools(self, tools):
        """``tools`` limited to the ones this session can offer the model:
        active only, with no ``model_name`` or one matching this session's
        bound model, and without the web tools while web search is off
        (:meth:`_has_web_search`)."""
        self.ensure_one()
        tools = tools.filtered('active')
        tools = tools.filtered(lambda tool: not tool.model_name or tool.model_name == self.res_model)
        if not self._has_web_search():
            tools = tools.filtered(lambda tool: not _is_web_tool(tool))
        return tools

    def _get_available_skills(self):
        """The agent's skills this session offers (the prompt's list, ``load_skills``).

        While web search is off, a skill whose every tool is a web tool is
        left out; a skill mixing web and other tools stays (:meth:`_get_tools`
        hides its web tools).
        """
        self.ensure_one()
        skills = self.agent_id._get_available_skills()
        if not self._has_web_search():
            skills = skills.filtered(
                lambda skill: not skill.tool_ids or not all(_is_web_tool(tool) for tool in skill.tool_ids))
        return skills

    def _prepare_tools(self):
        """Return the neutral tool list (see :meth:`_get_tools`) for the next round.

        When the session is interactive (bound to a channel), each tool's
        schema gets an extra, non-required ``tool_status`` string property.
        ``ask_user_question`` itself is dropped for non-interactive sessions
        (no channel: direct/automation mode) -- there is no one to answer it.
        """
        self.ensure_one()
        tools = self._get_tools()
        interactive = bool(self.channel_id)
        if not interactive:
            tools = tools.filtered(lambda tool: tool.builtin_key != 'interaction.ask_user_question')
        neutral_tools = []
        for tool in tools:
            neutral_tool = tool._to_neutral_tool()
            if interactive:
                schema = dict(neutral_tool['schema'])
                properties = dict(schema.get('properties') or {})
                properties['tool_status'] = {
                    'type': 'string',
                    'description': 'One short sentence telling the user what you are doing, '
                                    'e.g. "Looking up open invoices"',
                }
                schema['properties'] = properties
                neutral_tool = dict(neutral_tool, schema=schema)
            neutral_tools.append(neutral_tool)
        return neutral_tools

    # -- stateless direct response ------------------------------------------

    def _get_direct_response(self, instructions, message_parts, *, tools=None, record=None, agent=None,
                              schema=None, timeout=None, max_rounds=None, kind='direct', on_step=None,
                              tool_state=None) -> list:
        """Thin wrapper around :func:`engine.direct.run_direct`. See there for behaviour."""
        return direct.run_direct(
            self.env, self, instructions, message_parts, tools=tools, record=record, agent=agent,
            schema=schema, timeout=timeout, max_rounds=max_rounds, kind=kind, on_step=on_step,
            tool_state=tool_state)

    def _get_structured_response(self, instructions, message_parts, schema, **kw) -> dict:
        """Thin wrapper around :func:`engine.direct.run_structured`. See there for behaviour."""
        return direct.run_structured(self.env, self, instructions, message_parts, schema, **kw)

    # -- stateful chat loop ------------------------------------------------------

    def _finish_exchange(self, status='done', *, text=None, error_text=None):
        """End the current turn (see :func:`engine.loop.finish_exchange`).

        A ``failed`` turn without ``text`` posts a short apology quoting
        ``error_text`` (a user-facing reason, never a traceback).
        """
        self.ensure_one()
        if text is None and status == 'failed':
            text = loop.FAILED_ANSWER_TEXT.format(error=error_text or loop.GENERIC_ERROR_TEXT)
        loop.finish_exchange(self, text or '', status=status, error_text=error_text)

    def _abort_pending(self, reason='skipped'):
        """Drop a paused tool batch (see :func:`engine.tool_batch.abort_pending`)."""
        self.ensure_one()
        tool_batch.abort_pending(self, reason=reason)

    # -- admin views (Reports › Sessions) ---------------------------------------

    def _check_manager(self):
        if not self.env.user.has_group('ow_ai.group_ai_manager'):
            raise AccessError(self.env._("Only AI managers can do this."))

    def action_abort_pending(self):
        """Manager button: drop a paused confirmation/question/client-tool call."""
        self._check_manager()
        for session in self:
            if session.loop_state in ('waiting_confirmation', 'waiting_answer', 'waiting_client_result'):
                session.sudo()._abort_pending(reason='cancelled')

    def action_open_chat(self):
        """Manager button: open this session's channel in Discuss."""
        self.ensure_one()
        self._check_manager()
        if not self.channel_id:
            raise UserError(self.env._("This session has no chat channel."))
        return {
            'type': 'ir.actions.client',
            'tag': 'mail.action_discuss',
            'context': {'active_id': f'discuss.channel_{self.channel_id.id}'},
        }


def _summarize_metadata(metadata: dict) -> str:
    """First ~120 characters of an event's text, or its tool call/result names.

    Used by ``ow.ai.session.event.summary`` (Reports › Sessions, "Events"
    tab): a short at-a-glance label without opening the raw ``metadata``.
    """
    bits = []
    for part in metadata.get('content') or []:
        part_type = part.get('type')
        if part_type == 'text' and part.get('text'):
            bits.append(part['text'])
        elif part_type == 'tool_call':
            bits.append(part.get('name') or '')
        elif part_type == 'tool_result':
            bits.append(part.get('tool_name') or '')
    text = ' '.join(bit for bit in bits if bit)
    return text[:120]


class OwAiSessionEvent(models.Model):
    _name = 'ow.ai.session.event'
    _description = 'AI Session Event'
    _order = 'sequence, id'

    session_id = fields.Many2one('ow.ai.session', required=True, ondelete='cascade', index=True)
    sequence = fields.Integer(required=True)
    role = fields.Selection([('user', "User"), ('assistant', "Assistant")], required=True)
    # The transcript itself (and its excerpt, `summary`) is system-only: AI
    # managers see the session/event lists (Reports) but not what was said
    # or which tool data went back and forth. The engine reads events
    # through the sudo session only.
    metadata = fields.Json(required=True, groups='base.group_system')
    usage_id = fields.Many2one('ow.ai.usage', ondelete='set null')
    summary = fields.Char(compute='_compute_summary', groups='base.group_system')

    @api.depends('metadata')
    def _compute_summary(self):
        for event in self:
            event.summary = _summarize_metadata(event.metadata or {})

    def get_tool_params(self, call_id: str) -> dict:
        """Return the arguments of the ``tool_call`` part with ``call_id`` in this event.

        An event's ``metadata`` is system-only (field ``groups``), so this
        checks the *caller's* right to read the underlying conversation
        first, then reads the (sudo'd) event data: a channel session
        requires read access to that ``discuss.channel`` (the chat's own
        member); a session with no channel (e.g. a background/direct
        session) requires the system group, like reading ``metadata``
        directly. ``tool_status`` and ``explanation`` are stripped from the
        result: they are UI-only, not part of the tool's real arguments.
        """
        self.ensure_one()
        event = self.sudo()
        channel = event.session_id.channel_id
        if channel:
            self.env['discuss.channel'].browse(channel.id).check_access('read')
        elif not self.env.user.has_group('base.group_system'):
            raise AccessError(self.env._("You do not have access to this conversation."))

        for part in (event.metadata or {}).get('content', []):
            if part.get('type') == 'tool_call' and part.get('call_id') == call_id:
                args = dict(part.get('args') or {})
                args.pop('tool_status', None)
                args.pop('explanation', None)
                return args
        return {}
