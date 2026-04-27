from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.exceptions import AccessError, UserError

from odoo.addons.ow_mcp_server.protocol.tools import get_record


@tagged('post_install', '-at_install')
class TestGetRecord(TransactionCase):
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

    def test_get_single(self):
        out = get_record(self.env, {
            'model': 'res.partner', 'id': self.p1.id, 'fields': ['name'],
        })
        self.assertEqual(out['records'][0]['name'], 'Alpha')
        self.assertEqual(out['missing'], [])

    def test_get_many(self):
        out = get_record(self.env, {
            'model': 'res.partner',
            'ids': [self.p1.id, self.p2.id],
            'fields': ['name'],
        })
        self.assertEqual({r['name'] for r in out['records']}, {'Alpha', 'Beta'})

    def test_not_found_reports_missing(self):
        out = get_record(self.env, {'model': 'res.partner', 'ids': [999_999_999]})
        self.assertEqual(out['missing'], [999_999_999])
        self.assertEqual(out['records'], [])

    def test_field_allowlist_restricts_output(self):
        ma = self.env['ow.mcp.model.access'].search(
            [('model_name', '=', 'res.partner')]
        )
        ma.allowed_fields_json = '["name"]'
        out = get_record(self.env, {
            'model': 'res.partner',
            'id': self.p1.id,
            'fields': ['name', 'email'],
        })
        # 'email' must be filtered out
        self.assertNotIn('email', out['records'][0])
        self.assertIn('name', out['records'][0])

    def test_field_allowlist_rejects_when_no_requested_field_allowed(self):
        # C1 regression: allow-list ['name'] + request ['email'] must NOT
        # silently fall back to defaults ('display_name' etc).
        ma = self.env['ow.mcp.model.access'].search(
            [('model_name', '=', 'res.partner')]
        )
        ma.allowed_fields_json = '["name"]'
        with self.assertRaises(AccessError):
            get_record(self.env, {
                'model': 'res.partner',
                'id': self.p1.id,
                'fields': ['email'],
            })

    def test_field_allowlist_is_used_as_default_when_no_fields_requested(self):
        # C1 regression: when client sends no `fields`, the response must be
        # restricted to the allow-list, not default ('id', 'display_name').
        ma = self.env['ow.mcp.model.access'].search(
            [('model_name', '=', 'res.partner')]
        )
        ma.allowed_fields_json = '["name"]'
        out = get_record(self.env, {
            'model': 'res.partner', 'id': self.p1.id,
        })
        self.assertIn('name', out['records'][0])
        self.assertNotIn('display_name', out['records'][0])

    def test_missing_id_raises(self):
        # C2 regression: empty call must fail loudly, not silently return [].
        with self.assertRaises(UserError):
            get_record(self.env, {'model': 'res.partner'})
