import os

from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.exceptions import AccessError

from odoo.addons.ow_mcp_server.protocol.access import (
    check_model_access,
    filter_fields,
    assert_fields_allowed,
)


@tagged('post_install', '-at_install')
class TestAccess(TransactionCase):
    def setUp(self):
        super().setUp()
        self.cfg = self.env['ow.mcp.config'].get_singleton()
        self.cfg.yolo_mode = 'off'
        self.partner_model_id = self.env.ref('base.model_res_partner').id
        # Clear any pre-existing rule created outside tests (e.g. via the UI
        # while manually testing the module) — the sql_constraint is unique
        # per model_id.
        self.env['ow.mcp.model.access'].sudo().search(
            [('model_id', '=', self.partner_model_id)],
        ).unlink()
        self.ma = self.env['ow.mcp.model.access'].create({
            'model_id': self.partner_model_id,
            'allow_read': True,
            'allow_write': False,
        })
        # YOLO runtime gate requires this env var in addition to dev_mode.
        # Tests run with dev_mode on (see odoo.conf) so we only need the var.
        self._yolo_prev = os.environ.get('OW_MCP_ALLOW_YOLO')
        os.environ['OW_MCP_ALLOW_YOLO'] = '1'
        self.addCleanup(self._restore_yolo_env)

    def _restore_yolo_env(self):
        if self._yolo_prev is None:
            os.environ.pop('OW_MCP_ALLOW_YOLO', None)
        else:
            os.environ['OW_MCP_ALLOW_YOLO'] = self._yolo_prev

    def test_blocklist_always_blocks_even_in_yolo_full(self):
        self.cfg.yolo_mode = 'full'
        with self.assertRaises(AccessError):
            check_model_access(self.env, 'ir.config_parameter', 'read')

    def test_blocklist_apikeys(self):
        self.cfg.yolo_mode = 'full'
        with self.assertRaises(AccessError):
            check_model_access(self.env, 'res.users.apikeys', 'read')

    def test_read_allowed(self):
        self.assertTrue(check_model_access(self.env, 'res.partner', 'read'))

    def test_write_denied_without_flag(self):
        with self.assertRaises(AccessError):
            check_model_access(self.env, 'res.partner', 'write')

    def test_yolo_read_bypasses_for_read(self):
        self.cfg.yolo_mode = 'read'
        self.ma.unlink()
        self.assertTrue(check_model_access(self.env, 'res.partner', 'read'))

    def test_yolo_read_does_not_bypass_for_write(self):
        self.cfg.yolo_mode = 'read'
        self.ma.unlink()
        with self.assertRaises(AccessError):
            check_model_access(self.env, 'res.partner', 'write')

    def test_yolo_full_bypasses_for_write(self):
        self.cfg.yolo_mode = 'full'
        self.ma.unlink()
        self.assertTrue(check_model_access(self.env, 'res.partner', 'write'))

    def test_unknown_model_raises(self):
        with self.assertRaises(AccessError):
            check_model_access(self.env, 'does.not.exist', 'read')

    def test_unknown_op_raises(self):
        with self.assertRaises(AccessError):
            check_model_access(self.env, 'res.partner', 'bogus')

    def test_filter_fields_no_allowlist(self):
        out = filter_fields(self.env, 'res.partner', ['name', 'email'])
        self.assertEqual(out, ['name', 'email'])

    def test_filter_fields_with_allowlist(self):
        self.ma.allowed_fields_json = '["name"]'
        out = filter_fields(self.env, 'res.partner', ['name', 'email'])
        self.assertEqual(out, ['name'])

    def test_assert_fields_allowed_raises_on_extra(self):
        self.ma.allowed_fields_json = '["name"]'
        with self.assertRaises(AccessError):
            assert_fields_allowed(self.env, 'res.partner', ['name', 'email'])

    def test_assert_fields_allowed_passes_when_no_allowlist(self):
        assert_fields_allowed(self.env, 'res.partner', ['name', 'email', 'vat'])

    def test_yolo_gate_requires_env_var(self):
        """Without OW_MCP_ALLOW_YOLO=1 the field value is downgraded to 'off',
        so blocklist-free models still require MCP access rules.
        """
        from odoo.addons.ow_mcp_server.protocol.access import (
            _effective_yolo_mode,
        )
        self.cfg.yolo_mode = 'full'
        self.ma.unlink()
        os.environ.pop('OW_MCP_ALLOW_YOLO', None)
        self.assertEqual(_effective_yolo_mode(self.cfg), 'off')
        # Without a rule and without effective yolo, write is denied.
        with self.assertRaises(AccessError):
            check_model_access(self.env, 'res.partner', 'write')

    def test_yolo_gate_allows_when_env_set_and_dev_mode(self):
        from odoo.addons.ow_mcp_server.protocol.access import (
            _effective_yolo_mode,
        )
        # odoo.conf has dev_mode on in the test harness; env var is already
        # set by setUp. Expect 'full' to pass through.
        self.cfg.yolo_mode = 'full'
        self.assertEqual(_effective_yolo_mode(self.cfg), 'full')

    def test_yolo_off_passes_without_env_var(self):
        from odoo.addons.ow_mcp_server.protocol.access import (
            _effective_yolo_mode,
        )
        self.cfg.yolo_mode = 'off'
        os.environ.pop('OW_MCP_ALLOW_YOLO', None)
        self.assertEqual(_effective_yolo_mode(self.cfg), 'off')
