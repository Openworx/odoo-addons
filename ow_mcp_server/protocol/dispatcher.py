"""MCP JSON-RPC dispatcher.

Maps `initialize`, `ping`, `tools/list`, and `tools/call` to the right
handlers and wraps results / errors in the MCP envelope.
"""
import json
import logging
import uuid

from . import cache, jsonrpc, tools


_logger = logging.getLogger(__name__)


PROTOCOL_VERSION = '2025-06-18'

_COMPANY_ARG_SCHEMA = {
    'type': 'integer',
    'description': (
        "Run this call in the given company. Must be one of the "
        "user's allowed companies (see get_user_context). Defaults "
        "to the user's main company when omitted."
    ),
}


def _apply_company(env, args):
    """Switch env to the company in args['company_id'] when supplied.

    Validates the requested company is in env.user.company_ids; raises
    AccessError otherwise. Leaves the key in `args` so the cache key
    naturally segments by company.

    Setting `allowed_company_ids=[cid]` is enough to make env.company
    resolve to that company and to lock record-rules to it. There is no
    `Environment.with_company` — that helper lives on recordsets.
    """
    if not isinstance(args, dict):
        return env
    cid = args.get('company_id')
    if cid is None:
        return env
    if cid not in env.user.company_ids.ids:
        from odoo.exceptions import AccessError
        raise AccessError(
            f'Company {cid} is not in your allowed companies.'
        )
    return env(context=dict(env.context, allowed_company_ids=[cid]))

TOOL_SCHEMAS = [
    {
        'name': 'list_models',
        'description':
            'List Odoo models exposed via MCP with their CRUD capabilities '
            'and any per-model field allow-lists.',
        'inputSchema': {
            'type': 'object',
            'properties': {},
            'additionalProperties': False,
        },
    },
    {
        'name': 'search_records',
        'description':
            'Search Odoo records with a domain filter. Returns an LLM-friendly '
            'envelope: {records, total, offset, limit, has_more, next_offset}.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'model': {'type': 'string'},
                'domain': {
                    'type': 'array',
                    'description':
                        'Odoo domain, e.g. [["name","ilike","acme"]].',
                },
                'fields': {
                    'type': 'array',
                    'items': {'type': 'string'},
                },
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 500},
                'offset': {'type': 'integer', 'minimum': 0},
                'order': {'type': 'string'},
                'company_id': _COMPANY_ARG_SCHEMA,
            },
            'required': ['model'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'get_record',
        'description':
            'Fetch one or many records by id(s). Provide either `id` (single) '
            'or `ids` (list); one of them is required (enforced at runtime).',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'model': {'type': 'string'},
                'id': {'type': 'integer'},
                'ids': {'type': 'array', 'items': {'type': 'integer'}},
                'fields': {'type': 'array', 'items': {'type': 'string'}},
                'company_id': _COMPANY_ARG_SCHEMA,
            },
            'required': ['model'],
        },
    },
    {
        'name': 'create_record',
        'description': 'Create a new record. Returns {id, record} on success.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'model': {'type': 'string'},
                'values': {'type': 'object'},
                'company_id': _COMPANY_ARG_SCHEMA,
            },
            'required': ['model', 'values'],
        },
    },
    {
        'name': 'update_record',
        'description':
            'Update one or many records by id(s) with a values dict. '
            'Provide either `id` (single) or `ids` (list); one is required '
            '(enforced at runtime).',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'model': {'type': 'string'},
                'values': {'type': 'object'},
                'id': {'type': 'integer'},
                'ids': {'type': 'array', 'items': {'type': 'integer'}},
                'company_id': _COMPANY_ARG_SCHEMA,
            },
            'required': ['model', 'values'],
        },
    },
    {
        'name': 'delete_record',
        'description':
            'Delete one or many records by id(s). Provide either `id` '
            '(single) or `ids` (list); one is required (enforced at runtime).',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'model': {'type': 'string'},
                'id': {'type': 'integer'},
                'ids': {'type': 'array', 'items': {'type': 'integer'}},
                'company_id': _COMPANY_ARG_SCHEMA,
            },
            'required': ['model'],
        },
    },
    {
        'name': 'search_count',
        'description':
            'Count Odoo records matching a domain without fetching them. '
            'Use instead of search_records when you only need the number.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'model': {'type': 'string'},
                'domain': {
                    'type': 'array',
                    'description': 'Odoo domain, e.g. [["state","=","draft"]].',
                },
                'company_id': _COMPANY_ARG_SCHEMA,
            },
            'required': ['model'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'get_model_schema',
        'description':
            'Return field definitions for an Odoo model: name, type, label, '
            'required, readonly, relation model, and selection options. '
            'Call this before create_record or update_record to know which '
            'fields exist and what values are valid.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'model': {'type': 'string'},
                'company_id': _COMPANY_ARG_SCHEMA,
            },
            'required': ['model'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'read_group',
        'description':
            'Group Odoo records by one or more fields and aggregate values. '
            'Returns one row per group with a `count` of matching records. '
            'Use for totals, breakdowns, and pivot-style summaries.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'model': {'type': 'string'},
                'groupby': {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'description':
                        'Fields to group by. Supports date truncation, '
                        'e.g. ["state"] or ["date_invoice:month"].',
                },
                'fields': {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'description':
                        'Fields to aggregate, e.g. ["amount_total:sum", '
                        '"id:count"]. Leave empty to get only counts.',
                },
                'domain': {
                    'type': 'array',
                    'description': 'Odoo domain to pre-filter records.',
                },
                'company_id': _COMPANY_ARG_SCHEMA,
            },
            'required': ['model', 'groupby'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'get_user_context',
        'description':
            'Return information about the authenticated user: id, name, '
            'login, current and allowed companies, language, timezone, and '
            'Odoo group memberships.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'company_id': _COMPANY_ARG_SCHEMA,
            },
            'additionalProperties': False,
        },
    },
    {
        'name': 'list_modules',
        'description': 'List all installed Odoo modules with name, label, and version.',
        'inputSchema': {
            'type': 'object',
            'properties': {},
            'additionalProperties': False,
        },
    },
]

def _tool_error(message):
    """MCP-idiomatic tool error: isError=True in content, not JSON-RPC error."""
    return {'content': [{'type': 'text', 'text': message}], 'isError': True}


TOOL_HANDLERS = {
    'list_models': tools.list_models,
    'search_records': tools.search_records,
    'get_record': tools.get_record,
    'create_record': tools.create_record,
    'update_record': tools.update_record,
    'delete_record': tools.delete_record,
    'search_count': tools.search_count,
    'get_model_schema': tools.get_model_schema,
    'read_group': tools.read_group,
    'get_user_context': tools.get_user_context,
    'list_modules': tools.list_modules,
}


def dispatch(env, message: dict):
    """Dispatch a single JSON-RPC message. Returns a response dict or None
    for notifications (messages without an 'id' key, per JSON-RPC 2.0 §4.1).
    """
    is_notification = 'id' not in message
    response = _dispatch(env, message)
    return None if is_notification else response


def _dispatch(env, message: dict):
    method = message.get('method')
    msg_id = message.get('id')
    params = message.get('params') or {}

    try:
        if method == 'initialize':
            return jsonrpc.result(msg_id, {
                'protocolVersion': PROTOCOL_VERSION,
                'capabilities': {'tools': {}},
                'serverInfo': {
                    'name': 'ow_mcp_server',
                    'version': '18.0.1.0.0',
                },
            })
        if method in ('initialized', 'notifications/initialized'):
            return None
        if method == 'ping':
            return jsonrpc.result(msg_id, {})
        if method == 'tools/list':
            return jsonrpc.result(msg_id, {'tools': TOOL_SCHEMAS})
        if method == 'tools/call':
            name = params.get('name')
            args = params.get('arguments') or {}
            handler = TOOL_HANDLERS.get(name)
            if handler is None:
                return jsonrpc.result(msg_id, _tool_error(f'Unknown tool: {name}'))
            # Validate company membership BEFORE the cache lookup so a just-
            # revoked company access cannot replay a previously authorized
            # result out of the in-memory cache (H1).
            call_env = _apply_company(env, args)
            result = cache.lookup(call_env, name, args)
            if result is None:
                # Per-call savepoint so a failed handler (IntegrityError,
                # SerializationFailure, etc.) does not poison the outer
                # transaction for the rest of a JSON-RPC batch.
                with call_env.cr.savepoint():
                    result = handler(call_env, args)
                cache.store(call_env, name, args, result)
            cache.invalidate_for_write(call_env, name, args)
            return jsonrpc.result(msg_id, {
                'content': [{'type': 'text',
                             'text': json.dumps(result, default=str)}],
                'structuredContent': result,
                'isError': False,
            })
        return jsonrpc.error(
            msg_id, jsonrpc.METHOD_NOT_FOUND, f'Unknown method: {method}',
        )
    except Exception as e:
        # Odoo imports are lazy so pure-Python tests still work.
        from odoo.exceptions import AccessError, UserError, ValidationError
        # User-facing Odoo exceptions carry messages that are intentionally
        # safe to show. Anything else is treated as an internal error: the
        # real message is logged server-side and the client gets only a
        # correlation id, so we don't leak SQL fragments / column names /
        # offending row values out through MCP (H3).
        if isinstance(e, (AccessError, UserError, ValidationError)):
            msg = str(e)
            if method == 'tools/call':
                return jsonrpc.result(msg_id, _tool_error(msg))
            if isinstance(e, AccessError):
                return jsonrpc.error(msg_id, jsonrpc.PERMISSION_DENIED, msg)
            return jsonrpc.error(msg_id, jsonrpc.INVALID_PARAMS, msg)
        trace_id = uuid.uuid4().hex
        _logger.exception('MCP internal error [%s]', trace_id)
        generic = f'Internal server error (trace id: {trace_id}).'
        if method == 'tools/call':
            return jsonrpc.result(msg_id, _tool_error(generic))
        return jsonrpc.error(msg_id, jsonrpc.INTERNAL_ERROR, generic)
