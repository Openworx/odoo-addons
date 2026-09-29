# -*- coding: utf-8 -*-
"""``dates.compute_date``: let the model compute exact date/datetime boundaries.

Models are bad at day-of-week arithmetic and timezone conversion; this tool
does it precisely so relative expressions ("last month", "next Friday at
5pm") turn into an exact string the model can put straight into a
``read.search``/``read.read_group`` domain.
"""
from __future__ import annotations

import calendar
import datetime as dt
import zoneinfo

from dateutil.relativedelta import relativedelta

from ..engine.tools_registry import ToolResult, builtin_tool
from ..utils.access import check_model_access

_PERIODS = ('minute', 'hour', 'day', 'week', 'month', 'quarter', 'year')
_BOUNDARIES = ('start', 'end', 'none')


def _arithmetic_zone(tz):
    """A tz implementation whose UTC offset is derived per wall-clock time.

    ``env.tz`` is a ``pytz`` zone: each instance carries a *fixed* UTC
    offset, so ``replace()``/``relativedelta`` arithmetic that lands on a
    different date keeps the wrong offset across a DST change (the audit's
    finding 5). ``zoneinfo.ZoneInfo`` recomputes the offset for whatever
    local time it is attached to, so the same arithmetic on it is correct.
    Falls back to UTC for the ``'UTC'`` zone itself and for any key
    ``zoneinfo`` does not know (never raises). A local time that does not
    exist (a spring-forward gap) resolves with ``fold=0`` semantics -- the
    offset in effect just before the gap; there is no "correct" answer for
    a wall-clock time that was skipped, but this always returns *some*
    valid UTC instant instead of raising.
    """
    key = str(tz)
    if key == 'UTC':
        return dt.timezone.utc
    try:
        return zoneinfo.ZoneInfo(key)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return dt.timezone.utc


def _days_in_month(year, month):
    return calendar.monthrange(year, month)[1]


def _period_start(current, period):
    if period == 'minute':
        return current.replace(second=0, microsecond=0)
    if period == 'hour':
        return current.replace(minute=0, second=0, microsecond=0)
    if period == 'day':
        return current.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'week':
        # Monday-based week.
        start = current - dt.timedelta(days=current.weekday())
        return start.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'month':
        return current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if period == 'quarter':
        quarter_start_month = ((current.month - 1) // 3) * 3 + 1
        return current.replace(month=quarter_start_month, day=1, hour=0, minute=0, second=0, microsecond=0)
    if period == 'year':
        return current.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"Unknown period '{period}'.")


def _period_end(current, period):
    if period == 'minute':
        return current.replace(second=59, microsecond=0)
    if period == 'hour':
        return current.replace(minute=59, second=59, microsecond=0)
    if period == 'day':
        return current.replace(hour=23, minute=59, second=59, microsecond=0)
    if period == 'week':
        start = _period_start(current, 'week')
        return (start + dt.timedelta(days=6)).replace(hour=23, minute=59, second=59, microsecond=0)
    if period == 'month':
        start = _period_start(current, 'month')
        next_period = start + relativedelta(months=1)
        return (next_period - dt.timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=0)
    if period == 'quarter':
        start = _period_start(current, 'quarter')
        next_period = start + relativedelta(months=3)
        return (next_period - dt.timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=0)
    if period == 'year':
        start = _period_start(current, 'year')
        return start.replace(month=12, day=31, hour=23, minute=59, second=59, microsecond=0)
    raise ValueError(f"Unknown period '{period}'.")


def _navigate(current, op):
    period = op.get('period')
    if period not in _PERIODS:
        raise ValueError(f"Unknown navigate period '{period}'.")
    offset = int(op.get('offset', 0) or 0)
    boundary = op.get('boundary', 'none') or 'none'
    if boundary not in _BOUNDARIES:
        raise ValueError(f"Unknown boundary '{boundary}'.")

    if period == 'minute':
        current = current + relativedelta(minutes=offset)
    elif period == 'hour':
        current = current + relativedelta(hours=offset)
    elif period == 'day':
        current = current + relativedelta(days=offset)
    elif period == 'week':
        current = current + relativedelta(weeks=offset)
    elif period == 'month':
        current = current + relativedelta(months=offset)
    elif period == 'quarter':
        current = current + relativedelta(months=offset * 3)
    elif period == 'year':
        current = current + relativedelta(years=offset)

    if boundary == 'start':
        current = _period_start(current, period)
    elif boundary == 'end':
        current = _period_end(current, period)
    return current


def _find_weekday(current, op):
    weekday = op.get('weekday')
    if not isinstance(weekday, int) or isinstance(weekday, bool) or not 0 <= weekday <= 6:
        raise ValueError(f"Invalid weekday '{weekday}'.")
    direction = -1 if op.get('type') == 'find_previous' else 1
    candidate = current
    for _step in range(7):
        candidate = candidate + dt.timedelta(days=direction)
        if candidate.weekday() == weekday:
            return candidate
    raise ValueError(f"Could not find weekday '{weekday}'.")  # pragma: no cover - unreachable


def _apply_operation(current, op):
    op_type = op.get('type')
    if op_type == 'navigate':
        return _navigate(current, op)
    if op_type in ('find_previous', 'find_next'):
        return _find_weekday(current, op)
    raise ValueError(f"Unknown operation type '{op_type}'.")


def _pin_nth_weekday(current, weekday, occurrence):
    if not isinstance(weekday, int) or isinstance(weekday, bool) or not 0 <= weekday <= 6:
        raise ValueError(f"Invalid weekday '{weekday}'.")
    if not isinstance(occurrence, int) or isinstance(occurrence, bool) or occurrence == 0 or not -5 <= occurrence <= 5:
        raise ValueError(f"Invalid occurrence '{occurrence}'.")

    last_day = _days_in_month(current.year, current.month)
    if occurrence > 0:
        first = current.replace(day=1)
        offset = (weekday - first.weekday()) % 7
        day_num = 1 + offset + (occurrence - 1) * 7
        if day_num > last_day:
            raise ValueError(f"No occurrence {occurrence} of weekday {weekday} in this month.")
    else:
        last = current.replace(day=last_day)
        offset = (last.weekday() - weekday) % 7
        day_num = last_day - offset - (abs(occurrence) - 1) * 7
        if day_num < 1:
            raise ValueError(f"No occurrence {occurrence} of weekday {weekday} in this month.")
    return current.replace(day=day_num)


def _parse_time(time_str):
    parts = time_str.split(':')
    if len(parts) not in (2, 3):
        raise ValueError(f"Invalid time '{time_str}'.")
    try:
        hour, minute = int(parts[0]), int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
    except ValueError:
        raise ValueError(f"Invalid time '{time_str}'.") from None
    if not (0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60):
        raise ValueError(f"Invalid time '{time_str}'.")
    return hour, minute, second


def _apply_pin(current, pin):
    if not isinstance(pin, dict):
        raise ValueError("Invalid pin: expected an object.")

    day = pin.get('day')
    if isinstance(day, dict):
        current = _pin_nth_weekday(current, day.get('weekday'), day.get('occurrence'))
    elif day is not None:
        if isinstance(day, bool) or not isinstance(day, int):
            raise ValueError(f"Invalid pin day '{day}'.")
        # 0 is the placeholder models send for "no day" (like "" for the
        # time): leave the day alone rather than clamping it to the 1st.
        if day != 0:
            last_day = _days_in_month(current.year, current.month)
            current = current.replace(day=max(1, min(day, last_day)))

    time_str = pin.get('time')
    if time_str:
        hour, minute, second = _parse_time(time_str)
        current = current.replace(hour=hour, minute=minute, second=second, microsecond=0)

    return current


@builtin_tool('dates.compute_date')
def compute_date(ctx, model_name, field_name, operations=None, pin=None):
    """Compute an exact date/datetime boundary in the user's timezone."""
    env = ctx.env
    model = check_model_access(env, model_name, 'read')

    field = model._fields.get(field_name)
    if field is None or field.type not in ('date', 'datetime'):
        raise ValueError(f"Field '{field_name}' on '{model_name}' is not a date/datetime field.")
    field_type = field.type

    tz = env.tz
    current = dt.datetime.now(_arithmetic_zone(tz))

    for op in (operations or []):
        current = _apply_operation(current, op)

    if pin:
        current = _apply_pin(current, pin)

    if field_type == 'date':
        value = current.date().isoformat()
    else:
        value = current.astimezone(dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

    human = f"{current.strftime('%A %-d %B %Y %H:%M')} ({tz})"

    return ToolResult(response={
        'value': value,
        'field_type': field_type,
        'timezone': str(tz),
        'human': human,
    })
