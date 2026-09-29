# -*- coding: utf-8 -*-
"""Qweb preview rendering for the write tools (``write_create``/``write_update``).

Builds the HTML shown to the user before a write is confirmed: renders the
``ow_ai.preview_create``/``ow_ai.preview_update`` qweb templates (plain
Bootstrap tables, see ``views/ow_ai_preview_templates.xml``) and passes the
result through :func:`sanitize_ai_html` (the module's single choke point for
``markupsafe.Markup`` -- see ``html_output.py``'s docstring), storing the
plain ``str(...)`` for ``ToolContext.user_input_request['body']``.

Many2many fields are shown as the *resulting* set of linked records (old
ids with the write-format ``Command`` list applied), not as the raw ids the
command list happens to mention: a preview of ``{"unlink": [7]}`` should
read "A, B, C -> A, B", not "-> 7". One2many fields are never previewed
here: ``tools/write_common.py`` rejects them outright (see its module
docstring) before a preview is ever built.
"""
from __future__ import annotations

from odoo.tools.misc import format_date, format_datetime

from .html_output import sanitize_ai_html

MAX_UPDATE_RECORDS_SHOWN = 20


def format_value_for_preview(env, field, value):
    """Human-readable display of a non-relational-collection field ``value``.

    ``value`` may be a normalised write-format value (many2one id, a
    selection key, an ISO-ish date/datetime, ...) as built by
    ``tools/write_common.py``, or a value read straight off a record (a
    recordset, a python ``date``/``datetime``, ...): both shapes are
    handled, so the same helper formats both the "old" and "new" columns of
    an update preview. Many2many fields are handled separately by
    :func:`_resulting_many2many_ids`/``_format_id_list`` (their preview
    needs the *old* ids too, which this single-value helper never has).
    """
    ftype = field.type
    if ftype == 'many2one':
        record = env[field.comodel_name].browse(value).exists() if isinstance(value, int) else value
        return record.display_name if record else '—'
    if ftype == 'selection':
        if value in (None, False, ''):
            return '—'
        for key, label in field._description_selection(env):
            if key == value:
                return label
        return str(value)
    if ftype == 'boolean':
        return 'Yes' if value else 'No'
    if ftype == 'date':
        return format_date(env, value) if value else '—'
    if ftype == 'datetime':
        return format_datetime(env, value, tz=env.user.tz) if value else '—'
    if ftype == 'monetary':
        return f'{float(value):,.2f}' if value not in (None, False) else '—'
    if ftype in ('integer', 'float'):
        return str(value) if value is not None and value is not False else '—'
    if value in (None, False):
        return '—'
    return str(value)


def _resulting_many2many_ids(old_ids, commands):
    """The ids a many2many field will hold once write-format ``commands`` apply to ``old_ids``."""
    ids = list(old_ids or [])
    for command in commands or []:
        if not isinstance(command, (list, tuple)) or len(command) != 3:
            continue
        code, record_id, payload = command
        if code == 6:  # Command.set(ids): replaces the whole list
            ids = list(payload or [])
        elif code == 5:  # Command.clear(): empties it
            ids = []
        elif code == 4:  # Command.link(id): adds one, if not already there
            if record_id not in ids:
                ids.append(record_id)
        elif code in (2, 3):  # Command.delete(id) / Command.unlink(id): removes one
            ids = [existing for existing in ids if existing != record_id]
    return ids


def _format_id_list(env, comodel_name, ids):
    """``', '.join(display_name for each id)``, or ``'—'`` for an empty/all-gone list."""
    if not ids:
        return '—'
    records = env[comodel_name].browse(ids).exists()
    return ', '.join(records.mapped('display_name')) if records else '—'


def _preview_rows(env, model, vals, *, old_record=None):
    """One row per changed field: ``{'label', 'value'}`` (create) or ``{'label', 'old', 'new'}`` (update).

    ``old_record`` is ``None`` for a create preview (no "old" state at all)
    or the record being updated.
    """
    rows = []
    for name, value in vals.items():
        field = model._fields.get(name)
        if field is None:
            continue
        if field.type == 'many2many':
            old_ids = old_record[name].ids if old_record is not None else []
            new_ids = _resulting_many2many_ids(old_ids, value)
            new_display = _format_id_list(env, field.comodel_name, new_ids)
            if old_record is None:
                rows.append({'label': field.string, 'value': new_display})
            else:
                rows.append({
                    'label': field.string,
                    'old': _format_id_list(env, field.comodel_name, old_ids),
                    'new': new_display,
                })
        elif old_record is None:
            rows.append({'label': field.string, 'value': format_value_for_preview(env, field, value)})
        else:
            rows.append({
                'label': field.string,
                'old': format_value_for_preview(env, field, old_record[name]),
                'new': format_value_for_preview(env, field, value),
            })
    return rows


def render_create_preview(env, model, vals_list, explanation) -> str:
    """Render ``ow_ai.preview_create`` for the normalised ``vals_list``."""
    values = {
        'explanation': explanation or '',
        'model_label': model._description or model._name,
        'records': [_preview_rows(env, model, vals) for vals in vals_list],
    }
    html = env['ir.qweb']._render('ow_ai.preview_create', values)
    return str(sanitize_ai_html(html))


def render_update_preview(env, model, records, vals, explanation) -> str:
    """Render ``ow_ai.preview_update`` for writing ``vals`` on ``records``."""
    shown = records[:MAX_UPDATE_RECORDS_SHOWN]
    values = {
        'explanation': explanation or '',
        'model_label': model._description or model._name,
        'records': [
            {'display_name': record.display_name, 'changes': _preview_rows(env, model, vals, old_record=record)}
            for record in shown
        ],
        'total_count': len(records),
        'more_count': max(len(records) - len(shown), 0),
    }
    html = env['ir.qweb']._render('ow_ai.preview_update', values)
    return str(sanitize_ai_html(html))
