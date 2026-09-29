# -*- coding: utf-8 -*-
"""``update_records``: writes changes to records matched by a domain, after
a qweb preview of every affected record's old/new values and the user's
confirmation. See ``write_create.py``'s docstring for the confirm/execute
two-call shape this shares.
"""
from __future__ import annotations

import hashlib
import json

import psycopg2
from odoo.exceptions import UserError

from ..engine.previews import render_update_preview
from ..engine.tools_registry import ToolResult, builtin_tool
from ..utils.access import check_model_access, parse_domain
from . import write_common as wc

_MAX_UPDATES = 50
_MAX_MATCHES = 200
_PIN_MISMATCH_TEXT = "This confirmation no longer matches the pending change; ask again."


@builtin_tool('write.update_records', is_write=True)
def update_records(ctx, explanation=None, updates=None, preview_menus=None, **_kw):
    """Write the ``changes`` of each ``updates`` group to the records its ``domain`` matches.

    The records a group's ``domain`` actually matches are pinned into
    ``ctx.pending_pin`` (see that field's docstring) the moment the preview
    is built (Phase A); Phase B only trusts that pin when the engine
    confirms it belongs to *this exact, currently-resumed* call (never for
    a fresh call that merely runs confirmed because ``session.auto_confirm``
    is on) and its own arguments still match (``args_hash``) -- otherwise
    it searches fresh, exactly like an unconfirmed call would. Re-running
    the same domain at execution time unchecked could pick up records that
    started matching only *after* the user saw the preview (or drop ones
    that stopped matching), silently writing to records the user never
    saw; trusting a stale/foreign pin could do worse (write to a
    completely different call's records). Phase B also re-filters the
    pinned ids through ``_filtered_access('write')`` (never trusts a
    'nothing changed' assumption).
    """
    env = ctx.env
    updates = list(updates or [])
    if not updates:
        raise ValueError("Provide at least one update.")
    if len(updates) > _MAX_UPDATES:
        raise ValueError(f"Too many updates (max {_MAX_UPDATES}).")
    preview_menus = list(preview_menus or [])

    args_hash = _args_hash(updates, preview_menus)
    pinned_groups = _verify_pin(ctx) if ctx.tool_request_confirmed else None
    if pinned_groups is not None and pinned_groups.get('args_hash') != args_hash:
        raise ValueError(_PIN_MISMATCH_TEXT)
    pinned_list = pinned_groups.get('groups') if pinned_groups else None

    groups = [
        _prepare_group(
            env, update, preview_menus[index] if index < len(preview_menus) else None,
            pinned=pinned_list[index] if pinned_list is not None and index < len(pinned_list) else None)
        for index, update in enumerate(updates)
    ]
    total = sum(len(group['records']) for group in groups)

    if not ctx.tool_request_confirmed:
        preview_html = ''.join(
            render_update_preview(
                env, group['model'], group['records'], group['vals'], explanation if index == 0 else '')
            for index, group in enumerate(groups))
        ctx.user_input_request = wc.confirmation_request(preview_html)
        ctx.pending_pin = {
            'args_hash': args_hash,
            'groups': [
                {'model_name': group['model_name'], 'ids': group['records'].ids} for group in groups
            ],
        }
        return ToolResult(
            "Waiting for confirmation.",
            summary={'icon': 'edit', 'text': f"Proposed updating {total} record(s)"})

    agent_partner = env['ow.ai.agent'].browse(ctx.agent_id).partner_id
    updated = []
    links = []
    try:
        with env.cr.savepoint():
            for group in groups:
                records = group['records']
                if hasattr(records, '_track_set_log_author'):
                    records._track_set_log_author(agent_partner)
                records.write(group['vals'])
                group_items = [
                    {'id': record.id, 'display_name': record.display_name,
                     'url': wc.record_link(group['model_name'], record.id, group['action_id'])}
                    for record in records
                ]
                updated.extend(group_items)
                links.extend(wc.notification_links(group['model_name'], group_items))
    except psycopg2.IntegrityError as exc:
        raise UserError(_clean_db_error(exc)) from None

    ctx.notifications.append({
        'kind': 'preview', 'body': wc.notification_body("Updated", updated), 'links': links,
    })
    ctx.client_notifications.append({'name': 'reload'})
    return ToolResult(
        {'updated': updated, 'count': len(updated)},
        summary={'icon': 'check', 'text': f"Updated {len(updated)} record(s)"})


def _args_hash(updates, preview_menus):
    """A stable fingerprint of the arguments a pin was built from.

    Phase A and Phase B always run with the exact persisted arguments (the
    engine re-runs the *same* call), so this only ever mismatches when a
    pin is being misapplied to a different call's data -- the one case
    this guards against.
    """
    payload = json.dumps({'updates': updates, 'preview_menus': preview_menus}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _verify_pin(ctx):
    """``ctx.pending_pin`` if the engine supplied one for this exact resumed call, else ``None``.

    The engine only ever populates ``ctx.pending_pin`` for the one call
    actually being resumed (see its docstring); a confirmed call with no
    pin (e.g. ``session.auto_confirm`` running a call that was never
    paused) simply gets ``None`` here, and ``update_records`` searches
    fresh for it, exactly as an unconfirmed call would.
    """
    return ctx.pending_pin


def _prepare_group(env, update, menu_id, *, pinned=None):
    """Build one update group: the model, the records to write, the values.

    ``pinned`` (Phase B, verified: ``{'model_name', 'ids'}`` from
    ``ctx.pending_pin``) bypasses domain matching entirely, re-filtering
    those ids for write access; without it (Phase A, an unconfirmed/direct
    call, or a confirmed call whose pin didn't verify) the domain is
    matched fresh, with the usual guards against a domain too broad to
    have been reviewed meaningfully.
    """
    model_name = update.get('model_name')
    model = check_model_access(env, model_name, 'write')

    if pinned is not None:
        if pinned.get('model_name') != model_name:
            raise ValueError(_PIN_MISMATCH_TEXT)
        records = model.browse(pinned.get('ids') or []).exists()._filtered_access('write')
        if not records:
            raise ValueError("No records match.")
    else:
        parsed_domain = parse_domain(env, model, update.get('domain'))
        if wc.is_trivial_domain(parsed_domain):
            raise ValueError("Refusing to update all records; use a specific domain.")
        records = model.search(parsed_domain, limit=_MAX_MATCHES + 1)
        if len(records) > _MAX_MATCHES:
            raise ValueError(f"Too many records (max {_MAX_MATCHES}).")
        if not records:
            raise ValueError("No records match.")
        # Catches a domain that matches literally every (default-context,
        # i.e. normally "every active") record of the model even when it
        # isn't one of `is_trivial_domain`'s hard-coded patterns. Left
        # deliberately in the *default* context, not `active_test=False`:
        # when the domain itself disables the implicit active filter (e.g.
        # mentions `active`), `records` already contains the archived rows
        # too, so "no *active* record is left out of `records`" still
        # correctly refuses that case as well -- without the
        # `active_test=False` version's false positive on an ordinary,
        # narrow, id-specific domain against a model that simply has very
        # few rows right now (every one of which then happens to be *in*
        # `records`, even though the domain never intended to be
        # unbounded).
        if not model.search_count([('id', 'not in', records.ids)], limit=1):
            raise ValueError("Refusing to update all records of the model; use a narrower domain.")

    vals = _normalize_changes(env, model, update.get('changes') or [])
    return {
        'model_name': model_name, 'model': model, 'records': records, 'vals': vals,
        'action_id': wc.resolve_preview_action_id(env, model_name, menu_id),
    }


def _normalize_changes(env, model, changes):
    vals = {}
    unknown = []
    for item in changes:
        name = item.get('field')
        field = model._fields.get(name)
        if field is None:
            unknown.append(name)
            continue
        wc.check_field_writable(field, name)
        vals[name] = _resolve_value(env, field, item)
    if unknown:
        raise ValueError(f"Unknown field(s): {', '.join(str(name) for name in unknown)}")
    if not vals:
        raise ValueError("Provide at least one field change.")
    return vals


def _resolve_value(env, field, item):
    if field.type == 'many2many':
        return wc.resolve_x2m_commands(env, field, item.get('x2m_commands'))
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
