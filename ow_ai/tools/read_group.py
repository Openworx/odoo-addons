# -*- coding: utf-8 -*-
"""``read.read_group`` built-in tool: grouped/aggregated reads.

Split out of ``read.py`` to keep files small. Relies on Odoo's own
``Model._read_group`` for the heavy lifting (SQL generation, per-field
access checks, granularity/aggregate parsing) and mostly does argument
validation plus turning the resulting rows into JSON-safe dicts.
"""
from __future__ import annotations

import ast
import json

from ..engine.tools_registry import ToolResult, builtin_tool
from ..utils.access import check_model_access, parse_domain

_GRANULARITIES = frozenset({
    'year', 'quarter', 'month', 'week', 'day',
    'year_number', 'quarter_number', 'month_number', 'iso_week_number',
    'day_of_year', 'day_of_month', 'day_of_week', 'hour_number', 'minute_number', 'second_number',
})
_AGGREGATE_FUNCS = frozenset({'sum', 'avg', 'min', 'max', 'count', 'count_distinct'})
_NUMERIC_FIELD_TYPES = frozenset({'integer', 'float', 'monetary'})
_MAX_LIMIT = 500


def _validate_groupby(model, groupby):
    for spec in groupby:
        field_name, _, granularity = spec.partition(':')
        field = model._fields.get(field_name)
        if field is None:
            raise ValueError(f"Unknown groupby field '{field_name}'.")
        if granularity:
            if field.type not in ('date', 'datetime'):
                raise ValueError(f"Granularity requires a date/datetime field, got '{field_name}'.")
            if granularity not in _GRANULARITIES:
                raise ValueError(f"Unknown granularity '{granularity}'.")
        elif not field.store:
            raise ValueError(f"Field '{field_name}' is not groupable (not stored).")


def _validate_aggregates(model, aggregates):
    for spec in aggregates:
        if spec == '__count':
            continue
        field_name, _, func = spec.partition(':')
        if not func:
            raise ValueError(f"Aggregate '{spec}' must be 'field:function', e.g. '{spec}:sum'.")
        if func not in _AGGREGATE_FUNCS:
            if field_name in _AGGREGATE_FUNCS:
                raise ValueError(
                    f"Aggregate '{spec}' is written the wrong way round: use 'field:function', "
                    f"e.g. '{func}:{field_name}'.")
            raise ValueError(
                f"Unknown aggregate function '{func}' in '{spec}'; use one of: "
                f"{', '.join(sorted(_AGGREGATE_FUNCS))}.")
        field = model._fields.get(field_name)
        if field is None:
            raise ValueError(f"Unknown aggregate field '{field_name}'.")
        if func in ('sum', 'avg') and field.type not in _NUMERIC_FIELD_TYPES:
            raise ValueError(f"Aggregate '{func}' requires a numeric field, got '{field.type}' for '{field_name}'.")


def _validate_order(order, groupby, aggregates):
    if not order:
        return
    valid_specs = list(dict.fromkeys(list(groupby) + list(aggregates)))
    # Spelled out in the error so the model can fix its call right away
    # (small models tend to order by the bare field, e.g. "amount_total desc").
    hint = (f"order by one of: {', '.join(valid_specs)}, each optionally followed by asc or desc, "
            f"e.g. '{valid_specs[-1]} desc'.")
    for token in order.split(','):
        token = token.strip()
        if not token:
            continue
        parts = token.split()
        if len(parts) > 2 or (len(parts) == 2 and parts[1].lower() not in ('asc', 'desc')):
            raise ValueError(f"Invalid order token '{token}': {hint}")
        if parts[0] not in valid_specs:
            raise ValueError(f"Order references unknown groupby/aggregate '{parts[0]}': {hint}")


def _parse_having(having_input):
    if not having_input:
        return []
    if isinstance(having_input, (list, tuple)):
        return list(having_input)
    if isinstance(having_input, str):
        try:
            parsed = ast.literal_eval(having_input)
        except (ValueError, SyntaxError):
            try:
                parsed = json.loads(having_input)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid having: {exc}") from None
        if not isinstance(parsed, (list, tuple)):
            raise ValueError("Invalid having: expected a list.")
        return list(parsed)
    raise ValueError("Invalid having: expected a list or a string.")


def _serialize_cell(value):
    """Serialise one groupby/aggregate cell: recordsets, dates and plain scalars."""
    if hasattr(value, 'ids') and hasattr(value, 'display_name') and hasattr(value, 'env'):
        if not value:
            return False
        return {'id': value.id, 'display_name': value.display_name}
    if hasattr(value, 'isoformat'):
        return str(value)
    return value


@builtin_tool('read.read_group')
def read_group(
        ctx, model_name, domain=None, groupby=None, aggregates=None, having=None,
        offset=0, limit=100, order=None):
    """Group and aggregate records on ``model_name``."""
    env = ctx.env
    model = check_model_access(env, model_name, 'read')

    parsed_domain = parse_domain(env, model, domain if domain is not None else [])
    groupby = list(groupby or [])
    aggregates = list(aggregates or ['__count'])
    having_domain = _parse_having(having)

    limit = max(1, min(int(limit or 100), _MAX_LIMIT))
    offset = max(0, int(offset or 0))

    _validate_groupby(model, groupby)
    _validate_aggregates(model, aggregates)
    _validate_order(order, groupby, aggregates)

    rows = model._read_group(
        parsed_domain, groupby, aggregates, having=having_domain,
        offset=offset, limit=limit, order=order)

    groups = []
    for row in rows:
        entry = {}
        for index, spec in enumerate(groupby):
            entry[spec] = _serialize_cell(row[index])
        for index, spec in enumerate(aggregates):
            entry[spec] = _serialize_cell(row[len(groupby) + index])
        groups.append(entry)

    label = model._description or model_name
    return ToolResult(
        response={'groups': groups, 'count': len(groups)},
        summary={'icon': 'bar_chart', 'text': f"Grouped {label}: {len(groups)} group(s)"})
