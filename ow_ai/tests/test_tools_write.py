# -*- coding: utf-8 -*-
"""The write tools (``tools/write_create.py``/``write_update.py``): preview,
confirmation/decline/auto-confirm, argument validation, notifications with
record links, the non-blocking client ``reload`` notification, and the
native "Create Records"/"Update Records" skills.

Driven end-to-end through ``EngineLoopCase``/``OwAiCase`` helpers (see
``tests/common.py``, ``tests/test_engine_loop.py``): ``say()``/``resume()``
run a whole scripted chat turn with ``ow_ai_inline_jobs=True``, so a
confirmation pause and its resume happen within one Python call.
"""
from __future__ import annotations

import json

from odoo.tests import new_test_user

from ..engine import tools_registry
from .test_engine_loop import EngineLoopCase


class WriteToolCase(EngineLoopCase):

    def setUp(self):
        super().setUp()
        # `base.group_user` alone only has read access to `res.partner`
        # (create/delete need `base.group_partner_manager`, write is
        # otherwise limited to one's own partner record) -- add it so
        # `self.user` can exercise create_records/update_records on
        # `res.partner`/`res.partner.category` normally. The access-denied
        # tests use their own, separately-scoped user instead.
        self.user.write({'group_ids': [(4, self.env.ref('base.group_partner_manager').id)]})
        self.make_available('ow_ai.tool_create_records', 'ow_ai.tool_update_records')
        self.category = self.env['res.partner.category'].create({'name': "VIP"})

    def bus_client_notifications(self):
        self.env.cr.precommit.run()
        notifications = []
        for bus in self.env['bus.bus'].sudo().search([]):
            message = json.loads(bus.message)
            if message.get('payload', {}).get('client_tools') and 'session/client_tools' in bus.message:
                notifications.append(message['payload'])
        return notifications


class TestCreateRecordsConfirmation(WriteToolCase):

    def create_call(self, *, values=None, call_id='call_1'):
        values = values or [{'field_values': [
            {'field': 'name', 'value': 'Acme Corp'},
            {'field': 'email', 'value': 'info@acme.example'},
        ]}]
        self.queue_tool_calls([(
            'create_records',
            {'explanation': "Creating the new contact you described.", 'model_name': 'res.partner', 'values': values},
            call_id,
        )])

    def test_pause_shows_preview_card_with_explanation_and_values(self):
        self.create_call()

        self.say(self.channel, 'Create a contact called Acme Corp', self.user)

        session = self.session
        self.assertEqual(session.loop_state, 'waiting_confirmation')
        card = self.agent_messages(self.channel)[-1]
        self.assertIn('o_ow_ai_input_card', card.body)
        self.assertIn('Creating the new contact you described.', card.body)
        self.assertIn('Email', card.body)
        self.assertIn('info@acme.example', card.body)
        self.assertIn('Acme Corp', card.body)
        self.assertEqual(self.env['res.partner'].search_count([('name', '=', 'Acme Corp')]), 0)

        store_request = session._ow_ai_user_input_request()
        self.assertEqual(store_request['type'], 'confirmation')
        self.assertEqual(set(store_request['choices']), {'confirm_once', 'auto_confirm', 'decline'})
        self.assertEqual(store_request['labels']['confirm_once'], 'Yes, do it')

    def test_confirm_once_creates_record_notifies_and_reloads(self):
        self.env['bus.bus'].sudo().search([]).unlink()
        self.create_call()
        self.say(self.channel, 'Create a contact called Acme Corp', self.user)
        self.queue_text("Done, Acme Corp was created.")

        result = self.resume(self.channel, {'kind': 'confirmation', 'value': 'confirm_once'}, self.user)

        self.assertEqual(result, {'interactionConsumed': True, 'loop_state': 'ready'})
        partner = self.env['res.partner'].search([('name', '=', 'Acme Corp')])
        self.assertEqual(len(partner), 1)
        self.assertEqual(partner.email, 'info@acme.example')
        response = json.loads(self.tool_messages(1)['call_1'])
        self.assertEqual(response['created'], [
            {'id': partner.id, 'display_name': partner.display_name, 'url': f'/odoo/res.partner/{partner.id}'},
        ])
        self.assertEqual(self.last_answer(self.channel), "Done, Acme Corp was created.")

        last = self.agent_messages(self.channel)[-1]
        self.assertIn('data-oe-type="ow_ai_preview"', last.body)
        self.assertIn(f'/odoo/res.partner/{partner.id}', last.body)
        self.assertIn('Created', last.body)

        notifications = self.bus_client_notifications()
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]['client_tools'], [{'name': 'reload', 'params': {}}])
        self.assertFalse(notifications[0]['blocking'])
        # no browser tab named in the request: any tab may run it
        self.assertIs(notifications[0]['client_identifier'], False)
        self.assertEqual(self.session.loop_state, 'ready')

    def test_m2m_link_ids_link_existing_category(self):
        self.create_call(values=[{'field_values': [
            {'field': 'name', 'value': 'Tagged Co'},
            {'field': 'category_id', 'x2m_link_ids': [self.category.id]},
        ]}])
        self.say(self.channel, 'Create it', self.user)
        self.queue_text('Created.')

        self.resume(self.channel, {'kind': 'confirmation', 'value': 'confirm_once'}, self.user)

        partner = self.env['res.partner'].search([('name', '=', 'Tagged Co')])
        self.assertEqual(partner.category_id, self.category)

    def test_decline_creates_nothing_and_continues(self):
        self.create_call()
        self.say(self.channel, 'Create a contact called Acme Corp', self.user)
        self.queue_text("Ok, I did not create it.")

        result = self.resume(self.channel, {'kind': 'confirmation', 'value': 'decline'}, self.user)

        self.assertTrue(result['interactionConsumed'])
        self.assertEqual(self.env['res.partner'].search_count([('name', '=', 'Acme Corp')]), 0)
        self.assertIn('declined', self.tool_messages(1)['call_1'])
        self.assertEqual(self.last_answer(self.channel), "Ok, I did not create it.")
        self.assertEqual(self.session.loop_state, 'ready')

    def test_auto_confirm_second_call_has_no_card(self):
        self.create_call()
        self.say(self.channel, 'Create Acme Corp', self.user)
        self.queue_text('Created Acme Corp.')

        self.resume(self.channel, {'kind': 'confirmation', 'value': 'auto_confirm'}, self.user)

        self.assertTrue(self.session.auto_confirm)
        notes = self.agent_messages(self.channel).filtered(
            lambda message: 'data-oe-type="ow_ai_note"' in (message.body or ''))
        self.assertIn('Auto-approval enabled', notes.body)

        self.create_call(values=[{'field_values': [{'field': 'name', 'value': 'Second Co'}]}], call_id='call_2')
        self.queue_text('Created Second Co too.')
        cards_before = len(self.agent_messages(self.channel).filtered(
            lambda message: 'o_ow_ai_input_card' in (message.body or '')))

        self.say(self.channel, 'Now create Second Co', self.user)

        cards_after = len(self.agent_messages(self.channel).filtered(
            lambda message: 'o_ow_ai_input_card' in (message.body or '')))
        self.assertEqual(cards_before, cards_after)
        self.assertEqual(self.env['res.partner'].search_count([('name', '=', 'Second Co')]), 1)
        self.assertEqual(self.session.loop_state, 'ready')


class TestCreateRecordsValidation(WriteToolCase):

    def create_and_say(self, values, call_id='call_1', model_name='res.partner'):
        self.queue_tool_calls([(
            'create_records',
            {'explanation': "x", 'model_name': model_name, 'values': values},
            call_id,
        )])
        self.queue_text('Noted.')
        self.say(self.channel, 'Create it', self.user)

    def test_unknown_field_is_a_tool_error_not_a_pause(self):
        self.create_and_say([{'field_values': [{'field': 'not_a_real_field', 'value': 'x'}]}])

        self.assertIn('Unknown field', self.tool_messages(1)['call_1'])
        self.assertEqual(self.session.loop_state, 'ready')

    def test_required_field_missing_is_a_tool_error(self):
        # `res.partner.name` is NOT ORM-required (only conditionally, via a
        # DB CHECK constraint for `type='contact'`); use
        # `res.partner.category`, whose `name` genuinely is `required=True`.
        self.create_and_say(
            [{'field_values': [{'field': 'color', 'value': 3}]}], model_name='res.partner.category')

        self.assertIn('Missing required field', self.tool_messages(1)['call_1'])

    def test_missing_required_fields_skips_computed_fields(self):
        # Ruling 9: some core models have a `required=True`, `compute=...`,
        # `readonly=False` field on purpose (e.g. `crm.lead.name`,
        # `stage_id` on `crm.lead`/`project.task`) -- its value comes from
        # the compute method, not from the assistant directly, so it must
        # never be flagged as "missing". `mail.activity.plan.res_model_id`
        # (installed with the `mail` dependency) has exactly that shape;
        # checked at the pure-introspection level, no create/ACL involved.
        from ..tools import write_common as wc
        model = self.env['mail.activity.plan']
        field = model._fields['res_model_id']
        self.assertTrue(field.required)
        self.assertFalse(field.readonly)
        self.assertTrue(field.compute)

        self.assertNotIn('res_model_id', wc.missing_required_fields(model, {}))

    def test_readonly_field_is_a_tool_error(self):
        self.create_and_say([{'field_values': [
            {'field': 'name', 'value': 'Ro Co'}, {'field': 'create_date', 'value': '2020-01-01 00:00:00'},
        ]}])

        self.assertIn('cannot be set', self.tool_messages(1)['call_1'])

    def test_nonexistent_many2one_is_a_tool_error(self):
        missing_id = self.partner.id + 10 ** 6
        self.create_and_say([{'field_values': [
            {'field': 'name', 'value': 'Has Parent'}, {'field': 'parent_id', 'value': missing_id},
        ]}])

        self.assertIn('does not exist', self.tool_messages(1)['call_1'])

    def test_integrity_error_rolls_back_and_transaction_stays_usable(self):
        # A real DB-level violation, not a mocked one: `res.partner.name`
        # is NOT ORM-`required` (see above), so a record with no fields at
        # all sails through our own validation, defaults to `type='contact'`
        # and hits `res.partner`'s own `_check_name` CHECK constraint
        # ("CHECK (type='contact' AND name IS NOT NULL) OR type!='contact'")
        # at INSERT time -- a genuine `psycopg2.errors.CheckViolation`
        # (an `IntegrityError` subclass).
        self.queue_tool_calls([(
            'create_records',
            {'explanation': "x", 'model_name': 'res.partner', 'values': [{'field_values': []}]},
            'call_1',
        )])
        self.say(self.channel, 'Create it', self.user)
        self.queue_text('It failed.')

        self.resume(self.channel, {'kind': 'confirmation', 'value': 'confirm_once'}, self.user)

        self.assertIn('Tool call failed', self.tool_messages(1)['call_1'])
        self.assertIn('constraint', self.tool_messages(1)['call_1'])
        # The savepoint rollback leaves the transaction usable for later
        # work: a plain `search` and a follow-up `create` both still work.
        self.env['res.partner'].search([])
        after = self.env['res.partner'].create({'name': 'After Boom Co'})
        self.assertTrue(after.id)


class TestCreateRecordsDirectGuard(WriteToolCase):

    def test_unconfirmed_direct_call_never_creates_and_always_pauses(self):
        env = self.env(user=self.user)
        tool = env.ref('ow_ai.tool_create_records')
        ctx = tools_registry.ToolContext(env=env, agent_id=env.ref('ow_ai.agent_default').id)

        result = tools_registry.run_tool(tool, {
            'explanation': "x", 'model_name': 'res.partner',
            'values': [{'field_values': [{'field': 'name', 'value': 'Direct Co'}]}],
        }, ctx)

        self.assertTrue(result.success)
        self.assertIsNotNone(ctx.user_input_request)
        self.assertEqual(ctx.user_input_request['type'], 'confirmation')
        self.assertEqual(env['res.partner'].search_count([('name', '=', 'Direct Co')]), 0)


class TestOneToManyFieldsRejected(WriteToolCase):
    """One2many fields are always refused, regardless of ``readonly``.

    Odoo's ``One2many.write_batch`` implements ``unlink``/``clear``/``set``
    as deleting the removed lines outright when the inverse many2one has
    ``ondelete='cascade'`` (``bank_ids`` -> ``res.partner.bank``, itself
    write-blocklisted) and implements ``link``/``set`` by reparenting the
    linked lines to a different record -- both bypass
    ``check_model_access``'s write checks on the comodel entirely. Only a
    genuine many2many (``category_id``, already exercised by
    ``test_m2m_link_ids_link_existing_category``/
    ``test_x2m_commands_link_unlink_set_clear``) is ever accepted.
    """

    def _assert_one2many_field_refused(self, field_name, call_id='call_1'):
        self.queue_tool_calls([(
            'create_records',
            {'explanation': "x", 'model_name': 'res.partner', 'values': [{'field_values': [
                {'field': 'name', 'value': 'One2many Co'},
                {'field': field_name, 'x2m_link_ids': [self.partner.id]},
            ]}]},
            call_id,
        )])
        self.queue_text('Noted.')
        self.say(self.channel, 'Create it', self.user)

        self.assertIn('one2many fields cannot be set', self.tool_messages(1)[call_id])
        self.assertEqual(self.env['res.partner'].search_count([('name', '=', 'One2many Co')]), 0)

    def test_create_bank_ids_refused(self):
        self._assert_one2many_field_refused('bank_ids')

    def test_create_message_ids_refused(self):
        self._assert_one2many_field_refused('message_ids')

    def test_create_user_ids_refused(self):
        self._assert_one2many_field_refused('user_ids')

    def test_update_bank_ids_link_refused(self):
        self.queue_tool_calls([('update_records', {
            'explanation': "x",
            'updates': [{
                'model_name': 'res.partner', 'domain': [['id', '=', self.partner.id]],
                'changes': [{'field': 'bank_ids', 'x2m_commands': json.dumps({'link': [self.partner.id]})}],
            }],
        }, 'call_1')])
        self.queue_text('Noted.')
        self.say(self.channel, 'Do it', self.user)

        self.assertIn('one2many fields cannot be set', self.tool_messages(1)['call_1'])
        # Unaffected: nothing was reparented or deleted.
        self.assertFalse(self.partner.bank_ids)

    def test_update_bank_ids_clear_refused(self):
        self.queue_tool_calls([('update_records', {
            'explanation': "x",
            'updates': [{
                'model_name': 'res.partner', 'domain': [['id', '=', self.partner.id]],
                'changes': [{'field': 'bank_ids', 'x2m_commands': json.dumps({'clear': True})}],
            }],
        }, 'call_1')])
        self.queue_text('Noted.')
        self.say(self.channel, 'Do it', self.user)

        self.assertIn('one2many fields cannot be set', self.tool_messages(1)['call_1'])


class TestConfirmationGuardRollsBack(WriteToolCase):

    def test_write_tool_that_skips_confirmation_is_rolled_back(self):
        # A misbehaving write tool that writes and returns without ever
        # requesting confirmation or being called with
        # `tool_request_confirmed=True`: `run_tool`'s defensive guard must
        # catch it *and* roll back whatever it already wrote, since the
        # check now runs inside the same savepoint as the tool call itself.
        def sneaky_write(ctx):
            ctx.env['res.partner'].create({'name': 'Sneaky Co'})
            return "Done without asking."

        tool = self.register_builtin('test.sneaky_write', sneaky_write, 'sneaky_write', is_write=True)
        env = self.env(user=self.user)
        tool = env['ow.ai.tool'].browse(tool.id)
        ctx = tools_registry.ToolContext(env=env, agent_id=env.ref('ow_ai.agent_default').id)

        result = tools_registry.run_tool(tool, {}, ctx)

        self.assertFalse(result.success)
        self.assertIn('requires confirmation', result.response)
        self.assertFalse(env['res.partner'].search([('name', '=', 'Sneaky Co')]))


class TestUpdateRecordsValidation(WriteToolCase):

    def update_and_say(self, updates, call_id='call_1'):
        self.queue_tool_calls([('update_records', {'explanation': "x", 'updates': updates}, call_id)])
        self.queue_text('Noted.')
        self.say(self.channel, 'Update it', self.user)

    def test_trivial_domain_is_refused(self):
        self.update_and_say([{
            'model_name': 'res.partner', 'domain': [], 'changes': [{'field': 'name', 'value': 'x'}],
        }])

        self.assertIn('Refusing to update all records', self.tool_messages(1)['call_1'])
        self.assertEqual(self.session.loop_state, 'ready')

    def test_more_than_200_matches_is_refused(self):
        self.env['res.partner'].create([{'name': f'Bulk {i}'} for i in range(201)])

        self.update_and_say([{
            'model_name': 'res.partner', 'domain': [['name', 'like', 'Bulk %']],
            'changes': [{'field': 'name', 'value': 'renamed'}],
        }])

        self.assertIn('Too many records', self.tool_messages(1)['call_1'])

    def test_refuses_when_domain_matches_every_record_of_the_model(self):
        # `name` is required on `res.partner.category`, so `name != False`
        # matches literally every row of the model without being one of
        # `is_trivial_domain`'s hard-coded trivial-looking patterns --
        # exactly the case Ruling 10 hardens against. `color`'s own default
        # is random (1-11): pin it to a known value first.
        self.category.color = 0
        self.update_and_say([{
            'model_name': 'res.partner.category', 'domain': [['name', '!=', False]],
            'changes': [{'field': 'color', 'value': 5}],
        }])

        self.assertIn('Refusing to update all records of the model', self.tool_messages(1)['call_1'])
        self.assertEqual(self.category.color, 0)

    def test_refuses_when_domain_bypasses_the_active_filter_for_everything(self):
        # A domain that itself disables the implicit active filter (here,
        # by mentioning `active`) matches both active and archived records
        # -- literally every row of the model -- even though a naive
        # `model.search_count([])` (which still applies the *default*
        # active filter) would undercount and miss that: `len(records)`
        # would then exceed that count instead of equalling it. The
        # `active_test=False` "is anything left out" check catches this
        # regardless of which side the active filter distorts.
        # `color`'s own default is random (1-11): pin both records to a
        # known value first so the write-didn't-happen check below can't
        # coincidentally pass on a random default that happens to be 5.
        archived = self.env['res.partner.category'].create({'name': "Archived Tag", 'active': False, 'color': 0})
        self.category.color = 0
        self.update_and_say([{
            'model_name': 'res.partner.category', 'domain': [['active', 'in', [True, False]]],
            'changes': [{'field': 'color', 'value': 5}],
        }])

        self.assertIn('Refusing to update all records of the model', self.tool_messages(1)['call_1'])
        self.assertEqual(archived.color, 0)
        self.assertEqual(self.category.color, 0)

    def test_write_blocked_on_blocklisted_model(self):
        self.update_and_say([{
            'model_name': 'res.users', 'domain': [['id', '=', self.user.id]],
            'changes': [{'field': 'name', 'value': 'x'}],
        }])

        self.assertIn('Tool call failed', self.tool_messages(1)['call_1'])
        self.assertIn('write access', self.tool_messages(1)['call_1'])

    def test_write_denied_by_user_own_acl(self):
        # `res.partner.category` is read-only for plain `base.group_user`
        # users (only `base.group_partner_manager` may write it) -- a real
        # access-control denial distinct from our own WRITE_BLOCKLIST. Called
        # directly (not through the chat) to avoid needing channel
        # membership for a user who is otherwise irrelevant to this session.
        limited_user = new_test_user(self.env, login='ow_ai_write_limited', groups='base.group_user')
        env = self.env(user=limited_user)
        tool = env.ref('ow_ai.tool_update_records')
        ctx = tools_registry.ToolContext(env=env, agent_id=env.ref('ow_ai.agent_default').id)

        result = tools_registry.run_tool(tool, {
            'explanation': "x",
            'updates': [{
                'model_name': 'res.partner.category', 'domain': [['id', '=', self.category.id]],
                'changes': [{'field': 'name', 'value': 'Renamed'}],
            }],
        }, ctx)

        self.assertFalse(result.success)
        self.assertIn('Tool call failed', result.response)
        self.assertIn('write access', result.response)


class TestUpdateRecordsConfirmation(WriteToolCase):

    def setUp(self):
        super().setUp()
        self.target = self.env['res.partner'].create({'name': 'Old Name', 'email': 'old@example.com'})

    def update_call(self, changes, *, domain=None, call_id='call_1'):
        self.queue_tool_calls([('update_records', {
            'explanation': "Renaming the contact as requested.",
            'updates': [{
                'model_name': 'res.partner', 'domain': domain or [['id', '=', self.target.id]], 'changes': changes,
            }],
        }, call_id)])

    def test_preview_shows_old_and_new_value(self):
        self.update_call([{'field': 'name', 'value': 'New Name'}])

        self.say(self.channel, 'Rename it', self.user)

        card = self.agent_messages(self.channel)[-1]
        self.assertIn('Old Name', card.body)
        self.assertIn('New Name', card.body)
        self.assertEqual(self.target.name, 'Old Name')

    def test_confirm_writes_and_tracks_agent_as_author(self):
        self.env['bus.bus'].sudo().search([]).unlink()
        self.update_call([{'field': 'name', 'value': 'New Name'}])
        self.say(self.channel, 'Rename it', self.user)
        self.queue_text('Renamed.')

        self.resume(self.channel, {'kind': 'confirmation', 'value': 'confirm_once'}, self.user)

        self.assertEqual(self.target.name, 'New Name')
        response = json.loads(self.tool_messages(1)['call_1'])
        self.assertEqual(response['count'], 1)
        self.assertEqual(response['updated'][0]['id'], self.target.id)

        agent_partner = self.env.ref('ow_ai.agent_default').partner_id
        tracking = self.target.message_ids.filtered(lambda msg: msg.message_type == 'tracking')
        self.assertTrue(tracking)
        self.assertEqual(tracking[0].author_id, agent_partner)

        notifications = self.bus_client_notifications()
        self.assertEqual(notifications[0]['client_tools'], [{'name': 'reload', 'params': {}}])

    def test_x2m_commands_link_unlink_set_clear(self):
        other_category = self.env['res.partner.category'].create({'name': "Other"})
        self.target.category_id = self.category

        def apply(x2m_commands, call_id):
            # Exactly one turn per call: the tool call itself (paused for
            # confirmation, no second round yet) then, on confirm, exactly
            # one more model round for the final answer -- queuing more
            # than that would desync the FakeTransport's FIFO queue against
            # later calls in this same test.
            self.update_call(
                [{'field': 'category_id', 'x2m_commands': json.dumps(x2m_commands)}], call_id=call_id)
            self.say(self.channel, 'Change the tags', self.user)
            self.queue_text('Ok.')
            self.resume(self.channel, {'kind': 'confirmation', 'value': 'confirm_once'}, self.user)

        apply({'link': [other_category.id]}, 'call_1')
        self.assertEqual(set(self.target.category_id.ids), {self.category.id, other_category.id})

        apply({'unlink': [other_category.id]}, 'call_2')
        self.assertEqual(self.target.category_id, self.category)

        apply({'set': [other_category.id]}, 'call_3')
        self.assertEqual(self.target.category_id, other_category)

        apply({'clear': True}, 'call_4')
        self.assertFalse(self.target.category_id)

    def test_x2m_preview_shows_resulting_names_not_raw_command_ids(self):
        other_category = self.env['res.partner.category'].create({'name': "Other"})
        self.target.category_id = self.category

        self.update_call([{'field': 'category_id', 'x2m_commands': json.dumps({'link': [other_category.id]})}])
        self.say(self.channel, 'Add a tag', self.user)
        link_card = self.agent_messages(self.channel)[-1]
        # Old: just "VIP"; new: the resulting set "VIP, Other" -- not just
        # the linked id/name on its own.
        self.assertIn('VIP', link_card.body)
        self.assertIn('Other', link_card.body)
        self.queue_text('Ok, not doing that.')
        self.resume(self.channel, {'kind': 'confirmation', 'value': 'decline'}, self.user)

        self.update_call(
            [{'field': 'category_id', 'x2m_commands': json.dumps({'unlink': [self.category.id]})}],
            call_id='call_2')
        self.say(self.channel, 'Remove a tag', self.user)
        unlink_card = self.agent_messages(self.channel)[-1]
        # Old still shows "VIP" (unaffected by the declined link above);
        # new shows the empty result, not the unlinked id/name.
        self.assertIn('VIP', unlink_card.body)
        self.assertIn('—', unlink_card.body)

    def test_only_records_matched_at_preview_time_are_written(self):
        included = self.env['res.partner'].create({'name': 'Match A'})
        self.queue_tool_calls([('update_records', {
            'explanation': "x",
            'updates': [{
                'model_name': 'res.partner', 'domain': [['name', 'like', 'Match %']],
                'changes': [{'field': 'email', 'value': 'matched@example.com'}],
            }],
        }, 'call_1')])

        self.say(self.channel, 'Update matches', self.user)

        # A record that starts matching the same domain only after the
        # preview was shown (simulating a concurrent change) must not be
        # picked up by the confirmed write: only the ids pinned at preview
        # time are written.
        late = self.env['res.partner'].create({'name': 'Match B'})
        self.queue_text('Done.')

        self.resume(self.channel, {'kind': 'confirmation', 'value': 'confirm_once'}, self.user)

        self.assertEqual(included.email, 'matched@example.com')
        self.assertFalse(late.email)

    def test_declined_pin_is_not_reused_by_a_new_call_with_the_same_id(self):
        # The FakeTransport defaults an un-given call id to `call_{index}`
        # (`call_0` for a lone call), so two separate, un-given-id turns
        # naturally reuse the same call id -- exactly the "provider reuses
        # call ids" scenario the pin must be immune to.
        other = self.env['res.partner'].create({'name': 'Other Target', 'email': 'other@example.com'})
        self.queue_tool_calls([('update_records', {
            'explanation': "x",
            'updates': [{
                'model_name': 'res.partner', 'domain': [['id', '=', self.target.id]],
                'changes': [{'field': 'name', 'value': 'First New Name'}],
            }],
        })])
        self.say(self.channel, 'Rename it', self.user)
        self.queue_text('Ok, not doing that.')
        self.resume(self.channel, {'kind': 'confirmation', 'value': 'decline'}, self.user)

        # A brand new call, same default id, a different domain entirely:
        # must search fresh, never reuse the declined call's pinned ids.
        self.queue_tool_calls([('update_records', {
            'explanation': "x",
            'updates': [{
                'model_name': 'res.partner', 'domain': [['id', '=', other.id]],
                'changes': [{'field': 'name', 'value': 'Second New Name'}],
            }],
        })])
        self.say(self.channel, 'Now rename the other one', self.user)
        card = self.agent_messages(self.channel)[-1]
        self.assertIn('Other Target', card.body)
        self.assertNotIn('First New Name', card.body)

        self.queue_text('Done.')
        self.resume(self.channel, {'kind': 'confirmation', 'value': 'confirm_once'}, self.user)

        self.assertEqual(other.name, 'Second New Name')
        self.assertEqual(self.target.name, 'Old Name')

    def test_auto_confirm_with_reused_call_id_targets_the_new_domain_only(self):
        # `color`'s own default is random (1-11): pin both records first.
        # A second category also matters here for its own sake, not just
        # determinism: `res.partner.category` has no demo data in this
        # test database, so with only `self.category` in existence any
        # single-id domain against it would (correctly, but confusingly
        # for *this* test) also match literally every row of the model --
        # tripping the unrelated "domain matches everything" guard. With
        # two categories, each domain below targets a strict subset.
        other_category = self.env['res.partner.category'].create({'name': "Other Tag", 'color': 0})
        self.category.color = 0

        # First call (default id `call_0`, since the FakeTransport
        # defaults an ungiven id to `call_{index}`): pauses, then
        # declined -- its pin must never be reused.
        self.queue_tool_calls([('update_records', {
            'explanation': "x",
            'updates': [{
                'model_name': 'res.partner.category', 'domain': [['id', '=', self.category.id]],
                'changes': [{'field': 'color', 'value': 5}],
            }],
        })])
        self.say(self.channel, 'Recolor the VIP tag', self.user)
        self.queue_text('Ok, not doing that.')
        self.resume(self.channel, {'kind': 'confirmation', 'value': 'decline'}, self.user)
        self.assertEqual(self.category.color, 0)

        # Auto-confirm turned on independently of that call's own
        # confirmation (e.g. granted earlier in the chat for a different
        # tool): the *next* call therefore runs confirmed on its first and
        # only try -- it is never paused, so nothing is ever pinned for it.
        self.session.auto_confirm = True

        # A brand new call, same default id, aimed at a *different*
        # category: must search fresh and write only that record, never
        # reusing the declined call's pinned ids (which, if reused, would
        # silently recolor the VIP tag the user just said no to instead).
        self.queue_tool_calls([('update_records', {
            'explanation': "x",
            'updates': [{
                'model_name': 'res.partner.category', 'domain': [['id', '=', other_category.id]],
                'changes': [{'field': 'color', 'value': 7}],
            }],
        })])
        self.queue_text('Recolored the other tag.')

        self.say(self.channel, 'Now recolor the other tag', self.user)

        self.assertEqual(other_category.color, 7)
        self.assertEqual(self.category.color, 0)


class TestBooleanValues(WriteToolCase):
    """Audit finding 6: a boolean field's write value is parsed, not
    truthiness-tested -- ``bool("false")`` is ``True`` in plain Python, so
    the old ``normalize_scalar`` wrote the opposite of what was asked.
    Odoo 20's ``is_company`` is computed (read-only): the tests use
    ``employee``, a plain boolean of ``res.partner``."""

    def setUp(self):
        super().setUp()
        self.target = self.env['res.partner'].create({'name': 'Bool Co', 'employee': True})

    def update_call(self, value, call_id='call_1'):
        self.queue_tool_calls([('update_records', {
            'explanation': "x",
            'updates': [{
                'model_name': 'res.partner', 'domain': [['id', '=', self.target.id]],
                'changes': [{'field': 'employee', 'value': value}],
            }],
        }, call_id)])

    def confirm_update(self, value, call_id='call_1'):
        self.update_call(value, call_id=call_id)
        self.say(self.channel, 'Update it', self.user)
        self.queue_text('Done.')
        self.resume(self.channel, {'kind': 'confirmation', 'value': 'confirm_once'}, self.user)

    def test_python_false_is_parsed_as_false(self):
        self.confirm_update(False)
        self.assertFalse(self.target.employee)

    def test_string_false_is_parsed_as_false(self):
        self.confirm_update('false')
        self.assertFalse(self.target.employee)

    def test_string_zero_is_parsed_as_false(self):
        self.confirm_update('0')
        self.assertFalse(self.target.employee)

    def test_string_false_titlecase_is_parsed_as_false(self):
        self.confirm_update('False')
        self.assertFalse(self.target.employee)

    def test_python_true_is_parsed_as_true(self):
        self.target.employee = False
        self.confirm_update(True)
        self.assertTrue(self.target.employee)

    def test_string_yes_is_parsed_as_true(self):
        self.target.employee = False
        self.confirm_update('yes')
        self.assertTrue(self.target.employee)

    def test_update_invalid_boolean_string_is_an_llm_safe_error_and_does_not_write(self):
        self.update_call('maybe')
        self.queue_text('Noted.')

        self.say(self.channel, 'Update it', self.user)

        self.assertIn("'employee': expected true or false.", self.tool_messages(1)['call_1'])
        self.assertTrue(self.target.employee)
        self.assertEqual(self.session.loop_state, 'ready')

    def create_call(self, value, call_id='call_1'):
        self.queue_tool_calls([('create_records', {
            'explanation': "x", 'model_name': 'res.partner',
            'values': [{'field_values': [
                {'field': 'name', 'value': 'New Bool Co'}, {'field': 'employee', 'value': value},
            ]}],
        }, call_id)])

    def test_create_string_false_is_parsed_as_false(self):
        self.create_call('false')
        self.say(self.channel, 'Create it', self.user)
        self.queue_text('Created.')

        self.resume(self.channel, {'kind': 'confirmation', 'value': 'confirm_once'}, self.user)

        partner = self.env['res.partner'].search([('name', '=', 'New Bool Co')])
        self.assertEqual(len(partner), 1)
        self.assertFalse(partner.employee)

    def test_create_invalid_boolean_string_is_an_llm_safe_error_and_creates_nothing(self):
        self.create_call('maybe')
        self.queue_text('Noted.')

        self.say(self.channel, 'Create it', self.user)

        self.assertIn("'employee': expected true or false.", self.tool_messages(1)['call_1'])
        self.assertEqual(self.env['res.partner'].search_count([('name', '=', 'New Bool Co')]), 0)


class TestWriteToolsData(EngineLoopCase):

    def test_skills_linked_to_default_agent(self):
        agent = self.env.ref('ow_ai.agent_default')
        self.assertIn(self.env.ref('ow_ai.skill_create_records'), agent.skill_ids)
        self.assertIn(self.env.ref('ow_ai.skill_update_records'), agent.skill_ids)

    def test_tool_schemas_are_valid(self):
        from ..utils.schema import validate_schema
        for xmlid in ('ow_ai.tool_create_records', 'ow_ai.tool_update_records'):
            tool = self.env.ref(xmlid)
            validate_schema(tool._get_schema())
            self.assertTrue(tool.is_write)
            self.assertTrue(tool.requires_confirmation)
