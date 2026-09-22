from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.exceptions import AccessError

from odoo.addons.ow_mcp_server.protocol.tools import search_records


@tagged('post_install', '-at_install')
class TestSearchRecords(TransactionCase):
    def setUp(self):
        super().setUp()
        partner = self.env.ref('base.model_res_partner').id
        self.env['ow.mcp.model.access'].sudo().search(
            [('model_id', '=', partner)]
        ).unlink()
        self.env['ow.mcp.model.access'].create({
            'model_id': partner, 'allow_read': True,
        })
        self.p1 = self.env['res.partner'].create({'name': 'Alpha'})
        self.p2 = self.env['res.partner'].create({'name': 'Beta'})

    def test_domain_and_pagination(self):
        out = search_records(self.env, {
            'model': 'res.partner',
            'domain': [['name', 'in', ['Alpha', 'Beta']]],
            'fields': ['name'],
            'limit': 1,
            'offset': 0,
            'order': 'name asc',
        })
        self.assertEqual(out['total'], 2)
        self.assertEqual(len(out['records']), 1)
        self.assertTrue(out['has_more'])
        self.assertEqual(out['next_offset'], 1)
        self.assertEqual(out['records'][0]['name'], 'Alpha')

    def test_second_page_no_more(self):
        out = search_records(self.env, {
            'model': 'res.partner',
            'domain': [['name', 'in', ['Alpha', 'Beta']]],
            'limit': 1,
            'offset': 1,
            'order': 'name asc',
        })
        self.assertFalse(out['has_more'])
        self.assertIsNone(out['next_offset'])

    def test_limit_capped_to_max(self):
        self.env['ow.mcp.config'].get_singleton().max_limit = 50
        out = search_records(self.env, {'model': 'res.partner', 'limit': 9999})
        self.assertLessEqual(out['limit'], 50)

    def test_unexposed_model_denied(self):
        with self.assertRaises(AccessError):
            search_records(self.env, {'model': 'res.company'})

    def test_default_fields(self):
        out = search_records(self.env, {
            'model': 'res.partner',
            'domain': [['id', '=', self.p1.id]],
        })
        row = out['records'][0]
        self.assertIn('id', row)
        self.assertIn('display_name', row)
