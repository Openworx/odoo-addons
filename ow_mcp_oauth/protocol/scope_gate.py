"""Wrap each entry in ow_mcp_server.protocol.dispatcher.TOOL_HANDLERS so
that, when the active request was authenticated via OAuth, the bound
scope is required to match the tool category.

  - read tools:  require `mcp:read`
  - write tools: require `mcp:write`

API-key auth (no `request.ow_mcp_oauth_scope` attribute) bypasses this
guard so existing clients keep working unchanged.
"""
import functools
import logging

from odoo.exceptions import AccessError
from odoo.http import request

from odoo.addons.ow_mcp_server.protocol import dispatcher


_logger = logging.getLogger(__name__)


WRITE_TOOLS = frozenset({'create_record', 'update_record', 'delete_record'})


def missing_scope(name):
    """Return the scope the current OAuth request lacks for tool `name`, or
    None if it has it (or the request was not authenticated via OAuth).

    factored out of the guard so the controller
    can answer 403 insufficient_scope and the dispatcher can check it
    before the response cache.
    """
    try:
        scope_value = getattr(request, 'ow_mcp_oauth_scope', None)
    except Exception:
        scope_value = None
    if scope_value is None:
        return None
    scopes = set((scope_value or '').split())
    if name in WRITE_TOOLS:
        return None if 'mcp:write' in scopes else 'mcp:write'
    if 'mcp:read' not in scopes and 'mcp:write' not in scopes:
        return 'mcp:read'
    return None


def _check_scope(env, name, args):
    needed = missing_scope(name)
    if needed:
        raise AccessError(
            f'OAuth token lacks {needed} scope required by tool {name!r}.'
        )


def _make_guard(name, handler):
    @functools.wraps(handler)
    def guarded(env, params):
        _check_scope(env, name, params)
        return handler(env, params)
    guarded.__ow_mcp_oauth_guarded__ = True
    return guarded


def _install_guards():
    handlers = dispatcher.TOOL_HANDLERS
    for name, handler in list(handlers.items()):
        if getattr(handler, '__ow_mcp_oauth_guarded__', False):
            continue
        handlers[name] = _make_guard(name, handler)
    _logger.info(
        'ow_mcp_oauth: scope guards installed on %d tool handlers',
        len(handlers),
    )


_install_guards()
# also before the response cache lookup.
if _check_scope not in dispatcher.PRE_CALL_CHECKS:
    dispatcher.PRE_CALL_CHECKS.append(_check_scope)
