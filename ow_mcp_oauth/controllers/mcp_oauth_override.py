"""Subclass of ow_mcp_server's MCPController.

Adds OAuth 2.1 bearer-token authentication while keeping the original
API-key flow as a fallback. Also enriches the 401 challenge with the
RFC 9728 `resource_metadata` parameter so MCP clients can bootstrap
discovery and trigger Dynamic Client Registration.
"""
import logging
from datetime import datetime

from odoo import http
from odoo.http import request

from odoo.addons.ow_mcp_server.controllers.mcp_controller import MCPController
from odoo.addons.ow_mcp_server.protocol import jsonrpc

from ..protocol import scope_gate
from ..tools import jwt_helper


_logger = logging.getLogger(__name__)


WRITE_TOOLS = frozenset({'create_record', 'update_record', 'delete_record'})
READ_TOOLS = frozenset({
    'list_models', 'search_records', 'get_record', 'search_count',
    'get_model_schema', 'read_group', 'get_user_context', 'list_modules',
})


class MCPControllerOAuth(MCPController):

    # ------------------------------------------------------------------
    # Odoo binds routes to the controller class that declares them. We
    # must re-decorate the routes on this subclass so that requests are
    # dispatched against MCPControllerOAuth instances and pick up our
    # _authenticate / _unauthorized overrides.
    # ------------------------------------------------------------------
    @http.route(
        '/mcp', type='http', auth='none', methods=['POST'],
        csrf=False, save_session=False, readonly=False,
    )
    def mcp_post(self, **kwargs):
        return super().mcp_post(**kwargs)

    @http.route(
        '/mcp', type='http', auth='none', methods=['GET'],
        csrf=False, save_session=False, readonly=False,
    )
    def mcp_sse(self, **kwargs):
        return super().mcp_sse(**kwargs)

    @http.route(
        '/mcp', type='http', auth='none', methods=['OPTIONS'],
        csrf=False, save_session=False, readonly=False,
    )
    def mcp_options(self, **kwargs):
        return super().mcp_options(**kwargs)

    # ------------------------------------------------------------------
    # _authenticate is called by the parent's mcp_post / mcp_sse. We try
    # OAuth first; on miss we fall back to res.users.apikeys via super().
    # ------------------------------------------------------------------
    def _authenticate(self):
        auth = request.httprequest.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            token = auth[7:].strip()
            uid = self._authenticate_oauth_jwt(token)
            if uid:
                return uid
        return super()._authenticate()

    def _authenticate_oauth_jwt(self, token):
        if not token:
            return None
        try:
            cfg = request.env['ow.mcp.oauth.config'].sudo().get_singleton()
        except Exception:
            return None
        if not cfg.oauth_enabled:
            return None

        public_keys = cfg.public_keys_by_kid()
        if not public_keys:
            return None

        payload = jwt_helper.verify_access_token(
            token,
            public_pems_by_kid=public_keys,
            issuer=cfg.issuer_url(),
            audience=cfg.effective_canonical_resource_uri(),
        )
        if not payload:
            return None

        try:
            uid = int(payload.get('sub') or 0)
        except (TypeError, ValueError):
            return None
        if not uid:
            return None

        # Revocation / existence check via JTI.
        # the jti MUST be a live row issued by
        # this server to the same user and to an active client. The old code
        # accepted a signature-valid token whose jti was unknown, so anyone
        # holding a copy of the database (clone, dump, backup: the signing
        # key lives in ow_mcp_oauth_config) could mint tokens for any user.
        jti = payload.get('jti')
        if not jti:
            return None
        at = request.env['ow.mcp.oauth.access_token'].sudo().search(
            [('jti', '=', jti)], limit=1,
        )
        if (not at or at.revoked or at.user_id.id != uid
                or not at.client_id.active
                or at.client_id.client_id != payload.get('client_id')
                or (at.expires_at and at.expires_at < datetime.utcnow())):
            return None
        # Sanity: user must still exist and be active.
        user = request.env['res.users'].sudo().browse(uid).exists()
        if not user or not user.active:
            return None

        # Stash scope so the dispatcher-level guard can enforce it.
        request.ow_mcp_oauth_scope = payload.get('scope', '')
        request.ow_mcp_oauth_jti = jti
        return uid

    # ------------------------------------------------------------------
    # insufficient scope → HTTP 403 with a
    # RFC 6750 `insufficient_scope` challenge naming the scope to request
    # (MCP 2025-06-18 authorization, error handling), not a tool error.
    # ------------------------------------------------------------------
    def _precheck_message(self, cfg, msg):
        if msg.get('method') != 'tools/call':
            return None
        name = (msg.get('params') or {}).get('name')
        needed = scope_gate.missing_scope(name)
        if not needed:
            return None
        resp = self._json(
            jsonrpc.error(
                msg.get('id'), jsonrpc.PERMISSION_DENIED,
                f'OAuth token lacks {needed} scope required by tool '
                f'{name!r}.',
            ),
            403, cfg=cfg,
        )
        oauth_cfg = request.env['ow.mcp.oauth.config'].sudo().get_singleton()
        prm = f'{oauth_cfg.base_url()}/.well-known/oauth-protected-resource'
        resp.headers['WWW-Authenticate'] = (
            f'Bearer realm="ow_mcp", error="insufficient_scope", '
            f'scope="{needed}", resource_metadata="{prm}"'
        )
        return resp

    # ------------------------------------------------------------------
    # 401 challenge: add resource_metadata so MCP clients can discover
    # the OAuth metadata document and start the flow (RFC 9728 §5.1).
    # ------------------------------------------------------------------
    def _unauthorized(self):
        resp = super()._unauthorized()
        try:
            cfg = request.env['ow.mcp.oauth.config'].sudo().get_singleton()
        except Exception:
            return resp
        if not cfg.oauth_enabled:
            return resp
        prm = f'{cfg.base_url()}/.well-known/oauth-protected-resource'
        # RFC 6750 §3.1 — no error code when
        # the request carried no token at all.
        auth = request.httprequest.headers.get('Authorization', '')
        error = ''
        if auth.startswith('Bearer ') and auth[7:].strip():
            error = (
                'error="invalid_token", '
                'error_description="The access token is invalid, expired '
                'or revoked.", '
            )
        resp.headers['WWW-Authenticate'] = (
            f'Bearer realm="ow_mcp", {error}resource_metadata="{prm}"'
        )
        return resp
