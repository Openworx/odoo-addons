"""Runtime patch: install OAuth-scope guards over ow_mcp_server's
TOOL_HANDLERS dict.

Why a patch and not a model override:
  - The dispatcher at ow_mcp_server.protocol.dispatcher uses a flat dict
    of tool handlers and has no hook point. Subclassing the controller
    lets us inject auth, but not pre-tool checks.
  - Wrapping the handlers at import time is small, explicit, and only
    activates when the current request was authenticated via OAuth
    (signaled by `request.ow_mcp_oauth_scope`). The API-key path is
    completely unaffected.
"""
from . import scope_gate  # noqa: F401  (side effects — installs guards)
