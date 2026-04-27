from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.ow_mcp_server.protocol.tools import list_models


@tagged('post_install', '-at_install')
class TestListModels(TransactionCase):
    def setUp(self):
        super().setUp()
        self.env['ow.mcp.config'].get_singleton().yolo_mode = 'off'
        partner = self.env.ref('base.model_res_partner').id
        self.env['ow.mcp.model.access'].sudo().search(
            [('model_id', '=', partner)]
        ).unlink()
        self.env['ow.mcp.model.access'].create({
            'model_id': partner,
            'allow_read': True,
            'allow_create': True,
        })

    def test_returns_only_exposed(self):
        result = list_models(self.env, {})
        names = {m['model'] for m in result['models']}
        self.assertIn('res.partner', names)
        self.assertNotIn('ir.config_parameter', names)

    def test_flags_reflect_config(self):
        partner = next(
            m for m in list_models(self.env, {})['models']
            if m['model'] == 'res.partner'
        )
        self.assertTrue(partner['can_read'])
        self.assertTrue(partner['can_create'])
        self.assertFalse(partner['can_write'])
        self.assertFalse(partner['can_delete'])

    def test_inactive_rows_filtered(self):
        self.env['ow.mcp.model.access'].search([]).write({'active': False})
        self.assertEqual(list_models(self.env, {})['models'], [])
