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


def _make_guard(name, handler):
    @functools.wraps(handler)
    def guarded(env, params):
        scope_value = None
        try:
            scope_value = getattr(request, 'ow_mcp_oauth_scope', None)
        except Exception:
            scope_value = None
        if scope_value is not None:
            scopes = set((scope_value or '').split())
            if name in WRITE_TOOLS:
                if 'mcp:write' not in scopes:
                    raise AccessError(
                        f'OAuth token lacks mcp:write scope required by '
                        f'tool {name!r}.'
                    )
            else:
                if 'mcp:read' not in scopes and 'mcp:write' not in scopes:
                    raise AccessError(
                        f'OAuth token lacks mcp:read scope required by '
                        f'tool {name!r}.'
                    )
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
