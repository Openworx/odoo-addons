# -*- coding: utf-8 -*-
"""Mapping between the engine's provider-neutral message format and the
OpenAI-compatible chat-completions wire format.

This is the ONLY module in ow_ai allowed to build OpenAI-format dicts. The
rest of the engine deals exclusively in the neutral types defined in
``ow_ai.engine.types``.
"""
from __future__ import annotations

import json

from ..engine.types import (
    AssistantMessage,
    CompletionResult,
    EmbeddingResult,
    Message,
    Tool,
    Usage,
    assistant_message,
    inline_data_part,
    message_text,
    text_part,
    tool_call_part,
)
from .errors import ProviderError

_IMAGE_MIMETYPES = {'image/png', 'image/jpeg', 'image/webp', 'image/gif'}
_PARSE_ERROR_TRUNCATE = 500


def to_openai_messages(instructions: str | None, messages: list[Message]) -> list[dict]:
    """Build the OpenAI ``messages`` array for a chat-completions request.

    ``instructions`` (the system prompt), when non-empty, becomes a leading
    ``system`` message. Each neutral message is then translated per the
    rules documented in the task brief (tool results become their own
    ``tool`` messages emitted before any remaining user content of the same
    neutral message; tool-result images are moved to a trailing ``user``
    message).
    """
    result: list[dict] = []
    if instructions:
        result.append({'role': 'system', 'content': instructions})
    for message in messages:
        if message['role'] == 'user':
            result.extend(_map_user_message(message))
        else:
            result.append(_map_assistant_message(message))
    return result


def _map_user_message(message: dict) -> list[dict]:
    content_parts: list[dict] = []
    tool_messages: list[dict] = []
    trailing_images: list[dict] = []

    for part in message.get('content', []):
        part_type = part.get('type')
        if part_type == 'text':
            content_parts.append(text_part(part['text']))
        elif part_type == 'inline_data':
            content_parts.append(_map_inline_data(part))
        elif part_type == 'tool_result':
            tool_messages.append({
                'role': 'tool',
                'tool_call_id': part['tool_call_id'],
                'content': _tool_result_text(part),
            })
            images = [p for p in part.get('result', []) if p.get('type') == 'inline_data']
            if images:
                trailing_images.append(text_part(f"Images returned by tool {part['tool_name']}:"))
                trailing_images.extend(_map_inline_data(image) for image in images)

    out = list(tool_messages)
    if content_parts:
        if len(content_parts) == 1 and content_parts[0]['type'] == 'text':
            out.append({'role': 'user', 'content': content_parts[0]['text']})
        else:
            out.append({'role': 'user', 'content': content_parts})
    if trailing_images:
        out.append({'role': 'user', 'content': trailing_images})
    return out


def _map_inline_data(part: dict) -> dict:
    mimetype = part['mimetype']
    data = part['data']
    if mimetype in _IMAGE_MIMETYPES:
        return {'type': 'image_url', 'image_url': {'url': f'data:{mimetype};base64,{data}'}}
    if mimetype == 'application/pdf':
        filename = part.get('metadata', {}).get('filename', 'document.pdf')
        return {'type': 'file', 'file': {'filename': filename, 'file_data': f'data:application/pdf;base64,{data}'}}
    filename = part.get('metadata', {}).get('filename', 'file')
    return text_part(f'[Attachment {filename} of type {mimetype} omitted]')


def _tool_result_text(part: dict) -> str:
    text = ''.join(p.get('text', '') for p in part.get('result', []) if p.get('type') == 'text')
    if not part.get('success', True):
        text = f'Error: {text}'
    return text


def _map_assistant_message(message: AssistantMessage) -> dict:
    content = message.get('content', [])
    text = message_text(message)
    tool_calls = [part for part in content if part.get('type') == 'tool_call']

    out: dict = {'role': 'assistant'}
    if tool_calls:
        out['content'] = text if text else None
        out['tool_calls'] = [_map_tool_call(tc) for tc in tool_calls]
    else:
        out['content'] = text

    # a stored `reasoning: null` is not replayed: another endpoint may refuse it
    provider_metadata = message.get('provider_metadata') or {}
    if provider_metadata.get('reasoning_details') is not None:
        out['reasoning_details'] = provider_metadata['reasoning_details']
    if provider_metadata.get('reasoning') is not None:
        out['reasoning'] = provider_metadata['reasoning']
    return out


def _map_tool_call(tool_call: dict) -> dict:
    return {
        'id': tool_call['call_id'],
        'type': 'function',
        'function': {
            'name': tool_call['name'],
            'arguments': json.dumps(tool_call['args'], ensure_ascii=False),
        },
    }


def to_openai_tools(tools: list[Tool]) -> list[dict]:
    """Build the OpenAI ``tools`` array. Does not mutate the input schemas."""
    out = []
    for tool in tools:
        schema = dict(tool['schema'])
        schema.setdefault('type', 'object')
        schema.setdefault('properties', {})
        out.append({
            'type': 'function',
            'function': {
                'name': tool['name'],
                'description': tool['instructions'],
                'parameters': schema,
            },
        })
    return out


def to_openai_response_format(schema: dict | None, name: str = 'response') -> dict | None:
    """Build the OpenAI ``response_format`` for structured output.

    Strict mode (``strict: True``) requires the schema to set
    ``additionalProperties: False`` and list every property as required;
    that is the caller's responsibility, this function does not mutate
    or validate the schema.
    """
    if schema is None:
        return None
    return {'type': 'json_schema', 'json_schema': {'name': name, 'strict': True, 'schema': schema}}


def from_openai_response(body: dict) -> CompletionResult:
    """Parse an OpenAI-format chat-completions response body into a CompletionResult.

    Raises ProviderError (status 200) when ``choices`` is empty.
    """
    choices = body.get('choices') or []
    if not choices:
        raise ProviderError('Response has no choices', status=200, body=body)

    choice = choices[0]
    msg = choice['message']
    parts = []

    content = msg.get('content')
    if isinstance(content, str) and content:
        parts.append(text_part(content))
    elif isinstance(content, list):
        for item in content:
            if item.get('type') == 'text':
                parts.append(text_part(item.get('text', '')))

    for tool_call in msg.get('tool_calls') or []:
        parts.append(_parse_tool_call(tool_call))

    for image in msg.get('images') or []:
        part = _parse_image(image)
        if part is not None:
            parts.append(part)

    finish_reason = choice.get('finish_reason')
    provider_metadata = {'finish_reason': finish_reason, 'model': body.get('model'), 'id': body.get('id')}
    if 'reasoning' in msg:
        provider_metadata['reasoning'] = msg['reasoning']
    if 'reasoning_details' in msg:
        provider_metadata['reasoning_details'] = msg['reasoning_details']
    if 'annotations' in msg:
        provider_metadata['annotations'] = msg['annotations']

    return {
        'message': assistant_message(parts, provider_metadata),
        'usage': extract_usage(body),
        'request_id': body.get('id', ''),
        'model': body.get('model', ''),
        'finish_reason': finish_reason,
    }


def _parse_tool_call(tool_call: dict) -> dict:
    call_id = tool_call['id']
    name = tool_call['function']['name']
    raw = tool_call['function'].get('arguments') or '{}'
    try:
        args = json.loads(raw)
    except json.JSONDecodeError:
        args = {'__parse_error': raw[:_PARSE_ERROR_TRUNCATE]}
    else:
        if not isinstance(args, dict):
            args = {'__parse_error': raw[:_PARSE_ERROR_TRUNCATE]}
    return tool_call_part(name, args, call_id)


def _parse_image(image: dict) -> dict | None:
    url = (image.get('image_url') or {}).get('url', '')
    if not url.startswith('data:'):
        return None
    header, _, data = url[len('data:'):].partition(';base64,')
    if not data:
        return None
    return inline_data_part(header, data)


def extract_usage(body: dict) -> Usage:
    """Extract usage/cost information from an OpenAI-format response body."""
    usage = body.get('usage') or {}
    prompt_tokens = int(usage.get('prompt_tokens') or 0)
    completion_tokens = int(usage.get('completion_tokens') or 0)
    total_tokens = usage.get('total_tokens')
    total_tokens = int(total_tokens) if total_tokens is not None else prompt_tokens + completion_tokens
    cost = float(usage.get('cost') or 0.0)

    result: Usage = {
        'prompt_tokens': prompt_tokens,
        'completion_tokens': completion_tokens,
        'total_tokens': total_tokens,
        'cost': cost,
    }

    prompt_details = usage.get('prompt_tokens_details')
    if isinstance(prompt_details, dict) and prompt_details.get('cached_tokens') is not None:
        result['cached_tokens'] = int(prompt_details['cached_tokens'])

    completion_details = usage.get('completion_tokens_details')
    if isinstance(completion_details, dict) and completion_details.get('reasoning_tokens') is not None:
        result['reasoning_tokens'] = int(completion_details['reasoning_tokens'])

    return result


def from_openai_embeddings(body: dict) -> EmbeddingResult:
    """Parse an OpenAI-format embeddings response body, ordering vectors by index."""
    data = sorted(body.get('data', []), key=lambda item: item['index'])
    return {
        'vectors': [item['embedding'] for item in data],
        'usage': extract_usage(body),
        'model': body.get('model', ''),
    }
