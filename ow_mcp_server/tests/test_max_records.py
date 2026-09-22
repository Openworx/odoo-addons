"""M3: `ow.mcp.model.access.max_records` must narrow the global cap set
on `ow.mcp.config.max_limit`. max_records=0 falls back to the global cap.
"""
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.ow_mcp_server.protocol.access import per_model_limit
from odoo.addons.ow_mcp_server.protocol.tools import search_records


@tagged('post_install', '-at_install')
class TestPerModelLimitHelper(TransactionCase):
    def setUp(self):
        super().setUp()
        partner_id = self.env.ref('base.model_res_partner').id
        self.env['ow.mcp.model.access'].sudo().search(
            [('model_id', '=', partner_id)]
        ).unlink()
        self.ma = self.env['ow.mcp.model.access'].create({
            'model_id': partner_id, 'allow_read': True,
        })

    def test_zero_inherits_global(self):
        self.ma.max_records = 0
        self.assertEqual(per_model_limit(self.env, 'res.partner', 500), 500)

    def test_per_model_narrows_global(self):
        self.ma.max_records = 10
        self.assertEqual(per_model_limit(self.env, 'res.partner', 500), 10)

    def test_global_narrows_per_model(self):
        self.ma.max_records = 1000
        self.assertEqual(per_model_limit(self.env, 'res.partner', 50), 50)

    def test_no_ma_row_returns_global(self):
        # Model without an access row (YOLO path) — helper returns the
        # global cap unchanged.
        self.assertEqual(per_model_limit(self.env, 'res.company', 500), 500)


@tagged('post_install', '-at_install')
class TestSearchRecordsHonorsMaxRecords(TransactionCase):
    def setUp(self):
        super().setUp()
        partner_id = self.env.ref('base.model_res_partner').id
        self.env['ow.mcp.model.access'].sudo().search(
            [('model_id', '=', partner_id)]
        ).unlink()
        self.ma = self.env['ow.mcp.model.access'].create({
            'model_id': partner_id, 'allow_read': True,
        })
        # Ensure at least 6 partners exist so a cap of 3 is observable.
        for i in range(6):
            self.env['res.partner'].create({'name': f'P{i}'})

    def test_requested_limit_capped_by_max_records(self):
        self.ma.max_records = 3
        out = search_records(self.env, {
            'model': 'res.partner', 'limit': 100,
        })
        self.assertLessEqual(out['limit'], 3)
        self.assertLessEqual(len(out['records']), 3)

    def test_max_records_zero_falls_back_to_global(self):
        self.ma.max_records = 0
        self.env['ow.mcp.config'].get_singleton().max_limit = 2
        out = search_records(self.env, {
            'model': 'res.partner', 'limit': 100,
        })
        self.assertEqual(out['limit'], 2)
