"""Expose the ow.mcp.config singleton through Odoo's General Settings UI.

Standard Odoo pattern: plain (non-related) fields + get_values/set_values.
Related writes through a computed non-stored Many2one are unreliable in the
res.config.settings execute() flow, so we use the explicit pair instead.
"""
from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # ---- Mode / transport -------------------------------------------------

    ow_mcp_yolo_mode = fields.Selection(
        [('off', 'Off (production)'),
         ('read', 'YOLO read (dev only)'),
         ('full', 'YOLO full (dev only — DANGEROUS)')],
        default='off',
    )
    ow_mcp_enable_http_transport = fields.Boolean(default=True)
    ow_mcp_enable_sse = fields.Boolean(default=False)
    ow_mcp_require_https = fields.Boolean(default=False)
    ow_mcp_max_body_bytes = fields.Integer(default=1048576)

    # ---- Limits / formatting ---------------------------------------------

    ow_mcp_default_limit = fields.Integer(default=80)
    ow_mcp_max_limit = fields.Integer(default=500)
    ow_mcp_max_text_len = fields.Integer(default=2000)
    ow_mcp_max_relational_preview = fields.Integer(default=20)

    # ---- Security ---------------------------------------------------------

    ow_mcp_rate_limit_per_minute = fields.Integer(default=120)
    ow_mcp_enable_cors = fields.Boolean(default=False)
    ow_mcp_cors_origins = fields.Text()
    ow_mcp_audit_log = fields.Boolean(default=False)
    ow_mcp_audit_retention_days = fields.Integer(default=30)
    ow_mcp_cache_enabled = fields.Boolean(default=True)
    ow_mcp_cache_ttl_seconds = fields.Integer(default=60)
    ow_mcp_ip_whitelist = fields.Text()

    # ---- Read-only banners (computed from singleton) ----------------------

    ow_mcp_warning_banner = fields.Char(
        compute='_compute_ow_mcp_banners',
    )
    ow_mcp_proxy_mode_warning = fields.Char(
        compute='_compute_ow_mcp_banners',
    )

    @api.depends('ow_mcp_yolo_mode', 'ow_mcp_ip_whitelist')
    def _compute_ow_mcp_banners(self):
        singleton = self.env['ow.mcp.config'].sudo().get_singleton()
        for rec in self:
            rec.ow_mcp_warning_banner = singleton.warning_banner
            rec.ow_mcp_proxy_mode_warning = singleton.proxy_mode_warning

    def get_values(self):
        res = super().get_values()
        s = self.env['ow.mcp.config'].sudo().get_singleton()
        res.update({
            'ow_mcp_yolo_mode': s.yolo_mode,
            'ow_mcp_enable_http_transport': s.enable_http_transport,
            'ow_mcp_enable_sse': s.enable_sse,
            'ow_mcp_require_https': s.require_https,
            'ow_mcp_max_body_bytes': s.max_body_bytes,
            'ow_mcp_default_limit': s.default_limit,
            'ow_mcp_max_limit': s.max_limit,
            'ow_mcp_max_text_len': s.max_text_len,
            'ow_mcp_max_relational_preview': s.max_relational_preview,
            'ow_mcp_rate_limit_per_minute': s.rate_limit_per_minute,
            'ow_mcp_enable_cors': s.enable_cors,
            'ow_mcp_cors_origins': s.cors_origins,
            'ow_mcp_audit_log': s.audit_log,
            'ow_mcp_audit_retention_days': s.audit_retention_days,
            'ow_mcp_cache_enabled': s.cache_enabled,
            'ow_mcp_cache_ttl_seconds': s.cache_ttl_seconds,
            'ow_mcp_ip_whitelist': s.ip_whitelist,
        })
        return res

    def set_values(self):
        super().set_values()
        self.env['ow.mcp.config'].sudo().get_singleton().write({
            'yolo_mode': self.ow_mcp_yolo_mode,
            'enable_http_transport': self.ow_mcp_enable_http_transport,
            'enable_sse': self.ow_mcp_enable_sse,
            'require_https': self.ow_mcp_require_https,
            'max_body_bytes': self.ow_mcp_max_body_bytes,
            'default_limit': self.ow_mcp_default_limit,
            'max_limit': self.ow_mcp_max_limit,
            'max_text_len': self.ow_mcp_max_text_len,
            'max_relational_preview': self.ow_mcp_max_relational_preview,
            'rate_limit_per_minute': self.ow_mcp_rate_limit_per_minute,
            'enable_cors': self.ow_mcp_enable_cors,
            'cors_origins': self.ow_mcp_cors_origins,
            'audit_log': self.ow_mcp_audit_log,
            'audit_retention_days': self.ow_mcp_audit_retention_days,
            'cache_enabled': self.ow_mcp_cache_enabled,
            'cache_ttl_seconds': self.ow_mcp_cache_ttl_seconds,
            'ip_whitelist': self.ow_mcp_ip_whitelist,
        })
