# -*- coding: utf-8 -*-
"""Record read tools: ``search`` and ``read_records``.

Grouped/aggregated reads live in the sibling module ``read_group.py`` (split
out to keep files small, per the module's own size guideline).
"""
from __future__ import annotations

from ..engine.tools_registry import ToolResult, builtin_tool
from ..utils.access import check_model_access, parse_domain
from ..utils.serialize import serialize_records

_IMAGE_MIMETYPES = ('image/png', 'image/jpeg', 'image/webp', 'image/gif')
_MAX_SEARCH_LIMIT = 200
_MAX_READ_IDS = 50
_MAX_FILE_RECORDS = 5
_MAX_FILE_ATTACHMENTS = 5


def _validate_order(model, order):
    """Raise ValueError unless every token of ``order`` is a stored field."""
    for token in order.split(','):
        token = token.strip()
        if not token:
            continue
        parts = token.split()
        if len(parts) > 2:
            raise ValueError(f"Invalid order token '{token}'.")
        field_name = parts[0]
        if len(parts) == 2 and parts[1].lower() not in ('asc', 'desc'):
            raise ValueError(f"Invalid sort direction in order token '{token}'.")
        field = model._fields.get(field_name)
        if field is None:
            raise ValueError(f"Unknown field '{field_name}' in order.")
        if not field.store:
            raise ValueError(f"Field '{field_name}' is not sortable (not stored).")


@builtin_tool('read.search')
def search(ctx, model_name, domain=None, fields=None, offset=0, limit=50, order=None):
    """Search records on ``model_name`` and return a page of serialised records."""
    env = ctx.env
    model = check_model_access(env, model_name, 'read')

    parsed_domain = parse_domain(env, model, domain if domain is not None else [])

    if order:
        _validate_order(model, order)

    limit = max(1, min(int(limit or 50), _MAX_SEARCH_LIMIT))
    offset = max(0, int(offset or 0))

    records = model.search(parsed_domain, offset=offset, limit=limit, order=order or None)
    total = model.search_count(parsed_domain)
    field_names = fields or model._ow_ai_default_fields()

    response = {
        'records': serialize_records(env, records, field_names),
        'total_count': total,
        'offset': offset,
        'limit': limit,
        'has_more': offset + len(records) < total,
    }

    label = model._description or model_name
    return ToolResult(
        response=response,
        summary={'icon': 'search', 'text': f"Searched {label}: {len(records)} of {total}"})


@builtin_tool('read.read_records')
def read_records(ctx, model_name, record_ids, fields=None, include_files=False):
    """Read a specific list of records by id, with a report of missing ones."""
    env = ctx.env
    model = check_model_access(env, model_name, 'read')

    requested_ids = []
    for raw_id in (record_ids or []):
        record_id = int(raw_id)
        if record_id not in requested_ids:
            requested_ids.append(record_id)
        if len(requested_ids) >= _MAX_READ_IDS:
            break

    # Base model-level access was already checked by check_model_access()
    # above. We deliberately do NOT also call records.check_access('read')
    # here: on a recordset that mixes accessible and record-rule-excluded
    # ids, check_access() raises AccessError for the whole batch (see
    # odoo/orm/models.py BaseModel.check_access), which would turn a
    # partially-readable request into a hard failure instead of reporting
    # the excluded ids in missing_ids as intended. _filtered_access() alone
    # gives the graceful per-record split we want.
    records = model.browse(requested_ids).exists()
    accessible = records._filtered_access('read')

    accessible_ids = set(accessible.ids)
    missing_ids = [record_id for record_id in requested_ids if record_id not in accessible_ids]

    field_names = fields or model._ow_ai_default_fields()
    response = {
        'records': serialize_records(env, accessible, field_names),
        'missing_ids': missing_ids,
    }

    parts = []
    if include_files and accessible and len(accessible) <= _MAX_FILE_RECORDS:
        attachments = env['ir.attachment'].search([
            ('res_model', '=', model_name),
            ('res_id', 'in', accessible.ids),
            '|', ('mimetype', 'in', list(_IMAGE_MIMETYPES)), ('mimetype', '=', 'application/pdf'),
        ], limit=_MAX_FILE_ATTACHMENTS)
        for attachment in attachments:
            parts.extend(attachment._ow_ai_to_parts())

    label = model._description or model_name
    return ToolResult(
        response=response,
        parts=parts or None,
        summary={'icon': 'visibility', 'text': f"Read {len(accessible)} {label} record(s)"})
