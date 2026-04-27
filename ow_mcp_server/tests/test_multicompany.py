"""Multicompany support: per-call company switch via tool argument.

Covers:
- omitted company_id keeps the user's default company
- company_id switches the env (and thus record visibility) for the call
- a company outside the user's allowed companies → tool-level isError
- get_user_context exposes allowed_companies and default_company_id
- cache key segments by company_id so two companies cannot share a result
- audit log captures the requested company_id
"""
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.ow_mcp_server.protocol import cache
from odoo.addons.ow_mcp_server.protocol.dispatcher import dispatch


def _struct(out):
    return out['result']['structuredContent']


def _is_error(out):
    return out['result'].get('isError') is True


@tagged('post_install', '-at_install')
class TestMulticompany(TransactionCase):
    def setUp(self):
        super().setUp()
        Company = self.env['res.company']
        self.company_a = self.env.ref('base.main_company')
        self.company_b = Company.create({'name': 'MCP Co B'})
        self.company_c = Company.create({'name': 'MCP Co C (no access)'})

        self.user_ab = self.env['res.users'].create({
            'login': 'mcp_multi',
            'name': 'Multi-Company User',
            'company_id': self.company_a.id,
            'company_ids': [(6, 0, [self.company_a.id, self.company_b.id])],
            'groups_id': [(4, self.env.ref('base.group_user').id),
                          (4, self.env.ref('ow_mcp_server.group_mcp_user').id)],
        })

        partner_model = self.env.ref('base.model_res_partner').id
        self.env['ow.mcp.model.access'].sudo().search(
            [('model_id', '=', partner_model)]
        ).unlink()
        self.env['ow.mcp.model.access'].create({
            'model_id': partner_model,
            'allow_read': True,
        })

        Partner = self.env['res.partner']
        self.partner_a = Partner.create({
            'name': 'Partner-Only-A',
            'company_id': self.company_a.id,
        })
        self.partner_b = Partner.create({
            'name': 'Partner-Only-B',
            'company_id': self.company_b.id,
        })

        # Disable cache for the visibility tests so we always hit the live
        # path; cache isolation is asserted separately on the key function.
        cfg = self.env['ow.mcp.config'].sudo().get_singleton()
        cfg.cache_enabled = False

    def _dispatch(self, name, args, msg_id=1):
        env = self.env(user=self.user_ab)
        return dispatch(env, {
            'method': 'tools/call', 'id': msg_id,
            'params': {'name': name, 'arguments': args},
        })

    # ---- valid switch ------------------------------------------------------

    def test_switch_to_company_a(self):
        out = self._dispatch('search_records', {
            'model': 'res.partner',
            'company_id': self.company_a.id,
            'domain': [('name', 'in',
                        [self.partner_a.name, self.partner_b.name])],
        })
        self.assertFalse(
            _is_error(out),
            msg=(out['result'].get('content') or [{}])[0].get('text', ''),
        )
        names = {r['display_name'] for r in _struct(out)['records']}
        self.assertIn(self.partner_a.name, names)
        self.assertNotIn(self.partner_b.name, names)

    def test_switch_to_company_b(self):
        out = self._dispatch('search_records', {
            'model': 'res.partner',
            'company_id': self.company_b.id,
            'domain': [('name', 'in',
                        [self.partner_a.name, self.partner_b.name])],
        })
        self.assertFalse(
            _is_error(out),
            msg=(out['result'].get('content') or [{}])[0].get('text', ''),
        )
        names = {r['display_name'] for r in _struct(out)['records']}
        self.assertIn(self.partner_b.name, names)
        self.assertNotIn(self.partner_a.name, names)

    # ---- invalid company ---------------------------------------------------

    def test_company_outside_allowed_returns_iserror(self):
        out = self._dispatch('search_records', {
            'model': 'res.partner',
            'company_id': self.company_c.id,
        })
        self.assertTrue(_is_error(out))
        text = out['result']['content'][0]['text']
        self.assertIn(str(self.company_c.id), text)
        self.assertIn('not in your allowed companies', text)

    # ---- get_user_context --------------------------------------------------

    def test_user_context_lists_allowed_and_default(self):
        out = self._dispatch('get_user_context', {})
        self.assertFalse(
            _is_error(out),
            msg=(out['result'].get('content') or [{}])[0].get('text', ''),
        )
        sc = _struct(out)
        self.assertEqual(sc['default_company_id'], self.company_a.id)
        self.assertEqual(sc['company']['id'], self.company_a.id)
        ids = {c['id'] for c in sc['allowed_companies']}
        self.assertEqual(ids, {self.company_a.id, self.company_b.id})

    def test_user_context_reflects_switched_company(self):
        out = self._dispatch('get_user_context', {
            'company_id': self.company_b.id,
        })
        self.assertFalse(
            _is_error(out),
            msg=(out['result'].get('content') or [{}])[0].get('text', ''),
        )
        sc = _struct(out)
        # Current company reflects the switch …
        self.assertEqual(sc['company']['id'], self.company_b.id)
        # … but the user's persistent default is unchanged.
        self.assertEqual(sc['default_company_id'], self.company_a.id)

    # ---- cache isolation ---------------------------------------------------

    def test_cache_key_segments_by_company(self):
        base_args = {'model': 'res.partner', 'domain': []}
        k_default = cache._key(1, (), 'search_records', dict(base_args))
        k_a = cache._key(1, (), 'search_records',
                         dict(base_args, company_id=self.company_a.id))
        k_b = cache._key(1, (), 'search_records',
                         dict(base_args, company_id=self.company_b.id))
        self.assertNotEqual(k_default, k_a)
        self.assertNotEqual(k_a, k_b)


@tagged('post_install', '-at_install')
class TestMulticompanyAudit(TransactionCase):
    """Audit log captures the requested company id from tools/call args."""

    def test_company_id_helper_extracts_int(self):
        from odoo.addons.ow_mcp_server.controllers.mcp_controller import (
            _company_id,
        )
        msg = {
            'method': 'tools/call',
            'params': {
                'name': 'search_records',
                'arguments': {'model': 'res.partner', 'company_id': 7},
            },
        }
        self.assertEqual(_company_id(msg), 7)

    def test_company_id_helper_ignores_non_int(self):
        from odoo.addons.ow_mcp_server.controllers.mcp_controller import (
            _company_id,
        )
        msg = {
            'method': 'tools/call',
            'params': {
                'name': 'search_records',
                'arguments': {'model': 'res.partner', 'company_id': 'foo'},
            },
        }
        self.assertIsNone(_company_id(msg))

    def test_company_id_helper_skips_non_tools_call(self):
        from odoo.addons.ow_mcp_server.controllers.mcp_controller import (
            _company_id,
        )
        self.assertIsNone(_company_id({'method': 'ping'}))

    def test_audit_row_stores_company_id(self):
        company = self.env.ref('base.main_company')
        log = self.env['ow.mcp.audit.log'].sudo().create({
            'method': 'tools/call',
            'tool_name': 'search_records',
            'company_id': company.id,
            'success': True,
        })
        self.assertEqual(log.company_id, company)
