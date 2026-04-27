"""HTTP controller serving MCP JSON-RPC 2.0 on /mcp.

Authentication: Bearer token only, checked against Odoo's built-in
res.users.apikeys with scope='mcp'. Anything else → HTTP 401.

Transport-level hardening (all opt-in via ow.mcp.config):
  - enable_http_transport: master kill-switch → 503 when off.
  - enable_sse: gates GET /mcp probe → 405 when off.
  - ip_whitelist: CIDR/IP allow-list → 403 on mismatch.
  - enable_cors: permissive CORS headers + OPTIONS preflight.
  - rate_limit_per_minute: per-API-key sliding window → 429 on excess.
  - audit_log: persist every call to ow.mcp.audit.log.
"""
import ipaddress
import json
import logging
import time

from odoo import http
from odoo.http import request, Response
from odoo.tools import config as odoo_config

from ..protocol import authfail, dispatcher, jsonrpc, ratelimit


_logger = logging.getLogger(__name__)


_CORS_STATIC_HEADERS = [
    ('Access-Control-Allow-Methods', 'POST, GET, OPTIONS'),
    ('Access-Control-Allow-Headers',
     'Authorization, Content-Type, X-Odoo-Database, X-Odoo-Dbfilter, '
     'X-Odoo-MCP-No-Cache'),
    ('Access-Control-Max-Age', '86400'),
    ('Vary', 'Origin'),
]


def _cors_headers(cfg):
    """Build CORS headers for this request. Echoes the Origin header only if
    it matches cors_origins (exact match). Empty cors_origins falls back to
    `*` for backward compatibility with existing deployments.
    """
    if not (cfg and cfg.enable_cors):
        return []
    origin = request.httprequest.headers.get('Origin', '')
    allowlist_raw = (cfg.cors_origins or '').strip()
    if not allowlist_raw:
        # Legacy behavior: wildcard.
        return [('Access-Control-Allow-Origin', '*')] + _CORS_STATIC_HEADERS
    allowed = {
        line.strip() for line in allowlist_raw.splitlines()
        if line.strip() and not line.strip().startswith('#')
    }
    if origin and origin in allowed:
        return [
            ('Access-Control-Allow-Origin', origin),
        ] + _CORS_STATIC_HEADERS
    # Origin absent or not on the list: emit no Allow-Origin header → browser
    # blocks the response. Static CORS headers still sent so a future OPTIONS
    # probe from an allowed origin is clearly negotiable.
    return _CORS_STATIC_HEADERS


class MCPController(http.Controller):

    @http.route(
        '/mcp', type='http', auth='none', methods=['POST'],
        csrf=False, save_session=False, readonly=False,
    )
    def mcp_post(self, **kwargs):
        t0 = time.monotonic()
        cfg = self._cfg()

        gate = self._pre_auth_gates(cfg)
        if gate is not None:
            return gate

        too_big = self._body_too_large(cfg)
        if too_big is not None:
            return too_big

        client_ip = _effective_client_ip()
        if authfail.is_throttled(client_ip):
            return self._audit_and_return(
                cfg, self._auth_throttled(cfg), t0, user_id=None,
                method=None, tool_name=None, success=False,
                error_code=jsonrpc.AUTH_REQUIRED,
                error_message='auth throttled',
            )

        uid = self._authenticate()
        if not uid:
            authfail.register_failure(client_ip)
            return self._audit_and_return(
                cfg, self._unauthorized(), t0, user_id=None,
                method=None, tool_name=None, success=False,
                error_code=jsonrpc.AUTH_REQUIRED,
                error_message='auth required',
            )
        request.update_env(user=uid)
        if _no_cache_requested(request):
            request.update_env(
                context=dict(request.env.context, ow_mcp_no_cache=True),
            )

        if cfg and cfg.rate_limit_per_minute and not ratelimit.check_and_record(
            uid, cfg.rate_limit_per_minute,
        ):
            return self._audit_and_return(
                cfg, self._rate_limited(cfg), t0, user_id=uid,
                method=None, tool_name=None, success=False,
                error_code=-32004, error_message='rate limited',
            )

        try:
            raw = request.httprequest.get_data(as_text=True)
            limit = (cfg.max_body_bytes if cfg else 0) or 0
            if limit and len(raw.encode('utf-8')) > limit:
                return self._audit_and_return(
                    cfg, self._payload_too_large(cfg), t0, user_id=uid,
                    method=None, tool_name=None, success=False,
                    error_code=jsonrpc.INVALID_REQUEST,
                    error_message='body too large',
                    payload_bytes=len(raw or ''),
                )
            msg = json.loads(raw)
        except Exception as e:
            resp = self._json(
                jsonrpc.transport_error(jsonrpc.PARSE_ERROR, f'Parse error: {e}'),
                400, cfg=cfg,
            )
            return self._audit_and_return(
                cfg, resp, t0, user_id=uid, method=None, tool_name=None,
                success=False, error_code=jsonrpc.PARSE_ERROR,
                error_message=str(e), payload_bytes=len(raw or ''),
            )

        payload_bytes = len(raw or '')
        if isinstance(msg, list):
            out = [
                x for x in (dispatcher.dispatch(request.env, m) for m in msg)
                if x is not None
            ]
            resp = self._json(out, 200, cfg=cfg)
            # Audit one row per sub-call for accurate traceability.
            if cfg and cfg.audit_log:
                for m in msg:
                    self._record_audit(
                        cfg, t0, uid, m, success=True,
                        payload_bytes=payload_bytes,
                    )
            return resp

        resp_body = dispatcher.dispatch(request.env, msg)
        if resp_body is None:
            response = Response(status=204)
            for k, v in _cors_headers(cfg):
                response.headers[k] = v
        else:
            response = self._json(
                resp_body, 200, cfg=cfg,
            )
        return self._audit_and_return(
            cfg, response, t0, user_id=uid,
            method=(msg or {}).get('method') if isinstance(msg, dict) else None,
            tool_name=_tool_name(msg),
            model_name=_model_name(msg),
            success=_is_success(resp_body),
            error_code=_error_code(resp_body),
            error_message=_error_message(resp_body),
            payload_bytes=payload_bytes,
            message=msg,
            resp_body=resp_body,
        )

    @http.route(
        '/mcp', type='http', auth='none', methods=['GET'],
        csrf=False, save_session=False, readonly=False,
    )
    def mcp_sse(self, **kwargs):
        cfg = self._cfg()
        gate = self._pre_auth_gates(cfg, for_sse=True)
        if gate is not None:
            return gate
        if not (cfg and cfg.enable_sse):
            return self._method_not_allowed(cfg)
        uid = self._authenticate()
        if not uid:
            return self._unauthorized()

        def _stream():
            yield 'event: ready\ndata: {}\n\n'

        headers = [
            ('Cache-Control', 'no-cache'),
            ('Connection', 'keep-alive'),
            ('X-Accel-Buffering', 'no'),
        ]
        headers.extend(_cors_headers(cfg))
        return Response(_stream(), mimetype='text/event-stream', headers=headers)

    @http.route(
        '/mcp', type='http', auth='none', methods=['OPTIONS'],
        csrf=False, save_session=False, readonly=False,
    )
    def mcp_options(self, **kwargs):
        cfg = self._cfg()
        # OPTIONS is only meaningful when CORS is on; otherwise 405.
        if not (cfg and cfg.enable_cors):
            return self._method_not_allowed(cfg)
        resp = Response(status=204)
        for k, v in _cors_headers(cfg):
            resp.headers[k] = v
        return resp

    # -- gates ---------------------------------------------------------------

    def _body_too_large(self, cfg):
        """Cheap pre-read check on Content-Length. The real body is re-checked
        post-read because Content-Length can lie or be absent.
        """
        limit = (cfg.max_body_bytes if cfg else 0) or 0
        if not limit:
            return None
        length = request.httprequest.content_length or 0
        if length > limit:
            return self._payload_too_large(cfg)
        return None

    def _pre_auth_gates(self, cfg, for_sse=False):
        """Run cheap transport-level checks before touching auth or body.

        Returns a Response if the request must be rejected, else None.
        """
        if not for_sse and cfg and not cfg.enable_http_transport:
            return self._json(
                jsonrpc.transport_error(
                    jsonrpc.INTERNAL_ERROR,
                    'MCP HTTP transport is disabled in ow.mcp.config.',
                ),
                503, cfg=cfg,
            )
        if cfg and cfg.require_https and not _is_https_effective():
            return self._json(
                jsonrpc.transport_error(jsonrpc.INVALID_REQUEST, 'HTTPS required.'),
                400, cfg=cfg,
            )
        effective_ip = _effective_client_ip()
        if not _ip_allowed(
            effective_ip,
            cfg.ip_whitelist if cfg else None,
        ):
            _logger.warning(
                'MCP: IP %s blocked by ip_whitelist',
                effective_ip,
            )
            return self._json(
                jsonrpc.transport_error(
                    jsonrpc.PERMISSION_DENIED, 'Client IP not permitted.',
                ),
                403, cfg=cfg,
            )
        db_check = self._check_database(cfg)
        if db_check is not None:
            return db_check
        return None

    def _check_database(self, cfg):
        """Validate the target database if one was explicitly requested."""
        target = (
            request.httprequest.args.get('db')
            or request.httprequest.headers.get('X-Odoo-Database')
        )
        if not target:
            return None
        if request.db == target:
            return None
        return self._json(
            jsonrpc.transport_error(
                jsonrpc.NOT_FOUND,
                f'Request reached database {request.db!r} but client asked '
                f'for {target!r}. Configure Odoo dbfilter to route this '
                f'host to {target!r}, or use a host/URL that maps to it.',
            ),
            404, cfg=cfg,
        )

    def _authenticate(self):
        """Return uid for a valid Bearer API key (scope='mcp'), else None."""
        auth = request.httprequest.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            return None
        key = auth[7:].strip()
        if not key:
            return None
        try:
            uid = request.env['res.users.apikeys']._check_credentials(
                scope='mcp', key=key,
            )
        except Exception:
            _logger.exception('MCP: apikey credential check crashed')
            return None
        return uid or None

    # -- responses -----------------------------------------------------------

    def _unauthorized(self):
        cfg = self._cfg()
        resp = self._json(
            jsonrpc.transport_error(
                jsonrpc.AUTH_REQUIRED,
                'Authentication required: Bearer API key (scope=mcp)',
            ),
            401, cfg=cfg,
        )
        resp.headers['WWW-Authenticate'] = 'Bearer realm="mcp"'
        return resp

    def _auth_throttled(self, cfg):
        resp = self._json(
            jsonrpc.transport_error(
                -32005, 'Too many authentication failures; try again later.',
            ),
            429, cfg=cfg,
        )
        resp.headers['Retry-After'] = str(int(authfail.WINDOW_SECONDS))
        return resp

    def _rate_limited(self, cfg):
        resp = self._json(
            jsonrpc.transport_error(
                -32004, f'Rate limit exceeded ({cfg.rate_limit_per_minute}/min).',
            ),
            429, cfg=cfg,
        )
        resp.headers['Retry-After'] = '60'
        return resp

    def _payload_too_large(self, cfg):
        return self._json(
            jsonrpc.transport_error(jsonrpc.INVALID_REQUEST, 'Request body too large.'),
            413, cfg=cfg,
        )

    def _method_not_allowed(self, cfg):
        resp = self._json(
            jsonrpc.transport_error(jsonrpc.METHOD_NOT_FOUND, 'Method not allowed on /mcp.'),
            405, cfg=cfg,
        )
        resp.headers['Allow'] = 'POST'
        return resp

    def _json(self, payload, status, cfg=None):
        headers = list(_cors_headers(cfg))
        resp = Response(
            json.dumps(payload, default=str),
            status=status, mimetype='application/json',
            headers=headers,
        )
        return resp

    # -- audit ---------------------------------------------------------------

    def _cfg(self):
        """Fetch config singleton. Returns None if not yet installed
        (can happen mid-install before data.xml is loaded).
        """
        try:
            return request.env['ow.mcp.config'].sudo().get_singleton()
        except Exception:
            return None

    def _audit_and_return(self, cfg, response, t0, **kw):
        if cfg and cfg.audit_log:
            try:
                self._record_audit(cfg, t0, **kw)
            except Exception:
                _logger.exception('MCP: audit log write failed')
        return response

    def _record_audit(self, cfg, t0, user_id=None, method=None,
                      tool_name=None, model_name=None, success=True,
                      error_code=None, error_message=None,
                      payload_bytes=0, message=None, resp_body=None,
                      company_id=None, **_ignored):
        if message is not None:
            tool_name = tool_name or _tool_name(message)
            model_name = model_name or _model_name(message)
            method = method or (
                message.get('method') if isinstance(message, dict) else None
            )
            company_id = company_id or _company_id(message)
        duration_ms = int((time.monotonic() - t0) * 1000)
        login = None
        if user_id:
            u = request.env['res.users'].sudo().browse(user_id).exists()
            login = u.login if u else None
        request.env['ow.mcp.audit.log'].sudo().create({
            'user_id': user_id or False,
            'login': login,
            'company_id': company_id or False,
            'method': method,
            'tool_name': tool_name,
            'model_name': model_name,
            'success': success,
            'error_code': error_code or 0,
            'error_message': (error_message or '')[:255] or False,
            'ip_address': _effective_client_ip(),
            'duration_ms': duration_ms,
            'payload_bytes': payload_bytes,
            'request_payload': _request_payload(message),
            'response_summary': _response_summary(resp_body),
        })


# -- helpers -----------------------------------------------------------------

_PAYLOAD_LIMIT = 4000
_SUMMARY_LIMIT = 4000

# Keys whose value must never reach the audit log in plaintext. Matched
# case-insensitively as substrings so `new_password`, `totp_secret`,
# `x_api_key` and so on are all covered.
_SENSITIVE_KEY_SUBSTRINGS = (
    'password', 'passwd', 'secret', 'token', 'api_key', 'apikey',
    'totp', 'otp_secret', 'private_key', 'privatekey', 'signing_key',
    'client_secret', 'access_token', 'refresh_token',
    'acc_number', 'iban', 'bic', 'swift', 'cvv', 'cvc', 'card_number',
    'ssn', 'tax_id',
)
_REDACTED = '***REDACTED***'


def _is_sensitive_key(key):
    if not isinstance(key, str):
        return False
    low = key.lower()
    return any(s in low for s in _SENSITIVE_KEY_SUBSTRINGS)


def _redact(obj):
    """Recursively redact values under sensitive keys inside a JSON-ish tree."""
    if isinstance(obj, dict):
        return {
            k: (_REDACTED if _is_sensitive_key(k) else _redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_redact(v) for v in obj)
    return obj


def _request_payload(message):
    """Return the tool arguments JSON for a tools/call, else None."""
    if not isinstance(message, dict):
        return None
    if message.get('method') != 'tools/call':
        return None
    args = (message.get('params') or {}).get('arguments') or {}
    safe = _redact(args)
    try:
        text = json.dumps(safe, default=str, ensure_ascii=False)
    except Exception:
        return None
    return text[:_PAYLOAD_LIMIT] if len(text) > _PAYLOAD_LIMIT else text


def _response_summary(resp_body):
    """Return a compact representation of the dispatcher response."""
    if not isinstance(resp_body, dict):
        return None
    if 'error' in resp_body:
        err = resp_body['error'] or {}
        return json.dumps({'error': err.get('message'), 'code': err.get('code')})
    result = resp_body.get('result') or {}
    if isinstance(result, dict) and result.get('isError'):
        content = result.get('content') or []
        text = content[0].get('text', '') if content else ''
        return json.dumps({'error': text})
    sc = result.get('structuredContent')
    if sc is not None:
        try:
            text = json.dumps(_redact(sc), default=str, ensure_ascii=False)
            return text[:_SUMMARY_LIMIT] if len(text) > _SUMMARY_LIMIT else text
        except Exception:
            return None
    return None


def _ip_allowed(client_ip, whitelist_text):
    if not whitelist_text or not whitelist_text.strip():
        return True
    if not client_ip:
        return False
    client = _parse_client_ip(client_ip)
    if client is None:
        return False
    for line in whitelist_text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        try:
            net = ipaddress.ip_network(line, strict=False)
        except ValueError:
            continue
        if client.version != net.version:
            continue
        if client in net:
            return True
    return False


def _effective_client_ip():
    """Return the real client IP, honoring X-Forwarded-For when proxy_mode is
    set in odoo.conf. Without this, behind a reverse proxy the auth-failure
    throttle and IP allowlist key on the *proxy's* IP — one bad actor trips
    the throttle for every legitimate client.
    """
    remote = request.httprequest.remote_addr or ''
    if not odoo_config.get('proxy_mode'):
        return remote
    fwd = request.httprequest.headers.get('X-Forwarded-For', '')
    if not fwd:
        return remote
    # First entry = original client; subsequent entries are intermediate hops.
    first = fwd.split(',')[0].strip()
    return first or remote


def _is_https_effective():
    """True if the request is HTTPS, directly or via a trusted proxy.

    X-Forwarded-Proto is honored only when proxy_mode is set in odoo.conf —
    otherwise any client could forge the header.
    """
    scheme = (request.httprequest.scheme or '').lower()
    if scheme == 'https':
        return True
    if odoo_config.get('proxy_mode'):
        fwd = request.httprequest.headers.get('X-Forwarded-Proto', '')
        return fwd.split(',')[0].strip().lower() == 'https'
    return False


def _no_cache_requested(req):
    """True iff the client sent X-Odoo-MCP-No-Cache: 1/true/yes."""
    val = req.httprequest.headers.get('X-Odoo-MCP-No-Cache', '')
    return val.strip().lower() in ('1', 'true', 'yes', 'on')


def _parse_client_ip(client_ip):
    """Parse a remote_addr string into an IPv4Address or IPv6Address.

    Strips an IPv6 zone-id ('fe80::1%eth0' -> 'fe80::1') and collapses an
    IPv4-mapped IPv6 address ('::ffff:192.0.2.1') to its IPv4 form so that a
    v4 entry in the whitelist matches a client arriving on a v6 socket.
    """
    if not client_ip:
        return None
    raw = client_ip.split('%', 1)[0]
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        return addr.ipv4_mapped
    return addr


def _tool_name(message):
    if not isinstance(message, dict):
        return None
    if message.get('method') != 'tools/call':
        return None
    return (message.get('params') or {}).get('name')


def _model_name(message):
    if not isinstance(message, dict):
        return None
    if message.get('method') != 'tools/call':
        return None
    args = (message.get('params') or {}).get('arguments') or {}
    return args.get('model')


def _company_id(message):
    """Return the requested company_id from a tools/call message, or None.

    Returns the value as supplied by the client (logged even when the
    company is not in the user's allowed companies and the call fails),
    so admins can see which company was attempted.
    """
    if not isinstance(message, dict):
        return None
    if message.get('method') != 'tools/call':
        return None
    args = (message.get('params') or {}).get('arguments') or {}
    cid = args.get('company_id') if isinstance(args, dict) else None
    return cid if isinstance(cid, int) and not isinstance(cid, bool) else None


def _is_success(resp_body):
    if resp_body is None:
        return True  # notification, no response = success
    if not isinstance(resp_body, dict):
        return True
    if 'error' in resp_body:
        return False
    result = resp_body.get('result') or {}
    if isinstance(result, dict) and result.get('isError'):
        return False
    return True


def _error_code(resp_body):
    if not isinstance(resp_body, dict):
        return None
    if 'error' in resp_body:
        return (resp_body['error'] or {}).get('code')
    result = resp_body.get('result') or {}
    if isinstance(result, dict) and result.get('isError'):
        return jsonrpc.INTERNAL_ERROR
    return None


def _error_message(resp_body):
    if not isinstance(resp_body, dict):
        return None
    if 'error' in resp_body:
        return (resp_body['error'] or {}).get('message')
    result = resp_body.get('result') or {}
    if isinstance(result, dict) and result.get('isError'):
        content = result.get('content') or []
        return content[0].get('text', '') if content else ''
    return None
