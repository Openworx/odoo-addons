# -*- coding: utf-8 -*-
"""Shared helpers for the write tools (``write_create.py``/``write_update.py``):
normalising the assistant's field values into ORM write format, resolving
many2one/many2many targets (validating existence and read access), and
building the confirmation request / record links they both need.

One2many fields are never writable through these tools, full stop: Odoo's
``One2many.write_batch`` implements ``unlink``/``clear``/``set`` as an
``unlink()`` of the removed lines (which *deletes* them outright when the
inverse many2one has ``ondelete='cascade'``, e.g. ``res.partner.bank_ids``
-> ``res.partner.bank``, itself in ``WRITE_BLOCKLIST``) and implements
``link``/``set`` by writing the inverse field on the linked lines (i.e.
*reparenting* them to a different parent record). Both of those bypass
``check_model_access``'s write checks on the comodel entirely, so a
one2many is refused outright regardless of ``readonly``; only many2many
(a pure link table, never delete/reparent anything) is ever accepted.
"""
from __future__ import annotations

import json
from html import escape

from odoo import Command
from odoo import fields as odoo_fields
from odoo.exceptions import AccessError
from odoo.fields import Domain

from ..utils.access import check_model_access

_X2M_COMMAND_KEYS = frozenset({'link', 'unlink', 'clear', 'set'})
_UNWRITABLE_TYPES = ('binary', 'image', 'properties')
_TRIVIAL_DOMAIN_LITERALS = ([(1, '=', 1)], [('id', '!=', False)], [('id', '>', 0)])
_MAX_NOTIFICATION_LINKS = 10

CONFIRMATION_LABELS = {
    'confirm_once': "Yes, do it",
    'auto_confirm': "Yes, always approve in this chat",
    'decline': "No, I want something else",
}


# -- field validation / normalisation ---------------------------------------

def check_field_writable(field, name):
    """Raise ``ValueError`` unless ``field`` may be set by the assistant.

    ``readonly`` is the only writability gate for ordinary fields: Odoo
    itself marks a computed field without an ``inverse`` as ``readonly``
    unless its author explicitly opted out (``readonly=False``), and some
    core models rely on exactly that -- e.g. ``crm.lead.name`` and
    ``stage_id`` on ``crm.lead``/``project.task`` are ``required``,
    ``compute``d *and* ``readonly=False`` on purpose. Rejecting every
    computed field regardless of its own ``readonly`` would make those
    models impossible to create/update through these tools at all.
    One2many is always refused regardless of ``readonly`` (see the module
    docstring); binary/image/properties never carry assistant-meaningful
    values.
    """
    if field is None:
        raise ValueError(f"Unknown field '{name}'.")
    if field.type == 'one2many':
        raise ValueError(f"'{name}': one2many fields cannot be set here; update the related records directly.")
    if field.type in _UNWRITABLE_TYPES:
        raise ValueError(f"Field '{name}' cannot be set.")
    if field.readonly:
        raise ValueError(f"Field '{name}' cannot be set.")


def resolve_many2one(env, field, value):
    """A many2one write value: an id (int) or ``{'id': n}``, or False to unset."""
    if value in (None, False, 0, ''):
        return False
    record_id = value.get('id') if isinstance(value, dict) else value
    try:
        record_id = int(record_id)
    except (TypeError, ValueError):
        raise ValueError(f"'{field.name}': invalid record reference {value!r}.") from None
    comodel = env[field.comodel_name].browse(record_id).exists()
    if not comodel:
        raise ValueError(f"'{field.name}': record {record_id} does not exist on {field.comodel_name}.")
    _check_readable(comodel, field)
    return record_id


def resolve_x2m_link_ids(env, field, x2m_link_ids):
    """A many2many write value from ``x2m_link_ids`` (an id, a list of ids, or None)."""
    ids = _as_id_list(x2m_link_ids)
    if not ids:
        return [Command.clear()]
    _check_ids_exist_and_readable(env, field, ids)
    return [Command.set(ids)]


def resolve_x2m_commands(env, field, raw_commands):
    """A many2many write value from ``x2m_commands``, a JSON string.

    ``{"link": [ids], "unlink": [ids], "clear": true, "set": [ids]}`` --
    only these four keys are accepted; ``link``/``set`` ids must exist and
    be readable, ``unlink`` never needs to (removing an id that no longer
    exists is harmless).
    """
    if not raw_commands:
        raise ValueError(f"'{field.name}': provide x2m_commands to change this field.")
    try:
        parsed = json.loads(raw_commands) if isinstance(raw_commands, str) else raw_commands
    except (TypeError, ValueError) as exc:
        raise ValueError(f"'{field.name}': invalid x2m_commands ({exc}).") from None
    if not isinstance(parsed, dict) or not set(parsed) <= _X2M_COMMAND_KEYS:
        raise ValueError(f"'{field.name}': x2m_commands must only use link/unlink/clear/set.")

    commands = []
    if parsed.get('clear'):
        commands.append(Command.clear())
    if 'set' in parsed:
        ids = _as_id_list(parsed['set'])
        _check_ids_exist_and_readable(env, field, ids)
        commands.append(Command.set(ids))
    if 'link' in parsed:
        ids = _as_id_list(parsed['link'])
        _check_ids_exist_and_readable(env, field, ids)
        commands.extend(Command.link(record_id) for record_id in ids)
    if 'unlink' in parsed:
        commands.extend(Command.unlink(record_id) for record_id in _as_id_list(parsed['unlink']))
    if not commands:
        raise ValueError(f"'{field.name}': x2m_commands did not contain any operation.")
    return commands


def resolve_selection(field, env, value):
    """A selection write value: the key itself, or a case-insensitive label match."""
    if value in (None, False, ''):
        return False
    options = field._description_selection(env)
    for key, _label in options:
        if value == key:
            return key
    lowered = str(value).strip().lower()
    for key, label in options:
        if str(label).strip().lower() == lowered:
            return key
    raise ValueError(f"'{field.name}': '{value}' is not a valid option.")


def resolve_date(field, value):
    """A date/datetime write value, parsed from an ISO-ish string."""
    if value in (None, False, ''):
        return False
    try:
        if field.type == 'date':
            parsed = odoo_fields.Date.to_date(value)
        else:
            parsed = odoo_fields.Datetime.to_datetime(value)
    except Exception:  # noqa: BLE001 - normalise any parse error to an LLM-safe message
        raise ValueError(f"'{field.name}': invalid {field.type} value {value!r}.") from None
    if parsed is None:
        raise ValueError(f"'{field.name}': invalid {field.type} value {value!r}.")
    return parsed


_BOOLEAN_TRUE_STRINGS = frozenset({'true', '1', 'yes', 'on'})
_BOOLEAN_FALSE_STRINGS = frozenset({'false', '0', 'no', 'off', ''})


def _parse_boolean(field, value):
    """Parse ``value`` as a boolean; never truthiness-test a non-empty string.

    ``bool`` values pass through unchanged. ``int`` 0/1 map to False/True.
    Strings are matched case-insensitively, stripped of surrounding
    whitespace, against a fixed vocabulary
    (``true/1/yes/on``/``false/0/no/off/''``): anything else is an LLM-safe
    error rather than a silent, always-true guess (the audit's finding 6:
    ``bool("false")`` is ``True``).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _BOOLEAN_TRUE_STRINGS:
            return True
        if lowered in _BOOLEAN_FALSE_STRINGS:
            return False
    raise ValueError(f"'{field.name}': expected true or false.")


def normalize_scalar(field, value):
    """A plain (boolean/integer/float/monetary/char/text/html/...) write value."""
    if field.type == 'boolean':
        return _parse_boolean(field, value)
    if field.type == 'integer':
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ValueError(f"'{field.name}': expected an integer.") from None
    if field.type in ('float', 'monetary'):
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ValueError(f"'{field.name}': expected a number.") from None
    if field.type in ('char', 'text', 'html'):
        if value in (None, False):
            return False
        return str(value)
    return value


def _as_id_list(value):
    if value in (None, False):
        return []
    if isinstance(value, (list, tuple)):
        return [int(item) for item in value]
    return [int(value)]


def _check_readable(records, field):
    try:
        records.check_access('read')
    except AccessError:
        raise ValueError(f"'{field.name}': you do not have access to {field.comodel_name}.") from None


def _check_ids_exist_and_readable(env, field, ids):
    if not ids:
        return
    check_model_access(env, field.comodel_name, 'read')
    comodel = env[field.comodel_name].browse(ids).exists()
    missing = sorted(set(ids) - set(comodel.ids))
    if missing:
        raise ValueError(f"'{field.name}': record(s) {missing} do not exist on {field.comodel_name}.")
    _check_readable(comodel, field)


# -- required fields (create only) -------------------------------------------

def missing_required_fields(model, vals):
    """Required, writable fields neither in ``vals`` nor covered by a default.

    Computed fields are skipped even when ``required``: their value comes
    from the compute method (typically from other fields already in
    ``vals``, e.g. ``crm.lead.stage_id``), not from something the assistant
    must supply directly -- flagging them as "missing" would make such
    models impossible to create through this tool.
    """
    required_names = [
        name for name, field in model._fields.items()
        if field.required and not field.readonly and not field.compute and name not in vals
    ]
    if not required_names:
        return []
    defaults = model.default_get(required_names)
    return [name for name in required_names if name not in defaults]


# -- domains -------------------------------------------------------------

def is_trivial_domain(parsed_domain):
    """True for a domain that would match every record (refused for update_records)."""
    if not parsed_domain:  # Domain.__bool__ is False exactly for the TRUE domain
        return True
    return any(parsed_domain == Domain(literal) for literal in _TRIVIAL_DOMAIN_LITERALS)


# -- links / notifications ------------------------------------------------

def record_link(model_name, record_id, action_id=None):
    """The URL used to open ``record_id`` from a preview/notification link."""
    if action_id:
        return f"/odoo/action-{action_id}/{record_id}"
    return f"/odoo/{model_name}/{record_id}"


def resolve_preview_action_id(env, model_name, menu_id):
    """The ``ir.actions.act_window`` id of ``menu_id`` if it targets ``model_name``, else None."""
    if not menu_id:
        return None
    try:
        menu_id = int(menu_id)
    except (TypeError, ValueError):
        return None
    menu = env['ir.ui.menu'].browse(menu_id).exists()
    if not menu:
        return None
    try:
        menu.check_access('read')
        action = menu.action
        if not action or action._name != 'ir.actions.act_window' or action.res_model != model_name:
            return None
        action.check_access('read')
    except AccessError:
        return None
    return action.id


def notification_body(verb, items):
    """``<ul><li>Created <a href="...">Name</a></li>...</ul>`` (max 10 links)."""
    rows = ''.join(
        f'<li>{verb} <a href="{escape(item["url"])}">{escape(item["display_name"])}</a></li>'
        for item in items[:_MAX_NOTIFICATION_LINKS]
    )
    extra = len(items) - _MAX_NOTIFICATION_LINKS
    if extra > 0:
        rows += f'<li>…and {extra} more.</li>'
    return f'<ul>{rows}</ul>'


def notification_links(model_name, items):
    return [{'model': model_name, 'id': item['id'], 'name': item['display_name']} for item in items]


def confirmation_request(body_html):
    return {
        'type': 'confirmation',
        'body': body_html,
        'choices': ['confirm_once', 'auto_confirm', 'decline'],
        'labels': dict(CONFIRMATION_LABELS),
    }
