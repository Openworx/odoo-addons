"""Tests for the audit-log retention cron."""
from datetime import timedelta

from odoo import fields
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged('post_install', '-at_install')
class TestAuditCleanup(TransactionCase):
    def setUp(self):
        super().setUp()
        self.cfg = self.env['ow.mcp.config'].sudo().get_singleton()
        self.Log = self.env['ow.mcp.audit.log'].sudo()
        self.Log.search([]).unlink()

    def _make_log(self, age_days):
        """Create a log row then backdate its create_date to age_days ago."""
        log = self.Log.create({'method': 'ping', 'success': True})
        when = fields.Datetime.now() - timedelta(days=age_days)
        # create_date is auto-managed; use raw SQL to backdate.
        self.env.cr.execute(
            'UPDATE ow_mcp_audit_log SET create_date = %s WHERE id = %s',
            (when, log.id),
        )
        log.invalidate_recordset(['create_date'])
        return log

    def test_deletes_rows_older_than_retention(self):
        self.cfg.audit_retention_days = 30
        old = self._make_log(age_days=45)
        recent = self._make_log(age_days=5)
        deleted = self.Log.cron_cleanup_old_entries()
        self.assertEqual(deleted, 1)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_zero_disables_retention(self):
        self.cfg.audit_retention_days = 0
        old = self._make_log(age_days=365)
        deleted = self.Log.cron_cleanup_old_entries()
        self.assertEqual(deleted, 0)
        self.assertTrue(old.exists())

    def test_returns_zero_when_nothing_to_delete(self):
        self.cfg.audit_retention_days = 30
        recent = self._make_log(age_days=1)
        deleted = self.Log.cron_cleanup_old_entries()
        self.assertEqual(deleted, 0)
        self.assertTrue(recent.exists())
