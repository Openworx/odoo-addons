"""JSON-RPC 2.0 framing for MCP.

Pure Python; no Odoo imports, so the unit tests can run without an env.
"""
import json
from dataclasses import dataclass, field
from typing import Any


# Error codes — JSON-RPC 2.0 reserved range + MCP-specific.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
AUTH_REQUIRED = -32001
PERMISSION_DENIED = -32002
NOT_FOUND = -32003


@dataclass
class Message:
    method: str | None = None
    id: Any = None
    params: dict = field(default_factory=dict)
    is_notification: bool = False


def parse_message(raw: str):
    """Parse a JSON-RPC 2.0 message (single or batch).

    Returns a Message or a list[Message]. Raises ValueError on parse/shape errors.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f'parse error: {e}')
    if isinstance(data, list):
        return [_one(d) for d in data]
    return _one(data)


def _one(d) -> Message:
    if not isinstance(d, dict) or d.get('jsonrpc') != '2.0':
        raise ValueError('invalid request')
    return Message(
        method=d.get('method'),
        id=d.get('id'),
        params=d.get('params') or {},
        is_notification='id' not in d,
    )


def result(id, result):
    return {'jsonrpc': '2.0', 'id': id, 'result': result}


def error(id, code, message, data=None):
    err = {'code': code, 'message': message}
    if data is not None:
        err['data'] = data
    return {'jsonrpc': '2.0', 'id': id, 'error': err}


def transport_error(code, message):
    """HTTP-level error before a request id is known.

    JSON-RPC error responses require id to be string|number (MCP SDK Zod
    schema rejects null). For transport gates (auth, rate-limit, IP block)
    we never have a request id, so we return a plain envelope that the
    client reads via the HTTP status code instead.
    """
    return {'error': {'code': code, 'message': message}}
