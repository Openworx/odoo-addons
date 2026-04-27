"""Permission gate for MCP calls.

Layers (all must pass):
  1. Hard blocklist — models that are NEVER exposed, even in YOLO full.
  2. MCP access config (ow.mcp.model.access) — unless YOLO bypasses it.
  3. Odoo native ACL (check_access_rights) — always enforced.
"""
import logging
import os
import re

from odoo.exceptions import AccessError
from odoo.tools import config as odoo_config


_logger = logging.getLogger(__name__)


BLOCKLIST_RE = re.compile(
    r'^('
    # ir.* high-value surface: RCE, config, secrets, views, scheduling, ACL.
    r'ir\.(rule|config_parameter|logging|attachment|attachment\.history'
    r'|actions\.server|actions\.act_url|cron|ui\.view|mail_server'
    r'|model\.access|model\.data|filters|module\.module|sequence'
    r'|autovacuum|http)'
    # Users, API keys, auth helpers, automation.
    r'|res\.users(\.apikeys.*)?'
    r'|res\.users\.identitycheck'
    r'|res\.users\.log'
    r'|auth_.*'
    r'|base_automation.*'
    r'|base_import\..*'
    # Mail transport / outbound credentials.
    r'|fetchmail\.server'
    r'|mail\.mail'
    # Odoo internal registry / bus.
    r'|bus\..*'
    r')$'
)

_OP_FIELD = {
    'read': 'allow_read',
    'write': 'allow_write',
    'create': 'allow_create',
    'unlink': 'allow_delete',
}
_OP_ALIASES = {'delete': 'unlink'}


def check_model_access(env, model_name, op):
    """Raise AccessError unless all layers allow `op` on `model_name` for env.user."""
    op = _OP_ALIASES.get(op, op)
    if op not in _OP_FIELD:
        raise AccessError(f'Unknown MCP op: {op!r}')

    if BLOCKLIST_RE.match(model_name):
        raise AccessError(
            f'Model {model_name!r} is permanently blocked by the MCP server.'
        )

    if model_name not in env:
        raise AccessError(f'Model {model_name!r} does not exist.')

    cfg = env['ow.mcp.config'].sudo().get_singleton()
    yolo = _effective_yolo_mode(cfg)
    skip_mcp_layer = (yolo == 'full') or (yolo == 'read' and op == 'read')

    if not skip_mcp_layer:
        ma = env['ow.mcp.model.access'].sudo().search([
            ('model_name', '=', model_name),
            ('active', '=', True),
        ], limit=1)
        if not ma:
            raise AccessError(f'Model {model_name!r} is not exposed via MCP.')
        if not ma[_OP_FIELD[op]]:
            raise AccessError(f'Op {op!r} not allowed on {model_name!r} via MCP.')
        if ma.group_ids and not (env.user.groups_id & ma.group_ids):
            raise AccessError(
                f'User lacks group membership for MCP access to {model_name!r}.'
            )

    # Odoo-native enforcement, always:
    env[model_name].check_access_rights(op, raise_exception=True)
    return True


def _dev_or_test_mode():
    """True when the server runs with --dev=* or --test-enable.

    dev_mode is read from CLI only (Odoo ignores it in odoo.conf); test_enable
    is set by the test runner. Production never sets either, so this gates
    YOLO tightly to dev/test environments.
    """
    return bool(odoo_config.get('dev_mode')) or bool(
        odoo_config.get('test_enable'),
    )


def _effective_yolo_mode(cfg):
    """Return the yolo_mode that is actually honored at runtime.

    The config field may be set to 'read'/'full' but those modes are only
    active when BOTH conditions hold:
      - the server runs with --dev=* or --test-enable
      - the env var OW_MCP_ALLOW_YOLO is '1'

    Otherwise we silently downgrade to 'off' so that an accidentally-flipped
    field in production cannot open up access.
    """
    mode = cfg.yolo_mode if cfg else 'off'
    if mode == 'off':
        return 'off'
    if not _dev_or_test_mode():
        _logger.warning(
            'MCP: yolo_mode=%r ignored — server is not in --dev or '
            '--test-enable mode.', mode,
        )
        return 'off'
    if os.environ.get('OW_MCP_ALLOW_YOLO') != '1':
        _logger.warning(
            'MCP: yolo_mode=%r ignored — set OW_MCP_ALLOW_YOLO=1 in the '
            'environment to confirm.', mode,
        )
        return 'off'
    return mode


def get_model_allowlist(env, model_name):
    """Return the configured allow-list or None if no allow-list is set.

    Callers must distinguish None (unrestricted) from [] (deny-all).
    """
    ma = env['ow.mcp.model.access'].sudo().search(
        [('model_name', '=', model_name)], limit=1,
    )
    if not ma:
        return None
    return ma.allowed_fields()  # None = not set, [] = explicit deny-all, [...] = fields


def filter_fields(env, model_name, requested_fields):
    """Return the subset of requested_fields permitted by ow.mcp.model.access.

    If the model has no allow-list (allowed_fields_json empty/missing), the
    caller's list is returned unchanged. If the allow-list is empty list [],
    nothing is allowed.
    """
    ma = env['ow.mcp.model.access'].sudo().search(
        [('model_name', '=', model_name)], limit=1,
    )
    if not ma:
        return requested_fields
    allowed = ma.allowed_fields()
    if allowed is None:
        return requested_fields
    if not requested_fields:
        return list(allowed)
    return [f for f in requested_fields if f in allowed]


def per_model_limit(env, model_name, global_cap):
    """Return the narrower of (ow.mcp.model.access.max_records, global_cap).

    max_records=0 means "inherit the global cap", consistent with the field's
    help text.
    """
    ma = env['ow.mcp.model.access'].sudo().search(
        [('model_name', '=', model_name)], limit=1,
    )
    per = (ma.max_records or 0) if ma else 0
    if per <= 0:
        return global_cap
    return min(per, global_cap)


# Leaf operators + domain connectors we tolerate. Everything else (custom
# operators, malformed entries) is rejected.
_DOMAIN_CONNECTORS = frozenset(('&', '|', '!'))


def assert_domain_fields_allowed(env, model_name, domain):
    """Reject search domains that reference forbidden columns.

    Applied to `search_records`, `search_count`, and `read_group` so a
    caller cannot probe forbidden columns — e.g. a count oracle on
    `password ilike '$2b$%'` — by hiding them inside a domain.

    Dotted paths are walked hop-by-hop (H2). Each hop is rejected when:
      - the current model matches BLOCKLIST_RE, or
      - a non-root hop traverses into a model with no ow.mcp.model.access
        row (i.e. not exposed via MCP), or
      - the field at that hop is outside the per-model allow-list.

    Walking stops at the first non-relational field (the leaf) or at an
    unknown field name (deferred to the ORM).
    """
    if not domain:
        return
    bad = []
    for atom in domain:
        if isinstance(atom, str):
            if atom in _DOMAIN_CONNECTORS:
                continue
            bad.append(atom)
            continue
        if not isinstance(atom, (list, tuple)) or len(atom) != 3:
            bad.append(repr(atom))
            continue
        field_expr = atom[0]
        if not isinstance(field_expr, str):
            bad.append(repr(atom))
            continue
        _walk_domain_path(env, model_name, field_expr)
    if bad:
        raise AccessError(
            f'Malformed domain on {model_name}: {bad}'
        )


def _walk_domain_path(env, model_name, dotted):
    """Validate every hop of a (possibly dotted) domain path."""
    parts = dotted.split('.')
    current = model_name
    for i, hop in enumerate(parts):
        if BLOCKLIST_RE.match(current):
            raise AccessError(
                f'Domain traversal reaches blocked model {current!r} '
                f'via {dotted!r}.'
            )
        if current not in env:
            raise AccessError(
                f'Unknown model {current!r} in domain path {dotted!r}.'
            )
        Model = env[current]
        ma = env['ow.mcp.model.access'].sudo().search(
            [('model_name', '=', current)], limit=1,
        )
        if i == 0:
            # Root model: preserve the original noop semantics — when no
            # MA row exists, leave gating to check_model_access elsewhere.
            if not ma:
                return
        else:
            # Non-root hop: traversal into another model. Require an MA row;
            # otherwise the dotted path could reach data whose MCP allow-list
            # was never consulted.
            if not ma:
                raise AccessError(
                    f'Domain traverses into {current!r} which is not '
                    f'exposed via MCP (path: {dotted!r}).'
                )
        allow = ma.allowed_fields() if ma else None
        if allow is not None and hop not in allow:
            raise AccessError(
                f'Domain references field {current}.{hop!r} not allowed '
                f'via MCP (path: {dotted!r}).'
            )
        f = Model._fields.get(hop)
        if f is None:
            # Unknown field at this hop — defer to the ORM's own error.
            return
        if not f.relational:
            return  # leaf reached
        current = f.comodel_name


def assert_fields_allowed(env, model_name, fields):
    """Raise AccessError if any field is outside the model's allow-list (when set)."""
    ma = env['ow.mcp.model.access'].sudo().search(
        [('model_name', '=', model_name)], limit=1,
    )
    if not ma:
        return
    allowed = ma.allowed_fields()
    if allowed is None:
        return
    extra = [f for f in fields if f not in allowed]
    if extra:
        raise AccessError(f'Fields not allowed via MCP on {model_name}: {extra}')


# x2many command opcodes that mutate data beyond a simple link/unlink.
# 0 = create, 1 = update, 2 = unlink-and-delete. We block these because they
# write to a *different* model whose MCP allow-list would otherwise be
# bypassed via the parent's allow-list. Opcodes 3 (forget), 4 (link), 5
# (clear), 6 (replace) only touch relation rows, not the sub-model's fields.
_FORBIDDEN_X2M_OPS = frozenset({0, 1, 2})


def assert_no_nested_writes(env, model_name, values):
    """Reject x2many create/update/unlink commands in a write values dict.

    Protects the MCP field allow-list: without this, a caller with write
    access to model A can inject `child_ids=[(0, 0, {...})]` and create or
    mutate records on a related model B whose MCP allow-list was never
    consulted. Only the safe link-only opcodes (3, 4, 5, 6) are permitted.
    """
    if not isinstance(values, dict):
        return
    model = env[model_name] if model_name in env else None
    model_fields = getattr(model, '_fields', {}) if model is not None else {}
    for fname, val in values.items():
        field = model_fields.get(fname)
        if field is None or field.type not in ('one2many', 'many2many'):
            continue
        if not isinstance(val, (list, tuple)):
            continue
        for cmd in val:
            if not isinstance(cmd, (list, tuple)) or not cmd:
                raise AccessError(
                    f'Invalid x2many command on {model_name}.{fname!r}; '
                    f'only opcodes 3/4/5/6 (link-only) are allowed via MCP.'
                )
            op = cmd[0]
            if op in _FORBIDDEN_X2M_OPS:
                raise AccessError(
                    f'Nested write on {model_name}.{fname!r} is not '
                    f'permitted via MCP (opcode {op}). Use separate '
                    f'create_record / update_record / delete_record calls '
                    f'against the related model so its allow-list applies.'
                )
