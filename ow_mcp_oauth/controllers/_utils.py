"""Shared helpers for ow_mcp_oauth controllers.

Kept deliberately small: anything Odoo-specific lives here, while pure
functions (b64url, sha256) are inline at point-of-use.
"""
import base64
import hashlib
import json
import logging
import secrets

from odoo.http import request, Response
from odoo.tools import config as odoo_config


_logger = logging.getLogger(__name__)


_CORS_OAUTH_HEADERS = [
    ('Access-Control-Allow-Origin', '*'),
    ('Access-Control-Allow-Methods', 'GET, POST, OPTIONS'),
    ('Access-Control-Allow-Headers', 'Authorization, Content-Type'),
    ('Access-Control-Max-Age', '86400'),
]


def cors_headers():
    """OAuth metadata + token endpoints are called by browsers (claude.ai)
    so they need permissive CORS regardless of the MCP CORS toggle.
    """
    return list(_CORS_OAUTH_HEADERS)


def json_response(payload, status=200, extra_headers=None):
    headers = list(cors_headers())
    if extra_headers:
        headers.extend(extra_headers)
    return Response(
        json.dumps(payload, default=str),
        status=status,
        mimetype='application/json',
        headers=headers,
    )


def error_response(error, description=None, status=400):
    body = {'error': error}
    if description:
        body['error_description'] = description
    return json_response(body, status=status)


def b64url_decode(value):
    """Decode unpadded base64url to bytes."""
    if isinstance(value, bytes):
        value = value.decode('ascii')
    pad = '=' * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(value + pad)


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def sha256_b64url(text: str) -> str:
    return b64url_encode(hashlib.sha256(text.encode('utf-8')).digest())


def random_token(nbytes=32) -> str:
    return secrets.token_urlsafe(nbytes)


def constant_time_eq(a: str, b: str) -> bool:
    return secrets.compare_digest(a or '', b or '')


def effective_client_ip():
    """Real client IP, honoring X-Forwarded-For when proxy_mode is on."""
    remote = request.httprequest.remote_addr or ''
    if not odoo_config.get('proxy_mode'):
        return remote
    fwd = request.httprequest.headers.get('X-Forwarded-For', '')
    if not fwd:
        return remote
    return fwd.split(',')[0].strip() or remote


# DCR throttle is persisted in `ow.mcp.oauth.dcr_attempt` so the
# sliding-window count is consistent across all Odoo workers — the
# previous in-memory dict allowed `limit × workers` registrations
# per hour in any multi-worker deployment.
_DCR_WINDOW_SECONDS = 3600


def dcr_reset(env=None):
    """Test helper: clear the DCR ledger."""
    if env is None:
        env = request.env
    env.cr.execute('DELETE FROM ow_mcp_oauth_dcr_attempt')


def dcr_check_throttle(env, ip: str, limit_per_hour: int) -> bool:
    """Return True if a new DCR request from `ip` should be allowed.

    Counts attempts within the last hour atomically and inserts a new
    row when the budget is available. A small race remains where N
    parallel requests can each pass the count check before any of them
    inserts; the worst case is `limit + N_workers` rather than the
    intended `limit`, which is acceptable for an anti-abuse limiter.
    """
    if not ip or limit_per_hour <= 0:
        return True
    cr = env.cr
    cr.execute(
        "SELECT COUNT(*) FROM ow_mcp_oauth_dcr_attempt "
        " WHERE ip = %s "
        "   AND create_date > (NOW() AT TIME ZONE 'UTC') - INTERVAL %s",
        (ip, f'{_DCR_WINDOW_SECONDS} seconds'),
    )
    count = cr.fetchone()[0]
    if count >= limit_per_hour:
        return False
    env['ow.mcp.oauth.dcr_attempt'].sudo().create({'ip': ip})
    return True


def dcr_prune(env, retention_seconds: int = _DCR_WINDOW_SECONDS):
    """Cron helper: drop ledger rows older than the sliding window."""
    env.cr.execute(
        "DELETE FROM ow_mcp_oauth_dcr_attempt "
        " WHERE create_date < (NOW() AT TIME ZONE 'UTC') - INTERVAL %s",
        (f'{int(retention_seconds)} seconds',),
    )


def cfg():
    """Return the OAuth config singleton (sudo)."""
    return request.env['ow.mcp.oauth.config'].sudo().get_singleton()


def event_log(kind, **kwargs):
    request.env['ow.mcp.oauth.event'].sudo().log_event(
        kind,
        ip=effective_client_ip(),
        user_agent=request.httprequest.headers.get('User-Agent', ''),
        **kwargs,
    )
