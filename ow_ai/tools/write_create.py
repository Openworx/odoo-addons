# -*- coding: utf-8 -*-
"""``create_records``: creates one or more records on a model, after a qweb
preview of exactly what will be written and the user's confirmation.

See ``engine/tool_batch.py`` for the confirmation pause/resume mechanics
this relies on: the tool is called twice for one creation -- once to build
the preview (``ctx.tool_request_confirmed`` is False), once, with the exact
same persisted arguments, to actually write (``ctx.tool_request_confirmed``
is True).
"""
from __future__ import annotations

import psycopg2
from odoo.exceptions import UserError

from ..engine.previews import render_create_preview
from ..engine.tools_registry import ToolResult, builtin_tool
from ..utils.access import check_model_access
from . import write_common as wc

_MAX_RECORDS = 20


@builtin_tool('write.create_records', is_write=True)
def create_records(ctx, explanation=None, model_name=None, values=None, preview_menu_id=None, **_kw):
    """Create ``values`` (a list of records) on ``model_name``."""
    env = ctx.env
    model = check_model_access(env, model_name, 'create')

    values = list(values or [])
    if not values:
        raise ValueError("Provide at least one record to create.")
    if len(values) > _MAX_RECORDS:
        raise ValueError(f"Too many records (max {_MAX_RECORDS}).")

    vals_list = [_normalize_create_record(env, model, item.get('field_values') or []) for item in values]
    label = model._description or model_name

    if not ctx.tool_request_confirmed:
        preview_html = render_create_preview(env, model, vals_list, explanation)
        ctx.user_input_request = wc.confirmation_request(preview_html)
        return ToolResult(
            "Waiting for confirmation.",
            summary={'icon': 'add', 'text': f"Proposed creating {len(vals_list)} {label}"})

    action_id = wc.resolve_preview_action_id(env, model_name, preview_menu_id)
    try:
        with env.cr.savepoint():
            records = model.create(vals_list)
    except psycopg2.IntegrityError as exc:
        raise UserError(_clean_db_error(exc)) from None

    created = [
        {'id': record.id, 'display_name': record.display_name, 'url': wc.record_link(model_name, record.id, action_id)}
        for record in records
    ]
    ctx.notifications.append({
        'kind': 'preview',
        'body': wc.notification_body("Created", created),
        'links': wc.notification_links(model_name, created),
    })
    ctx.client_notifications.append({'name': 'reload'})
    return ToolResult(
        {'created': created}, summary={'icon': 'check', 'text': f"Created {len(created)} {label}"})


def _normalize_create_record(env, model, field_values):
    vals = {}
    unknown = []
    for item in field_values:
        name = item.get('field')
        field = model._fields.get(name)
        if field is None:
            unknown.append(name)
            continue
        wc.check_field_writable(field, name)
        vals[name] = _resolve_value(env, field, item)
    if unknown:
        raise ValueError(f"Unknown field(s): {', '.join(str(name) for name in unknown)}")

    missing = wc.missing_required_fields(model, vals)
    if missing:
        raise ValueError(f"Missing required field(s): {', '.join(missing)}")
    return vals


def _resolve_value(env, field, item):
    if field.type == 'many2many':
        return wc.resolve_x2m_link_ids(env, field, item.get('x2m_link_ids'))
    if field.type == 'many2one':
        return wc.resolve_many2one(env, field, item.get('value'))
    if field.type == 'selection':
        return wc.resolve_selection(field, env, item.get('value'))
    if field.type in ('date', 'datetime'):
        return wc.resolve_date(field, item.get('value'))
    return wc.normalize_scalar(field, item.get('value'))


def _clean_db_error(exc):
    message = str(exc).strip().splitlines()[0] if str(exc).strip() else "a database constraint was violated"
    return f"This would violate a database constraint: {message}"
