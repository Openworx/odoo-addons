from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.ow_mcp_server.protocol.formatter import serialize_records


@tagged('post_install', '-at_install')
class TestFormatter(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Partner = self.env['res.partner']

    def test_many2one_is_object(self):
        country = self.env.ref('base.be')
        partner = self.Partner.create({'name': 'ACME', 'country_id': country.id})
        out = serialize_records(partner, ['name', 'country_id'], 2000, 20)
        self.assertEqual(out[0]['country_id'],
                         {'id': country.id, 'name': country.name})

    def test_many2one_null(self):
        partner = self.Partner.create({'name': 'NoCountry'})
        out = serialize_records(partner, ['country_id'], 2000, 20)
        self.assertIsNone(out[0]['country_id'])

    def test_datetime_isoformat_with_z(self):
        partner = self.Partner.create({'name': 'X'})
        out = serialize_records(partner, ['create_date'], 2000, 20)
        self.assertTrue(out[0]['create_date'].endswith('Z'))
        self.assertIn('T', out[0]['create_date'])

    def test_text_truncation(self):
        partner = self.Partner.create({'name': 'X', 'comment': 'A' * 5000})
        out = serialize_records(partner, ['comment'], 100, 20)
        self.assertLessEqual(len(out[0]['comment']), 101)
        self.assertTrue(out[0]['comment'].endswith('…'))

    def test_binary_omitted_when_not_requested(self):
        attach = self.env['ir.attachment'].create({'name': 'x', 'datas': b'aGVsbG8='})  # 'hello' b64
        out = serialize_records(attach, ['name'], 2000, 20)
        self.assertNotIn('datas', out[0])

    def test_binary_returns_metadata_when_requested(self):
        attach = self.env['ir.attachment'].create({'name': 'x', 'datas': b'aGVsbG8='})
        out = serialize_records(attach, ['name', 'datas'], 2000, 20)
        self.assertEqual(out[0]['datas'], {'size': 5, 'truncated': True})

    def test_x2many_preview_truncated(self):
        parent = self.Partner.create({'name': 'Parent'})
        for i in range(5):
            self.Partner.create({'name': f'Child{i}', 'parent_id': parent.id})
        out = serialize_records(parent, ['child_ids'], 2000, 3)
        val = out[0]['child_ids']
        self.assertTrue(val['truncated'])
        self.assertEqual(val['total'], 5)
        self.assertEqual(len(val['preview']), 3)

    def test_id_always_included(self):
        partner = self.Partner.create({'name': 'X'})
        out = serialize_records(partner, ['name'], 2000, 20)
        self.assertEqual(out[0]['id'], partner.id)
