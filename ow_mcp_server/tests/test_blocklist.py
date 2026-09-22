"""Tests for the expanded BLOCKLIST_RE — models that are never reachable
via MCP, even when yolo_mode='full' bypasses the per-model access layer.
"""
import os

from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.exceptions import AccessError

from odoo.addons.ow_mcp_server.protocol.access import (
    BLOCKLIST_RE, check_model_access,
)


# Models that must always be refused. Not every entry exists in every Odoo
# install; we only assert the regex matches. Runtime `check_model_access`
# is tested on a subset that is present in base.
_ALWAYS_BLOCKED_NAMES = [
    # Existing entries preserved.
    'ir.rule',
    'ir.config_parameter',
    'ir.logging',
    'ir.attachment.history',
    'res.users.apikeys',
    'res.users.apikeys.description',
    'auth_oauth.provider',
    'base_import.import',
    # New entries added in the security hardening pass.
    'ir.actions.server',
    'ir.actions.act_url',
    'ir.cron',
    'ir.ui.view',
    'ir.mail_server',
    'ir.attachment',
    'ir.model.access',
    'ir.model.data',
    'ir.filters',
    'ir.module.module',
    'ir.sequence',
    'ir.autovacuum',
    'ir.http',
    'res.users',
    'res.users.identitycheck',
    'res.users.log',
    'base_automation',
    'base_automation.line',
    'fetchmail.server',
    'mail.mail',
    'bus.bus',
    'bus.presence',
]


_NEVER_BLOCKED_NAMES = [
    'res.partner',
    'res.company',
    'ir.model',           # metadata; not in blocklist.
    'product.template',
]


@tagged('post_install', '-at_install')
class TestBlocklistRegex(TransactionCase):
    """Pure regex coverage — runs without any DB state."""

    def test_blocked_models_match(self):
        for name in _ALWAYS_BLOCKED_NAMES:
            with self.subTest(model=name):
                self.assertTrue(
                    BLOCKLIST_RE.match(name),
                    f'Expected {name!r} to be blocklisted.',
                )

    def test_harmless_models_do_not_match(self):
        for name in _NEVER_BLOCKED_NAMES:
            with self.subTest(model=name):
                self.assertFalse(
                    BLOCKLIST_RE.match(name),
                    f'Expected {name!r} NOT to be blocklisted.',
                )


@tagged('post_install', '-at_install')
class TestBlocklistEnforcement(TransactionCase):
    """Runtime check: even under yolo_mode='full' the blocklist wins."""

    def setUp(self):
        super().setUp()
        self.cfg = self.env['ow.mcp.config'].get_singleton()
        self.cfg.yolo_mode = 'full'
        self._yolo_prev = os.environ.get('OW_MCP_ALLOW_YOLO')
        os.environ['OW_MCP_ALLOW_YOLO'] = '1'
        self.addCleanup(self._restore)

    def _restore(self):
        if self._yolo_prev is None:
            os.environ.pop('OW_MCP_ALLOW_YOLO', None)
        else:
            os.environ['OW_MCP_ALLOW_YOLO'] = self._yolo_prev

    def test_ir_actions_server_blocked(self):
        with self.assertRaises(AccessError):
            check_model_access(self.env, 'ir.actions.server', 'read')

    def test_ir_cron_blocked(self):
        with self.assertRaises(AccessError):
            check_model_access(self.env, 'ir.cron', 'write')

    def test_ir_ui_view_blocked(self):
        with self.assertRaises(AccessError):
            check_model_access(self.env, 'ir.ui.view', 'read')

    def test_ir_mail_server_blocked(self):
        with self.assertRaises(AccessError):
            check_model_access(self.env, 'ir.mail_server', 'read')

    def test_ir_attachment_blocked(self):
        with self.assertRaises(AccessError):
            check_model_access(self.env, 'ir.attachment', 'read')

    def test_ir_model_access_blocked(self):
        with self.assertRaises(AccessError):
            check_model_access(self.env, 'ir.model.access', 'read')

    def test_ir_module_module_blocked(self):
        with self.assertRaises(AccessError):
            check_model_access(self.env, 'ir.module.module', 'write')

    def test_res_users_blocked(self):
        with self.assertRaises(AccessError):
            check_model_access(self.env, 'res.users', 'write')

    def test_bus_bus_blocked(self):
        with self.assertRaises(AccessError):
            check_model_access(self.env, 'bus.bus', 'read')
