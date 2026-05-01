"""Bridge ow.mcp.oauth.config singleton into General Settings."""
from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    ow_mcp_oauth_enabled = fields.Boolean(default=True)
    ow_mcp_oauth_dcr_enabled = fields.Boolean(default=True)
    ow_mcp_oauth_canonical_resource_uri = fields.Char()
    ow_mcp_oauth_access_token_ttl_seconds = fields.Integer(default=900)
    ow_mcp_oauth_refresh_token_ttl_seconds = fields.Integer(default=2592000)
    ow_mcp_oauth_auth_code_ttl_seconds = fields.Integer(default=60)
    ow_mcp_oauth_consent_remember_days = fields.Integer(default=30)
    ow_mcp_oauth_unused_client_purge_days = fields.Integer(default=7)
    ow_mcp_oauth_dcr_rate_limit_per_ip_per_hour = fields.Integer(default=20)
    ow_mcp_oauth_active_kid = fields.Char(
        compute='_compute_ow_mcp_oauth_key_status',
    )
    ow_mcp_oauth_previous_kid = fields.Char(
        compute='_compute_ow_mcp_oauth_key_status',
    )

    @api.depends('ow_mcp_oauth_enabled')
    def _compute_ow_mcp_oauth_key_status(self):
        s = self.env['ow.mcp.oauth.config'].sudo().get_singleton()
        for rec in self:
            rec.ow_mcp_oauth_active_kid = s.rsa_private_key_kid or ''
            rec.ow_mcp_oauth_previous_kid = s.rsa_previous_public_key_kid or ''

    def get_values(self):
        res = super().get_values()
        s = self.env['ow.mcp.oauth.config'].sudo().get_singleton()
        res.update({
            'ow_mcp_oauth_enabled': s.oauth_enabled,
            'ow_mcp_oauth_dcr_enabled': s.oauth_dcr_enabled,
            'ow_mcp_oauth_canonical_resource_uri': s.canonical_resource_uri,
            'ow_mcp_oauth_access_token_ttl_seconds': s.access_token_ttl_seconds,
            'ow_mcp_oauth_refresh_token_ttl_seconds': s.refresh_token_ttl_seconds,
            'ow_mcp_oauth_auth_code_ttl_seconds': s.auth_code_ttl_seconds,
            'ow_mcp_oauth_consent_remember_days': s.consent_remember_days,
            'ow_mcp_oauth_unused_client_purge_days': s.unused_client_purge_days,
            'ow_mcp_oauth_dcr_rate_limit_per_ip_per_hour':
                s.dcr_rate_limit_per_ip_per_hour,
        })
        return res

    def set_values(self):
        super().set_values()
        self.env['ow.mcp.oauth.config'].sudo().get_singleton().write({
            'oauth_enabled': self.ow_mcp_oauth_enabled,
            'oauth_dcr_enabled': self.ow_mcp_oauth_dcr_enabled,
            'canonical_resource_uri': self.ow_mcp_oauth_canonical_resource_uri,
            'access_token_ttl_seconds': self.ow_mcp_oauth_access_token_ttl_seconds,
            'refresh_token_ttl_seconds': self.ow_mcp_oauth_refresh_token_ttl_seconds,
            'auth_code_ttl_seconds': self.ow_mcp_oauth_auth_code_ttl_seconds,
            'consent_remember_days': self.ow_mcp_oauth_consent_remember_days,
            'unused_client_purge_days': self.ow_mcp_oauth_unused_client_purge_days,
            'dcr_rate_limit_per_ip_per_hour':
                self.ow_mcp_oauth_dcr_rate_limit_per_ip_per_hour,
        })

    def action_ow_mcp_oauth_rotate_key(self):
        self.env['ow.mcp.oauth.config'].sudo().get_singleton().rotate_keypair()
        return {
            'type': 'ir.actions.client',
            'tag': 'reload',
        }
