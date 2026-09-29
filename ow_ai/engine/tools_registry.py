# -*- coding: utf-8 -*-
"""In-memory registry of built-in tool implementations, plus the tool runtime.

Built-in tools (Python callables run in-process) register themselves here
with ``@builtin_tool('some.key')`` at import time. The ``ow.ai.tool`` model
references a registered key through its ``builtin_key`` field; the registry
lets model constraints and the (future) execution engine look up the
callable without hard-coding a dispatch table.

``ToolContext``/``ToolResult``/``run_tool`` are the runtime the engine (task
1.8+) uses to actually execute a tool call coming from the model: they carry
per-call state in and out (mutable session state, user-input requests,
client-side actions, notifications) and turn whatever a builtin raises into
an LLM-safe error message, never a raw traceback.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from odoo.exceptions import AccessError, UserError, ValidationError

from ..utils import params
from ..utils.access import ModelAccessError
from ..utils.schema import ArgumentError, validate_args
from ..utils.serialize import compact_json, truncate_text

_logger = logging.getLogger(__name__)

_DEFAULT_MAX_TOOL_RESULT_CHARS = 60000


@dataclass(frozen=True)
class ToolSpec:
    """A registered built-in tool: its key, implementation and write flag."""
    key: str
    func: Callable
    is_write: bool = False


_REGISTRY: dict[str, ToolSpec] = {}


def builtin_tool(key: str, *, is_write: bool = False):
    """Decorator registering ``func`` as the built-in tool named ``key``.

    Raises ValueError if ``key`` is already registered (module import order
    bugs should fail loudly, not silently shadow an earlier registration).
    """
    def decorator(func: Callable) -> Callable:
        if key in _REGISTRY:
            raise ValueError(f"builtin tool '{key}' is already registered")
        _REGISTRY[key] = ToolSpec(key=key, func=func, is_write=is_write)
        return func
    return decorator


def get_builtin(key: str) -> ToolSpec | None:
    """Return the ToolSpec registered under ``key``, or None."""
    return _REGISTRY.get(key)


def all_builtin_keys() -> list[str]:
    """Return all currently registered built-in tool keys."""
    return list(_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Tool execution runtime
# ---------------------------------------------------------------------------

@dataclass
class ToolContext:
    """Per-call context threaded through a built-in/server-action tool.

    Inputs describe who is calling and what they already agreed to; outputs
    are populated by the tool implementation for the engine to act on after
    the call returns (asking the user something, triggering a client-side
    action, showing a notification, ending the turn with a final message).
    """
    env: Any                          # Environment, user env, su=False (enforced by run_tool)
    session_id: int | None = None     # ow.ai.session id (Task 1.8+)
    agent_id: int | None = None
    tool_call_id: str = ''
    state: dict = field(default_factory=dict)          # session.state, mutable, engine persists it
    tool_request_confirmed: bool = False
    auto_confirm: bool = False
    record: Any = None                # bound record ("Ask AI about this record") or None
    # In/out: a small JSON-safe dict a write tool uses to pin something
    # (e.g. the exact record ids a preview showed) between its Phase-A call
    # (unconfirmed: the tool sets this as an *output*, alongside
    # `user_input_request`) and its Phase-B call (confirmed: the engine
    # feeds the pinned dict back in as an *input*). The engine
    # (`tool_batch.advance_tool_batch`) only ever populates this input for
    # the one call actually being resumed right now -- never for a call
    # that merely happens to run with `tool_request_confirmed=True` because
    # `session.auto_confirm` is on (that call was never paused, so nothing
    # was ever pinned for it). A tool must treat a `None` pin here the same
    # as "confirmed but never previewed": re-derive everything from its
    # arguments fresh, never from state left over from a differently-argued
    # or differently-timed call. This lives entirely inside
    # `session.pending_tool_call` (`pending['pins'][call_id]`), which is
    # dropped whole on finish/decline/abort -- never in `session.state`,
    # which would outlive the batch and could be replayed against a later,
    # unrelated call that happens to reuse the same provider-issued call id.
    pending_pin: dict | None = None
    # Outputs the tool may set:
    user_input_request: dict | None = None   # {'type': 'question'|'confirmation', ...}
    client_tools: list = field(default_factory=list)    # [{'name': 'reload'|'do_action', 'args': {...}}]
    # Non-blocking client-side actions (e.g. {'name': 'reload'}): unlike
    # `client_tools` above, these never pause the turn -- they are sent as a
    # `blocking: False` `ow_ai.session/client_tools` bus notification once
    # the turn actually ends (see `engine/loop.py::post_turn_notifications`).
    client_notifications: list = field(default_factory=list)
    notifications: list = field(default_factory=list)   # [{'kind': 'note'|'preview', 'body': str, 'links': [...]}]
    final_message: str | None = None
    tool_status: str | None = None


@dataclass
class ToolResult:
    """The outcome of a tool call: what goes back to the model."""
    # A builtin may return a str, dict or list here; `run_tool` always hands
    # back a (truncated) str: dict/list are serialised with `compact_json`.
    response: Any
    summary: dict | None = None       # {'icon': 'search', 'text': 'Searched 12 contacts'} (html_output.ICON_CLASSES)
    parts: list | None = None         # optional inline_data parts (images) to append to the tool result
    success: bool = True


_CAUGHT_ODOO_EXCEPTIONS = (UserError, AccessError, ValidationError)


class _ConfirmationRequiredError(Exception):
    """Internal: a write tool returned without requesting confirmation.

    Raised (and only ever caught) inside ``run_tool``'s own savepoint, so
    whatever the tool already wrote before returning is rolled back with
    it -- this must never surface to a caller or a test as a real error
    type, it exists purely to reuse the savepoint's rollback-on-exception
    behaviour for one extra condition.
    """


def _max_tool_result_chars(env) -> int:
    return params.get_int(env, 'ow_ai.max_tool_result_chars', _DEFAULT_MAX_TOOL_RESULT_CHARS)


def _coerce_result(raw) -> ToolResult:
    """Normalise a builtin's return value (ToolResult | str | dict | list) to a ToolResult."""
    if isinstance(raw, ToolResult):
        return raw
    return ToolResult(response=raw)


def run_tool(tool_record, args: dict, ctx: ToolContext) -> ToolResult:
    """Validate arguments and run ``tool_record`` (a ``ow.ai.tool``), catching errors.

    Never runs as superuser: raises ``RuntimeError`` (before running
    anything) when ``ctx.env.su`` is set or the calling uid is the
    superuser id -- an explicit check, not an ``assert``, so it also holds
    under ``python -O``.

    The returned ``ToolResult.response`` is always a string: a dict/list a
    tool returns is serialised with ``compact_json`` first, then every
    response is cut to the ``ow_ai.max_tool_result_chars`` config parameter
    (with a "[truncated N characters]" note), so no single tool result can
    blow up the conversation history.
    """
    from odoo import SUPERUSER_ID  # local import: avoid a module-load-time Odoo dependency

    if ctx.env.su or ctx.env.uid == SUPERUSER_ID:
        raise RuntimeError("run_tool must never run with a superuser environment")

    args = dict(args or {})
    ctx.tool_status = args.pop('tool_status', None)

    try:
        clean_args = validate_args(args, tool_record._get_schema())
    except ArgumentError as exc:
        return ToolResult(response=f"Invalid arguments: {exc}", success=False)

    if tool_record.kind == 'builtin':
        spec = get_builtin(tool_record.builtin_key)
        if spec is None:
            return ToolResult(response=f"Tool '{tool_record.tool_name}' is not available.", success=False)
        runner = spec.func
    else:
        return ToolResult(response="Server-action tools are not available yet", success=False)

    try:
        with ctx.env.cr.savepoint():
            raw = runner(ctx, **clean_args)
            result = _coerce_result(raw)
            # Defensive, and checked *inside* the savepoint on purpose: a
            # write tool must always either ask for confirmation (Phase A)
            # or run with `tool_request_confirmed=True` (Phase B -- the
            # engine re-running the exact persisted call after the user
            # agreed). A tool that silently wrote data without going
            # through either path would defeat the whole confirmation
            # mechanism; raising here (instead of just returning a failed
            # result after the `with` block) rolls back whatever it just
            # wrote along with everything else the savepoint covers. A
            # falsy `user_input_request` (e.g. `{}`) counts as "did not
            # request it", not just `None`. `spec.is_write` covers a
            # builtin registered as a write tool whose `ow.ai.tool` record
            # itself does not (yet) have `requires_confirmation` set.
            if (spec.is_write or tool_record.requires_confirmation) and not ctx.tool_request_confirmed \
                    and not ctx.user_input_request:
                raise _ConfirmationRequiredError
    except _ConfirmationRequiredError:
        return ToolResult(
            "This tool requires confirmation but did not request it.", success=False)
    except ModelAccessError as exc:
        return ToolResult(response=f"Tool call failed: {exc}", success=False)
    except _CAUGHT_ODOO_EXCEPTIONS as exc:
        message = exc.args[0] if exc.args else str(exc)
        return ToolResult(response=f"Tool call failed: {message}", success=False)
    except ValueError as exc:
        return ToolResult(response=f"Tool call failed: {exc}", success=False)
    except Exception:  # noqa: BLE001 - never leak an internal traceback to the model
        _logger.exception("Unexpected error running tool '%s'", tool_record.tool_name)
        return ToolResult(response="Tool call failed: unexpected error", success=False)

    response = result.response
    if not isinstance(response, str):
        response = compact_json(response)
    result.response = truncate_text(response, _max_tool_result_chars(ctx.env))
    return result
