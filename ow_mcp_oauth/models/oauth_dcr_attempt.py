"""ow.mcp.oauth.dcr_attempt — per-IP DCR rate-limit ledger.

Persisted in the DB so the sliding-window counter is shared across
all Odoo workers (the previous in-memory map allowed
`limit × workers` registrations per hour in production).
"""
from odoo import fields, models


class OauthDcrAttempt(models.Model):
    _name = 'ow.mcp.oauth.dcr_attempt'
    _description = 'MCP OAuth DCR Attempt'
    _order = 'id desc'
    _rec_name = 'ip'

    ip = fields.Char(required=True, index=True)

    def init(self):
        # Composite (ip, create_date): the throttle query selects rows
        # for one IP within a sliding window, so the planner walks
        # `ip` first then range-scans `create_date`. A single-column
        # index on `ip` would force a heap fetch per matching row.
        self.env.cr.execute(
            'CREATE INDEX IF NOT EXISTS '
            'ow_mcp_oauth_dcr_attempt_ip_create_date_idx '
            'ON ow_mcp_oauth_dcr_attempt (ip, create_date)'
        )
