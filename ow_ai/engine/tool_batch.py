# -*- coding: utf-8 -*-
"""Running the batch of tool calls from one assistant message, pausing it
for the user (confirmation/question card, client-side tool), resuming it
and aborting it.

The batch lives in ``session.pending_tool_call`` so it survives between
the job that started it and the HTTP request that resumes it::

    {'calls': [tool_call parts], 'results': [{'call', 'response', 'success', 'parts'}],
     'index': <next call to run>, 'assistant_event_id': <event id>,
     'final_message': <str, optional>,
     'pins': {call_id: <ToolContext.pending_pin dict>, ...},  # only for a currently-paused call
     'user_input_request': {...} | 'client_tool': {...}   # only while paused}

A paused call keeps its ``index``: a confirmed resume re-runs exactly that
persisted call (same arguments) with ``tool_request_confirmed=True``; the
other resumes record its result without re-running it.
"""
from __future__ import annotations

import copy
import secrets

from odoo.exceptions import AccessError

from . import history, loop, user_input
from .html_output import input_card_markup, sanitize_ai_html
from .tools_registry import ToolResult, run_tool

CLIENT_TOOLS_NOTIFICATION = 'ow_ai.session/client_tools'
MAX_STEPS_TEXT = "I reached the maximum number of steps for this request. Please ask again more specifically."
AUTO_CONFIRM_NOTE = "Auto-approval enabled for this chat"
ABORT_TEXTS = {
    'skipped': "Aborted: the user sent a new message.",
    'expired': "Aborted: the user did not respond in time.",
    'cancelled': "Aborted: cancelled by an administrator.",
}
_ACCEPTED_KINDS = {
    'waiting_confirmation': ('confirmation', 'skip'),
    'waiting_answer': ('question', 'skip'),
    'waiting_client_result': ('client_result', 'client_error'),
}
_DEFAULT_MAX_TOOL_CALLS = 20


# -- running the batch -----------------------------------------------------------------

def advance_tool_batch(env, session, *, confirmed=False):
    """Run the pending calls from ``index`` on until the batch ends or pauses.

    ``confirmed`` only applies to the first call run (the one the user just
    approved); later calls are confirmed only by ``session.auto_confirm``.
    """
    pending = copy.deepcopy(session.pending_tool_call or {})
    pending.pop('user_input_request', None)
    pending.pop('client_tool', None)
    calls = pending.get('calls') or []
    results = pending.setdefault('results', [])
    limit = env['ir.config_parameter'].sudo().get_int('ow_ai.max_tool_calls_per_round', _DEFAULT_MAX_TOOL_CALLS)
    tools_by_name = {tool.tool_name: tool for tool in session._get_tools()}

    index = pending.get('index', 0)
    while index < len(calls):
        call = calls[index]
        call_id = call.get('call_id', '')
        tool = tools_by_name.get(call.get('name'))
        if index >= limit:
            _add_result(results, call, ToolResult(
                f"Too many tool calls in one step; call at most {limit}.", success=False))
        elif tool is None:
            _add_result(results, call, ToolResult(f"Unknown tool '{call.get('name')}'", success=False))
        else:
            ctx = session._build_tools_context(
                tool_call_id=call_id, confirmed=confirmed or session.auto_confirm, env=env)
            if confirmed:
                # `confirmed` (the parameter, before OR-ing with
                # `session.auto_confirm` above) is only ever True for the
                # one call actually being resumed right now -- see
                # `ToolContext.pending_pin`'s docstring.
                ctx.pending_pin = (pending.get('pins') or {}).get(call_id)
            result = run_tool(tool, call.get('args') or {}, ctx)
            # This call is resolved (or about to be re-pinned below if it
            # paused again): whatever was pinned for it no longer applies.
            pins = dict(pending.get('pins') or {})
            pins.pop(call_id, None)
            pending['pins'] = pins
            if result.success:
                session.write({
                    'state': ctx.state,
                    'turn_notifications': list(session.turn_notifications or []) + list(ctx.notifications),
                    'turn_client_notifications':
                        list(session.turn_client_notifications or []) + list(ctx.client_notifications),
                })
                if ctx.user_input_request:
                    if ctx.pending_pin is not None:
                        pending['pins'][call_id] = ctx.pending_pin
                    _pause_for_user(session, pending, index, ctx.user_input_request)
                    return
                if ctx.client_tools:
                    _pause_for_client(session, pending, index, ctx.client_tools)
                    return
                if ctx.final_message:
                    pending['final_message'] = ctx.final_message
            _add_result(results, call, result)
            _post_tool_summary(env, session, tool, result, call, pending.get('assistant_event_id'))
        confirmed = False
        index += 1
        pending['index'] = index

    session.write({'pending_tool_call': pending})
    finish_tool_batch(env, session)


def _add_result(results, call, result):
    results.append({
        'call': call,
        'response': result.response,
        'success': result.success,
        'parts': list(result.parts or []),
    })


def _result_pairs(results):
    return [
        (item['call'], ToolResult(response=item['response'], success=item['success'], parts=item.get('parts') or None))
        for item in results
    ]


def _post_tool_summary(env, session, tool, result, call, event_id):
    summary = result.summary or {}
    icon = summary.get('icon') or ('settings' if result.success else 'warning')
    text = summary.get('text') or f"Used {tool.with_context(lang=env.lang).name}"
    session._post_tool_summary(icon, text, call.get('call_id', ''), event_id=event_id)


def _pause_for_user(session, pending, index, request):
    request = dict(request)
    loop_state = 'waiting_confirmation' if request.get('type') == 'confirmation' else 'waiting_answer'
    pending.update(index=index, user_input_request=request)
    session.write({
        'loop_state': loop_state,
        'resume_token': secrets.token_urlsafe(32),
        'pending_tool_call': pending,
    })
    body = sanitize_ai_html(request.get('body') or '', base_url=session.get_base_url())
    if body:
        # Posted so the chat history keeps the card's text; the live card
        # itself is rendered from the session's Store data.
        session._post_answer(input_card_markup(body))
    session._notify_typing(False)
    session._publish_state()


def _pause_for_client(session, pending, index, client_tools):
    tools = [
        {'name': item.get('name'), 'params': item.get('params', item.get('args')) or {}}
        for item in client_tools
    ]
    pending.update(index=index, client_tool=tools[0])
    session.write({
        'loop_state': 'waiting_client_result',
        'resume_token': secrets.token_urlsafe(32),
        'pending_tool_call': pending,
    })
    if session.channel_id:
        session.channel_id._bus_send(CLIENT_TOOLS_NOTIFICATION, {
            'session_id': session.id,
            'client_tools': tools,
            'resume_token': session.resume_token,
            'client_identifier': client_identifier(session),
        })
    session._notify_typing(False)
    session._publish_state()


def client_identifier(session):
    """The browser tab that sent the turn's message (``/ow_ai/session/advance``)
    or, once it answered, the tab that answered the turn's last card or
    client tool request (``/ow_ai/session/resume``); False when none did.

    Sent with ``ow_ai.session/client_tools`` so only that tab runs the
    tools; without one, any tab showing the chat may.
    """
    return (session.request_context or {}).get('client_identifier') or False


def finish_tool_batch(env, session):
    """Store the batch's results and queue the next round (or end the turn)."""
    pending = session.pending_tool_call or {}
    session._append_event('user', history.tool_results_message(_result_pairs(pending.get('results') or [])))
    session.write({'pending_tool_call': False})
    if pending.get('final_message'):
        loop.finish_exchange(session, pending['final_message'])
        return
    if session.request_round >= session.request_round_limit:
        loop.finish_exchange(session, MAX_STEPS_TEXT)
        return
    session.write({
        'request_round': session.request_round + 1,
        'loop_state': 'waiting_model',
        'resume_token': False,
    })
    session._notify_typing(True)
    session._publish_state()
    env['ow.ai.job']._enqueue(
        session, 'agent_round', user=session.request_user_id, context=session.request_context or {})


# -- resuming -------------------------------------------------------------------------

def resume_pending(env, session, response, *, resume_token, client_identifier=None):
    """Consume the user's (or browser's) response to the paused tool call.

    ``response`` is ``{'kind': 'confirmation'|'question'|'skip'|
    'client_result'|'client_error', 'value': ...}``. ``resume_token`` must
    match the session's (single-use) token. Returns ``{'interactionConsumed':
    bool, 'loop_state': str}``; a response that does not fit the session's
    current state (or a wrong/used token) is not consumed and changes
    nothing. Invalid values raise ``UserError`` before anything changes.
    Only the requesting user may resume.

    ``client_identifier`` names the browser tab that answered: once the
    response is consumed, the rest of the turn's client tools target that
    tab (see :func:`client_identifier`) instead of the one that sent the
    message.
    """
    response = response or {}
    kind = response.get('kind')
    value = response.get('value')
    not_consumed = {'interactionConsumed': False, 'loop_state': session.loop_state}
    if kind not in _ACCEPTED_KINDS.get(session.loop_state, ()) or not session.resume_token:
        return not_consumed
    if not secrets.compare_digest(str(session.resume_token), str(resume_token)):
        return not_consumed
    if session.request_user_id.id != env.uid:
        raise AccessError(env._("Only the person who made this request can answer it."))
    if not session.try_lock_for_update(allow_referencing=True):
        return not_consumed

    pending = copy.deepcopy(session.pending_tool_call or {})
    calls = pending.get('calls') or []
    index = pending.get('index', 0)
    if index >= len(calls):
        return not_consumed
    request = pending.get('user_input_request') or {}
    if session.loop_state == 'waiting_confirmation':
        choice_text, confirmed, auto_confirm, result = user_input.confirmation_outcome(env, kind, value, request)
    elif session.loop_state == 'waiting_answer':
        choice_text, result = user_input.question_outcome(env, kind, value, request)
        confirmed = auto_confirm = False
    else:
        choice_text, result = None, user_input.client_outcome(env, kind, value)
        confirmed = auto_confirm = False

    # Single use: the token is gone before anything else happens, and the
    # session is busy (not resumable) while the batch continues.
    vals = {'resume_token': False, 'loop_state': 'waiting_model'}
    if client_identifier:
        vals['request_context'] = dict(session.request_context or {}, client_identifier=client_identifier)
    session.write(vals)
    if choice_text:
        session._post_user_choice(choice_text)
    if auto_confirm:
        session.write({'auto_confirm': True})
        session._post_notification('note', AUTO_CONFIRM_NOTE)
    pending.pop('user_input_request', None)
    pending.pop('client_tool', None)
    if result is not None:
        _add_result(pending.setdefault('results', []), calls[index], result)
        pending['index'] = index + 1
    session.write({'pending_tool_call': pending})
    session._notify_typing(True)
    session._publish_state()

    advance_tool_batch(env, session, confirmed=confirmed)
    return {'interactionConsumed': True, 'loop_state': session.loop_state}


# -- aborting -------------------------------------------------------------------------

def abort_pending(session, reason='skipped'):
    """Drop the paused batch: every call without a result gets an "aborted" one.

    Used when the user sends a new message instead of answering the card
    (``reason='skipped'``) and by the sweeper for stale cards
    (``reason='expired'``). No answer is posted: the session silently
    returns to ``ready`` (notifications of work already done are posted).
    """
    pending = session.pending_tool_call or {}
    results = list(pending.get('results') or [])
    answered = {item['call'].get('call_id') for item in results}
    aborted = ToolResult(ABORT_TEXTS.get(reason, ABORT_TEXTS['skipped']), success=False)
    for call in pending.get('calls') or []:
        if call.get('call_id') not in answered:
            _add_result(results, call, aborted)
    if results:
        session._append_event('user', history.tool_results_message(_result_pairs(results)))
    if session.channel_id:
        loop.post_turn_notifications(session)
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
    })
    session._notify_typing(False)
    session._publish_state()
