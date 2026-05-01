"""POST /ow_mcp/oauth/token  — RFC 6749 §3.2 + refresh-token rotation
   POST /ow_mcp/oauth/revoke — RFC 7009

Grants supported: authorization_code, refresh_token.

Authorization-code grant:
  - validates the code is single-use (consumed flag) and not expired
  - PKCE: BASE64URL(SHA256(code_verifier)) == stored code_challenge
  - redirect_uri exact match against the original request
  - resource (RFC 8707) must match what was bound on the auth request
  - issues an RS256 JWT access token + opaque refresh token

Refresh-token grant:
  - rotation: consume the presented token and issue a new pair
  - reuse-detection: if a previously-consumed refresh token is presented,
    revoke the entire chain (rooted at the original)
"""
import base64
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta

from odoo import http
from odoo.http import request

from . import _utils as u
from ..tools import jwt_helper


_logger = logging.getLogger(__name__)


def _parse_basic_auth(header_value: str):
    if not header_value or not header_value.startswith('Basic '):
        return None, None
    try:
        decoded = base64.b64decode(header_value[6:].strip()).decode('utf-8')
    except Exception:
        return None, None
    if ':' not in decoded:
        return None, None
    cid, _, secret = decoded.partition(':')
    return cid, secret


def _verify_pkce(verifier: str, challenge: str) -> bool:
    if not verifier or not challenge:
        return False
    if not (43 <= len(verifier) <= 128):
        return False
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')
    return u.constant_time_eq(computed, challenge)


class OauthToken(http.Controller):

    @http.route(
        '/ow_mcp/oauth/token',
        type='http', auth='none', methods=['POST', 'OPTIONS'],
        csrf=False, save_session=False, readonly=False,
    )
    def token(self, **kwargs):
        if request.httprequest.method == 'OPTIONS':
            from odoo.http import Response
            return Response(status=204, headers=u.cors_headers())
        c = u.cfg()
        if not c.oauth_enabled:
            return u.error_response('oauth_disabled', status=404)

        # Read form-encoded body (RFC 6749 §3.2 mandates form-urlencoded).
        params = dict(kwargs)
        # Allow JSON body too — Claude has been seen sending JSON.
        if request.httprequest.content_type and \
                request.httprequest.content_type.startswith('application/json'):
            try:
                params.update(
                    json.loads(request.httprequest.get_data(as_text=True) or '{}')
                )
            except Exception:
                return u.error_response('invalid_request', 'malformed JSON body.')

        grant_type = params.get('grant_type', '')
        client = self._authenticate_client(params)
        if client is None:
            return u.error_response('invalid_client',
                                    'Client authentication failed.',
                                    status=401)

        if grant_type == 'authorization_code':
            return self._grant_authorization_code(c, client, params)
        if grant_type == 'refresh_token':
            return self._grant_refresh_token(c, client, params)
        return u.error_response('unsupported_grant_type',
                                f'Unsupported grant_type {grant_type!r}.')

    def _authenticate_client(self, params):
        """Resolve and authenticate the requesting client.

        Returns the client recordset, or None if authentication failed.
        For public clients (token_endpoint_auth_method=none), only the
        client_id is verified.
        """
        # Try Basic header first.
        auth_header = request.httprequest.headers.get('Authorization', '')
        cid, secret = _parse_basic_auth(auth_header)
        if cid is None:
            cid = params.get('client_id', '')
            secret = params.get('client_secret') or None

        if not cid:
            return None
        client = request.env['ow.mcp.oauth.client'].sudo().search(
            [('client_id', '=', cid), ('active', '=', True)], limit=1,
        )
        if not client:
            return None
        if client.token_endpoint_auth_method == 'none':
            return client
        if not secret or not client.client_secret_hash:
            return None
        if not u.constant_time_eq(
            u.sha256_hex(secret), client.client_secret_hash,
        ):
            return None
        return client

    # ------------------------------------------------------------------
    def _grant_authorization_code(self, c, client, params):
        code = params.get('code', '')
        redirect_uri = params.get('redirect_uri', '')
        verifier = params.get('code_verifier', '')
        resource = (params.get('resource') or '').rstrip('/')

        if not code or not redirect_uri or not verifier:
            return u.error_response(
                'invalid_request',
                'code, redirect_uri, and code_verifier are required.',
            )

        # Atomic single-use claim: the UPDATE ... RETURNING flips the
        # `consumed` flag inside Postgres so two concurrent redemptions
        # of the same code can never both succeed (OAuth 2.1 §4.1.3).
        # Validation that does NOT depend on database state (PKCE,
        # redirect_uri, resource) still happens after the claim — if it
        # fails the code stays burned, which is the desired outcome.
        code_hash = u.sha256_hex(code)
        request.env.cr.execute(
            "UPDATE ow_mcp_oauth_authorization_code "
            "   SET consumed = TRUE "
            " WHERE code_hash = %s "
            "   AND consumed = FALSE "
            "   AND (expires_at IS NULL OR expires_at >= (NOW() AT TIME ZONE 'UTC')) "
            "RETURNING id, client_id, user_id, redirect_uri, scope, "
            "          resource, code_challenge",
            (code_hash,),
        )
        row = request.env.cr.fetchone()
        if not row:
            return u.error_response(
                'invalid_grant',
                'unknown, expired, or already-used authorization code.',
            )
        (_id, rec_client_id, rec_user_id, rec_redirect_uri,
         rec_scope, rec_resource, rec_challenge) = row

        if rec_client_id != client.id:
            return u.error_response('invalid_grant', 'client mismatch.')
        if rec_redirect_uri != redirect_uri:
            return u.error_response('invalid_grant',
                                    'redirect_uri mismatch.')
        if not _verify_pkce(verifier, rec_challenge):
            return u.error_response('invalid_grant', 'PKCE verification failed.')

        canonical = c.effective_canonical_resource_uri()
        if resource and resource != canonical:
            return u.error_response('invalid_target',
                                    'resource does not match.')
        bound_resource = rec_resource or canonical

        user = request.env['res.users'].sudo().browse(rec_user_id)
        return self._issue_token_pair(c, client, user, rec_scope,
                                      bound_resource)

    # ------------------------------------------------------------------
    def _grant_refresh_token(self, c, client, params):
        refresh = params.get('refresh_token', '')
        resource = (params.get('resource') or '').rstrip('/')
        if not refresh:
            return u.error_response('invalid_request',
                                    'refresh_token is required.')
        rt_hash = u.sha256_hex(refresh)

        # Atomic consume: only one concurrent redemption can flip
        # consumed_at from NULL to NOW(). All other rotation/reuse paths
        # are derived from the result of this UPDATE.
        request.env.cr.execute(
            "UPDATE ow_mcp_oauth_refresh_token "
            "   SET consumed_at = (NOW() AT TIME ZONE 'UTC') "
            " WHERE token_hash = %s "
            "   AND consumed_at IS NULL "
            "   AND revoked = FALSE "
            "   AND (expires_at IS NULL "
            "        OR expires_at >= (NOW() AT TIME ZONE 'UTC')) "
            "RETURNING id, client_id, user_id, scope, audience",
            (rt_hash,),
        )
        row = request.env.cr.fetchone()
        if not row:
            # Disambiguate: if the row exists and is already consumed,
            # the presented token was replayed → revoke the whole chain
            # (OAuth 2.1 §4.3.1 reuse-detection).
            existing = request.env['ow.mcp.oauth.refresh_token'].sudo().search(
                [('token_hash', '=', rt_hash)], limit=1,
            )
            if existing and existing.consumed_at and not existing.revoked:
                existing.revoke_chain()
                u.event_log('token_revoke', client=client,
                            user=existing.user_id,
                            payload={'reason': 'refresh_reuse'})
                return u.error_response(
                    'invalid_grant',
                    'refresh_token replayed; chain revoked.',
                )
            return u.error_response(
                'invalid_grant',
                'unknown, expired, or revoked refresh_token.',
            )
        rec_id, rec_client_id, rec_user_id, rec_scope, rec_audience = row

        if rec_client_id != client.id:
            return u.error_response('invalid_grant', 'client mismatch.')

        canonical = c.effective_canonical_resource_uri()
        if resource and resource != canonical:
            return u.error_response('invalid_target', 'resource mismatch.')
        bound_resource = rec_audience

        rec = request.env['ow.mcp.oauth.refresh_token'].sudo().browse(rec_id)
        user = request.env['res.users'].sudo().browse(rec_user_id)
        return self._issue_token_pair(
            c, client, user, rec_scope, bound_resource,
            parent_refresh=rec, kind='token_refresh',
        )

    # ------------------------------------------------------------------
    def _issue_token_pair(self, c, client, user, scope, resource,
                          parent_refresh=None, kind='token_issue'):
        access_token, jti, iat, exp = jwt_helper.sign_access_token(
            c.rsa_private_key_pem,
            c.rsa_private_key_kid,
            issuer=c.issuer_url(),
            subject=str(user.id),
            audience=resource,
            client_id=client.client_id,
            scope=scope,
            ttl_seconds=c.access_token_ttl_seconds,
        )
        request.env['ow.mcp.oauth.access_token'].sudo().create({
            'jti': jti,
            'client_id': client.id,
            'user_id': user.id,
            'scope': scope,
            'audience': resource,
            'issued_at': iat,
            'expires_at': datetime.fromtimestamp(exp),
        })

        refresh_plain = u.random_token(32)
        new_refresh = request.env['ow.mcp.oauth.refresh_token'].sudo().create({
            'token_hash': u.sha256_hex(refresh_plain),
            'client_id': client.id,
            'user_id': user.id,
            'scope': scope,
            'audience': resource,
            'parent_id': parent_refresh.id if parent_refresh else False,
            'issued_at': int(time.time()),
            'expires_at': datetime.utcnow() + timedelta(
                seconds=c.refresh_token_ttl_seconds,
            ),
        })
        if parent_refresh:
            parent_refresh.replaced_by_id = new_refresh.id

        client.last_used_at = datetime.utcnow()

        u.event_log(kind, client=client, user=user,
                    payload={'scope': scope, 'audience': resource})

        return u.json_response({
            'access_token': access_token,
            'token_type': 'Bearer',
            'expires_in': c.access_token_ttl_seconds,
            'refresh_token': refresh_plain,
            'scope': scope,
        }, status=200, extra_headers=[
            ('Cache-Control', 'no-store'),
            ('Pragma', 'no-cache'),
        ])

    # ------------------------------------------------------------------
    @http.route(
        '/ow_mcp/oauth/revoke',
        type='http', auth='none', methods=['POST', 'OPTIONS'],
        csrf=False, save_session=False, readonly=False,
    )
    def revoke(self, **kwargs):
        if request.httprequest.method == 'OPTIONS':
            from odoo.http import Response
            return Response(status=204, headers=u.cors_headers())
        c = u.cfg()
        if not c.oauth_enabled:
            return u.error_response('oauth_disabled', status=404)

        params = dict(kwargs)
        client = self._authenticate_client(params)
        if client is None:
            return u.error_response('invalid_client', status=401)

        token_value = params.get('token', '')
        hint = params.get('token_type_hint', '')
        if not token_value:
            # RFC 7009 §2.2: invalid tokens MUST return 200 to avoid
            # information leak. Same here when the token is empty.
            return u.json_response({})

        h = u.sha256_hex(token_value)
        if hint != 'access_token':
            rt = request.env['ow.mcp.oauth.refresh_token'].sudo().search(
                [('token_hash', '=', h), ('client_id', '=', client.id)],
                limit=1,
            )
            if rt:
                rt.revoke_chain()
                u.event_log('token_revoke', client=client, user=rt.user_id,
                            payload={'kind': 'refresh'})
                return u.json_response({})
        if hint != 'refresh_token':
            # Access tokens are JWTs — the value isn't stored verbatim;
            # but we can revoke by jti if the caller passes the JWT.
            try:
                import jwt as _jwt
                payload = _jwt.decode(
                    token_value,
                    options={'verify_signature': False, 'verify_exp': False},
                )
                jti = payload.get('jti')
                if jti:
                    at = request.env['ow.mcp.oauth.access_token'].sudo().search(
                        [('jti', '=', jti), ('client_id', '=', client.id)],
                        limit=1,
                    )
                    if at:
                        at.revoked = True
                        u.event_log('token_revoke', client=client,
                                    user=at.user_id,
                                    payload={'kind': 'access', 'jti': jti})
            except Exception:
                pass
        return u.json_response({})
