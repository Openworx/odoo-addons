# -*- coding: utf-8 -*-
import io
import json

from odoo.tests import TransactionCase, new_test_user

from ..engine.tools_registry import ToolContext, run_tool


def _png_bytes():
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (4, 4), color=(10, 20, 30)).save(buf, format='PNG')
    return buf.getvalue()


class ToolReadTestCase(TransactionCase):
    """Shared fixtures: an internal user, demo partners, and a ctx() helper."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.internal_user = new_test_user(cls.env, login='ow_ai_reader', groups='base.group_user')
        cls.category = cls.env['res.partner.category'].create({'name': "OW AI Test Tag"})
        cls.partner_a = cls.env['res.partner'].create({
            'name': "Alpha Testing BV",
            'city': "Rotterdam",
            'country_id': cls.env.ref('base.nl').id,
            'category_id': [(6, 0, cls.category.ids)],
        })
        cls.partner_b = cls.env['res.partner'].create({
            'name': "Beta Testing BV",
            'city': "Amsterdam",
            'country_id': cls.env.ref('base.nl').id,
        })
        cls.partner_c = cls.env['res.partner'].create({
            'name': "Charlie Testing GmbH",
            'city': "Berlin",
            'country_id': cls.env.ref('base.de').id,
        })

    def ctx(self, user=None, **kwargs):
        env = self.env(user=user) if user is not None else self.env
        return ToolContext(env=env, **kwargs)

    def run_tool(self, xmlid, args, ctx):
        # Tool *definitions* (schema, builtin_key, ...) are shared config,
        # not user data: the (future) engine resolves them with elevated
        # rights, then runs the tool body against the caller's own,
        # non-sudo `ctx.env`. Mirror that split here instead of forcing the
        # tool record itself onto the restricted user env.
        tool = self.env.ref(xmlid).sudo()
        return run_tool(tool, args, ctx)


class TestRunTool(ToolReadTestCase):

    def test_invalid_arguments(self):
        result = self.run_tool('ow_ai.tool_search', {}, self.ctx(self.internal_user))
        self.assertFalse(result.success)
        self.assertIn("Invalid arguments", result.response)

    def test_sudo_env_raises(self):
        tool = self.env.ref('ow_ai.tool_search')
        su_ctx = ToolContext(env=self.env(su=True))
        with self.assertRaises(RuntimeError):
            run_tool(tool.with_env(su_ctx.env), {'model_name': 'res.partner'}, su_ctx)

    def test_model_access_error_yields_failed_result(self):
        result = self.run_tool(
            'ow_ai.tool_search', {'model_name': 'ir.cron'}, self.ctx(self.internal_user))
        self.assertFalse(result.success)
        self.assertIn("Tool call failed", result.response)

    def test_unexpected_exception_is_generic(self):
        from ..engine import tools_registry

        original = tools_registry.get_builtin('read.search')

        def boom(ctx, **args):
            raise RuntimeError("boom, secret traceback detail")

        tools_registry._REGISTRY['read.search'] = tools_registry.ToolSpec(
            key='read.search', func=boom, is_write=False)
        try:
            result = self.run_tool(
                'ow_ai.tool_search', {'model_name': 'res.partner'}, self.ctx(self.internal_user))
        finally:
            tools_registry._REGISTRY['read.search'] = original

        self.assertFalse(result.success)
        self.assertEqual(result.response, "Tool call failed: unexpected error")
        self.assertNotIn("boom", result.response)
        self.assertNotIn("Traceback", result.response)

    def test_truncation_applied(self):
        self.env['ir.config_parameter'].sudo().set_str('ow_ai.max_tool_result_chars', '100')
        result = self.run_tool(
            'ow_ai.tool_get_fields', {'model_name': 'res.partner'}, self.ctx(self.internal_user))
        self.assertTrue(result.success)
        self.assertLessEqual(len(result.response), 140)
        self.assertIn('truncated', result.response)


    def test_dict_response_is_serialised_and_truncated(self):
        # A search result (a dict) is capped like any text result: it is
        # serialised to compact JSON first, then cut with a "[truncated" note.
        self.env['ir.config_parameter'].sudo().set_str('ow_ai.max_tool_result_chars', '200')
        result = self.run_tool(
            'ow_ai.tool_search',
            {'model_name': 'res.partner', 'domain': [['name', 'ilike', 'Testing']]},
            self.ctx(self.internal_user))
        self.assertTrue(result.success)
        self.assertIsInstance(result.response, str)
        self.assertTrue(result.response.startswith('{"'))
        self.assertIn('[truncated', result.response)
        self.assertLess(len(result.response), 260)


class TestGetModels(ToolReadTestCase):

    def test_lists_res_partner(self):
        result = self.run_tool('ow_ai.tool_get_models', {}, self.ctx(self.internal_user))
        self.assertTrue(result.success)
        lines = result.response.splitlines()
        partner_lines = [line for line in lines if line.startswith('res.partner |')]
        self.assertTrue(partner_lines)
        self.assertIn('Contact', partner_lines[0])

    def test_excludes_transient_and_blocklisted(self):
        result = self.run_tool('ow_ai.tool_get_models', {}, self.ctx(self.internal_user))
        model_names = [line.split(' | ')[0] for line in result.response.splitlines()]
        self.assertNotIn('ir.cron', model_names)
        self.assertNotIn('res.config.settings', model_names)

    def test_portal_user_gets_smaller_list_without_access_error(self):
        portal_user = new_test_user(self.env, login='ow_ai_portal', groups='base.group_portal')
        result = self.run_tool('ow_ai.tool_get_models', {}, self.ctx(portal_user))
        self.assertTrue(result.success)
        internal_result = self.run_tool('ow_ai.tool_get_models', {}, self.ctx(self.internal_user))
        self.assertLessEqual(len(result.response.splitlines()), len(internal_result.response.splitlines()))


class TestGetFields(ToolReadTestCase):

    def test_header_and_name_line(self):
        result = self.run_tool(
            'ow_ai.tool_get_fields', {'model_name': 'res.partner'}, self.ctx(self.internal_user))
        self.assertTrue(result.success)
        lines = result.response.splitlines()
        self.assertIn('Contact', lines[0])
        self.assertIn('res.partner', lines[0])
        name_lines = [line for line in lines if line.startswith('name | ')]
        self.assertTrue(name_lines)
        self.assertIn('Name | char', name_lines[0])

    def test_selection_rendered(self):
        result = self.run_tool(
            'ow_ai.tool_get_fields', {'model_name': 'res.partner'}, self.ctx(self.internal_user))
        type_lines = [line for line in result.response.splitlines() if line.startswith('type | ')]
        self.assertTrue(type_lines)
        self.assertIn('selection:', type_lines[0])

    def test_message_ids_absent(self):
        result = self.run_tool(
            'ow_ai.tool_get_fields', {'model_name': 'res.partner'}, self.ctx(self.internal_user))
        self.assertNotIn('message_ids | ', result.response)


class TestSearch(ToolReadTestCase):

    def test_domain_as_string_and_list(self):
        ctx = self.ctx(self.internal_user)
        result_list = self.run_tool(
            'ow_ai.tool_search',
            {'model_name': 'res.partner', 'domain': [['id', '=', self.partner_a.id]]}, ctx)
        result_str = self.run_tool(
            'ow_ai.tool_search',
            {'model_name': 'res.partner', 'domain': f"[['id', '=', {self.partner_a.id}]]"}, ctx)
        self.assertTrue(result_list.success)
        self.assertTrue(result_str.success)
        self.assertEqual(json.loads(result_list.response)['total_count'], 1)
        self.assertEqual(json.loads(result_str.response)['total_count'], 1)

    def test_default_and_explicit_fields(self):
        ctx = self.ctx(self.internal_user)
        default_result = self.run_tool(
            'ow_ai.tool_search',
            {'model_name': 'res.partner', 'domain': [['id', '=', self.partner_a.id]]}, ctx)
        record = json.loads(default_result.response)['records'][0]
        self.assertIn('city', record)

        explicit_result = self.run_tool(
            'ow_ai.tool_search',
            {'model_name': 'res.partner', 'domain': [['id', '=', self.partner_a.id]], 'fields': ['city']}, ctx)
        explicit_record = json.loads(explicit_result.response)['records'][0]
        self.assertEqual(set(explicit_record) - {'id', 'display_name'}, {'city'})

    def test_bad_order_field_fails(self):
        result = self.run_tool(
            'ow_ai.tool_search',
            {'model_name': 'res.partner', 'order': 'does_not_exist desc'}, self.ctx(self.internal_user))
        self.assertFalse(result.success)
        self.assertIn("Tool call failed", result.response)

    def test_limit_clamped_to_200(self):
        result = self.run_tool(
            'ow_ai.tool_search',
            {'model_name': 'res.partner', 'limit': 5000}, self.ctx(self.internal_user))
        self.assertTrue(result.success)
        self.assertEqual(json.loads(result.response)['limit'], 200)

    def test_pagination_has_more(self):
        ctx = self.ctx(self.internal_user)
        result = self.run_tool(
            'ow_ai.tool_search',
            {'model_name': 'res.partner', 'domain': [['id', 'in',
                [self.partner_a.id, self.partner_b.id, self.partner_c.id]]], 'limit': 2}, ctx)
        self.assertTrue(result.success)
        self.assertEqual(json.loads(result.response)['total_count'], 3)
        self.assertTrue(json.loads(result.response)['has_more'])

        result_2 = self.run_tool(
            'ow_ai.tool_search',
            {'model_name': 'res.partner', 'domain': [['id', 'in',
                [self.partner_a.id, self.partner_b.id, self.partner_c.id]]], 'limit': 2, 'offset': 2}, ctx)
        self.assertFalse(json.loads(result_2.response)['has_more'])

    def test_record_rule_excludes_inaccessible_partner(self):
        # Odoo 20 folded `ir.rule` into `ir.access`: an `ir.access` row now
        # carries a `domain` column directly (see
        # odoo/addons/base/models/ir_access.py, `IrAccess._access_domain`),
        # and per-group 'r' rows are OR-combined into a `permissions` domain
        # while group-less rows are AND-combined as `restrictions`
        # (`Domain.OR(permissions) & Domain.AND(restrictions)`).
        #
        # base.group_user already carries an *unrestricted* 'r' access row on
        # res.partner (base.res_partner_rule_user, empty domain = TRUE). Since
        # permissions OR together, adding another group with a *narrower*
        # domain on top of that would have no effect at all: TRUE | anything
        # is still TRUE. To make a domain-restricted permission actually
        # bite, either (a) use a group-less *restriction* row (AND-combined,
        # applies to everyone), or (b) as the review asked, pick a scope
        # where the user's *only* 'r' permission is our new, domain-scoped
        # one. We do (b) here: temporarily deactivate the base unrestricted
        # row for the duration of this test (TransactionCase rolls the whole
        # test back afterwards, so this is not a permanent change), so the
        # fresh group's domain is the sole source of read access to
        # res.partner for this user.
        self.env.ref('base.res_partner_rule_user').active = False

        scoped_group = self.env['res.groups'].create({'name': "OW AI Test Scoped Partner Reader"})
        self.internal_user.write({'group_ids': [(4, scoped_group.id)]})
        self.env['ir.access'].create({
            'name': "OW AI test: hide one partner by name",
            'model_id': self.env.ref('base.model_res_partner').id,
            'group_id': scoped_group.id,
            'operation': 'r',
            'domain': "[('name', '!=', 'Hidden Partner')]",
        })

        visible_partner = self.env['res.partner'].create({'name': "Visible Partner"})
        hidden_partner = self.env['res.partner'].create({'name': "Hidden Partner"})

        ctx = self.ctx(self.internal_user)
        search_result = self.run_tool(
            'ow_ai.tool_search',
            {'model_name': 'res.partner', 'domain': [['id', 'in', [visible_partner.id, hidden_partner.id]]]},
            ctx)
        self.assertTrue(search_result.success)
        self.assertEqual(json.loads(search_result.response)['total_count'], 1)
        self.assertEqual(json.loads(search_result.response)['records'][0]['id'], visible_partner.id)

        read_result = self.run_tool(
            'ow_ai.tool_read_records',
            {'model_name': 'res.partner', 'record_ids': [visible_partner.id, hidden_partner.id]},
            ctx)
        self.assertTrue(read_result.success)
        self.assertEqual([r['id'] for r in json.loads(read_result.response)['records']], [visible_partner.id])
        self.assertEqual(json.loads(read_result.response)['missing_ids'], [hidden_partner.id])

    def test_portal_user_denied_model_yields_failed_result(self):
        portal_user = new_test_user(self.env, login='ow_ai_portal_bank', groups='base.group_portal')
        result = self.run_tool(
            'ow_ai.tool_search', {'model_name': 'res.bank'}, self.ctx(portal_user))
        self.assertFalse(result.success)
        self.assertIn("Tool call failed", result.response)
        self.assertNotIn("Traceback", result.response)


class TestReadRecords(ToolReadTestCase):

    def test_missing_ids_reported(self):
        missing_id = self.partner_c.id + 1000000
        result = self.run_tool(
            'ow_ai.tool_read_records',
            {'model_name': 'res.partner', 'record_ids': [self.partner_a.id, missing_id]},
            self.ctx(self.internal_user))
        self.assertTrue(result.success)
        self.assertEqual(len(json.loads(result.response)['records']), 1)
        self.assertEqual(json.loads(result.response)['missing_ids'], [missing_id])

    def test_capped_at_50(self):
        partners = self.env['res.partner'].create([{'name': f"Bulk {i}"} for i in range(60)])
        result = self.run_tool(
            'ow_ai.tool_read_records',
            {'model_name': 'res.partner', 'record_ids': partners.ids}, self.ctx(self.internal_user))
        self.assertTrue(result.success)
        response = json.loads(result.response)
        self.assertEqual(len(response['records']) + len(response['missing_ids']), 50)

    def test_duplicates_deduplicated_before_capping_at_50(self):
        # Regression test: record_ids must be de-duplicated (preserving
        # order) *before* the 50-id cap is applied, not sliced first and
        # de-duplicated after (which would silently waste slots on repeats
        # of the same id and could return fewer than 50 distinct records
        # even when 50+ distinct ids were requested).
        partners = self.env['res.partner'].create([{'name': f"Dup {i}"} for i in range(55)])
        record_ids = [partners[0].id, partners[0].id, partners[0].id] + partners.ids
        result = self.run_tool(
            'ow_ai.tool_read_records',
            {'model_name': 'res.partner', 'record_ids': record_ids}, self.ctx(self.internal_user))
        self.assertTrue(result.success)
        returned_ids = [r['id'] for r in json.loads(result.response)['records']]
        self.assertEqual(len(returned_ids), len(set(returned_ids)), "duplicate ids returned")
        self.assertEqual(len(returned_ids) + len(json.loads(result.response)['missing_ids']), 50)
        # the 3 leading duplicates of partners[0] must only ever count once
        self.assertEqual(returned_ids[0], partners[0].id)
        self.assertEqual(returned_ids.count(partners[0].id), 1)

    def test_include_files_returns_image_parts(self):
        partner = self.env['res.partner'].create({'name': "With Image"})
        self.env['ir.attachment'].create({
            'name': 'photo.png',
            'res_model': 'res.partner',
            'res_id': partner.id,
            'mimetype': 'image/png',
            'raw': _png_bytes(),
        })
        result = self.run_tool(
            'ow_ai.tool_read_records',
            {'model_name': 'res.partner', 'record_ids': [partner.id], 'include_files': True},
            self.ctx(self.internal_user))
        self.assertTrue(result.success)
        self.assertTrue(result.parts)
        self.assertEqual(result.parts[0]['type'], 'inline_data')


class TestReadGroup(ToolReadTestCase):

    def test_groupby_country_ordered_by_count(self):
        result = self.run_tool(
            'ow_ai.tool_read_group',
            {
                'model_name': 'res.partner',
                'domain': [['id', 'in', [self.partner_a.id, self.partner_b.id, self.partner_c.id]]],
                'groupby': ['country_id'],
                'aggregates': ['__count'],
                'order': '__count desc',
                'limit': 5,
            },
            self.ctx(self.internal_user))
        self.assertTrue(result.success)
        groups = json.loads(result.response)['groups']
        self.assertLessEqual(len(groups), 5)
        counts = [group['__count'] for group in groups]
        self.assertEqual(counts, sorted(counts, reverse=True))
        nl_group = next(g for g in groups if g['country_id'] and g['country_id']['display_name'] == 'Netherlands')
        self.assertEqual(nl_group['__count'], 2)

    def test_order_on_a_bare_field_names_the_valid_order_values(self):
        """A model ordering by the field instead of its aggregate spec is told what to use."""
        result = self.run_tool(
            'ow_ai.tool_read_group',
            {
                'model_name': 'res.partner',
                'groupby': ['country_id'],
                'aggregates': ['color:sum'],
                'order': 'color desc',
            },
            self.ctx(self.internal_user))
        self.assertFalse(result.success)
        self.assertIn("'color'", result.response)
        self.assertIn("country_id, color:sum", result.response)
        self.assertIn("e.g. 'color:sum desc'", result.response)

    def test_groupby_date_granularity(self):
        result = self.run_tool(
            'ow_ai.tool_read_group',
            {'model_name': 'res.partner', 'groupby': ['create_date:month'], 'aggregates': ['__count']},
            self.ctx(self.internal_user))
        self.assertTrue(result.success)
        self.assertTrue(json.loads(result.response)['groups'])

    def test_invalid_aggregate_field_fails(self):
        result = self.run_tool(
            'ow_ai.tool_read_group',
            {'model_name': 'res.partner', 'aggregates': ['does_not_exist:sum']},
            self.ctx(self.internal_user))
        self.assertFalse(result.success)
        self.assertIn("Tool call failed", result.response)

    def test_reversed_aggregate_spec_names_the_right_spelling(self):
        result = self.run_tool(
            'ow_ai.tool_read_group',
            {'model_name': 'res.partner', 'groupby': ['country_id'], 'aggregates': ['sum:color']},
            self.ctx(self.internal_user))
        self.assertFalse(result.success)
        self.assertIn("e.g. 'color:sum'", result.response)

    def test_invalid_having_fails(self):
        result = self.run_tool(
            'ow_ai.tool_read_group',
            {
                'model_name': 'res.partner', 'groupby': ['country_id'], 'aggregates': ['__count'],
                'having': "[['does_not_exist', '>', 1]]",
            },
            self.ctx(self.internal_user))
        self.assertFalse(result.success)
        self.assertIn("Tool call failed", result.response)


class TestComputeDate(ToolReadTestCase):

    def setUp(self):
        super().setUp()
        try:
            import freezegun
        except ImportError:
            self.skipTest("freezegun is not available in this environment")
        self.freezegun = freezegun
        self.internal_user.tz = 'Europe/Amsterdam'

    def test_yesterday_start_date_field(self):
        with self.freezegun.freeze_time('2025-10-01 10:00:00'):
            result = self.run_tool(
                'ow_ai.tool_compute_date',
                {
                    'model_name': 'res.partner', 'field_name': 'activity_date_deadline',
                    'operations': [{'type': 'navigate', 'period': 'day', 'offset': -1, 'boundary': 'start'}],
                },
                self.ctx(self.internal_user))
        self.assertTrue(result.success)
        self.assertEqual(json.loads(result.response)['value'], '2025-09-30')
        self.assertEqual(json.loads(result.response)['field_type'], 'date')

    def test_yesterday_start_datetime_field(self):
        with self.freezegun.freeze_time('2025-10-01 10:00:00'):
            result = self.run_tool(
                'ow_ai.tool_compute_date',
                {
                    'model_name': 'res.partner', 'field_name': 'create_date',
                    'operations': [{'type': 'navigate', 'period': 'day', 'offset': -1, 'boundary': 'start'}],
                },
                self.ctx(self.internal_user))
        self.assertTrue(result.success)
        self.assertEqual(json.loads(result.response)['value'], '2025-09-29 22:00:00')
        self.assertEqual(json.loads(result.response)['field_type'], 'datetime')

    def test_this_quarter_end(self):
        with self.freezegun.freeze_time('2025-10-15 10:00:00'):
            result = self.run_tool(
                'ow_ai.tool_compute_date',
                {
                    'model_name': 'res.partner', 'field_name': 'activity_date_deadline',
                    'operations': [{'type': 'navigate', 'period': 'quarter', 'offset': 0, 'boundary': 'end'}],
                },
                self.ctx(self.internal_user))
        self.assertTrue(result.success)
        self.assertEqual(json.loads(result.response)['value'], '2025-12-31')

    def test_placeholder_pin_is_ignored(self):
        """Models fill the optional pin with zero values ({"day": 0, "time": ""}):
        that must not move the end of the quarter to the 1st of its last month."""
        with self.freezegun.freeze_time('2025-10-15 10:00:00'):
            result = self.run_tool(
                'ow_ai.tool_compute_date',
                {
                    'model_name': 'res.partner', 'field_name': 'activity_date_deadline',
                    'operations': [{'type': 'navigate', 'period': 'quarter', 'offset': 0, 'boundary': 'end'}],
                    'pin': {'day': 0, 'time': ''},
                },
                self.ctx(self.internal_user))
        self.assertTrue(result.success)
        self.assertEqual(json.loads(result.response)['value'], '2025-12-31')

    def test_next_friday_at_1700(self):
        with self.freezegun.freeze_time('2025-10-01 10:00:00'):  # Wednesday
            result = self.run_tool(
                'ow_ai.tool_compute_date',
                {
                    'model_name': 'res.partner', 'field_name': 'create_date',
                    'operations': [{'type': 'find_next', 'weekday': 4}],
                    'pin': {'time': '17:00'},
                },
                self.ctx(self.internal_user))
        self.assertTrue(result.success)
        self.assertEqual(json.loads(result.response)['value'], '2025-10-03 15:00:00')

    def test_first_monday_of_last_month(self):
        with self.freezegun.freeze_time('2025-10-15 10:00:00'):
            result = self.run_tool(
                'ow_ai.tool_compute_date',
                {
                    'model_name': 'res.partner', 'field_name': 'activity_date_deadline',
                    'operations': [{'type': 'navigate', 'period': 'month', 'offset': -1, 'boundary': 'start'}],
                    'pin': {'day': {'weekday': 0, 'occurrence': 1}},
                },
                self.ctx(self.internal_user))
        self.assertTrue(result.success)
        self.assertEqual(json.loads(result.response)['value'], '2025-09-01')

    def test_invalid_weekday_fails(self):
        result = self.run_tool(
            'ow_ai.tool_compute_date',
            {
                'model_name': 'res.partner', 'field_name': 'activity_date_deadline',
                'operations': [{'type': 'find_next', 'weekday': 9}],
            },
            self.ctx(self.internal_user))
        self.assertFalse(result.success)
        # rejected either by schema validation (weekday out of 0-6 range) or
        # by the tool's own runtime check; either way it must be a failed
        # result, never a traceback.
        self.assertNotIn("Traceback", result.response)

    def test_non_date_field_fails(self):
        result = self.run_tool(
            'ow_ai.tool_compute_date',
            {'model_name': 'res.partner', 'field_name': 'name'},
            self.ctx(self.internal_user))
        self.assertFalse(result.success)
        self.assertIn("Tool call failed", result.response)

    # -- daylight saving time (audit finding 5) ------------------------------

    def test_month_boundary_over_march_dst_start(self):
        """The audit's own reproduction: clock pinned to 20 April 2026,
        Europe/Amsterdam, navigate one month back to its start. 1 March is
        still CET (DST starts 29 March): the correct UTC instant is
        2026-02-28 23:00:00, not the pytz-fixed-offset 22:00:00."""
        with self.freezegun.freeze_time('2026-04-20 10:00:00'):
            result = self.run_tool(
                'ow_ai.tool_compute_date',
                {
                    'model_name': 'res.partner', 'field_name': 'create_date',
                    'operations': [{'type': 'navigate', 'period': 'month', 'offset': -1, 'boundary': 'start'}],
                },
                self.ctx(self.internal_user))
        self.assertTrue(result.success)
        self.assertEqual(json.loads(result.response)['value'], '2026-02-28 23:00:00')

    def test_month_boundary_over_october_dst_end(self):
        """From 1 October (CEST) to 1 November's start: 1 November is
        already CET (DST ends 25 October), so the correct UTC instant is
        2026-10-31 23:00:00, not the CEST-fixed-offset 22:00:00."""
        with self.freezegun.freeze_time('2026-10-01 08:00:00'):
            result = self.run_tool(
                'ow_ai.tool_compute_date',
                {
                    'model_name': 'res.partner', 'field_name': 'create_date',
                    'operations': [{'type': 'navigate', 'period': 'month', 'offset': 1, 'boundary': 'start'}],
                },
                self.ctx(self.internal_user))
        self.assertTrue(result.success)
        self.assertEqual(json.loads(result.response)['value'], '2026-10-31 23:00:00')

    def test_find_previous_crosses_a_dst_change(self):
        """"Now" is Thursday 2 April 2026 (CEST); the previous Saturday is
        28 March, still CET. Stepping back day by day must not keep the
        CEST offset once the date crosses the transition."""
        with self.freezegun.freeze_time('2026-04-02 08:00:00'):
            result = self.run_tool(
                'ow_ai.tool_compute_date',
                {
                    'model_name': 'res.partner', 'field_name': 'create_date',
                    'operations': [{'type': 'find_previous', 'weekday': 5}],
                },
                self.ctx(self.internal_user))
        self.assertTrue(result.success)
        self.assertEqual(json.loads(result.response)['value'], '2026-03-28 09:00:00')

    def test_pin_on_a_nonexistent_local_time_yields_a_valid_utc_value(self):
        """02:30 on 29 March 2026 never happens locally (clocks jump from
        02:00 to 03:00): this must still return a valid UTC string instead
        of raising. ``fold=0`` resolves it with the pre-transition (CET,
        UTC+1) offset."""
        with self.freezegun.freeze_time('2026-03-29 09:00:00'):
            result = self.run_tool(
                'ow_ai.tool_compute_date',
                {
                    'model_name': 'res.partner', 'field_name': 'create_date',
                    'pin': {'time': '02:30'},
                },
                self.ctx(self.internal_user))
        self.assertTrue(result.success)
        self.assertEqual(json.loads(result.response)['value'], '2026-03-29 01:30:00')

    def test_date_field_stays_a_plain_date_across_dst(self):
        with self.freezegun.freeze_time('2026-04-20 10:00:00'):
            result = self.run_tool(
                'ow_ai.tool_compute_date',
                {
                    'model_name': 'res.partner', 'field_name': 'activity_date_deadline',
                    'operations': [{'type': 'navigate', 'period': 'month', 'offset': -1, 'boundary': 'start'}],
                },
                self.ctx(self.internal_user))
        self.assertTrue(result.success)
        payload = json.loads(result.response)
        self.assertEqual(payload['value'], '2026-03-01')
        self.assertEqual(payload['field_type'], 'date')
