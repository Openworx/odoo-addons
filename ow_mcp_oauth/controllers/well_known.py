"""Discovery endpoints required by the MCP Authorization spec.

  /.well-known/oauth-protected-resource    — RFC 9728
  /.well-known/oauth-authorization-server  — RFC 8414
  /ow_mcp/oauth/jwks.json                  — RFC 7517

All three are unauthenticated, CORS-permissive, and read-only.
"""
from odoo import http
from odoo.http import Response, request

from . import _utils as u


SUPPORTED_SCOPES = ['mcp:read', 'mcp:write']


class OauthWellKnown(http.Controller):

    @http.route(
        '/.well-known/oauth-protected-resource',
        type='http', auth='none', methods=['GET', 'OPTIONS'],
        csrf=False, save_session=False,
    )
    def protected_resource_metadata(self, **_kw):
        if request.httprequest.method == 'OPTIONS':
            return Response(status=204, headers=u.cors_headers())
        c = u.cfg()
        if not c.oauth_enabled:
            return u.error_response('oauth_disabled', status=404)
        base = c.base_url()
        body = {
            'resource': c.effective_canonical_resource_uri(),
            'authorization_servers': [base],
            'scopes_supported': SUPPORTED_SCOPES,
            'bearer_methods_supported': ['header'],
            'resource_documentation': f'{base}/web',
        }
        return u.json_response(body)

    @http.route(
        '/.well-known/oauth-authorization-server',
        type='http', auth='none', methods=['GET', 'OPTIONS'],
        csrf=False, save_session=False,
    )
    def authorization_server_metadata(self, **_kw):
        if request.httprequest.method == 'OPTIONS':
            return Response(status=204, headers=u.cors_headers())
        c = u.cfg()
        if not c.oauth_enabled:
            return u.error_response('oauth_disabled', status=404)
        base = c.base_url()
        body = {
            'issuer': c.issuer_url(),
            'authorization_endpoint': f'{base}/ow_mcp/oauth/authorize',
            'token_endpoint': f'{base}/ow_mcp/oauth/token',
            'registration_endpoint': f'{base}/ow_mcp/oauth/register',
            'revocation_endpoint': f'{base}/ow_mcp/oauth/revoke',
            'jwks_uri': f'{base}/ow_mcp/oauth/jwks.json',
            'response_types_supported': ['code'],
            'grant_types_supported': ['authorization_code', 'refresh_token'],
            'code_challenge_methods_supported': ['S256'],
            'token_endpoint_auth_methods_supported': [
                'none', 'client_secret_basic', 'client_secret_post',
            ],
            'scopes_supported': SUPPORTED_SCOPES,
            'service_documentation': f'{base}/web',
        }
        return u.json_response(body)

    @http.route(
        '/ow_mcp/oauth/jwks.json',
        type='http', auth='none', methods=['GET', 'OPTIONS'],
        csrf=False, save_session=False,
    )
    def jwks(self, **_kw):
        if request.httprequest.method == 'OPTIONS':
            return Response(status=204, headers=u.cors_headers())
        c = u.cfg()
        return u.json_response(c.jwks_document())
