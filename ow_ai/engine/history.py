# -*- coding: utf-8 -*-
"""Turning ``ow.ai.session.event`` rows into neutral messages and back,
building the ``<odoo_context>`` block appended to every user turn, and
trimming history to fit inside a character budget.

Everything here works in the engine's provider-neutral message shape
(``engine/types.py``); no OpenAI-format dicts are built here.
"""
from __future__ import annotations

from odoo import fields as odoo_fields

from ..utils.serialize import compact_json
from .types import UserMessage, text_part, tool_result_part, user_message

_WEEKDAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
TOOL_RESULT_PLACEHOLDER = "[result truncated]"
IMAGE_PLACEHOLDER = "[image omitted]"
FILE_PLACEHOLDER = "[file omitted]"


def events_to_messages(events) -> list:
    """Return the ``metadata`` (already a neutral message) of each event, in order."""
    return [dict(event.metadata or {}) for event in events]


def tool_results_message(results: list) -> UserMessage:
    """Build one user message carrying a ``tool_result`` part per ``(call, ToolResult)``.

    The result's ``response`` becomes the part's text (compact JSON when it
    is a dict/list); any ``ToolResult.parts`` (e.g. images) are appended
    after that text part.
    """
    parts = []
    for call, result in results:
        response = result.response
        text = response if isinstance(response, str) else compact_json(response)
        result_parts = [text_part(text), *(result.parts or [])]
        parts.append(tool_result_part(call['name'], call['call_id'], result_parts, success=result.success))
    return user_message(*parts)


def context_input(env, *, record=None, session=None, now=None) -> str:
    """Build the ``<odoo_context>`` block appended to every stateless/direct turn."""
    user = env.user
    lines = ['<odoo_context>']
    lines.append(f'user: {user.name} (id {env.uid})')

    companies = ', '.join(f'{company.name} ({company.id})' for company in env.companies)
    lines.append(f'companies: {companies}; current: {env.company.name} ({env.company.id})')

    tz = user.tz or 'UTC'
    now = now or odoo_fields.Datetime.now()
    local_now = odoo_fields.Datetime.context_timestamp(user, now)
    weekday = _WEEKDAYS[local_now.weekday()]
    lines.append(
        f"language: {user.lang}; timezone: {tz}; "
        f"now: {local_now.strftime('%Y-%m-%d %H:%M')} ({tz}), {weekday}")

    if record:
        bound = record.with_env(env)
        snapshot = bound._ow_ai_record_context()
        lines.append(f'record: {bound._name}/{bound.id}/{bound.display_name} {compact_json(snapshot)}')

    lines.append('</odoo_context>')
    return '\n'.join(lines)


def _is_plain_user_message(message: dict) -> bool:
    """True for a user message that carries no ``tool_result`` parts.

    A tool round is ``assistant(tool_calls)`` -> ``user(tool_results)``:
    the ``tool_result`` parts refer back to call ids on the assistant
    message right before them. Truncated history must never start with
    such a message, or the tool results become orphans once mapped to
    OpenAI-format ``role: tool`` messages (see ``mapping.py``), which
    providers reject.
    """
    if message.get('role') != 'user':
        return False
    return not any(part.get('type') == 'tool_result' for part in message.get('content', []))


def truncate_history(messages: list, *, max_chars: int) -> list:
    """Fit ``messages`` into a ``max_chars`` budget without breaking its structure.

    The *current turn* runs from the last plain user message (the question
    being answered) to the end; everything before it is made of *old turns*
    (a plain user message up to the next one). Rules:

    - The current turn is never dropped: its question always stays, and an
      ``assistant(tool_calls)`` -> ``user(tool_results)`` pair is never
      split (only whole turns are dropped, so the result always starts with
      a plain user message).
    - ``inline_data`` parts (images, PDFs) do not count toward the budget;
      in old turns they are replaced by a short text placeholder so they
      are not re-sent with every later round.
    - Over budget, first the tool results of old turns are shrunk to a
      placeholder (oldest first), then old turns are dropped whole from the
      front, and only then are the older tool results of the current turn
      shrunk (never its last message, i.e. the latest results the model has
      not answered yet). What is left may still exceed ``max_chars``.

    The input messages (cached event metadata) are never modified: changed
    messages are new dicts, untouched ones are returned as they are.
    """
    messages = list(messages)
    current_start = _current_turn_start(messages)
    old = [_without_inline_data(message) for message in messages[:current_start]]
    current = list(messages[current_start:])
    # Leading orphans (tool results/assistant messages whose question is
    # already gone) can never be sent on their own.
    while old and not _is_plain_user_message(old[0]):
        old.pop(0)

    old_sizes = [_message_chars(message) for message in old]
    current_sizes = [_message_chars(message) for message in current]

    def over_budget():
        separators = len(old) + len(current) + 1
        return sum(old_sizes) + sum(current_sizes) + separators > max_chars

    # 1. Shrink the tool results of old turns, oldest first.
    for index, message in enumerate(old):
        if not over_budget():
            break
        if _has_tool_results(message):
            old[index] = _shrink_tool_results(message)
            old_sizes[index] = _message_chars(old[index])

    # 2. Drop whole old turns from the front.
    while old and over_budget():
        end = next((index for index in range(1, len(old)) if _is_plain_user_message(old[index])), len(old))
        del old[:end]
        del old_sizes[:end]

    # 3. Shrink the current turn's older tool results (never its last message).
    for index in range(len(current) - 1):
        if not over_budget():
            break
        if _has_tool_results(current[index]):
            current[index] = _shrink_tool_results(current[index])
            current_sizes[index] = _message_chars(current[index])

    return old + current


def _current_turn_start(messages: list) -> int:
    """Index of the last plain user message (0 when there is none)."""
    for index in range(len(messages) - 1, -1, -1):
        if _is_plain_user_message(messages[index]):
            return index
    return 0


def _has_tool_results(message: dict) -> bool:
    return message.get('role') == 'user' and any(
        part.get('type') == 'tool_result' for part in message.get('content', []))


def _shrink_tool_results(message: dict) -> dict:
    """``message`` with the content of every ``tool_result`` part replaced by a placeholder."""
    content = [
        dict(part, result=[text_part(TOOL_RESULT_PLACEHOLDER)]) if part.get('type') == 'tool_result' else part
        for part in message.get('content', [])
    ]
    return dict(message, content=content)


def _inline_data_placeholder(part: dict) -> dict:
    mimetype = part.get('mimetype') or ''
    return text_part(IMAGE_PLACEHOLDER if mimetype.startswith('image/') else FILE_PLACEHOLDER)


def _map_inline_data(message: dict, replace) -> dict:
    """``message`` with ``replace(part)`` substituted for every ``inline_data``
    part, both directly in its content and inside its tool results."""
    def map_parts(parts):
        return [replace(part) if part.get('type') == 'inline_data' else part for part in parts]

    content = [
        dict(part, result=map_parts(part.get('result') or [])) if part.get('type') == 'tool_result' else part
        for part in map_parts(message.get('content', []))
    ]
    return dict(message, content=content)


def _without_inline_data(message: dict) -> dict:
    """``message`` with every image/file replaced by a short text placeholder."""
    return _map_inline_data(message, _inline_data_placeholder)


def _message_chars(message: dict) -> int:
    """Serialised size of ``message``, not counting ``inline_data`` payloads."""
    return len(compact_json(_map_inline_data(message, lambda part: {'type': 'inline_data'})))
