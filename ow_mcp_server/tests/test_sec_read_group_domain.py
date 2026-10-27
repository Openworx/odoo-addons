"""The per-model field
allow-list, the blocklist and max_records must also hold for
  - read_group aggregates (`alias:agg(field)`), dotted groupby and the
    number of groups returned;
  - `any` / `not any` sub-domains and relational leaves searched by name;
  - the `order` clause of search_records.
"""
import json

from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.ow_mcp_server.protocol.tools import (
    read_group, search_count, search_records,
)


@tagged('post_install', '-at_install')
class TestAllowListHoles(TransactionCase):
    def setUp(self):
        super().setUp()
        MA = self.env['ow.mcp.model.access'].sudo()
        partner = self.env.ref('base.model_res_partner').id
        MA.search([('model_id', '=', partner)]).unlink()
        self.ma = MA.create({
            'model_id': partner, 'allow_read': True,
            'allowed_fields_json': json.dumps(
                ['id', 'name', 'is_company', 'parent_id']),
        })
        self.parent = self.env['res.partner'].create(
            {'name': 'RG Parent', 'email': 'secret@x.invalid'})
        for i in range(4):
            self.env['res.partner'].create(
                {'name': f'RGLIM {i}', 'parent_id': self.parent.id})

    def _rg(self, **kw):
        return read_group(self.env, dict({'model': 'res.partner'}, **kw))

    # ---- read_group (M5 / H1) ----
    def test_alias_aggregate_on_forbidden_field_rejected(self):
        with self.assertRaises(AccessError):
            self._rg(groupby=['is_company'], fields=['name:max(email)'])

    def test_array_agg_on_forbidden_field_rejected(self):
        with self.assertRaises(AccessError):
            self._rg(groupby=['id'], fields=['id:array_agg(email)'])

    def test_allowed_alias_aggregate_still_works(self):
        out = self._rg(groupby=['is_company'], fields=['n:count(id)'],
                       domain=[['name', '=like', 'RGLIM%']])
        self.assertEqual(sum(g['count'] for g in out['groups']), 4)

    def test_dotted_groupby_into_blocked_model_rejected(self):
        self.ma.allowed_fields_json = False  # no allow-list on the root
        with self.assertRaises(AccessError):
            self._rg(groupby=['user_id.login'])

    def test_groups_capped_by_max_records(self):
        self.ma.max_records = 2
        out = self._rg(groupby=['id'], domain=[['name', '=like', 'RGLIM%']])
        self.assertLessEqual(len(out['groups']), 2)

    # ---- any / not any and name-search leaves (m1 / H2) ----
    def test_any_into_blocked_model_rejected(self):
        self.ma.allowed_fields_json = False
        for op in ('any', 'not any'):
            with self.subTest(op=op), self.assertRaises(AccessError):
                search_count(self.env, {'model': 'res.partner', 'domain': [
                    ['user_ids', op, [['login', '!=', False]]]]})

    def test_any_subdomain_respects_comodel_allow_list(self):
        with self.assertRaises(AccessError):
            search_count(self.env, {'model': 'res.partner', 'domain': [
                ['parent_id', 'any', [['email', '=', 'secret@x.invalid']]]]})

    def test_any_subdomain_on_allowed_field_passes(self):
        out = search_count(self.env, {'model': 'res.partner', 'domain': [
            ['parent_id', 'any', [['name', '=', 'RG Parent']]]]})
        self.assertEqual(out['count'], 4)

    def test_name_search_leaf_into_blocked_model_rejected(self):
        self.ma.allowed_fields_json = False
        with self.assertRaises(AccessError):
            search_count(self.env, {'model': 'res.partner', 'domain': [
                ['user_id', 'ilike', 'adm']]})

    # ---- order (H3) ----
    def test_order_on_forbidden_field_rejected(self):
        with self.assertRaises(AccessError):
            search_records(self.env, {'model': 'res.partner',
                                      'fields': ['name'], 'order': 'email desc'})

    def test_order_on_allowed_fields_passes(self):
        out = search_records(self.env, {
            'model': 'res.partner', 'fields': ['name'],
            'domain': [['name', '=like', 'RGLIM%']],
            'order': 'name desc, id'})
        self.assertEqual(out['records'][0]['name'], 'RGLIM 3')

    def test_default_and_id_order_need_no_allow_list_entry(self):
        # `id` reveals nothing; the default order must work with any list.
        self.ma.allowed_fields_json = json.dumps(['name'])
        out = search_records(self.env, {'model': 'res.partner',
                                        'fields': ['name'],
                                        'domain': [['name', '=like', 'RGLIM%']]})
        self.assertEqual(len(out['records']), 4)
        out = search_records(self.env, {'model': 'res.partner',
                                        'fields': ['name'], 'order': 'id desc',
                                        'domain': [['name', '=like', 'RGLIM%']]})
        self.assertEqual(out['records'][0]['name'], 'RGLIM 3')

    def test_malformed_order_rejected(self):
        with self.assertRaises(AccessError):
            search_records(self.env, {'model': 'res.partner',
                                      'fields': ['name'],
                                      'order': 'name; select 1'})
