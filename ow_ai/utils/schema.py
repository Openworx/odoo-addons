# -*- coding: utf-8 -*-
"""JSON-schema subset for AI tool definitions and argument validation.

validate_schema(schema) checks a *tool definition's* parameter schema
(raises SchemaError). validate_args(args, schema) validates and coerces
*LLM-supplied arguments* against an already-valid schema (raises
ArgumentError), returning a new, JSON-serialisable dict. Error messages
are safe to send back to the LLM: no tracebacks, no internal paths, just
a JSON path and what went wrong.
"""
import json
import re

PRIMITIVE_TYPES = ('string', 'number', 'integer', 'boolean', 'array', 'object', 'null')
_JSON_SCALAR_TYPES = {str: 'string', bool: 'boolean', int: 'integer', float: 'number'}
_RANGE_KEYS = (('minLength', 'maxLength'), ('minimum', 'maximum'), ('minItems', 'maxItems'))


class SchemaError(ValueError):
    """Raised when a tool parameter schema itself is invalid."""


class ArgumentError(ValueError):
    """Raised when arguments do not satisfy an (assumed valid) schema."""


def _types_of(prop_schema):
    """Return the list of JSON types declared by a property schema."""
    type_value = prop_schema.get('type')
    if isinstance(type_value, list):
        return type_value
    return [] if type_value is None else [type_value]


# ---------------------------------------------------------------------------
# Schema (tool definition) validation
# ---------------------------------------------------------------------------

def validate_schema(schema):
    """Validate a tool parameter schema, raising SchemaError if invalid."""
    if not isinstance(schema, dict):
        raise SchemaError('root: schema must be an object')
    if schema.get('type') != 'object':
        raise SchemaError("root: schema 'type' must be 'object'")
    _validate_object_schema(schema, 'root')


def _validate_object_schema(schema, path):
    properties = schema.get('properties', {})
    if not isinstance(properties, dict):
        raise SchemaError(f'{path}.properties: must be an object')

    required = schema.get('required', [])
    if not isinstance(required, list):
        raise SchemaError(f'{path}.required: must be a list')
    for name in required:
        if name not in properties:
            raise SchemaError(f"{path}.required: '{name}' is not defined in properties")

    additional = schema.get('additionalProperties')
    if additional is not None and not isinstance(additional, bool):
        raise SchemaError(f'{path}.additionalProperties: must be a boolean')

    for prop_name, prop_schema in properties.items():
        prop_path = f'{path}.properties.{prop_name}'
        if not isinstance(prop_schema, dict):
            raise SchemaError(f'{prop_path}: must be an object')
        _validate_property_schema(prop_schema, prop_path)


def _validate_range_keys(prop_schema, path):
    for low_key, high_key in _RANGE_KEYS:
        expected = int if low_key != 'minimum' else (int, float)
        for key in (low_key, high_key):
            if key in prop_schema and not isinstance(prop_schema[key], expected):
                raise SchemaError(f'{path}.{key}: must be a number')
        if low_key in prop_schema and high_key in prop_schema:
            if prop_schema[low_key] > prop_schema[high_key]:
                raise SchemaError(f'{path}: {low_key} must not exceed {high_key}')


def _validate_property_schema(prop_schema, path):
    if 'anyOf' in prop_schema:
        any_of = prop_schema['anyOf']
        if not isinstance(any_of, list) or not any_of:
            raise SchemaError(f'{path}.anyOf: must be a non-empty list')
        for index, sub_schema in enumerate(any_of):
            if not isinstance(sub_schema, dict):
                raise SchemaError(f'{path}.anyOf[{index}]: must be an object')
            _validate_property_schema(sub_schema, f'{path}.anyOf[{index}]')
        return

    types = _types_of(prop_schema)
    if not types:
        raise SchemaError(f"{path}: missing 'type' (or use 'anyOf')")
    for type_name in types:
        if type_name not in PRIMITIVE_TYPES:
            raise SchemaError(
                f"{path}: unknown type '{type_name}'; expected one of {', '.join(PRIMITIVE_TYPES)}")

    if 'enum' in prop_schema and not isinstance(prop_schema['enum'], list):
        raise SchemaError(f'{path}.enum: must be a list')

    if 'pattern' in prop_schema:
        try:
            re.compile(prop_schema['pattern'])
        except (re.error, TypeError) as exc:
            raise SchemaError(f'{path}.pattern: invalid regular expression ({exc})') from exc

    _validate_range_keys(prop_schema, path)

    if 'array' in types:
        items = prop_schema.get('items')
        if items is None:
            raise SchemaError(f"{path}: array must declare 'items'")
        if not isinstance(items, dict):
            raise SchemaError(f'{path}.items: must be an object')
        _validate_property_schema(items, f'{path}.items')

    if 'object' in types:
        _validate_object_schema(prop_schema, path)


# ---------------------------------------------------------------------------
# Argument validation / coercion
# ---------------------------------------------------------------------------

def validate_args(args, schema):
    """Validate and coerce LLM-supplied arguments against `schema`.

    Returns a new dict; never mutates `args`. Raises ArgumentError.
    """
    if not isinstance(args, dict):
        raise ArgumentError('arguments: expected an object')
    return _validate_object_args(args, schema, '')


def _validate_object_args(args, schema, path):
    if not isinstance(args, dict):
        raise ArgumentError(f'{_label(path)}expected object')

    properties = schema.get('properties', {})
    required = schema.get('required', [])
    additional_allowed = schema.get('additionalProperties', False)

    missing = [name for name in required if name not in args]
    if missing:
        raise ArgumentError(f"{_label(path)}missing required argument(s): {', '.join(missing)}")

    result = {}
    for name, value in args.items():
        prop_schema = properties.get(name)
        child_path = f'{path}.{name}' if path else name
        if prop_schema is None:
            if additional_allowed:
                result[name] = value
            continue

        if value is None:
            if _null_allowed(prop_schema):
                result[name] = None
            elif name in required:
                raise ArgumentError(f'{child_path}: null is not allowed')
            # optional, non-nullable, explicit None -> drop the key
            continue

        result[name] = _validate_value(value, prop_schema, child_path)

    return result


def _null_allowed(prop_schema):
    if 'anyOf' in prop_schema:
        return any(_null_allowed(sub) for sub in prop_schema['anyOf'])
    return 'null' in _types_of(prop_schema)


def _label(path):
    return f'{path}: ' if path else ''


def _validate_value(value, prop_schema, path):
    if 'anyOf' in prop_schema:
        last_error = None
        for sub_schema in prop_schema['anyOf']:
            try:
                return _validate_value(value, sub_schema, path)
            except ArgumentError as exc:
                last_error = exc
        raise ArgumentError(f'{path}: value did not match any allowed schema ({last_error})')

    types = [t for t in _types_of(prop_schema) if t != 'null']
    last_error = None
    for type_name in types:
        try:
            return _validate_typed_value(value, type_name, prop_schema, path)
        except ArgumentError as exc:
            last_error = exc
    if len(types) <= 1:
        raise last_error if last_error is not None else ArgumentError(f'{path}: no type declared')
    raise ArgumentError(f'{path}: value did not match any allowed type ({last_error})')


def _validate_typed_value(value, type_name, prop_schema, path):
    if type_name == 'string':
        return _validate_string(value, prop_schema, path)
    if type_name in ('integer', 'number'):
        return _validate_numeric(value, prop_schema, path, integer=type_name == 'integer')
    if type_name == 'boolean':
        return _validate_boolean(value, path)
    if type_name == 'array':
        return _validate_array(value, prop_schema, path)
    if type_name == 'object':
        return _validate_object_args(_coerce_object(value, path), prop_schema, path)
    raise ArgumentError(f"{path}: unsupported type '{type_name}'")


def _coerce_object(value, path):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError as exc:
            raise ArgumentError(f'{path}: expected object, could not parse JSON string ({exc})') from exc
        if not isinstance(parsed, dict):
            raise ArgumentError(f'{path}: expected object')
        return parsed
    raise ArgumentError(f'{path}: expected object')


def _validate_string(value, prop_schema, path):
    if not isinstance(value, str):
        raise ArgumentError(f'{path}: expected string')
    if 'maxLength' in prop_schema:
        value = value[:prop_schema['maxLength']]
    if 'minLength' in prop_schema and len(value) < prop_schema['minLength']:
        raise ArgumentError(f"{path}: string shorter than minLength {prop_schema['minLength']}")
    if 'pattern' in prop_schema and not re.search(prop_schema['pattern'], value):
        raise ArgumentError(f"{path}: does not match pattern '{prop_schema['pattern']}'")
    _check_enum(value, prop_schema, path)
    return value


def _validate_numeric(value, prop_schema, path, integer):
    kind = 'integer' if integer else 'number'
    if isinstance(value, bool):
        raise ArgumentError(f'{path}: expected {kind}')
    if isinstance(value, int):
        coerced = value
    elif isinstance(value, float):
        if integer:
            if not value.is_integer():
                raise ArgumentError(f'{path}: expected integer, got non-integral number')
            coerced = int(value)
        else:
            coerced = value
    elif isinstance(value, str):
        try:
            coerced = int(value.strip()) if integer else float(value.strip())
        except ValueError as exc:
            raise ArgumentError(f'{path}: expected {kind}, could not parse string') from exc
    else:
        raise ArgumentError(f'{path}: expected {kind}')

    if 'minimum' in prop_schema and coerced < prop_schema['minimum']:
        raise ArgumentError(f"{path}: below minimum {prop_schema['minimum']}")
    if 'maximum' in prop_schema and coerced > prop_schema['maximum']:
        raise ArgumentError(f"{path}: above maximum {prop_schema['maximum']}")
    _check_enum(coerced, prop_schema, path)
    return coerced


def _check_enum(value, prop_schema, path):
    if 'enum' in prop_schema and value not in prop_schema['enum']:
        raise ArgumentError(f"{path}: value not in allowed set {prop_schema['enum']}")


def _validate_boolean(value, path):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ('true', 'false'):
        return value.strip().lower() == 'true'
    raise ArgumentError(f'{path}: expected boolean')


def _validate_array(value, prop_schema, path):
    items_schema = prop_schema.get('items', {})

    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            value = parsed
        elif _scalar_matches_items(value, items_schema):
            value = [value]
        else:
            raise ArgumentError(f'{path}: expected array, could not parse JSON string')
    elif not isinstance(value, list):
        if _scalar_matches_items(value, items_schema):
            value = [value]
        else:
            raise ArgumentError(f'{path}: expected array')

    if 'minItems' in prop_schema and len(value) < prop_schema['minItems']:
        raise ArgumentError(f"{path}: fewer than minItems {prop_schema['minItems']}")
    if 'maxItems' in prop_schema and len(value) > prop_schema['maxItems']:
        raise ArgumentError(f"{path}: more than maxItems {prop_schema['maxItems']}")

    return [_validate_value(item, items_schema, f'{path}[{index}]') for index, item in enumerate(value)]


def _scalar_matches_items(value, items_schema):
    """True if a bare scalar's JSON type matches the array's item type(s)."""
    if isinstance(value, list):
        return False
    item_types = set(_types_of(items_schema))
    for sub in items_schema.get('anyOf', []):
        item_types |= set(_types_of(sub))
    value_type = _JSON_SCALAR_TYPES.get(type(value))
    if value_type is None:
        return False
    return value_type in item_types or (value_type == 'integer' and 'number' in item_types)


def schema_summary(schema):
    """One-line human summary, e.g. "model_name: string (required); limit: integer"."""
    properties = schema.get('properties', {})
    required = set(schema.get('required', []))
    parts = []
    for name, prop_schema in properties.items():
        type_value = prop_schema.get('type')
        if isinstance(type_value, list):
            type_label = '|'.join(type_value)
        elif type_value is not None:
            type_label = type_value
        elif 'anyOf' in prop_schema:
            type_label = '|'.join('|'.join(_types_of(s)) or 'any' for s in prop_schema['anyOf'])
        else:
            type_label = 'any'
        suffix = ' (required)' if name in required else ''
        parts.append(f'{name}: {type_label}{suffix}')
    return '; '.join(parts)
