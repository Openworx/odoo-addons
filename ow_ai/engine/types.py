# -*- coding: utf-8 -*-
"""Provider-neutral message and result types used across the ow_ai engine.

These types are our own design, independent of any single AI provider's
wire format. The OpenAI-compatible adapter (``ow_ai.provider.mapping``)
translates to/from this shape; the rest of the engine only ever deals with
these types.
"""
from __future__ import annotations

from typing import TypedDict, Union


class TextPart(TypedDict):
    type: str  # 'text'
    text: str


class InlineDataPart(TypedDict, total=False):
    type: str  # 'inline_data'
    mimetype: str
    data: str  # base64-encoded
    metadata: dict  # optional, e.g. {'filename': ...}


class ToolCallPart(TypedDict):
    type: str  # 'tool_call'
    name: str
    args: dict
    call_id: str


class ToolResultPart(TypedDict):
    type: str  # 'tool_result'
    tool_name: str
    tool_call_id: str
    result: list  # list[TextPart | InlineDataPart]
    success: bool


class UserMessage(TypedDict):
    role: str  # 'user'
    content: list  # list[TextPart | InlineDataPart | ToolResultPart]


class AssistantMessage(TypedDict, total=False):
    role: str  # 'assistant'
    content: list  # list[TextPart | ToolCallPart]
    provider_metadata: dict


Message = Union[UserMessage, AssistantMessage]


class Tool(TypedDict):
    name: str
    instructions: str
    schema: dict  # JSON schema object for the parameters


class _UsageRequired(TypedDict):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost: float


class Usage(_UsageRequired, total=False):
    # Optional, provider-dependent extras.
    cached_tokens: int
    reasoning_tokens: int


class CompletionResult(TypedDict):
    message: AssistantMessage
    usage: Usage
    request_id: str
    model: str
    finish_reason: str


class EmbeddingResult(TypedDict):
    vectors: list  # list[list[float]]
    usage: Usage
    model: str


def text_part(text: str) -> TextPart:
    """Build a TextPart."""
    return {'type': 'text', 'text': text}


def inline_data_part(mimetype: str, data: str, **metadata) -> InlineDataPart:
    """Build an InlineDataPart. Extra keyword arguments become ``metadata``."""
    part: InlineDataPart = {'type': 'inline_data', 'mimetype': mimetype, 'data': data}
    if metadata:
        part['metadata'] = metadata
    return part


def tool_call_part(name: str, args: dict, call_id: str) -> ToolCallPart:
    """Build a ToolCallPart."""
    return {'type': 'tool_call', 'name': name, 'args': args, 'call_id': call_id}


def tool_result_part(tool_name: str, tool_call_id: str, result_parts: list, success: bool = True) -> ToolResultPart:
    """Build a ToolResultPart."""
    return {
        'type': 'tool_result',
        'tool_name': tool_name,
        'tool_call_id': tool_call_id,
        'result': result_parts,
        'success': success,
    }


def user_message(*parts) -> UserMessage:
    """Build a UserMessage from one or more content parts."""
    return {'role': 'user', 'content': list(parts)}


def assistant_message(parts: list, provider_metadata: dict | None = None) -> AssistantMessage:
    """Build an AssistantMessage from a list of content parts."""
    message: AssistantMessage = {'role': 'assistant', 'content': list(parts)}
    if provider_metadata is not None:
        message['provider_metadata'] = provider_metadata
    return message


def message_text(message: Message) -> str:
    """Concatenate the text of all TextPart entries in a message's content."""
    return ''.join(
        part.get('text', '')
        for part in message.get('content', [])
        if part.get('type') == 'text'
    )
