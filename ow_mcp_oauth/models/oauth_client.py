"""ow.mcp.oauth.client — RFC 7591 Dynamic Client Registration record.

One row per registered client (Claude.ai, Claude Desktop, ChatGPT, custom).
"""
import json
import secrets

from odoo import api, fields, models
from odoo.exceptions import ValidationError


def _new_client_id(_self=None):
    return secrets.token_urlsafe(24)


class OauthClient(models.Model):
    _name = 'ow.mcp.oauth.client'
    _description = 'MCP OAuth Client'
    _order = 'create_date desc'

    client_id = fields.Char(
        required=True, index=True, copy=False, default=_new_client_id,
    )
    client_secret_hash = fields.Char(
        groups='ow_mcp_server.group_mcp_admin',
        help='sha256 hash of the issued client_secret. Empty for public '
             'clients (token_endpoint_auth_method=none).',
    )
    client_id_issued_at = fields.Integer()  # unix seconds
    client_secret_expires_at = fields.Integer(default=0)  # 0 = never
    client_name = fields.Char()
    redirect_uris = fields.Text(
        help='JSON array of registered redirect URIs. Exact-match validated.',
    )
    grant_types = fields.Char(default='authorization_code refresh_token')
    response_types = fields.Char(default='code')
    token_endpoint_auth_method = fields.Selection(
        [('none', 'none (public client / PKCE)'),
         ('client_secret_basic', 'client_secret_basic'),
         ('client_secret_post', 'client_secret_post')],
        default='none', required=True,
    )
    scope = fields.Char(default='mcp:read mcp:write')
    software_id = fields.Char()
    software_version = fields.Char()
    registration_access_token_hash = fields.Char(
        groups='ow_mcp_server.group_mcp_admin',
        help='sha256 hash of the RFC 7592 management token; if set, the '
             'holder may update or delete this registration.',
    )
    is_dynamic = fields.Boolean(
        default=True,
        help='True when registered via /register (DCR), False when an admin '
             'created it manually.',
    )
    last_used_at = fields.Datetime()
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ('client_id_uniq', 'unique(client_id)',
         'OAuth client_id must be unique.'),
    ]

    @api.constrains('redirect_uris')
    def _check_redirect_uris(self):
        for rec in self:
            if not rec.redirect_uris:
                raise ValidationError('At least one redirect_uri is required.')
            try:
                uris = json.loads(rec.redirect_uris)
            except Exception:
                raise ValidationError('redirect_uris must be a JSON array.')
            if not isinstance(uris, list) or not uris:
                raise ValidationError('redirect_uris must be a non-empty JSON array.')
            for u in uris:
                if not isinstance(u, str) or not u:
                    raise ValidationError('redirect_uris entries must be strings.')

    def redirect_uri_list(self):
        self.ensure_one()
        try:
            return json.loads(self.redirect_uris or '[]')
        except Exception:
            return []

    def matches_redirect_uri(self, candidate):
        """Exact-match per OAuth 2.1 §7.5.1. No prefix / wildcard."""
        self.ensure_one()
        return candidate in self.redirect_uri_list()

    def grant_types_list(self):
        return (self.grant_types or '').split()

    def scope_list(self):
        return (self.scope or '').split()
