import hashlib
import json
import threading
import time

_store = {}  # {key: (result, expires_at, model_name)}
_lock = threading.Lock()
_MAX_ENTRIES = 5000

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


def lookup(env, tool, args):
    if tool not in _READ_TOOLS:
        return None
    cfg = _cfg(env)
    if not cfg.cache_enabled or env.context.get('ow_mcp_no_cache'):
        return None
    key = _key(env.uid, _user_fp(env), tool, args)
    with _lock:
        entry = _store.get(key)
        if not entry:
            return None
        result, expires_at, _ = entry
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
    key = _key(env.uid, _user_fp(env), tool, args)
    expires_at = time.time() + cfg.cache_ttl_seconds
    with _lock:
        if len(_store) >= _MAX_ENTRIES:
            _evict()
        _store[key] = (result, expires_at, model_name)


def invalidate_for_write(env, tool, args):
    if tool not in _WRITE_TOOLS or not isinstance(args, dict):
        return
    model_name = args.get('model')
    if not model_name:
        return
    with _lock:
        keys = [k for k, (_, _, m) in _store.items() if m == model_name]
        for k in keys:
            del _store[k]


def _evict():
    now = time.time()
    expired = [k for k, (_, exp, _) in _store.items() if now > exp]
    for k in expired:
        del _store[k]
    if len(_store) >= _MAX_ENTRIES:
        oldest = sorted(_store, key=lambda k: _store[k][1])[:_MAX_ENTRIES // 4]
        for k in oldest:
            del _store[k]
