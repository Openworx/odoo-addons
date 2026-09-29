# -*- coding: utf-8 -*-
"""Typed ``ir.config_parameter`` access (Odoo 19).

Odoo 19 only offers ``get_param``/``set_param`` on strings; the 20.0 branch
calls the typed ``get_str/get_int/get_float/get_bool`` and ``set_*`` methods
of ``ir.config_parameter`` instead. These helpers give the call sites the same
shape (``params.get_int(env, key, default)``) so that the rest of the module
stays identical on both branches.

Reads and writes go through ``sudo()``: every caller used to do so, and the
engine reads its limits in the requesting user's environment, which may not
read ``ir.config_parameter``. An empty or missing value returns the default;
a value that does not parse returns the default and logs a warning, so a
mistyped setting never crashes a chat round.
"""
from __future__ import annotations

import logging

_logger = logging.getLogger(__name__)

_TRUE_VALUES = frozenset({'true', '1', 'yes', 'on'})
_FALSE_VALUES = frozenset({'false', '0', 'no', 'off', ''})


def _get(env, key):
    """Return the raw string of ``key``, or ``None`` when it is unset or empty."""
    value = env['ir.config_parameter'].sudo().get_param(key)
    if value is False or value is None or value == '':
        return None
    return value


def _invalid(key, value, type_name, default):
    _logger.warning(
        "ir.config_parameter %s has invalid value %r for type %s, using %r instead",
        key, value, type_name, default)
    return default


def get_str(env, key, default=''):
    value = _get(env, key)
    return default if value is None else value


def get_int(env, key, default=0):
    value = _get(env, key)
    if value is None:
        return default
    try:
        return int(value.strip())
    except ValueError:
        return _invalid(key, value, 'int', default)


def get_float(env, key, default=0.0):
    value = _get(env, key)
    if value is None:
        return default
    try:
        return float(value.strip())
    except ValueError:
        return _invalid(key, value, 'float', default)


def get_bool(env, key, default=False):
    value = _get(env, key)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return _invalid(key, value, 'bool', default)


def _set(env, key, value):
    """Store ``value`` (``None``/``False`` unsets the parameter)."""
    env['ir.config_parameter'].sudo().set_param(key, False if value is None or value is False else value)


def set_str(env, key, value):
    """Store ``value``; an empty string unsets the parameter."""
    _set(env, key, str(value) if value else None)


def set_int(env, key, value):
    _set(env, key, None if value is None or value is False else str(value))


def set_float(env, key, value):
    _set(env, key, None if value is None or value is False else str(value))


def set_bool(env, key, value):
    """Store ``'True'``/``'False'``: unlike ``set_param(key, False)``, a stored
    ``'False'`` survives a default of ``True``."""
    _set(env, key, None if value is None else str(bool(value)))
