from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged('post_install', '-at_install')
class TestMcpConfig(TransactionCase):
    def test_singleton_created_by_data(self):
        cfg = self.env['ow.mcp.config'].get_singleton()
        self.assertTrue(cfg)
        self.assertEqual(cfg.yolo_mode, 'off')
        self.assertEqual(cfg.default_limit, 80)
        self.assertEqual(cfg.max_limit, 500)

    def test_singleton_idempotent(self):
        a = self.env['ow.mcp.config'].get_singleton()
        b = self.env['ow.mcp.config'].get_singleton()
        self.assertEqual(a.id, b.id)

    def test_yolo_full_sets_warning_banner(self):
        cfg = self.env['ow.mcp.config'].get_singleton()
        cfg.yolo_mode = 'full'
        self.assertIn('YOLO', cfg.warning_banner or '')

    def test_yolo_off_has_no_banner(self):
        cfg = self.env['ow.mcp.config'].get_singleton()
        cfg.yolo_mode = 'off'
        self.assertFalse(cfg.warning_banner)
