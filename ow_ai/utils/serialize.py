# -*- coding: utf-8 -*-
"""Record serialisation for the LLM: JSON-safe field values, records, schemas.

Everything here turns Odoo values (recordsets, field values, model
introspection) into plain, JSON-serialisable Python data suitable for
sending to the LLM as text. Binary content is never included (no raw
bytes/base64 of arbitrary binary fields); attachments are handled
separately by ``ir.attachment._ow_ai_to_parts`` (see ``models/ir_attachment.py``).

Access control (blocklists, ``check_model_access``, ``parse_domain``) lives
in the sibling module ``utils/access.py`` and is kept separate on purpose:
this module is pure data shaping, that one is pure access policy.
"""
from __future__ import annotations

import json

from odoo import fields as odoo_fields
from odoo.tools.mail import html2plaintext

_EXCLUDED_FIELD_PREFIXES = ('message_', 'activity_', 'rating_')
_EXCLUDED_FIELD_NAMES = frozenset({'website_message_ids'})
_MAX_X2MANY = 20


def truncate_text(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` characters, appending a note when cut."""
    if text is None:
        return text
    if len(text) <= limit:
        return text
    removed = len(text) - limit
    return f"{text[:limit]}\n…[truncated {removed} characters]"


def compact_json(data) -> str:
    """Dump ``data`` as compact JSON, tolerating non-JSON-native types."""
    return json.dumps(data, ensure_ascii=False, separators=(',', ':'), default=str)


def _field_is_excluded_from_default_serialisation(name):
    if name in _EXCLUDED_FIELD_NAMES:
        return True
    return any(name.startswith(prefix) for prefix in _EXCLUDED_FIELD_PREFIXES)


def serialize_value(env, field, value, *, max_text=2000):
    """Serialise a single field value into JSON-safe data.

    ``field`` is the ``odoo.fields.Field`` instance (e.g.
    ``record._fields[name]``); ``value`` is the value as read from the
    record (``record[name]``).
    """
    ftype = field.type

    if ftype in ('binary', 'image'):
        return None

    if ftype == 'selection':
        if not value and value != 0:
            return None
        label = value
        for key, text in field._description_selection(env):
            if key == value:
                label = text
                break
        return {'value': value, 'label': label}

    if ftype == 'html':
        return truncate_text(html2plaintext(value or ''), max_text)

    if ftype in ('char', 'text'):
        if value is False or value is None:
            return None
        return truncate_text(value, max_text)

    if ftype == 'many2one':
        if not value:
            return None
        return {'id': value.id, 'display_name': value.display_name}

    if ftype in ('one2many', 'many2many'):
        records = value
        total = len(records)
        items = [{'id': rec.id, 'display_name': rec.display_name} for rec in records[:_MAX_X2MANY]]
        if total > _MAX_X2MANY:
            items.append(f'…(+{total - _MAX_X2MANY} more)')
        return items

    if ftype == 'date':
        if not value:
            return None
        return odoo_fields.Date.to_string(value)

    if ftype == 'datetime':
        if not value:
            return None
        tz_value = odoo_fields.Datetime.context_timestamp(env.user, value)
        return tz_value.isoformat()

    if ftype == 'monetary':
        # Resolving the actual currency requires the owning record (the
        # currency field's value); serialize_record uses the record-aware
        # _monetary_value() helper instead. Called standalone (no record
        # context), we can only give the bare amount.
        return float(value or 0.0)

    if ftype == 'many2one_reference':
        # Resolving the related model name requires the owning record (see
        # _many2one_reference_value(), used by serialize_record).
        return {'model': None, 'id': value or None}

    if ftype == 'properties':
        # Resolving property labels/values requires the owning record (see
        # _properties_value(), used by serialize_record).
        return []

    if ftype == 'json':
        return value

    if ftype in ('integer', 'float', 'boolean'):
        return value

    # Fallback: best-effort JSON-safe coercion.
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _monetary_value(record, field, value):
    """Serialise a Monetary field value using the record's own currency."""
    currency_field_name = field.get_currency_field(record)
    if currency_field_name and currency_field_name in record._fields:
        currency = record[currency_field_name]
        if currency:
            return {'amount': float(value or 0.0), 'currency': currency.name}
    return float(value or 0.0)


def _properties_value(record, field, value):
    try:
        raw_values = value._values if hasattr(value, '_values') else (value or [])
        raw = field.convert_to_read(raw_values, record, use_display_name=True)
    except Exception:  # noqa: BLE001
        return []
    result = []
    for prop in raw or []:
        result.append({
            'name': prop.get('name'),
            'label': prop.get('string'),
            'value': prop.get('value'),
        })
    return result


def _many2one_reference_value(record, field, value):
    if not value:
        return None
    model_name = record[field.model_field] if field.model_field in record._fields else None
    return {'model': model_name or None, 'id': value}


def serialize_field_on_record(env, record, name):
    """Serialise field ``name`` on the (singleton) ``record``.

    Handles the field types that need the owning record (monetary currency
    resolution, properties, many2one_reference) by dispatching to
    record-aware helpers; delegates everything else to ``serialize_value``.
    """
    field = record._fields[name]
    value = record[name]
    if field.type == 'monetary':
        return _monetary_value(record, field, value)
    if field.type == 'properties':
        return _properties_value(record, field, value)
    if field.type == 'many2one_reference':
        return _many2one_reference_value(record, field, value)
    return serialize_value(env, field, value)


def serialize_record(env, record, field_names: list | None = None, *, max_text=2000) -> dict:
    """Serialise a single record into a JSON-safe dict.

    ``field_names`` defaults to ``record._ow_ai_default_fields()``. Unknown
    or inaccessible fields are silently skipped, as are binary fields.
    """
    record.ensure_one()
    if field_names is None:
        field_names = record._ow_ai_default_fields()

    result = {'id': record.id, 'display_name': record.display_name}
    for name in field_names:
        if name in ('id', 'display_name'):
            continue
        field = record._fields.get(name)
        if field is None or field.type in ('binary', 'image'):
            continue
        try:
            value = serialize_field_on_record(env, record, name)
        except Exception:  # noqa: BLE001 - never let one bad field break serialisation
            continue
        result[name] = value
    return result


def serialize_records(env, records, field_names=None, **kw) -> list:
    """Serialise a recordset. Relies on the ORM's own prefetch (looping over
    records lets the ORM batch-fetch field values for the whole recordset).
    """
    return [serialize_record(env, record, field_names, **kw) for record in records]


def describe_fields(model, *, include_help=False, only=None) -> list:
    """Describe a model's fields for the LLM: name, label, type, and flags.

    Skips binary/image fields, message_*/activity_*/rating_*/website_message_ids
    fields, and fields whose name starts with '_'. Sorted with id,
    display_name, name first, then alphabetically.
    """
    attributes = [
        'string', 'type', 'required', 'readonly', 'searchable', 'sortable', 'groupable', 'relation', 'selection',
    ]
    if include_help:
        attributes.append('help')

    fields_info = model.fields_get(allfields=only, attributes=attributes)

    described = []
    for name, info in fields_info.items():
        if name.startswith('_'):
            continue
        if info.get('type') in ('binary', 'image'):
            continue
        if _field_is_excluded_from_default_serialisation(name):
            continue

        entry = {
            'name': name,
            'label': info.get('string') or name,
            'type': info.get('type'),
            'required': bool(info.get('required')),
            'readonly': bool(info.get('readonly')),
            'searchable': bool(info.get('searchable')),
            'sortable': bool(info.get('sortable')),
        }
        if 'groupable' in info:
            entry['groupable'] = bool(info.get('groupable'))
        if info.get('relation'):
            entry['relation'] = info['relation']
        if info.get('selection'):
            entry['selection'] = [list(pair) for pair in info['selection'][:50]]
        if include_help and info.get('help'):
            entry['help'] = info['help']
        described.append(entry)

    def sort_key(entry):
        name = entry['name']
        if name == 'id':
            return (0, '')
        if name == 'display_name':
            return (1, '')
        if name == 'name':
            return (2, '')
        return (3, name)

    described.sort(key=sort_key)
    return described
