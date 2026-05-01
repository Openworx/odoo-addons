"""ow.mcp.oauth.event — OAuth-flow audit log.

Distinct from ow.mcp.audit.log (which records MCP RPC calls) — this records
authorization-server events: registrations, authorize requests/grants,
token issuance, refresh, revoke, rejections.
"""
import json

from odoo import api, fields, models
from odoo.tools import config as odoo_config


_REDACT_KEYS = (
    'client_secret', 'code', 'code_verifier', 'access_token',
    'refresh_token', 'authorization', 'private_key',
)
_REDACTED = '***REDACTED***'


def _redact(obj):
    if isinstance(obj, dict):
        return {
            k: (_REDACTED if any(s in k.lower() for s in _REDACT_KEYS)
                else _redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


class OauthEvent(models.Model):
    _name = 'ow.mcp.oauth.event'
    _description = 'MCP OAuth Event'
    _order = 'create_date desc'

    kind = fields.Selection(
        [('register', 'Register'),
         ('authorize_request', 'Authorize request'),
         ('authorize_grant', 'Authorize grant'),
         ('authorize_deny', 'Authorize deny'),
         ('token_issue', 'Token issue'),
         ('token_refresh', 'Token refresh'),
         ('token_revoke', 'Token revoke'),
         ('token_reject', 'Token reject')],
        required=True, index=True,
    )
    client_id = fields.Many2one('ow.mcp.oauth.client', ondelete='set null')
    user_id = fields.Many2one('res.users', ondelete='set null')
    ip = fields.Char(index=True)
    user_agent = fields.Char()
    payload_json = fields.Text()
    error_message = fields.Char()

    @api.model
    def log_event(self, kind, *, client=None, user=None, ip=None,
                  user_agent=None, payload=None, error=None):
        try:
            text = None
            if payload is not None:
                text = json.dumps(_redact(payload), default=str)[:4000]
            self.sudo().create({
                'kind': kind,
                'client_id': client.id if client else False,
                'user_id': user.id if user else False,
                'ip': ip or False,
                'user_agent': (user_agent or '')[:255] or False,
                'payload_json': text,
                'error_message': (error or '')[:255] or False,
            })
        except Exception:
            # Never let audit failure break the request flow.
            import logging
            logging.getLogger(__name__).exception(
                'ow_mcp_oauth: failed to write audit event'
            )

    @api.model
    def cron_cleanup_old_entries(self):
        """Delete events older than 90 days (configurable via odoo.conf
        if needed). Mirrors ow.mcp.audit.log retention pattern.
        """
        retention = int(odoo_config.get('ow_mcp_oauth_event_retention_days', 90))
        if retention <= 0:
            return 0
        self.env.cr.execute(
            "DELETE FROM ow_mcp_oauth_event "
            "WHERE create_date < (NOW() - INTERVAL %s)",
            (f'{retention} days',),
        )
        return self.env.cr.rowcount
