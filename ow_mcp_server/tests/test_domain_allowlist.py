"""M6: search domains must honor the MCP field allow-list. Without this
check a caller could probe forbidden columns (e.g. `password`) via a
count oracle even though the allow-list blocks them from appearing in
the `fields` list.
"""
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.exceptions import AccessError

from odoo.addons.ow_mcp_server.protocol.access import (
    assert_domain_fields_allowed,
)
from odoo.addons.ow_mcp_server.protocol.tools import (
    search_records, search_count, read_group,
)


@tagged('post_install', '-at_install')
class TestAssertDomainFieldsAllowed(TransactionCase):
    def setUp(self):
        super().setUp()
        partner_id = self.env.ref('base.model_res_partner').id
        self.env['ow.mcp.model.access'].sudo().search(
            [('model_id', '=', partner_id)]
        ).unlink()
        self.ma = self.env['ow.mcp.model.access'].create({
            'model_id': partner_id,
            'allow_read': True,
            'allowed_fields_json': '["name", "email"]',
        })

    def test_empty_domain_is_fine(self):
        assert_domain_fields_allowed(self.env, 'res.partner', [])

    def test_no_allowlist_disables_check(self):
        self.ma.allowed_fields_json = False
        assert_domain_fields_allowed(self.env, 'res.partner', [
            ['password', '!=', False],
        ])

    def test_leaf_on_allowed_field_passes(self):
        assert_domain_fields_allowed(self.env, 'res.partner', [
            ['name', 'ilike', 'acme'],
        ])

    def test_leaf_on_forbidden_field_raises(self):
        with self.assertRaises(AccessError):
            assert_domain_fields_allowed(self.env, 'res.partner', [
                ['password', '!=', False],
            ])

    def test_dotted_path_checks_root_only(self):
        # `name.x` → root is `name`, allowed. The ORM will reject the bogus
        # traversal later; the MCP layer only guards the root.
        assert_domain_fields_allowed(self.env, 'res.partner', [
            ['name', 'ilike', 'x'],
        ])
        # `user_ids.password` → root `user_ids` not in allow-list.
        with self.assertRaises(AccessError):
            assert_domain_fields_allowed(self.env, 'res.partner', [
                ['user_ids.password', '!=', False],
            ])

    def test_connectors_allowed(self):
        assert_domain_fields_allowed(self.env, 'res.partner', [
            '|',
            ['name', 'ilike', 'a'],
            ['email', '=', 'b@b'],
        ])

    def test_malformed_atom_rejected(self):
        with self.assertRaises(AccessError):
            assert_domain_fields_allowed(self.env, 'res.partner', [
                'bogus_string_not_a_connector',
            ])
        with self.assertRaises(AccessError):
            assert_domain_fields_allowed(self.env, 'res.partner', [
                ['only', 'two'],
            ])

    def test_no_ma_row_noop(self):
        assert_domain_fields_allowed(self.env, 'res.company', [
            ['name', '=', 'x'],
        ])


@tagged('post_install', '-at_install')
class TestDomainGateOnTools(TransactionCase):
    def setUp(self):
        super().setUp()
        partner_id = self.env.ref('base.model_res_partner').id
        self.env['ow.mcp.model.access'].sudo().search(
            [('model_id', '=', partner_id)]
        ).unlink()
        self.ma = self.env['ow.mcp.model.access'].create({
            'model_id': partner_id,
            'allow_read': True,
            'allowed_fields_json': '["name"]',
        })
        self.env['res.partner'].create({'name': 'Gate'})

    def test_search_records_rejects_forbidden_domain_field(self):
        with self.assertRaises(AccessError):
            search_records(self.env, {
                'model': 'res.partner',
                'domain': [['email', '=', 'x@x.com']],
            })

    def test_search_count_rejects_forbidden_domain_field(self):
        with self.assertRaises(AccessError):
            search_count(self.env, {
                'model': 'res.partner',
                'domain': [['email', '=', 'x@x.com']],
            })

    def test_read_group_rejects_forbidden_groupby(self):
        with self.assertRaises(AccessError):
            read_group(self.env, {
                'model': 'res.partner',
                'groupby': ['email'],
            })

    def test_read_group_accepts_allowed_groupby(self):
        # `name` is on the allow-list, so the call must pass gating and
        # return a list of groups (possibly one).
        out = read_group(self.env, {
            'model': 'res.partner',
            'groupby': ['name'],
        })
        self.assertIn('groups', out)
