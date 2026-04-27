from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.exceptions import ValidationError


@tagged('post_install', '-at_install')
class TestMcpModelAccess(TransactionCase):
    def setUp(self):
        super().setUp()
        partner = self.env.ref('base.model_res_partner').id
        self.env['ow.mcp.model.access'].sudo().search(
            [('model_id', '=', partner)]
        ).unlink()

    def test_create_minimal(self):
        ma = self.env['ow.mcp.model.access'].create({
            'model_id': self.env.ref('base.model_res_partner').id,
            'allow_read': True,
        })
        self.assertFalse(ma.allow_write)
        self.assertEqual(ma.model_name, 'res.partner')

    def test_unique_per_model(self):
        partner = self.env.ref('base.model_res_partner').id
        self.env['ow.mcp.model.access'].create({'model_id': partner, 'allow_read': True})
        with self.assertRaises(Exception):
            self.env['ow.mcp.model.access'].create({'model_id': partner, 'allow_read': True})

    def test_allowed_fields_parse_valid(self):
        ma = self.env['ow.mcp.model.access'].create({
            'model_id': self.env.ref('base.model_res_partner').id,
            'allowed_fields_json': '["name", "email"]',
        })
        self.assertEqual(ma.allowed_fields(), ['name', 'email'])

    def test_allowed_fields_empty_means_all(self):
        ma = self.env['ow.mcp.model.access'].create({
            'model_id': self.env.ref('base.model_res_partner').id,
        })
        self.assertIsNone(ma.allowed_fields())

    def test_allowed_fields_parse_invalid(self):
        with self.assertRaises(ValidationError):
            self.env['ow.mcp.model.access'].create({
                'model_id': self.env.ref('base.model_res_partner').id,
                'allowed_fields_json': 'not-json',
            })

    def test_allowed_fields_must_be_array_of_strings(self):
        with self.assertRaises(ValidationError):
            self.env['ow.mcp.model.access'].create({
                'model_id': self.env.ref('base.model_res_partner').id,
                'allowed_fields_json': '{"not": "a list"}',
            })
