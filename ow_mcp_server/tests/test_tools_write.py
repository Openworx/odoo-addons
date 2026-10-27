from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.exceptions import AccessError, UserError

from odoo.addons.ow_mcp_server.protocol.tools import (
    create_record, update_record, delete_record,
)


@tagged('post_install', '-at_install')
class TestWriteTools(TransactionCase):
    def setUp(self):
        super().setUp()
        partner = self.env.ref('base.model_res_partner').id
        self.env['ow.mcp.model.access'].sudo().search(
            [('model_id', '=', partner)]
        ).unlink()
        self.ma = self.env['ow.mcp.model.access'].create({
            'model_id': partner, 'allow_read': True,
        })
        self.p1 = self.env['res.partner'].create({'name': 'Alpha'})
        self.p2 = self.env['res.partner'].create({'name': 'Beta'})

    # ---- create ----

    def test_create_denied_without_flag(self):
        with self.assertRaises(AccessError):
            create_record(self.env, {
                'model': 'res.partner', 'values': {'name': 'X'},
            })

    def test_create_success(self):
        self.ma.allow_create = True
        out = create_record(self.env, {
            'model': 'res.partner', 'values': {'name': 'NewOne'},
        })
        self.assertTrue(out['id'])
        self.assertEqual(out['record']['name'], 'NewOne')

    def test_create_echo_ignores_vals_mutated_by_overrides(self):
        """The echo lists only the client's keys, even when a
        create() override mutates the dict (and the caller's dict is left
        untouched)."""
        self.ma.allow_create = True
        Partner = type(self.env['res.partner'])
        orig_create = Partner.create

        def mutating_create(model, vals_list):
            vals_list = vals_list if isinstance(vals_list, list) else [vals_list]
            for vals in vals_list:
                vals['comment'] = 'added by an override'
            return orig_create(model, vals_list)

        values = {'name': 'Echo'}
        with patch.object(Partner, 'create', mutating_create):
            out = create_record(self.env, {
                'model': 'res.partner', 'values': values,
            })
        self.assertEqual(values, {'name': 'Echo'})
        self.assertEqual(set(out['record']), {'id', 'name', 'display_name'})
        self.assertEqual(out['record']['name'], 'Echo')

    def test_create_respects_field_allowlist(self):
        self.ma.allow_create = True
        self.ma.allowed_fields_json = '["name"]'
        with self.assertRaises(AccessError):
            create_record(self.env, {
                'model': 'res.partner',
                'values': {'name': 'X', 'email': 'x@x.com'},
            })

    # ---- update ----

    def test_update_denied_without_flag(self):
        with self.assertRaises(AccessError):
            update_record(self.env, {
                'model': 'res.partner', 'id': self.p1.id,
                'values': {'name': 'Z'},
            })

    def test_update_single(self):
        self.ma.allow_write = True
        out = update_record(self.env, {
            'model': 'res.partner', 'id': self.p1.id,
            'values': {'name': 'AA'},
        })
        self.p1.invalidate_recordset(['name'])
        self.assertEqual(self.p1.name, 'AA')
        self.assertEqual(out['updated'], [self.p1.id])

    def test_update_many(self):
        self.ma.allow_write = True
        out = update_record(self.env, {
            'model': 'res.partner',
            'ids': [self.p1.id, self.p2.id],
            'values': {'comment': 'hi'},
        })
        self.assertEqual(set(out['updated']), {self.p1.id, self.p2.id})

    # ---- delete ----

    def test_delete_denied_without_flag(self):
        with self.assertRaises(AccessError):
            delete_record(self.env, {
                'model': 'res.partner', 'id': self.p1.id,
            })

    def test_delete_success(self):
        self.ma.allow_delete = True
        pid = self.p1.id
        out = delete_record(self.env, {'model': 'res.partner', 'id': pid})
        self.assertEqual(out['deleted'], [pid])
        self.assertFalse(self.env['res.partner'].browse(pid).exists())

    # ---- C2: empty-id calls must fail loudly ----

    def test_update_missing_id_raises(self):
        self.ma.allow_write = True
        with self.assertRaises(UserError):
            update_record(self.env, {
                'model': 'res.partner', 'values': {'name': 'Z'},
            })

    def test_delete_missing_id_raises(self):
        self.ma.allow_delete = True
        with self.assertRaises(UserError):
            delete_record(self.env, {'model': 'res.partner'})
