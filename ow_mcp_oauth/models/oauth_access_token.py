"""ow.mcp.oauth.access_token — JTI registry for issued JWTs.

The JWT itself is stateless; this model exists so admins can revoke a
specific token (revoked=True → /mcp rejects it on signature-valid hits) and
so audit/cleanup work.
"""
from odoo import fields, models


class OauthAccessToken(models.Model):
    _name = 'ow.mcp.oauth.access_token'
    _description = 'MCP OAuth Access Token (JWT JTI registry)'
    _order = 'create_date desc'

    jti = fields.Char(required=True, index=True, copy=False)
    client_id = fields.Many2one(
        'ow.mcp.oauth.client', required=True, ondelete='cascade', index=True,
    )
    user_id = fields.Many2one(
        'res.users', required=True, ondelete='cascade', index=True,
    )
    scope = fields.Char(default='')
    audience = fields.Char(required=True)
    issued_at = fields.Integer(required=True)
    expires_at = fields.Datetime(required=True, index=True)
    revoked = fields.Boolean(default=False, index=True)

    _sql_constraints = [
        ('jti_uniq', 'unique(jti)', 'JTI must be unique.'),
    ]
