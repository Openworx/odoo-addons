from datetime import timedelta

from odoo import api, fields, models


class MCPAuditLog(models.Model):
    _name = 'ow.mcp.audit.log'
    _description = 'MCP Audit Log'
    _order = 'id desc'

    user_id = fields.Many2one('res.users', ondelete='set null', index=True)
    login = fields.Char(help='Login at call time (preserved if user deleted).')
    company_id = fields.Many2one(
        'res.company', ondelete='set null', index=True,
        help='Company under which the call ran (NULL = user default).',
    )
    method = fields.Char(index=True)
    tool_name = fields.Char(index=True)
    model_name = fields.Char(index=True)
    operation = fields.Selection([
        ('read', 'read'),
        ('write', 'write'),
        ('create', 'create'),
        ('unlink', 'unlink'),
    ])
    record_ids = fields.Char(help='JSON-serialised ids when applicable.')
    success = fields.Boolean(default=True, index=True)
    error_code = fields.Integer()
    error_message = fields.Char()
    ip_address = fields.Char(index=True)
    duration_ms = fields.Integer()
    payload_bytes = fields.Integer()
    request_payload = fields.Text(help='Tool arguments (JSON).')
    response_summary = fields.Text(help='Truncated structured result or error detail.')

    # Batch size for cron cleanup. Each batch commits so the cron can be
    # interrupted cleanly and never holds a transaction open over millions
    # of rows.
    _CLEANUP_BATCH_SIZE = 5000

    @api.model
    def cron_cleanup_old_entries(self):
        """Delete rows older than audit_retention_days on ow.mcp.config.

        0 or a missing config record disables retention (rows kept forever).
        Deletes in fixed-size batches with an intermediate commit so the cron
        scales to large histories without a long-running transaction.
        """
        cfg = self.env['ow.mcp.config'].sudo().get_singleton()
        days = cfg.audit_retention_days or 0
        if days <= 0:
            return 0
        cutoff = fields.Datetime.now() - timedelta(days=days)
        total = 0
        while True:
            batch = self.sudo().search(
                [('create_date', '<', cutoff)],
                limit=self._CLEANUP_BATCH_SIZE,
                order='id asc',
            )
            if not batch:
                break
            n = len(batch)
            batch.unlink()
            total += n
            # Commit between batches; test runs inside a TransactionCase
            # will roll the whole thing back, so this is only material in
            # cron context.
            if not self.env.registry.in_test_mode():
                self.env.cr.commit()
            if n < self._CLEANUP_BATCH_SIZE:
                break
        return total
