"""Authorization endpoint — RFC 6749 §4.1 + RFC 7636 PKCE + RFC 8707 resource.

GET  /ow_mcp/oauth/authorize  — render consent screen (auth='user')
POST /ow_mcp/oauth/authorize  — submit consent decision (auth='user', CSRF on)
"""
import logging
from datetime import datetime, timedelta
from urllib.parse import urlencode

from odoo import http
from odoo.http import request

from . import _utils as u


_logger = logging.getLogger(__name__)


def _redirect_with_error(redirect_uri: str, error: str, state: str | None,
                         description: str | None = None):
    qs = {'error': error}
    if description:
        qs['error_description'] = description
    if state:
        qs['state'] = state
    sep = '&' if '?' in redirect_uri else '?'
    return request.redirect(
        f'{redirect_uri}{sep}{urlencode(qs)}', code=302, local=False,
    )


class OauthAuthorize(http.Controller):

    @http.route(
        '/ow_mcp/oauth/authorize',
        type='http', auth='user', methods=['GET'],
        csrf=False, save_session=True, website=False, readonly=False,
    )
    def authorize_get(self, **kwargs):
        c = u.cfg()
        if not c.oauth_enabled:
            return u.error_response(
                'oauth_disabled', 'OAuth is disabled.', status=404,
            )

        # Required params
        client_id = (kwargs.get('client_id') or '').strip()
        redirect_uri = (kwargs.get('redirect_uri') or '').strip()
        response_type = (kwargs.get('response_type') or '').strip()
        code_challenge = (kwargs.get('code_challenge') or '').strip()
        code_challenge_method = (
            kwargs.get('code_challenge_method') or 'S256'
        ).strip()
        scope = (kwargs.get('scope') or 'mcp:read mcp:write').strip()
        state = (kwargs.get('state') or '').strip()
        resource = (kwargs.get('resource') or '').strip()

        # Look up client first — without it we can't safely redirect errors.
        client = request.env['ow.mcp.oauth.client'].sudo().search(
            [('client_id', '=', client_id), ('active', '=', True)], limit=1,
        )
        if not client:
            u.event_log('authorize_deny', user=request.env.user,
                        payload={'reason': 'unknown_client',
                                 'client_id': client_id})
            return u.error_response('invalid_client',
                                    'Unknown client_id.', status=400)
        if not client.matches_redirect_uri(redirect_uri):
            u.event_log('authorize_deny', client=client, user=request.env.user,
                        payload={'reason': 'redirect_uri_mismatch',
                                 'redirect_uri': redirect_uri})
            return u.error_response('invalid_redirect_uri',
                                    'redirect_uri does not match registration.',
                                    status=400)

        # From here on, errors redirect back to the client.
        if response_type != 'code':
            return _redirect_with_error(
                redirect_uri, 'unsupported_response_type', state,
            )
        if not code_challenge:
            return _redirect_with_error(
                redirect_uri, 'invalid_request', state,
                'code_challenge is required (PKCE).',
            )
        if code_challenge_method != 'S256':
            return _redirect_with_error(
                redirect_uri, 'invalid_request', state,
                'code_challenge_method must be S256.',
            )

        canonical = c.effective_canonical_resource_uri()
        if resource and resource.rstrip('/') != canonical:
            return _redirect_with_error(
                redirect_uri, 'invalid_target', state,
                'resource does not match this MCP server.',
            )
        # If client did not send `resource` we still bind to canonical.
        bound_resource = canonical

        # Intersect scope with what client + server support.
        requested = set(scope.split())
        granted_set = requested & set(client.scope_list())
        if not granted_set:
            return _redirect_with_error(
                redirect_uri, 'invalid_scope', state,
                'no overlap with client-registered scopes.',
            )
        granted_scope = ' '.join(sorted(granted_set))

        u.event_log('authorize_request', client=client, user=request.env.user,
                    payload={'scope': granted_scope, 'resource': bound_resource})

        # Skip consent if remembered.
        existing = request.env['ow.mcp.oauth.consent'].sudo().search([
            ('user_id', '=', request.env.user.id),
            ('client_id', '=', client.id),
            ('scope', '=', granted_scope),
            ('expires_at', '>', datetime.utcnow()),
        ], limit=1)
        if existing:
            return self._issue_code_and_redirect(
                client, request.env.user, redirect_uri, granted_scope,
                bound_resource, code_challenge, code_challenge_method, state,
            )

        return request.render(
            'ow_mcp_oauth.consent_template',
            {
                'client': client,
                'scopes': sorted(granted_set),
                'redirect_uri': redirect_uri,
                'state': state,
                'code_challenge': code_challenge,
                'code_challenge_method': code_challenge_method,
                'resource': bound_resource,
                'scope_str': granted_scope,
                'csrf_token': request.csrf_token(),
            },
        )

    @http.route(
        '/ow_mcp/oauth/authorize',
        type='http', auth='user', methods=['POST'],
        csrf=True, save_session=True, website=False, readonly=False,
    )
    def authorize_post(self, **kwargs):
        c = u.cfg()
        if not c.oauth_enabled:
            return u.error_response('oauth_disabled', status=404)

        decision = (kwargs.get('decision') or '').strip()
        client_id = (kwargs.get('client_id') or '').strip()
        redirect_uri = (kwargs.get('redirect_uri') or '').strip()
        scope = (kwargs.get('scope') or '').strip()
        state = (kwargs.get('state') or '').strip()
        code_challenge = (kwargs.get('code_challenge') or '').strip()
        code_challenge_method = (
            kwargs.get('code_challenge_method') or 'S256'
        ).strip()
        resource = (kwargs.get('resource') or '').strip()
        remember = (kwargs.get('remember') or '').strip() in ('1', 'on', 'true')

        client = request.env['ow.mcp.oauth.client'].sudo().search(
            [('client_id', '=', client_id), ('active', '=', True)], limit=1,
        )
        if not client or not client.matches_redirect_uri(redirect_uri):
            return u.error_response('invalid_request',
                                    'Unknown client or redirect_uri.',
                                    status=400)

        canonical = c.effective_canonical_resource_uri()
        if resource and resource.rstrip('/') != canonical:
            return _redirect_with_error(
                redirect_uri, 'invalid_target', state,
                'resource does not match this MCP server.',
            )
        bound_resource = canonical

        if decision != 'allow':
            u.event_log('authorize_deny', client=client, user=request.env.user)
            return _redirect_with_error(
                redirect_uri, 'access_denied', state,
                'User denied the authorization request.',
            )

        if remember and c.consent_remember_days > 0:
            request.env['ow.mcp.oauth.consent'].sudo().create({
                'user_id': request.env.user.id,
                'client_id': client.id,
                'scope': scope,
                'expires_at': datetime.utcnow() + timedelta(
                    days=c.consent_remember_days,
                ),
            })

        return self._issue_code_and_redirect(
            client, request.env.user, redirect_uri, scope, bound_resource,
            code_challenge, code_challenge_method, state,
        )

    def _issue_code_and_redirect(self, client, user, redirect_uri, scope,
                                 resource, code_challenge,
                                 code_challenge_method, state):
        c = u.cfg()
        code_plain = u.random_token(32)
        code_hash = u.sha256_hex(code_plain)
        request.env['ow.mcp.oauth.authorization_code'].sudo().create({
            'code_hash': code_hash,
            'client_id': client.id,
            'user_id': user.id,
            'redirect_uri': redirect_uri,
            'scope': scope,
            'resource': resource,
            'code_challenge': code_challenge,
            'code_challenge_method': code_challenge_method,
            'state': state or False,
            'expires_at': datetime.utcnow() + timedelta(
                seconds=c.auth_code_ttl_seconds,
            ),
            'consumed': False,
        })
        u.event_log('authorize_grant', client=client, user=user,
                    payload={'scope': scope, 'resource': resource})
        qs = {'code': code_plain}
        if state:
            qs['state'] = state
        sep = '&' if '?' in redirect_uri else '?'
        return request.redirect(
            f'{redirect_uri}{sep}{urlencode(qs)}', code=302, local=False,
        )
