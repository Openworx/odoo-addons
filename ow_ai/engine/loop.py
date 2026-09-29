# -*- coding: utf-8 -*-
"""The stateful agent loop behind an ``ow_ai_chat`` conversation.

A turn starts in :func:`submit_user_message` (HTTP request): the user's
message is stored as an event, the session moves to ``waiting_model`` and an
``agent_round`` job is queued. Each job runs :func:`run_round` on a cron
worker: one model request, then either the final answer
(:func:`finish_exchange`) or a batch of tool calls
(``tool_batch.advance_tool_batch``) which ends in the next round, a pause
for the user (confirmation/question card, client tool) or the answer.

Every function takes ``env`` -- the requesting user's own environment
(``su=False``, never the cron's superuser environment); tools only ever see
that ``env`` -- and ``session``, a ``sudo()`` ``ow.ai.session`` used for the
session/event/channel plumbing only.
"""
from __future__ import annotations

import copy
import logging
import secrets

from odoo.exceptions import AccessError, UserError

from ..provider.client import ProviderClient
from ..provider.errors import AIProviderError, BadRequest
from ..provider.mapping import from_openai_response, to_openai_messages, to_openai_tools
from ..utils.prompts import CHANNEL_TITLE_INSTRUCTIONS, ROUND_LIMIT_WARNING
from . import history, tool_batch, usage_log
from .html_output import markdown_to_plaintext_title, render_ai_markdown, sanitize_ai_html
from .types import message_text, text_part, user_message

_logger = logging.getLogger(__name__)

INTERACTION_STATES = ('waiting_confirmation', 'waiting_answer', 'waiting_client_result')
EMPTY_ANSWER_TEXT = "I could not come up with an answer. Please try rephrasing your request."
FAILED_ANSWER_TEXT = "I could not complete this request: {error}"
GENERIC_ERROR_TEXT = "Something went wrong while answering."
_TITLE_INPUT_MAX_CHARS = 2000
_TITLE_MAX_CHARS = 60
# A cap, not a target: a reasoning model (e.g. openai/gpt-6-luna) spends its
# hidden reasoning from the same budget, and 30 tokens left it no room for
# the title itself. The title is cut to _TITLE_MAX_CHARS anyway.
_TITLE_MAX_TOKENS = 400
_DEFAULT_MAX_HISTORY_CHARS = 200000


# -- turn start -----------------------------------------------------------------

def submit_user_message(session, user_env, parts, *, session_config=None, extra_context=None):
    """Store ``parts`` as the user's next message and queue the first round.

    ``session`` is the sudo root session of the chat, ``user_env`` the
    posting user's environment. A pending card is skipped (see
    ``tool_batch.abort_pending``); a session still waiting on the model
    refuses the message. Returns the ``agent_round`` job.
    """
    session.ensure_one()
    if session.loop_state != 'ready':
        if session.loop_state not in INTERACTION_STATES:
            raise UserError(user_env._("The assistant is still answering. Please wait."))
        tool_batch.abort_pending(session, reason='skipped')
    if session_config:
        _apply_session_config(session, session_config)

    is_first_message = not session.event_ids
    message = user_message(*parts)
    session._append_event('user', message)

    state = dict(session.state or {})
    state.setdefault('loaded_skills', [])
    if 'available_tools' not in state:
        state['available_tools'] = session.agent_id._get_default_tools().ids
    request_context = user_env['ow.ai.session']._get_request_context_snapshot()
    request_context.update({key: value for key, value in (extra_context or {}).items() if value is not None})
    max_rounds = user_env['ir.config_parameter'].sudo().get_int('ow_ai.max_rounds', 30)
    session.write({
        'state': state,
        'request_round': 1,
        'request_round_limit': max(max_rounds, 1),
        'request_user_id': user_env.uid,
        'request_turn': secrets.token_hex(8),
        'request_context': request_context,
        'loop_state': 'waiting_model',
        'pending_tool_call': False,
        'resume_token': False,
        'turn_notifications': [],
        'turn_client_notifications': [],
        'last_error': False,
    })
    session._notify_typing(True)
    session._publish_state()

    jobs = user_env['ow.ai.job']
    job = jobs._enqueue(session, 'agent_round', user=user_env.user, context=request_context)
    if is_first_message and _channel_needs_title(session):
        text = message_text(message)[:_TITLE_INPUT_MAX_CHARS]
        if text.strip():
            jobs._enqueue(session, 'channel_title', user=user_env.user, context=request_context,
                          payload={'text': text})
    return job


def _apply_session_config(session, config):
    vals = {key: bool(config[key]) for key in ('auto_confirm', 'show_agent_steps') if key in config}
    if vals:
        session.write(vals)


def _channel_needs_title(session):
    """True while the chat still carries its default (agent) name."""
    channel = session.channel_id
    if not channel:
        return False
    name = (channel.name or '').strip()
    return not name or name == session.agent_id.name


def resume_pending(env, session, response, *, resume_token, client_identifier=None):
    """Resume a paused tool batch: see :func:`tool_batch.resume_pending`.

    Re-exported so callers (the HTTP controller) only deal with ``loop``.
    """
    return tool_batch.resume_pending(
        env, session, response, resume_token=resume_token, client_identifier=client_identifier)


def abort_pending(session, reason='skipped'):
    """Drop a paused tool batch: see :func:`tool_batch.abort_pending`."""
    tool_batch.abort_pending(session, reason=reason)


# -- one model round ------------------------------------------------------------------

def run_round(env, session, job):
    """Send the conversation to the model once and act on its reply."""
    agent = session.agent_id.with_context(lang=env.lang)
    state = session.state or {}
    params = env['ir.config_parameter'].sudo()
    client = ProviderClient.from_env(env)

    max_chars = params.get_int('ow_ai.max_history_chars', _DEFAULT_MAX_HISTORY_CHARS)
    messages = history.truncate_history(session._get_history_messages(), max_chars=max_chars)
    record = _readable_bound_record(env, session)
    extra_texts = [history.context_input(env, record=record, session=session)]
    if session.request_round >= session.request_round_limit - 2:
        extra_texts.append(ROUND_LIMIT_WARNING(session.request_round_limit - session.request_round + 1))
    messages = _append_to_last_user_message(messages, extra_texts)

    tools = session._prepare_tools()
    composer = session.composer_id.with_context(lang=env.lang)
    instructions = agent._get_instructions(
        loaded_skill_ids=state.get('loaded_skills') or [],
        usage_context=composer.default_prompt or None,
        record=record)

    model = agent._get_model()
    try:
        raw = client.chat_completion(
            model=model,
            messages=to_openai_messages(instructions, messages),
            tools=to_openai_tools(tools) if tools else None,
            **agent._get_model_options(),
        )
    except AIProviderError as exc:
        usage_log.log_error(env, exc, kind='chat', model=model, agent=agent, session=session)
        raise

    result = from_openai_response(raw)
    usage = usage_log.log_usage(
        env, kind='chat', model=result['model'], usage=result['usage'], agent=agent, session=session,
        request_id=result['request_id'], latency_ms=raw.get('_ow_ai_latency_ms', 0))
    event = session._append_event('assistant', result['message'], usage=usage)
    continue_after_completion(env, session, result['message'], event)


def _readable_bound_record(env, session):
    """The session's record as the user sees it, or None (gone/unreadable)."""
    record = session._get_bound_record(env)
    if record is None:
        return None
    try:
        record.check_access('read')
    except AccessError:
        return None
    return record


def _append_to_last_user_message(messages, texts):
    """Return ``messages`` with ``texts`` appended to a copy of the last user message.

    Only the request gets them: the stored event (and the cached Json value
    it was read from) is left untouched.
    """
    messages = list(messages)
    extra_parts = [text_part(text) for text in texts if text]
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get('role') == 'user':
            message = copy.deepcopy(messages[index])
            message['content'] = list(message.get('content') or []) + extra_parts
            messages[index] = message
            return messages
    messages.append(user_message(*extra_parts))
    return messages


def continue_after_completion(env, session, assistant_msg, event):
    """Answer the user, or start running the tool calls the model asked for.

    At the last allowed round, any tool call other than ``ask_user_question``
    is refused outright (never run): the model gets no further round to act
    on its result anyway, so the turn ends with the "maximum number of
    steps" message right away instead of spending that last round on a tool
    call whose result nobody will read. A round of only ``ask_user_question``
    calls still runs and pauses the turn as usual -- the model is not
    forced into asking (no ``tool_choice`` is sent for this); only the
    "steps left" warning in the previous round's prompt nudges it there.
    """
    content = assistant_msg.get('content') or []
    text = message_text(assistant_msg)
    calls = [part for part in content if part.get('type') == 'tool_call']
    if not calls:
        finish_exchange(session, text)
        return
    if session.request_round >= session.request_round_limit and any(
            call.get('name') != 'ask_user_question' for call in calls):
        finish_exchange(session, tool_batch.MAX_STEPS_TEXT)
        return
    if text.strip():
        session._post_agent_step(render_ai_markdown(text, base_url=session.get_base_url()), event.id)
    session.write({'pending_tool_call': {'calls': calls, 'results': [], 'index': 0, 'assistant_event_id': event.id}})
    tool_batch.advance_tool_batch(env, session)


# -- turn end ----------------------------------------------------------------------

def finish_exchange(session, text, *, status='done', error_text=None):
    """Post ``text`` as the agent's answer and return the session to ``ready``.

    ``status`` is ``done`` or ``failed``; ``error_text`` (the short,
    user-facing reason of a failure) is stored as ``last_error``.
    """
    if session.channel_id:
        if not (text or '').strip():
            text = EMPTY_ANSWER_TEXT
        session._post_answer(render_ai_markdown(text, base_url=session.get_base_url()))
        post_turn_notifications(session)
    session.write({
        'loop_state': 'ready',
        'request_round': 0,
        'request_round_limit': 0,
        'request_user_id': False,
        'request_turn': False,
        'pending_tool_call': False,
        'resume_token': False,
        'turn_notifications': [],
        'turn_client_notifications': [],
        'last_error': error_text or False,
    })
    if status == 'failed':
        _logger.info("ow_ai session %s: turn failed: %s", session.id, error_text)
    session._notify_typing(False)
    session._publish_state()


def post_turn_notifications(session):
    """Post the notifications the tools of this turn collected (e.g. "Created X")
    and flush any non-blocking client-side action they queued (e.g. "reload")."""
    base_url = session.get_base_url()
    for notification in session.turn_notifications or []:
        body = sanitize_ai_html(notification.get('body') or '', base_url=base_url)
        if body:
            session._post_notification(notification.get('kind') or 'note', body)
    _send_turn_client_notifications(session)


def _send_turn_client_notifications(session):
    """Send this turn's non-blocking client actions over the bus, if any.

    Unlike a paused `client_tool` (`tool_batch._pause_for_client`), this
    never waits for a result and carries `blocking: False`: the browser is
    free to ignore it (e.g. no matching view open) without breaking the
    turn, which already finished.
    """
    client_tools = session.turn_client_notifications or []
    if not client_tools or not session.channel_id:
        return
    session.channel_id._bus_send(tool_batch.CLIENT_TOOLS_NOTIFICATION, {
        'session_id': session.id,
        'client_tools': [
            {'name': item.get('name'), 'params': item.get('args', item.get('params')) or {}}
            for item in client_tools
        ],
        'blocking': False,
        'client_identifier': tool_batch.client_identifier(session),
    })


# -- chat title ----------------------------------------------------------------------

def run_channel_title(env, session, job):
    """Name a fresh chat after its first message. Never fails the chat itself."""
    text = (job.payload or {}).get('text') or ''
    if not text.strip() or not _channel_needs_title(session):
        return
    agent = session.agent_id
    model = agent._get_model()
    messages = to_openai_messages(CHANNEL_TITLE_INSTRUCTIONS, [user_message(text_part(text))])
    try:
        client = ProviderClient.from_env(env)
        try:
            raw = client.chat_completion(model=model, messages=messages, max_tokens=_TITLE_MAX_TOKENS)
        except BadRequest as exc:
            # Some models refuse `max_tokens` (they want `max_completion_tokens`):
            # ask once more without the cap.
            usage_log.log_error(env, exc, kind='title', model=model, agent=agent, session=session)
            raw = client.chat_completion(model=model, messages=messages)
    except AIProviderError as exc:
        usage_log.log_error(env, exc, kind='title', model=model, agent=agent, session=session)
        _logger.warning("ow_ai session %s: could not generate a chat title: %s", session.id, exc.code)
        return

    result = from_openai_response(raw)
    usage_log.log_usage(
        env, kind='title', model=result['model'], usage=result['usage'], agent=agent, session=session,
        request_id=result['request_id'], latency_ms=raw.get('_ow_ai_latency_ms', 0))
    title = markdown_to_plaintext_title(message_text(result['message']))[:_TITLE_MAX_CHARS].strip()
    if title and _channel_needs_title(session):
        # `name` is one of discuss.channel's bus-synced fields
        # (`_sync_field_names`): the write itself pushes the new name to
        # every open client of the channel.
        session.channel_id.sudo().write({'name': title})
