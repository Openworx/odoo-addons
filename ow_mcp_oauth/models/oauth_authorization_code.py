"""ow.mcp.oauth.authorization_code — single-use authorization code.

Created by /authorize, consumed by /token (auth_code grant).
"""
from odoo import fields, models


class OauthAuthorizationCode(models.Model):
    _name = 'ow.mcp.oauth.authorization_code'
    _description = 'MCP OAuth Authorization Code'
    _order = 'create_date desc'

    code_hash = fields.Char(required=True, index=True, copy=False)
    client_id = fields.Many2one(
        'ow.mcp.oauth.client', required=True, ondelete='cascade', index=True,
    )
    user_id = fields.Many2one(
        'res.users', required=True, ondelete='cascade', index=True,
    )
    redirect_uri = fields.Char(required=True)
    scope = fields.Char(default='')
    resource = fields.Char(
        help='RFC 8707 resource indicator — must equal the canonical MCP URI.',
    )
    code_challenge = fields.Char(required=True)
    code_challenge_method = fields.Char(default='S256', required=True)
    state = fields.Char()
    expires_at = fields.Datetime(required=True, index=True)
    consumed = fields.Boolean(default=False, index=True)

    _sql_constraints = [
        ('code_hash_uniq', 'unique(code_hash)',
         'Authorization code hash must be unique.'),
    ]
