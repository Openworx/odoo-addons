# -*- coding: utf-8 -*-
"""Turning the user's answer to a paused tool call (a confirmation or
question card) or the browser's client-tool result into that call's
outcome, validating it first: an invalid answer raises ``UserError``
before ``tool_batch.resume_pending`` changes anything.
"""
from __future__ import annotations

from odoo.exceptions import UserError

from ..utils import params
from ..utils.serialize import truncate_text
from .tools_registry import ToolResult

DECLINED_TEXT = "The user declined this action."
SKIPPED_QUESTION_TEXT = "The user skipped the question."
SKIPPED_CHOICE_TEXT = "(skipped)"
CONFIRMATION_CHOICES = ('confirm_once', 'auto_confirm', 'decline')
_DEFAULT_CONFIRMATION_LABELS = {
    'confirm_once': "Yes, do it",
    'auto_confirm': "Yes, always approve in this chat",
    'decline': "No",
}
_DEFAULT_MAX_TOOL_RESULT_CHARS = 60000


def confirmation_outcome(env, kind, value, request):
    """``(choice_text, confirmed, auto_confirm, result)`` for a confirmation card."""
    labels = dict(_DEFAULT_CONFIRMATION_LABELS, **(request.get('labels') or {}))
    if kind == 'skip':
        return SKIPPED_CHOICE_TEXT, False, False, ToolResult(DECLINED_TEXT, success=False)
    allowed = request.get('choices') or CONFIRMATION_CHOICES
    if value not in CONFIRMATION_CHOICES or value not in allowed:
        raise UserError(env._("This choice is not available."))
    if value == 'decline':
        return labels['decline'], False, False, ToolResult(DECLINED_TEXT, success=False)
    return labels[value], True, value == 'auto_confirm', None


def question_outcome(env, kind, value, request):
    """``(choice_text, result)`` for a question card."""
    if kind == 'skip':
        return SKIPPED_CHOICE_TEXT, ToolResult(SKIPPED_QUESTION_TEXT)
    offered = request.get('choices') or []
    answer = _normalise_answer(env, value, offered)
    if any(choice not in offered for choice in answer['choices']):
        raise UserError(env._("Invalid choice"))
    if len(answer['choices']) > 1 and not request.get('multi_select'):
        raise UserError(env._("Invalid choice"))
    if answer['text'] and not request.get('allow_free_text'):
        raise UserError(env._("This question does not accept a free-text answer."))
    if not answer['choices'] and not answer['text']:
        raise UserError(env._("Please choose an option or type an answer."))
    choice_text = ', '.join(answer['choices'])
    if answer['text']:
        choice_text = f"{choice_text} — {answer['text']}" if choice_text else answer['text']
    return choice_text, ToolResult({'answer': answer})


def _normalise_answer(env, value, offered):
    """``{'choices': [str], 'text': str}`` from the card's answer.

    The card sends ``{'choices': [...], 'text': '...'}``. A bare list of
    strings (offered options, anything else being free text) or a bare
    string (free text) are accepted too.
    """
    if isinstance(value, dict):
        choices, text = value.get('choices') or [], value.get('text') or ''
    elif isinstance(value, (list, tuple)):
        if not all(isinstance(item, str) for item in value):
            raise UserError(env._("Invalid choice"))
        choices = [item for item in value if item in offered]
        text = ' '.join(item for item in value if item not in offered)
    else:
        choices, text = [], value or ''
    if not isinstance(choices, (list, tuple)) or not all(isinstance(choice, str) for choice in choices):
        raise UserError(env._("Invalid choice"))
    if not isinstance(text, str):
        raise UserError(env._("Invalid answer"))
    return {'choices': list(dict.fromkeys(choice.strip() for choice in choices if choice.strip())),
            'text': text.strip()}


def client_outcome(env, kind, value):
    """The result of a client-side tool, as reported by the browser."""
    limit = params.get_int(env, 'ow_ai.max_tool_result_chars', _DEFAULT_MAX_TOOL_RESULT_CHARS)
    if kind == 'client_error':
        return ToolResult(truncate_text(f"The client-side action failed: {value}", limit), success=False)
    if value is None:
        return ToolResult("Done.")
    return ToolResult(truncate_text(value, limit) if isinstance(value, str) else value)
