"""C1: `create_record` / `update_record` must refuse x2many command tuples
whose opcode would cause writes on a *related* model (opcodes 0, 1, 2).
Link-only opcodes (3, 4, 5, 6) must still pass so legitimate reassignment
of related records remains possible via MCP.
"""
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.exceptions import AccessError

from odoo.addons.ow_mcp_server.protocol.access import assert_no_nested_writes
from odoo.addons.ow_mcp_server.protocol.tools import (
    create_record, update_record,
)


@tagged('post_install', '-at_install')
class TestAssertNoNestedWrites(TransactionCase):
    """Unit coverage for the helper — no MA row required."""

    def test_plain_scalars_pass(self):
        assert_no_nested_writes(self.env, 'res.partner', {
            'name': 'X', 'email': 'x@x.com', 'active': True,
        })

    def test_non_x2many_list_ignored(self):
        # A scalar-list value on a non-x2many field must not trigger the
        # nested-write guard (there's nothing to validate).
        assert_no_nested_writes(self.env, 'res.partner', {
            'name': 'X', 'ref': '123',
        })

    def test_create_command_rejected(self):
        with self.assertRaises(AccessError):
            assert_no_nested_writes(self.env, 'res.partner', {
                'child_ids': [(0, 0, {'name': 'sub'})],
            })

    def test_update_command_rejected(self):
        with self.assertRaises(AccessError):
            assert_no_nested_writes(self.env, 'res.partner', {
                'child_ids': [(1, 1, {'name': 'edited'})],
            })

    def test_delete_command_rejected(self):
        with self.assertRaises(AccessError):
            assert_no_nested_writes(self.env, 'res.partner', {
                'child_ids': [(2, 1)],
            })

    def test_link_only_commands_accepted(self):
        # Opcodes 3 (forget), 4 (link), 5 (clear), 6 (replace) touch only
        # the relation, never mutate the sub-model's fields.
        assert_no_nested_writes(self.env, 'res.partner', {
            'category_id': [(6, 0, [])],
        })
        assert_no_nested_writes(self.env, 'res.partner', {
            'category_id': [(4, 1)],
        })
        assert_no_nested_writes(self.env, 'res.partner', {
            'category_id': [(5,)],
        })
        assert_no_nested_writes(self.env, 'res.partner', {
            'category_id': [(3, 1)],
        })

    def test_unknown_field_ignored(self):
        # Typos on the values dict should not trip this guard; the ORM will
        # raise its own error when it processes the write.
        assert_no_nested_writes(self.env, 'res.partner', {
            'this_field_does_not_exist': [(0, 0, {'x': 1})],
        })


@tagged('post_install', '-at_install')
class TestNestedWritesViaTools(TransactionCase):
    """End-to-end: create_record / update_record refuse nested writes."""

    def setUp(self):
        super().setUp()
        partner_id = self.env.ref('base.model_res_partner').id
        self.env['ow.mcp.model.access'].sudo().search(
            [('model_id', '=', partner_id)]
        ).unlink()
        self.ma = self.env['ow.mcp.model.access'].create({
            'model_id': partner_id,
            'allow_read': True,
            'allow_write': True,
            'allow_create': True,
        })
        self.partner = self.env['res.partner'].create({'name': 'Parent'})

    def test_create_rejects_nested_create(self):
        with self.assertRaises(AccessError):
            create_record(self.env, {
                'model': 'res.partner',
                'values': {
                    'name': 'P',
                    'child_ids': [(0, 0, {'name': 'child'})],
                },
            })

    def test_update_rejects_nested_create(self):
        with self.assertRaises(AccessError):
            update_record(self.env, {
                'model': 'res.partner',
                'id': self.partner.id,
                'values': {'child_ids': [(0, 0, {'name': 'child'})]},
            })

    def test_update_allows_link_command(self):
        # Replace tags wholesale — a link-only op; must be permitted.
        tag = self.env['res.partner.category'].create({'name': 'vip'})
        update_record(self.env, {
            'model': 'res.partner',
            'id': self.partner.id,
            'values': {'category_id': [(6, 0, [tag.id])]},
        })
        self.partner.invalidate_recordset(['category_id'])
        self.assertIn(tag, self.partner.category_id)
