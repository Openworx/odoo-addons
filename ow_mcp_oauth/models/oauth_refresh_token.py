"""ow.mcp.oauth.refresh_token — opaque refresh tokens with rotation chain.

OAuth 2.1 §4.3.1 mandates rotation + reuse-detection: when a refresh token
is exchanged, mark it consumed and link the new one. If a consumed token
is presented again, revoke the entire chain (treat as theft).
"""
from odoo import api, fields, models


class OauthRefreshToken(models.Model):
    _name = 'ow.mcp.oauth.refresh_token'
    _description = 'MCP OAuth Refresh Token'
    _order = 'create_date desc'

    token_hash = fields.Char(required=True, index=True, copy=False)
    client_id = fields.Many2one(
        'ow.mcp.oauth.client', required=True, ondelete='cascade', index=True,
    )
    user_id = fields.Many2one(
        'res.users', required=True, ondelete='cascade', index=True,
    )
    scope = fields.Char(default='')
    audience = fields.Char(required=True)
    parent_id = fields.Many2one(
        'ow.mcp.oauth.refresh_token', ondelete='set null',
        help='Refresh token this one replaced (rotation chain).',
    )
    replaced_by_id = fields.Many2one(
        'ow.mcp.oauth.refresh_token', ondelete='set null',
    )
    issued_at = fields.Integer(required=True)
    chain_issued_at = fields.Integer(
        help='Unix time the rotation chain started (the first refresh token '
             'of an authorization). Bounds the chain with '
             'refresh_token_absolute_ttl_seconds.',
    )
    expires_at = fields.Datetime(required=True, index=True)
    consumed_at = fields.Datetime(index=True)
    revoked = fields.Boolean(default=False, index=True)

    _sql_constraints = [
        ('token_hash_uniq', 'unique(token_hash)',
         'Refresh token hash must be unique.'),
    ]

    def revoke_chain(self):
        """Revoke this token and every token reachable via parent_id /
        replaced_by_id, in one atomic SQL statement so a crash mid-walk
        cannot leave the chain partially revoked.

        the same statement revokes the access
        tokens already issued to the chain's client + user, so a replayed
        (stolen) refresh token or an explicit /revoke also kills them
        instead of leaving them valid until `exp`.
        """
        if not self:
            return True
        # The recursive CTE walks the rotation graph in BOTH directions:
        # from each known node `cur`, step to its parent, its children,
        # the token it was replaced by, and the tokens it replaced.
        # That guarantees we touch every node in the connected component
        # regardless of where in the chain `self` happens to sit.
        self.env.cr.execute(
            """
            WITH RECURSIVE chain(id) AS (
                SELECT id FROM ow_mcp_oauth_refresh_token
                 WHERE id = ANY(%s)
                UNION
                SELECT t.id
                  FROM ow_mcp_oauth_refresh_token t, chain c,
                       ow_mcp_oauth_refresh_token cur
                 WHERE cur.id = c.id
                   AND ( t.id = cur.parent_id
                      OR t.parent_id = cur.id
                      OR t.id = cur.replaced_by_id
                      OR t.replaced_by_id = cur.id )
            ), revoke_refresh AS (
                UPDATE ow_mcp_oauth_refresh_token
                   SET revoked = TRUE
                 WHERE id IN (SELECT id FROM chain)
                   AND revoked = FALSE
            )
            UPDATE ow_mcp_oauth_access_token a
               SET revoked = TRUE
              FROM ow_mcp_oauth_refresh_token r
             WHERE r.id IN (SELECT id FROM chain)
               AND a.client_id = r.client_id
               AND a.user_id = r.user_id
               AND a.revoked = FALSE
            """,
            (list(self.ids),),
        )
        # Raw UPDATE bypasses the ORM cache; drop stale `revoked` so any
        # reads that follow within the same transaction see the new value.
        self.invalidate_recordset(['revoked'])
        self.env['ow.mcp.oauth.access_token'].invalidate_model(['revoked'])
        return True

    @api.model
    def revoke_for_users(self, user_ids):
        """Revoke every refresh and access
        token of `user_ids` (password change, deactivation)."""
        if not user_ids:
            return True
        for table in ('ow_mcp_oauth_refresh_token',
                      'ow_mcp_oauth_access_token'):
            self.env.cr.execute(
                f'UPDATE {table} SET revoked = TRUE '
                f' WHERE user_id = ANY(%s) AND revoked = FALSE',
                (list(user_ids),),
            )
        self.invalidate_model(['revoked'])
        self.env['ow.mcp.oauth.access_token'].invalidate_model(['revoked'])
        return True
