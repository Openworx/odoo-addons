# -*- coding: utf-8 -*-
"""Usage and error logging for provider requests.

Successful requests (and, in test mode, everything) are logged in the
current transaction: if the caller's transaction later rolls back, so does
the usage row, which is fine since there was no real side effect to record.
Errors are logged in a *separate*, immediately committed cursor so the row
survives even when the caller's own transaction is rolled back after the
error (e.g. by the request dispatcher) -- we still want a record that the
call was attempted and failed. Opening a second cursor while a
``TransactionCase`` test holds the (only) row-level locks on tables we also
write to can deadlock, so in test mode we log in the current transaction
instead, same as the success path.
"""
from __future__ import annotations

from odoo import SUPERUSER_ID, api
from odoo.tools import config


def log_usage(env, *, kind, model, usage, status='ok', error_code=None, latency_ms=0,
              agent=None, session=None, request_id='', user=None, company=None):
    """Write one ``ow.ai.usage`` row for this request.

    Returns the row when it was written in the caller's transaction, None
    when it went through the separate error cursor (see module docstring).
    """
    user = user or env.user
    company = company or env.company

    if status == 'ok' or config['test_enable']:
        return env['ow.ai.usage']._log(
            env, kind=kind, model=model, usage=usage, status=status, error_code=error_code,
            latency_ms=latency_ms, agent=agent, user=user, company=company,
            request_id=request_id, session=session)

    # Error path, outside tests: log in a separate, immediately-committed
    # cursor so the row survives a rollback of the caller's own transaction.
    # The session (if any) may not be committed yet in that other
    # transaction, so it is deliberately not attached here to avoid a
    # foreign-key failure on an as-yet-invisible row; only the (already
    # persisted) agent/user/company references are safe to reuse by id.
    with env.registry.cursor() as cr:
        env2 = api.Environment(cr, SUPERUSER_ID, {})
        env2['ow.ai.usage']._log(
            env2, kind=kind, model=model, usage=usage, status=status, error_code=error_code,
            latency_ms=latency_ms, agent=agent, user=user, company=company, request_id=request_id)
        cr.commit()
    return None


def log_error(env, exc, **kw) -> None:
    """Log a failed request from an :class:`AIProviderError` (or subclass)."""
    kw.setdefault('usage', {})
    kw.setdefault('model', '')
    log_usage(env, status='error', error_code=getattr(exc, 'code', 'provider_error'), **kw)
