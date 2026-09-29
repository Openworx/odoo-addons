# -*- coding: utf-8 -*-
"""Model access control for AI tools: blocklists, access checks, domain parsing.

Tools (built-in and server-action based) act on arbitrary models chosen by the
LLM. Everything here runs in the calling user's own environment (never
``sudo``) so that record rules and ACLs are enforced as usual; on top of that
we apply an explicit blocklist of models the assistant must never touch, even
if the user technically has ORM access to them (security-sensitive or
infrastructure models).

``utils/serialize.py`` is a separate module (record serialisation), imported
by callers alongside this one; the two are kept distinct per the module's
design brief rather than merged into a single "utils" file.
"""
from __future__ import annotations

import ast
import json

from odoo.exceptions import AccessError
from odoo.fields import Domain

# Models never exposed to the assistant, regardless of the user's own access
# rights: security/authentication internals, API keys, buses, automation
# rules that could be used to escalate, etc.
MODEL_BLOCKLIST = frozenset({
    'base.automation', 'res.groups', 'res.groups.privilege', 'res.users.settings', 'res.users.apikeys',
    'res.users.apikeys.description', 'ir.config_parameter', 'res.users.log', 'auth.totp.device', 'res.device',
    'res.device.log', 'bus.bus', 'bus.presence', 'mail.mail',
})

# Model name prefixes never exposed to the assistant. Note: 'ir.attachment',
# 'ir.ui.menu', 'ir.model' and 'ir.model.fields' start with 'ir.' but are
# explicitly ALLOWED for read (they are excluded from this tuple's effect by
# check_model_access, see below).
MODEL_PREFIX_BLOCKLIST = ('ir.', 'ow.ai.', 'base_import.', 'web_tour.', 'auth_', 'change.password')

# Exact models that always stay read-only for the assistant, even when the
# calling user has write access via their own ACLs/record rules.
WRITE_BLOCKLIST = frozenset({
    'res.company', 'res.users', 'res.partner.bank', 'ir.attachment', 'ir.ui.menu', 'ir.model', 'ir.model.fields',
    'mail.message', 'mail.template', 'mail.scheduled.message', 'discuss.channel', 'discuss.channel.member',
    'website.page', 'website.menu',
})

# 'ir.' prefixed models that stay readable despite the 'ir.' prefix blocklist.
_IR_PREFIX_READ_ALLOWLIST = frozenset({'ir.attachment', 'ir.ui.menu', 'ir.model', 'ir.model.fields'})

_MAX_DOMAIN_CHARS = 5000
_MAX_DOMAIN_DEPTH = 20


class ModelAccessError(Exception):
    """Raised when a model is not available to the assistant.

    The message is safe to show to the LLM as-is: no tracebacks, no
    filesystem paths, no internal identifiers beyond the model name itself.
    """


def _is_prefix_blocked(model_name):
    for prefix in MODEL_PREFIX_BLOCKLIST:
        if model_name.startswith(prefix) and model_name not in _IR_PREFIX_READ_ALLOWLIST:
            return True
    return False


def check_model_access(env, model_name: str, operation: str = 'read'):
    """Check that the assistant may perform ``operation`` on ``model_name``.

    Returns ``env[model_name]`` (an empty recordset) on success. Raises
    ``ModelAccessError`` when the model is unknown, transient/abstract in a
    way that forbids the operation, blocklisted, or when the calling user's
    own ACLs deny the operation (an ``AccessError`` is caught and re-raised
    as a ``ModelAccessError`` with an LLM-safe message).

    Never runs as superuser: raises ``RuntimeError`` if ``env.su`` is set,
    since tools must always run under the calling user's own rights (an
    explicit check, not an ``assert``, so it also holds under ``python -O``).
    """
    if env.su:
        raise RuntimeError("check_model_access must never run with env.su")

    if model_name in MODEL_BLOCKLIST or _is_prefix_blocked(model_name):
        raise ModelAccessError(f"Model '{model_name}' is not available to the assistant.")

    try:
        model = env[model_name]
    except KeyError as exc:
        raise ModelAccessError(f"Model '{model_name}' is not available to the assistant.") from exc

    if model._abstract:
        raise ModelAccessError(f"Model '{model_name}' is not available to the assistant.")

    if model._transient and operation != 'read':
        raise ModelAccessError(f"Model '{model_name}' is not available to the assistant.")

    if operation != 'read' and model_name in WRITE_BLOCKLIST:
        raise ModelAccessError(f"You do not have write access to '{model_name}'.")

    try:
        model.check_access(operation)
    except AccessError as exc:
        verb = 'read' if operation == 'read' else operation
        raise ModelAccessError(f"You do not have {verb} access to '{model_name}'.") from exc

    return model


def _check_domain_depth(domain, depth=0):
    if depth > _MAX_DOMAIN_DEPTH:
        raise ValueError("Invalid domain: too deeply nested.")
    if isinstance(domain, (list, tuple)):
        for item in domain:
            _check_domain_depth(item, depth + 1)


def parse_domain(env, model, domain_input):
    """Parse, size/depth-check and validate a domain for ``model``.

    ``domain_input`` may already be a Python list (as parsed from JSON tool
    arguments), or a string (parsed with ``ast.literal_eval`` first, falling
    back to ``json.loads``). Raises ``ValueError`` with an LLM-safe message
    ("Invalid domain: <reason>") on any problem.
    """
    if isinstance(domain_input, str):
        if len(domain_input) > _MAX_DOMAIN_CHARS:
            raise ValueError("Invalid domain: too long.")
        try:
            domain = ast.literal_eval(domain_input)
        except (ValueError, SyntaxError):
            try:
                domain = json.loads(domain_input)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid domain: {exc}") from None
    elif isinstance(domain_input, (list, tuple)):
        domain = domain_input
    else:
        raise ValueError("Invalid domain: expected a list or a string.")

    if not isinstance(domain, (list, tuple)):
        raise ValueError("Invalid domain: expected a list.")

    _check_domain_depth(domain)

    model_recordset = env[model] if isinstance(model, str) else model

    try:
        parsed = Domain(domain)
        parsed.validate(model_recordset)
    except ValueError as exc:
        raise ValueError(f"Invalid domain: {exc}") from None
    except Exception as exc:  # noqa: BLE001 - normalise any ORM-side error to an LLM-safe message
        raise ValueError(f"Invalid domain: {exc}") from None

    return parsed
