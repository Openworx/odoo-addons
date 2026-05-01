"""ow.mcp.oauth.consent — remember a user's per-client scope grant.

While a row exists with expires_at in the future and matching scopes, the
authorize endpoint skips the consent prompt and immediately issues a code.
"""
from odoo import fields, models


class OauthConsent(models.Model):
    _name = 'ow.mcp.oauth.consent'
    _description = 'MCP OAuth Consent'
    _order = 'create_date desc'

    user_id = fields.Many2one(
        'res.users', required=True, ondelete='cascade', index=True,
    )
    client_id = fields.Many2one(
        'ow.mcp.oauth.client', required=True, ondelete='cascade', index=True,
    )
    scope = fields.Char(required=True)
    expires_at = fields.Datetime(required=True, index=True)

    _sql_constraints = [
        ('user_client_scope_uniq',
         'unique(user_id, client_id, scope)',
         'Duplicate consent for the same user/client/scope.'),
    ]
