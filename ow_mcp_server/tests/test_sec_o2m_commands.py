"""On a one2many, the "link-only" commands
are not link-only (Odoo 19 `One2many.write_real`): UNLINK(3), CLEAR(5)
and SET(6) delete the lines when the inverse is ondelete='cascade' (else
they write the inverse), and LINK(4) rewrites the inverse of the line.
They must go through the comodel's MCP row (allow_write / allow_delete).
"""
from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.ow_mcp_server.protocol.tools import update_record


@tagged('post_install', '-at_install')
class TestOne2manyCommands(TransactionCase):
    def setUp(self):
        super().setUp()
        MA = self.env['ow.mcp.model.access'].sudo()
        self.m_partner = self.env.ref('base.model_res_partner').id
        self.m_bank = self.env.ref('base.model_res_partner_bank').id
        MA.search([('model_id', 'in', [self.m_partner, self.m_bank])]).unlink()
        MA.create({'model_id': self.m_partner, 'allow_read': True,
                   'allow_write': True})
        self.p1 = self.env['res.partner'].create({'name': 'O2M one'})
        self.p2 = self.env['res.partner'].create({'name': 'O2M two'})
        Bank = self.env['res.partner.bank']
        self.b1 = Bank.create({'account_number': 'O2M-1', 'partner_id': self.p1.id})
        self.b2 = Bank.create({'account_number': 'O2M-2', 'partner_id': self.p2.id})

    def _write(self, partner, cmds):
        return update_record(self.env, {
            'model': 'res.partner', 'id': partner.id,
            'values': {'bank_ids': cmds}})

    def test_clear_without_comodel_row_rejected(self):
        with self.assertRaises(AccessError):
            self._write(self.p1, [[5]])
        self.assertTrue(self.b1.exists())

    def test_unlink_and_set_rejected_without_delete_right(self):
        self.env['ow.mcp.model.access'].sudo().create({
            'model_id': self.m_bank, 'allow_read': True, 'allow_write': True})
        for cmds in ([[3, self.b1.id]], [[6, 0, []]], [[5]]):
            with self.subTest(cmds=cmds), self.assertRaises(AccessError):
                self._write(self.p1, cmds)
        self.assertTrue(self.b1.exists())

    def test_link_steals_line_only_with_comodel_write(self):
        with self.assertRaises(AccessError):
            self._write(self.p1, [[4, self.b2.id]])
        self.assertEqual(self.b2.partner_id, self.p2)

    def test_allowed_when_comodel_row_grants_it(self):
        self.env['ow.mcp.model.access'].sudo().create({
            'model_id': self.m_bank, 'allow_read': True, 'allow_write': True,
            'allow_delete': True})
        self._write(self.p1, [[4, self.b2.id]])
        self.assertEqual(self.b2.partner_id, self.p1)
        self._write(self.p1, [[5]])
        # CLEAR deletes the cascade lines (res.partner.bank archives them).
        self.assertFalse(self.b1.exists().filtered('active'))

    def test_many2many_link_commands_unchanged(self):
        tag = self.env['res.partner.category'].create({'name': 'o2m-vip'})
        update_record(self.env, {
            'model': 'res.partner', 'id': self.p1.id,
            'values': {'category_id': [[6, 0, [tag.id]]]}})
        self.assertIn(tag, self.p1.category_id)
