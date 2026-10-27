"""M4 cache served before permission checks,
m6 unbounded ids / cache entries, i1 db name disclosure, i2 schema leak
with an explicit deny-all allow-list.
"""
import json
from datetime import datetime, timedelta

from odoo.exceptions import UserError
from odoo.tests import HttpCase, tagged
from odoo.tests.common import TransactionCase

from odoo.addons.ow_mcp_server.protocol import authfail, cache, dispatcher
from odoo.addons.ow_mcp_server.protocol.tools import (
    get_model_schema, get_record,
)


def _call(env, name, args):
    out = dispatcher.dispatch(env, {
        'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': name, 'arguments': args}})
    return out['result']


@tagged('post_install', '-at_install')
class TestCacheAfterChecks(TransactionCase):
    def setUp(self):
        super().setUp()
        cache._store.clear()
        self.addCleanup(cache._store.clear)
        cfg = self.env['ow.mcp.config'].sudo().get_singleton()
        cfg.write({'cache_enabled': True, 'cache_ttl_seconds': 300})
        partner = self.env.ref('base.model_res_partner').id
        MA = self.env['ow.mcp.model.access'].sudo()
        MA.search([('model_id', '=', partner)]).unlink()
        self.ma = MA.create({
            'model_id': partner, 'allow_read': True,
            'allowed_fields_json': json.dumps(['id', 'name', 'email'])})
        self.env['res.partner'].create(
            {'name': 'CACHE P', 'email': 'cache@x.invalid'})

    def test_revoked_model_not_served_from_cache(self):
        args = {'model': 'res.partner'}
        self.assertFalse(_call(self.env, 'search_count', args).get('isError'))
        self.ma.allow_read = False
        self.assertTrue(_call(self.env, 'search_count', args).get('isError'))

    def test_allow_list_change_not_served_from_cache(self):
        args = {'model': 'res.partner', 'domain': [['name', '=', 'CACHE P']]}
        rec = _call(self.env, 'search_records', args)['structuredContent']
        self.assertIn('email', rec['records'][0])
        self.ma.allowed_fields_json = json.dumps(['id', 'name'])
        rec = _call(self.env, 'search_records', args)['structuredContent']
        self.assertNotIn('email', rec['records'][0])

    def test_oversized_result_not_cached(self):
        args = {'model': 'res.partner', 'k': 1}
        cache.store(self.env, 'search_records', args,
                    {'blob': 'x' * (cache._MAX_ENTRY_BYTES + 1)})
        self.assertIsNone(cache.lookup(self.env, 'search_records', args))


@tagged('post_install', '-at_install')
class TestIdsCapAndSchema(TransactionCase):
    def setUp(self):
        super().setUp()
        partner = self.env.ref('base.model_res_partner').id
        MA = self.env['ow.mcp.model.access'].sudo()
        MA.search([('model_id', '=', partner)]).unlink()
        self.ma = MA.create({'model_id': partner, 'allow_read': True})
        self.env['ow.mcp.config'].sudo().get_singleton().max_limit = 3

    def test_too_many_ids_rejected(self):
        with self.assertRaises(UserError):
            get_record(self.env, {'model': 'res.partner',
                                  'ids': list(range(1, 11))})

    def test_schema_with_deny_all_allow_list_is_empty(self):
        self.ma.allowed_fields_json = '[]'
        out = get_model_schema(self.env, {'model': 'res.partner'})
        self.assertEqual(out['fields'], [])


@tagged('post_install', '-at_install')
class TestDbNameNotDisclosed(HttpCase):
    def test_db_mismatch_does_not_echo_db_name(self):
        authfail.reset()
        r = self.url_open('/mcp?db=some_other_db', data=json.dumps(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}).encode(),
            headers={'Content-Type': 'application/json'})
        self.assertEqual(r.status_code, 404)
        self.assertNotIn(self.env.cr.dbname, r.text)
