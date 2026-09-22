"""POST /ow_mcp/oauth/register — RFC 7591 Dynamic Client Registration.

Request body (JSON):
    {
      "client_name": "Claude",
      "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
      "grant_types": ["authorization_code","refresh_token"],
      "response_types": ["code"],
      "token_endpoint_auth_method": "none",
      "scope": "mcp:read mcp:write",
      "software_id": "...", "software_version": "..."
    }

Response: 201 with the registered client. `client_secret` is returned in
plaintext exactly once (and only when token_endpoint_auth_method != "none").
"""
import json
import logging
import time
from urllib.parse import urlparse

from odoo import http
from odoo.http import Response, request

from . import _utils as u


_logger = logging.getLogger(__name__)


_DEFAULT_GRANT_TYPES = ['authorization_code', 'refresh_token']
_DEFAULT_RESPONSE_TYPES = ['code']
_ALLOWED_AUTH_METHODS = {'none', 'client_secret_basic', 'client_secret_post'}
_ALLOWED_GRANT_TYPES = {'authorization_code', 'refresh_token'}


def _validate_redirect_uri(uri: str) -> str | None:
    """Return None if valid, else error message.

    OAuth 2.1 §7.5.1 / RFC 8252: HTTPS only, except loopback (127.0.0.1,
    ::1, localhost) which may be plain HTTP. Fragments forbidden.
    """
    if not isinstance(uri, str) or not uri:
        return 'redirect_uri must be a non-empty string'
    try:
        p = urlparse(uri)
    except Exception:
        return 'redirect_uri is not a valid URL'
    if p.fragment:
        return 'redirect_uri must not contain a fragment'
    if not p.scheme or not p.hostname:
        return 'redirect_uri must include scheme and host'
    if p.scheme == 'https':
        return None
    if p.scheme == 'http' and p.hostname in ('127.0.0.1', '::1', 'localhost'):
        return None
    # Custom schemes (claude://, cursor://) are allowed for native apps.
    if p.scheme not in ('http', 'https') and ':' in uri:
        return None
    return f'redirect_uri scheme {p.scheme!r} is not allowed'


class OauthRegistration(http.Controller):

    @http.route(
        '/ow_mcp/oauth/register',
        type='http', auth='none', methods=['POST', 'OPTIONS'],
        csrf=False, save_session=False, readonly=False,
    )
    def register(self, **_kw):
        if request.httprequest.method == 'OPTIONS':
            return Response(status=204, headers=u.cors_headers())
        c = u.cfg()
        if not c.oauth_enabled:
            return u.error_response(
                'oauth_disabled', 'OAuth is disabled.', status=404,
            )
        if not c.oauth_dcr_enabled:
            return u.error_response(
                'access_denied',
                'Dynamic client registration is disabled on this server.',
                status=403,
            )

        ip = u.effective_client_ip()
        if not u.dcr_check_throttle(
            request.env, ip, c.dcr_rate_limit_per_ip_per_hour,
        ):
            return u.error_response(
                'too_many_requests',
                'DCR rate limit exceeded.',
                status=429,
            )

        try:
            raw = request.httprequest.get_data(as_text=True) or '{}'
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError('body must be an object')
        except Exception as e:
            return u.error_response('invalid_client_metadata', str(e))

        redirect_uris = body.get('redirect_uris') or []
        if not isinstance(redirect_uris, list) or not redirect_uris:
            return u.error_response(
                'invalid_redirect_uri',
                'redirect_uris must be a non-empty array.',
            )
        for uri in redirect_uris:
            err = _validate_redirect_uri(uri)
            if err:
                return u.error_response('invalid_redirect_uri', err)

        grant_types = body.get('grant_types') or _DEFAULT_GRANT_TYPES
        if not isinstance(grant_types, list) or not all(
            isinstance(g, str) for g in grant_types
        ):
            return u.error_response(
                'invalid_client_metadata',
                'grant_types must be an array of strings.',
            )
        bad = set(grant_types) - _ALLOWED_GRANT_TYPES
        if bad:
            return u.error_response(
                'invalid_client_metadata',
                f'unsupported grant_types: {sorted(bad)}',
            )

        response_types = body.get('response_types') or _DEFAULT_RESPONSE_TYPES
        if response_types != ['code']:
            return u.error_response(
                'invalid_client_metadata',
                'only response_types=["code"] is supported.',
            )

        auth_method = body.get('token_endpoint_auth_method', 'none')
        if auth_method not in _ALLOWED_AUTH_METHODS:
            return u.error_response(
                'invalid_client_metadata',
                f'unsupported token_endpoint_auth_method: {auth_method}',
            )

        scope = body.get('scope') or 'mcp:read mcp:write'
        if not isinstance(scope, str):
            return u.error_response('invalid_client_metadata',
                                    'scope must be a string.')
        # Intersect requested scope with what we support.
        requested = set(scope.split())
        supported = {'mcp:read', 'mcp:write'}
        granted = requested & supported
        if not granted:
            granted = supported
        scope = ' '.join(sorted(granted))

        client_secret_plain = None
        client_secret_hash = False
        if auth_method != 'none':
            client_secret_plain = u.random_token(32)
            client_secret_hash = u.sha256_hex(client_secret_plain)

        registration_token_plain = u.random_token(32)
        registration_token_hash = u.sha256_hex(registration_token_plain)

        now = int(time.time())
        client = request.env['ow.mcp.oauth.client'].sudo().create({
            'client_secret_hash': client_secret_hash,
            'client_id_issued_at': now,
            'client_secret_expires_at': 0,
            'client_name': (body.get('client_name') or '')[:255] or False,
            'redirect_uris': json.dumps(redirect_uris),
            'grant_types': ' '.join(grant_types),
            'response_types': 'code',
            'token_endpoint_auth_method': auth_method,
            'scope': scope,
            'software_id': (body.get('software_id') or '')[:255] or False,
            'software_version': (body.get('software_version') or '')[:64]
                                 or False,
            'registration_access_token_hash': registration_token_hash,
            'is_dynamic': True,
            'active': True,
        })

        u.event_log(
            'register', client=client, payload={
                'client_name': body.get('client_name'),
                'redirect_uris': redirect_uris,
                'grant_types': grant_types,
                'auth_method': auth_method,
                'scope': scope,
            },
        )

        base = c.base_url()
        out = {
            'client_id': client.client_id,
            'client_id_issued_at': client.client_id_issued_at,
            'client_secret_expires_at': client.client_secret_expires_at,
            'client_name': client.client_name or '',
            'redirect_uris': redirect_uris,
            'grant_types': grant_types,
            'response_types': ['code'],
            'token_endpoint_auth_method': auth_method,
            'scope': scope,
            'registration_client_uri':
                f'{base}/ow_mcp/oauth/register/{client.client_id}',
            'registration_access_token': registration_token_plain,
        }
        if client_secret_plain:
            out['client_secret'] = client_secret_plain
        return u.json_response(out, status=201)
