# -*- coding: utf-8 -*-
"""The stateless "direct response" loop: ask a model something, let it call
tools, and return the final answer -- with no session/event persistence and
no ability to pause for user confirmation.

This is the actual implementation behind ``ow.ai.session._get_direct_response``
and ``_get_structured_response``, which are thin wrappers around
:func:`run_direct`/:func:`run_structured` (kept here, not on the model, to
keep ``models/ow_ai_session.py`` focused on persistence).
"""
from __future__ import annotations

import copy
import json

from ..provider.client import ProviderClient
from ..provider.errors import AIProviderError
from ..provider.mapping import (
    from_openai_response,
    to_openai_messages,
    to_openai_response_format,
    to_openai_tools,
)
from . import history, usage_log
from .tools_registry import ToolContext, ToolResult, run_tool
from .types import text_part, user_message

_DEFAULT_TIMEOUT = 60
_ROUND_LIMIT_TEXT = "I could not finish within the allowed number of steps."
_TOO_MANY_TOOL_CALLS_TEXT = "Too many tool calls in one step"
_NEEDS_CONFIRMATION_TEXT = "This action needs user confirmation and cannot run here."


def _neutral_tools(tools):
    return [tool._to_neutral_tool() for tool in tools] if tools else []


def _find_tool(tools, name):
    if not tools:
        return None
    match = tools.filtered(lambda tool: tool.tool_name == name)
    return match[:1] if match else None


def _run_one_tool_call(env, tools, call, *, record, agent, tool_state=None):
    tool_record = _find_tool(tools, call['name'])
    if not tool_record:
        return ToolResult(response=f"Unknown tool '{call['name']}'", success=False)

    ctx = ToolContext(
        env=env,
        agent_id=agent.id,
        tool_call_id=call['call_id'],
        state=copy.deepcopy(tool_state or {}),
        record=record,
        tool_request_confirmed=False,
        auto_confirm=False,
    )
    result = run_tool(tool_record, call['args'], ctx)
    if ctx.user_input_request is not None:
        return ToolResult(response=_NEEDS_CONFIRMATION_TEXT, success=False)
    return result


def run_direct(env, session, instructions, message_parts, *, tools=None, record=None, agent=None,
                schema=None, timeout=None, max_rounds=None, kind='direct', on_step=None, tool_state=None) -> list:
    """Run the stateless request/tool-call loop. Returns the final list of content parts.

    ``session`` may be an empty ``ow.ai.session`` recordset (or ``None``):
    when truthy it is attached to the usage rows this call logs. ``tools``
    is an ``ow.ai.tool`` recordset (or ``None``/empty for no tools).
    ``tool_state`` (a JSON-safe dict) is the initial ``ToolContext.state`` of
    every tool call -- a fresh deep copy per call, never persisted -- e.g.
    the run identity the unattended tools of ``ow_ai_automation`` need.
    """
    agent = agent or env.ref('ow_ai.agent_default')
    tools = tools if tools is not None else env['ow.ai.tool']
    client = ProviderClient.from_env(env, timeout=timeout or _DEFAULT_TIMEOUT)
    model = agent._get_model()
    options = agent._get_model_options()

    params = env['ir.config_parameter'].sudo()
    max_rounds = max_rounds or params.get_int('ow_ai.max_rounds', 30)
    max_tool_calls_per_round = params.get_int('ow_ai.max_tool_calls_per_round', 20)

    context_block = history.context_input(env, record=record)
    messages = [user_message(*(list(message_parts) + [text_part(context_block)]))]

    neutral_tools = _neutral_tools(tools)
    response_format = to_openai_response_format(schema)

    for _round_index in range(max_rounds):
        try:
            raw = client.chat_completion(
                model=model,
                messages=to_openai_messages(instructions, messages),
                tools=to_openai_tools(neutral_tools) if neutral_tools else None,
                response_format=response_format,
                **options,
            )
        except AIProviderError as exc:
            usage_log.log_error(env, exc, kind=kind, model=model, agent=agent, session=session or None)
            raise

        result = from_openai_response(raw)
        usage_log.log_usage(
            env, kind=kind, model=result['model'], usage=result['usage'], agent=agent,
            session=session or None, request_id=result['request_id'],
            latency_ms=raw.get('_ow_ai_latency_ms', 0))

        messages.append(result['message'])
        content = result['message'].get('content', [])
        tool_calls = [part for part in content if part.get('type') == 'tool_call']

        if not tool_calls:
            return [part for part in content if part.get('type') != 'tool_call']

        step_results = []
        for index, call in enumerate(tool_calls):
            if index >= max_tool_calls_per_round:
                tool_result = ToolResult(response=_TOO_MANY_TOOL_CALLS_TEXT, success=False)
            else:
                tool_result = _run_one_tool_call(
                    env, tools, call, record=record, agent=agent, tool_state=tool_state)
            step_results.append((call, tool_result))
            if on_step is not None:
                on_step(call, tool_result)

        messages.append(history.tool_results_message(step_results))

    return [text_part(_ROUND_LIMIT_TEXT)]


def run_structured(env, session, instructions, message_parts, schema, **kw) -> dict:
    """Run :func:`run_direct` with structured output, parsing the answer as JSON.

    Retries once (with a nudge appended to the user message) on invalid
    JSON; raises ``ValueError`` if the retry also fails to parse.
    """
    parts = run_direct(env, session, instructions, message_parts, schema=schema, **kw)
    text = ''.join(part.get('text', '') for part in parts if part.get('type') == 'text')
    try:
        return json.loads(text)
    except ValueError:
        pass

    retry_parts = list(message_parts) + [text_part("Return only valid JSON matching the schema.")]
    parts = run_direct(env, session, instructions, retry_parts, schema=schema, **kw)
    text = ''.join(part.get('text', '') for part in parts if part.get('type') == 'text')
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ValueError("The model did not return valid JSON matching the schema.") from exc
