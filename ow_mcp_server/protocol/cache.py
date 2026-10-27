import hashlib
import json
import threading
import time

_store = {}  # {key: (result, expires_at, model_name, size_bytes)}
_lock = threading.Lock()
_MAX_ENTRIES = 5000
# bound the memory each worker may spend on
# cached responses (5000 entries x max_limit records could OOM a worker).
_MAX_ENTRY_BYTES = 256 * 1024
_MAX_TOTAL_BYTES = 32 * 1024 * 1024

_READ_TOOLS = frozenset({
    'list_models', 'search_records', 'get_record',
    'search_count', 'get_model_schema', 'read_group',
    'get_user_context', 'list_modules',
})
_WRITE_TOOLS = frozenset({'create_record', 'update_record', 'delete_record'})


def _key(uid, user_fp, tool, args):
    payload = json.dumps(
        {'uid': uid, 'user': user_fp, 'tool': tool, 'args': args},
        sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _cfg(env):
    return env['ow.mcp.config'].sudo().get_singleton()


def _user_fp(env):
    """Fingerprint of the user's group + company membership.

    Changes in either invalidate the cache entry immediately, so a user
    whose group OR company access was just revoked cannot replay a
    previously-authorized read out of the in-memory cache (H1).
    """
    return (
        tuple(sorted(env.user.groups_id.ids)),
        tuple(sorted(env.user.company_ids.ids)),
    )


def _policy_fp(env, args):
    """Fingerprint of the MCP row governing `args['model']`.

    a cached read must not outlive a change
    of the model's allow-list / limits / required groups (the dispatcher
    re-checks allow_read / active before the lookup).
    """
    model_name = args.get('model') if isinstance(args, dict) else None
    if not isinstance(model_name, str):
        return None
    ma = env['ow.mcp.model.access'].sudo().search(
        [('model_name', '=', model_name)], limit=1,
    )
    if not ma:
        return None
    return (ma.id, ma.allowed_fields_json or '', ma.max_records,
            tuple(sorted(ma.group_ids.ids)))


def _entry_key(env, tool, args):
    return _key(env.uid, (_user_fp(env), _policy_fp(env, args)), tool, args)


def _total_bytes():
    return sum(entry[3] for entry in _store.values())


def lookup(env, tool, args):
    if tool not in _READ_TOOLS:
        return None
    cfg = _cfg(env)
    if not cfg.cache_enabled or env.context.get('ow_mcp_no_cache'):
        return None
    key = _entry_key(env, tool, args)
    with _lock:
        entry = _store.get(key)
        if not entry:
            return None
        result, expires_at, _, _ = entry
        if time.time() > expires_at:
            del _store[key]
            return None
        return result


def store(env, tool, args, result):
    if tool not in _READ_TOOLS:
        return
    cfg = _cfg(env)
    if not cfg.cache_enabled or cfg.cache_ttl_seconds <= 0:
        return
    model_name = args.get('model') if isinstance(args, dict) else None
    try:
        size = len(json.dumps(result, default=str))
    except Exception:
        return
    if size > _MAX_ENTRY_BYTES:
        return
    key = _entry_key(env, tool, args)
    expires_at = time.time() + cfg.cache_ttl_seconds
    with _lock:
        _store.pop(key, None)
        if len(_store) >= _MAX_ENTRIES or \
                _total_bytes() + size > _MAX_TOTAL_BYTES:
            _evict(size)
        _store[key] = (result, expires_at, model_name, size)


def invalidate_for_write(env, tool, args):
    if tool not in _WRITE_TOOLS or not isinstance(args, dict):
        return
    model_name = args.get('model')
    if not model_name:
        return
    with _lock:
        keys = [k for k, entry in _store.items() if entry[2] == model_name]
        for k in keys:
            del _store[k]


def _evict(incoming=0):
    now = time.time()
    expired = [k for k, entry in _store.items() if now > entry[1]]
    for k in expired:
        del _store[k]
    if len(_store) >= _MAX_ENTRIES:
        oldest = sorted(_store, key=lambda k: _store[k][1])[:_MAX_ENTRIES // 4]
        for k in oldest:
            del _store[k]
    # Byte budget: drop the oldest entries until the new one fits.
    total = _total_bytes()
    for k in sorted(_store, key=lambda k: _store[k][1]):
        if total + incoming <= _MAX_TOTAL_BYTES:
            break
        total -= _store.pop(k)[3]
