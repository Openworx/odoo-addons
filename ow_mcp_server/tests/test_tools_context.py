"""M1, M2: `list_modules` is admin-only; `get_user_context` hides the
group topology from non-admin MCP users.
"""
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.exceptions import AccessError

from odoo.addons.ow_mcp_server.protocol.tools import (
    list_modules, get_user_context,
)


@tagged('post_install', '-at_install')
class TestListModulesGate(TransactionCase):
    def setUp(self):
        super().setUp()
        self.mcp_user_group = self.env.ref('ow_mcp_server.group_mcp_user')
        self.mcp_admin_group = self.env.ref('ow_mcp_server.group_mcp_admin')

    def test_non_admin_is_refused(self):
        user = self.env['res.users'].create({
            'login': 'mcp_nonadmin',
            'name': 'NoAdmin',
            'groups_id': [
                (6, 0, [
                    self.mcp_user_group.id,
                    self.env.ref('base.group_user').id,
                ]),
            ],
        })
        env = self.env(user=user)
        with self.assertRaises(AccessError):
            list_modules(env, {})

    def test_admin_sees_module_list(self):
        user = self.env['res.users'].create({
            'login': 'mcp_admin_call',
            'name': 'IsAdmin',
            'groups_id': [
                (6, 0, [
                    self.mcp_admin_group.id,
                    self.env.ref('base.group_user').id,
                ]),
            ],
        })
        env = self.env(user=user)
        out = list_modules(env, {})
        self.assertIn('modules', out)
        self.assertGreater(out['count'], 0)
        # `base` is always installed in a running Odoo — sanity check the
        # shape of each row.
        base_row = next((m for m in out['modules'] if m['name'] == 'base'), None)
        self.assertIsNotNone(base_row)
        self.assertIn('label', base_row)
        self.assertIn('version', base_row)


@tagged('post_install', '-at_install')
class TestUserContextGate(TransactionCase):
    def setUp(self):
        super().setUp()
        self.mcp_user_group = self.env.ref('ow_mcp_server.group_mcp_user')
        self.mcp_admin_group = self.env.ref('ow_mcp_server.group_mcp_admin')

    def test_non_admin_gets_flag_not_group_list(self):
        user = self.env['res.users'].create({
            'login': 'ctx_nonadmin',
            'name': 'NoAdmin',
            'groups_id': [
                (6, 0, [
                    self.mcp_user_group.id,
                    self.env.ref('base.group_user').id,
                ]),
            ],
        })
        env = self.env(user=user)
        out = get_user_context(env, {})
        self.assertNotIn('groups', out)
        self.assertEqual(out.get('is_mcp_admin'), False)
        # Core identity fields still present.
        self.assertEqual(out['login'], 'ctx_nonadmin')
        self.assertIn('company', out)

    def test_admin_gets_full_group_list(self):
        user = self.env['res.users'].create({
            'login': 'ctx_admin',
            'name': 'IsAdmin',
            'groups_id': [
                (6, 0, [
                    self.mcp_admin_group.id,
                    self.env.ref('base.group_user').id,
                ]),
            ],
        })
        env = self.env(user=user)
        out = get_user_context(env, {})
        self.assertIn('groups', out)
        self.assertIsInstance(out['groups'], list)
        self.assertTrue(any('MCP' in g for g in out['groups']))
