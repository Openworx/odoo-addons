# -*- coding: utf-8 -*-
"""Model/field introspection tools: let the model discover what it can query.

``get_models`` and ``get_fields`` are meant to be called before ``search`` /
``read_records`` / ``read_group`` so the model learns real model and field
names instead of guessing them.
"""
from __future__ import annotations

from ..engine.tools_registry import ToolResult, builtin_tool
from ..utils.access import ModelAccessError, check_model_access
from ..utils.serialize import describe_fields

_ALLOWLISTED_MODELS = frozenset({
    'res.partner', 'res.users', 'res.company', 'mail.activity', 'ir.attachment', 'calendar.event',
})
_MAX_MODEL_LINES = 300
_MAX_FIELD_LINES = 200


@builtin_tool('introspection.get_models')
def get_models(ctx, **args):
    """List the models the assistant can query, with their main menu path."""
    env = ctx.env

    # Menu records are UI configuration, not sensitive user data (and some
    # groups, e.g. portal, have no direct read access to ir.ui.menu at all);
    # ``_visible_menu_ids`` already restricts to what this user may see, so
    # browsing them with sudo here does not leak anything the user
    # shouldn't otherwise be able to reach.
    menu_ids = env['ir.ui.menu'].sudo()._visible_menu_ids()
    menus = env['ir.ui.menu'].sudo().search(
        [('id', 'in', list(menu_ids)), ('action', '!=', False)], order='sequence, id')

    best_menu = {}  # model_name -> (sequence, complete_name)
    for menu in menus:
        action = menu.action
        if not action or action._name != 'ir.actions.act_window':
            continue
        res_model = action.res_model
        if not res_model or res_model in best_menu:
            continue
        best_menu[res_model] = (menu.sequence, menu.complete_name)

    candidate_models = set(best_menu) | _ALLOWLISTED_MODELS

    entries = []
    for model_name in candidate_models:
        try:
            model = check_model_access(env, model_name, 'read')
        except ModelAccessError:
            continue
        if model._transient or model._abstract:
            continue
        sequence, path = best_menu.get(model_name, (10000, ''))
        entries.append((sequence, model_name, model._description or model_name, path))

    entries.sort(key=lambda entry: (entry[0], entry[1]))

    lines = []
    for _sequence, model_name, label, path in entries[:_MAX_MODEL_LINES]:
        line = f"{model_name} | {label}"
        if path:
            line += f" | {path}"
        lines.append(line)

    response = '\n'.join(lines) if lines else "No models available."
    return ToolResult(
        response=response,
        summary={'icon': 'view_list', 'text': f"Listed {len(lines)} available models"})


@builtin_tool('introspection.get_fields')
def get_fields(ctx, model_name, include_description=False):
    """Describe a model's fields: name, label, type, flags and selection values."""
    env = ctx.env
    model = check_model_access(env, model_name, 'read')

    fields_info = describe_fields(model, include_help=include_description)[:_MAX_FIELD_LINES]

    lines = [f"Model {model._description} ({model_name})"]
    for info in fields_info:
        type_label = info['type']
        if info.get('relation'):
            type_label += f"({info['relation']})"

        flags = []
        if info['required']:
            flags.append('req')
        if info['readonly']:
            flags.append('ro')
        if info['searchable']:
            flags.append('search')
        if info['sortable']:
            flags.append('sort')

        line = f"{info['name']} | {info['label']} | {type_label}"
        if flags:
            line += f" | {','.join(flags)}"
        if info.get('selection'):
            selection = ', '.join(f"{key}={value}" for key, value in info['selection'][:20])
            line += f" | selection: {selection}"
        if include_description and info.get('help'):
            line += f" | {info['help']}"
        lines.append(line)

    return ToolResult(
        response='\n'.join(lines),
        summary={'icon': 'table', 'text': f"Listed fields of {model._description}"})
