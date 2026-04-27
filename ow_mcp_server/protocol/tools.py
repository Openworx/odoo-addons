"""MCP tool implementations.

Each tool takes `(env, params)` and returns a JSON-serialisable dict.
Permission checks and field filtering are delegated to `access` and
serialisation to `formatter`.
"""
from odoo.exceptions import AccessError, UserError

from . import access as acc
from . import formatter as fmt


_DEFAULT_FIELDS = ('id', 'display_name')


def _resolve_fields(env, model_name, requested):
    """Resolve the field list for a read op, honoring the model's allow-list.

    - Explicit request whose intersection with the allow-list is empty →
      AccessError (prevents silently dropping every requested field).
    - Empty/None request + explicit deny-all allow-list → AccessError.
    - Empty/None request + no allow-list configured → _DEFAULT_FIELDS.
    - Empty/None request + non-empty allow-list → use the allow-list.
    """
    if requested:
        allowed = acc.filter_fields(env, model_name, list(requested))
        if not allowed:
            raise AccessError(
                f'None of the requested fields are allowed via MCP '
                f'on {model_name!r}.'
            )
        return allowed
    allowlist = acc.get_model_allowlist(env, model_name)
    if allowlist is None:
        return list(_DEFAULT_FIELDS)
    if not allowlist:
        raise AccessError(
            f'No fields are accessible via MCP on {model_name!r}.'
        )
    return list(allowlist)


# ---------- list_models -------------------------------------------------------

def list_models(env, params):
    """Return the models exposed via MCP and their capabilities."""
    rows = env['ow.mcp.model.access'].sudo().search([('active', '=', True)])
    out = []
    for r in rows:
        if r.model_name not in env:
            continue
        model = env[r.model_name]
        out.append({
            'model': r.model_name,
            'label': model._description or r.model_name,
            'can_read': r.allow_read,
            'can_write': r.allow_write,
            'can_create': r.allow_create,
            'can_delete': r.allow_delete,
            'allowed_fields': r.allowed_fields(),
        })
    return {'models': out}


# ---------- search_records ----------------------------------------------------

def search_records(env, params):
    model_name = params['model']
    acc.check_model_access(env, model_name, 'read')
    domain = params.get('domain') or []
    acc.assert_domain_fields_allowed(env, model_name, domain)
    fields = _resolve_fields(env, model_name, params.get('fields'))
    cfg = env['ow.mcp.config'].sudo().get_singleton()
    requested = int(params.get('limit') or cfg.default_limit)
    hard_cap = acc.per_model_limit(env, model_name, cfg.max_limit)
    limit = min(requested, hard_cap)
    offset = int(params.get('offset') or 0)
    order = params.get('order') or 'id asc'
    Model = env[model_name]
    total = Model.search_count(domain)
    recs = Model.search(domain, limit=limit, offset=offset, order=order)
    data = fmt.serialize_records(recs, fields, cfg.max_text_len, cfg.max_relational_preview)
    fetched = len(recs)
    return {
        'model': model_name,
        'records': data,
        'total': total,
        'offset': offset,
        'limit': limit,
        'has_more': offset + fetched < total,
        'next_offset': (offset + fetched) if offset + fetched < total else None,
    }


# ---------- get_record --------------------------------------------------------

def get_record(env, params):
    model_name = params['model']
    acc.check_model_access(env, model_name, 'read')
    ids = _collect_ids(params)
    fields = _resolve_fields(env, model_name, params.get('fields'))
    cfg = env['ow.mcp.config'].sudo().get_singleton()
    recs = env[model_name].search([('id', 'in', ids)])
    missing = [i for i in ids if i not in recs.ids]
    data = fmt.serialize_records(recs, fields, cfg.max_text_len, cfg.max_relational_preview)
    return {'model': model_name, 'records': data, 'missing': missing}


# ---------- create_record -----------------------------------------------------

def create_record(env, params):
    model_name = params['model']
    acc.check_model_access(env, model_name, 'create')
    values = params.get('values') or {}
    acc.assert_fields_allowed(env, model_name, list(values.keys()))
    acc.assert_no_nested_writes(env, model_name, values)
    cfg = env['ow.mcp.config'].sudo().get_singleton()
    rec = env[model_name].create(values)
    echo_fields = list({*values.keys(), 'id', 'display_name'})
    data = fmt.serialize_records(
        rec, echo_fields, cfg.max_text_len, cfg.max_relational_preview,
    )
    return {'model': model_name, 'id': rec.id, 'record': data[0]}


# ---------- update_record -----------------------------------------------------

def update_record(env, params):
    model_name = params['model']
    acc.check_model_access(env, model_name, 'write')
    values = params.get('values') or {}
    acc.assert_fields_allowed(env, model_name, list(values.keys()))
    acc.assert_no_nested_writes(env, model_name, values)
    ids = _collect_ids(params)
    recs = env[model_name].browse(ids).exists()
    recs.write(values)
    return {
        'model': model_name,
        'updated': recs.ids,
        'missing': [i for i in ids if i not in recs.ids],
    }


# ---------- delete_record -----------------------------------------------------

def delete_record(env, params):
    model_name = params['model']
    acc.check_model_access(env, model_name, 'unlink')
    ids = _collect_ids(params)
    recs = env[model_name].browse(ids).exists()
    deleted_ids = list(recs.ids)
    recs.unlink()
    return {
        'model': model_name,
        'deleted': deleted_ids,
        'missing': [i for i in ids if i not in deleted_ids],
    }


def _collect_ids(params):
    if 'ids' in params and params['ids']:
        return list(params['ids'])
    if 'id' in params and params['id'] is not None:
        return [params['id']]
    raise UserError('Missing required parameter: provide `id` or `ids`.')


# ---------- search_count ------------------------------------------------------

def search_count(env, params):
    model_name = params['model']
    acc.check_model_access(env, model_name, 'read')
    domain = params.get('domain') or []
    acc.assert_domain_fields_allowed(env, model_name, domain)
    return {'model': model_name, 'count': env[model_name].search_count(domain)}


# ---------- get_model_schema --------------------------------------------------

def get_model_schema(env, params):
    model_name = params['model']
    acc.check_model_access(env, model_name, 'read')
    allowed = acc.filter_fields(env, model_name, [])
    fields_meta = env[model_name].fields_get(
        allowed or None,
        ['string', 'type', 'required', 'readonly', 'relation', 'selection', 'help'],
    )
    out = []
    for fname, meta in sorted(fields_meta.items()):
        entry = {'name': fname, 'type': meta['type'], 'label': meta.get('string', fname)}
        if meta.get('required'):
            entry['required'] = True
        if meta.get('readonly'):
            entry['readonly'] = True
        if meta.get('relation'):
            entry['relation'] = meta['relation']
        if meta.get('selection'):
            entry['selection'] = meta['selection']
        if meta.get('help'):
            entry['help'] = meta['help']
        out.append(entry)
    return {'model': model_name, 'fields': out}


# ---------- read_group --------------------------------------------------------

def read_group(env, params):
    model_name = params['model']
    acc.check_model_access(env, model_name, 'read')
    groupby = params.get('groupby') or []
    if not groupby:
        raise UserError('`groupby` is required for read_group.')
    domain = params.get('domain') or []
    acc.assert_domain_fields_allowed(env, model_name, domain)
    fields = params.get('fields') or []
    # Strip any field aggregator suffix (`amount:sum`) before validating.
    agg_bare = [f.split(':', 1)[0] for f in fields]
    gb_bare = [g.split(':', 1)[0] for g in groupby]
    acc.assert_fields_allowed(env, model_name, agg_bare + gb_bare)
    rows = env[model_name].read_group(domain, fields, groupby)
    groups = []
    for row in rows:
        entry = {k: v for k, v in row.items() if not k.startswith('__')}
        entry['count'] = row.get('__count', 0)
        groups.append(entry)
    return {'model': model_name, 'groups': groups, 'total_groups': len(groups)}


# ---------- get_user_context --------------------------------------------------

def get_user_context(env, params):
    user = env.user
    out = {
        'id': user.id,
        'name': user.name,
        'login': user.login,
        'company': {'id': env.company.id, 'name': env.company.name},
        'allowed_companies': [
            {'id': c.id, 'name': c.name}
            for c in user.company_ids.sorted('id')
        ],
        'default_company_id': user.company_id.id,
        'lang': user.lang,
        'tz': user.tz,
    }
    # Group membership is a privilege map — expose only to MCP admins; other
    # callers get just their own "is_mcp_admin" flag so the LLM can branch
    # without leaking the full group topology.
    is_admin = user.has_group('ow_mcp_server.group_mcp_admin')
    if is_admin:
        out['groups'] = sorted(user.groups_id.mapped('full_name'))
    else:
        out['is_mcp_admin'] = False
    return out


# ---------- list_modules ------------------------------------------------------

def list_modules(env, params):
    if not env.user.has_group('ow_mcp_server.group_mcp_admin'):
        raise AccessError(
            'list_modules is restricted to MCP Administrators. Inventory '
            'information is not available to MCP Users.'
        )
    rows = env['ir.module.module'].sudo().search(
        [('state', '=', 'installed')], order='name asc',
    )
    return {
        'modules': [
            {'name': r.name, 'label': r.shortdesc, 'version': r.installed_version or ''}
            for r in rows
        ],
        'count': len(rows),
    }
