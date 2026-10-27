"""Audit trail hardening — wider secret
redaction (also inside domains), rows MCP admins cannot delete, and the
audit log on by default.
"""
import json

from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.ow_mcp_server.controllers import mcp_controller as ctrl


@tagged('post_install', '-at_install')
class TestAuditHardening(TransactionCase):
    def test_redacts_pass_key_credential(self):
        out = json.dumps(ctrl._redact({'values': {
            'smtp_pass': 'v1', 'openai_key': 'v2', 'credential': 'v3',
            'bearer_token': 'v4', 'client_secret': 'v5', 'name': 'keep'}}))
        for v in ('v1', 'v2', 'v3', 'v4', 'v5'):
            self.assertNotIn(v, out)
        self.assertIn('keep', out)

    def test_redacts_sensitive_domain_values(self):
        out = json.dumps(ctrl._redact({'domain': [
            ['password', '=', 'hunter2'], '|',
            ['name', '=', 'visible']]}))
        self.assertNotIn('hunter2', out)
        self.assertIn('visible', out)

    def test_mcp_admin_cannot_delete_audit_rows(self):
        admin = self.env['res.users'].create({
            'login': 'mcpaud', 'name': 'MCP Aud', 'password': 'mcpaud',
            'group_ids': [
                (4, self.env.ref('ow_mcp_server.group_mcp_admin').id),
                (4, self.env.ref('base.group_user').id)],
        })
        row = self.env['ow.mcp.audit.log'].sudo().create({'method': 'x'})
        with self.assertRaises(AccessError):
            row.with_user(admin).unlink()

    def test_audit_on_by_default(self):
        dflt = self.env['ow.mcp.config'].default_get(['audit_log'])
        self.assertTrue(dflt.get('audit_log'))
