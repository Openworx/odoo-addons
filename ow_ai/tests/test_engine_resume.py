# -*- coding: utf-8 -*-
"""Pausing a tool batch for the user (confirmation/question cards, client
tools), resuming it (``resume_pending``) and aborting it (``abort_pending``).

The real interaction tools arrive in Task 1.11/1.12; these tests drive the
engine's generic handling through temporary builtins that set
``ctx.user_input_request``/``ctx.client_tools`` themselves.
"""
import json

from odoo.exceptions import AccessError, UserError
from odoo.tests import new_test_user
from odoo.tools.mail import html2plaintext

from .test_engine_loop import EngineLoopCase

_CONFIRMATION = {
    'type': 'confirmation',
    'body': '<p>Create <b>Acme</b>?</p><img src="https://evil.example/x.png">',
    'choices': ['confirm_once', 'auto_confirm', 'decline'],
}
_QUESTION = {
    'type': 'question',
    'body': '<p>Which one?</p>',
    'choices': ['A', 'B', 'C'],
    'multi_select': False,
    'allow_free_text': False,
}


class ResumeCase(EngineLoopCase):

    def setUp(self):
        super().setUp()
        self.confirm_calls = []

        def confirm_tool(ctx):
            self.confirm_calls.append((ctx.tool_request_confirmed, ctx.tool_call_id))
            if not ctx.tool_request_confirmed:
                ctx.user_input_request = dict(_CONFIRMATION)
                return "Waiting for confirmation."
            return "Created Acme."

        def question_tool(ctx):
            ctx.user_input_request = dict(_QUESTION)
            return "Waiting for the user's answer."

        def client_tool(ctx):
            ctx.client_tools.append({'name': 'reload', 'params': {'full': True}})
            return "Reload requested."

        self.confirm_tool = self.register_builtin('test.confirm', confirm_tool, 'test_confirm', is_write=True)
        self.question_tool = self.register_builtin('test.question', question_tool, 'test_question')
        self.client_tool = self.register_builtin('test.client', client_tool, 'test_client')
        self.make_available(self.confirm_tool, self.question_tool, self.client_tool)

    def pause_on(self, tool_name, *extra_calls):
        calls = [(tool_name, {}, 'call_1'), *extra_calls]
        self.queue_tool_calls(calls)
        self.say(self.channel, 'Please do it', self.user)

    def user_messages(self):
        return self.env['mail.message'].sudo().search([
            ('model', '=', 'discuss.channel'), ('res_id', '=', self.channel.id),
            ('author_id', '=', self.user.partner_id.id),
        ], order='id asc')

    def last_user_text(self):
        return html2plaintext(self.user_messages()[-1].body or '')


class TestConfirmationPause(ResumeCase):

    def test_pause_state_and_card(self):
        self.pause_on('test_confirm')

        session = self.session
        self.assertEqual(session.loop_state, 'waiting_confirmation')
        self.assertTrue(session.resume_token)
        pending = session.pending_tool_call
        self.assertEqual(pending['index'], 0)
        self.assertEqual(pending['user_input_request']['type'], 'confirmation')
        self.assertEqual([call['call_id'] for call in pending['calls']], ['call_1'])
        # The card body is posted (sanitised) so the history shows it.
        card = self.agent_messages(self.channel)[-1]
        self.assertIn('o_ow_ai_input_card', card.body)
        self.assertIn('Create <b>Acme</b>?', card.body)
        self.assertNotIn('evil.example', card.body)
        self.assertEqual(self.typing[-1], (session.id, False))
        self.assertEqual(self.confirm_calls, [(False, 'call_1')])
        store_request = session._ow_ai_user_input_request()
        self.assertEqual(store_request['type'], 'confirmation')
        self.assertEqual(store_request['choices'], ['confirm_once', 'auto_confirm', 'decline'])
        self.assertEqual(store_request['resumeToken'], session.resume_token)

    def test_confirm_once_reruns_the_same_call_confirmed(self):
        self.pause_on('test_confirm')
        self.queue_text('Acme was created.')

        result = self.resume(self.channel, {'kind': 'confirmation', 'value': 'confirm_once'}, self.user)

        self.assertEqual(result, {'interactionConsumed': True, 'loop_state': 'ready'})
        self.assertEqual(self.confirm_calls, [(False, 'call_1'), (True, 'call_1')])
        self.assertEqual(self.tool_messages(1)['call_1'], 'Created Acme.')
        self.assertEqual(self.last_answer(self.channel), 'Acme was created.')
        self.assertEqual(self.last_user_text(), 'Yes, do it')
        self.assertFalse(self.session.resume_token)
        self.assertFalse(self.session.auto_confirm)

    def test_confirmation_labels_from_the_request(self):
        labelled_request = dict(_CONFIRMATION, labels={'confirm_once': 'Go ahead'})

        def labelled(ctx):
            if not ctx.tool_request_confirmed:
                ctx.user_input_request = dict(labelled_request)
            return "ok"

        tool = self.register_builtin('test.labelled', labelled, 'test_labelled')
        self.make_available(tool)
        self.pause_on('test_labelled')
        self.queue_text('Done.')

        self.resume(self.channel, {'kind': 'confirmation', 'value': 'confirm_once'}, self.user)

        self.assertEqual(self.last_user_text(), 'Go ahead')

    def test_resume_token_is_single_use(self):
        self.pause_on('test_confirm')
        token = self.session.resume_token
        self.queue_text('Done.')
        self.resume(self.channel, {'kind': 'confirmation', 'value': 'confirm_once'}, self.user, token=token)

        again = self.resume(self.channel, {'kind': 'confirmation', 'value': 'confirm_once'}, self.user, token=token)

        self.assertEqual(again, {'interactionConsumed': False, 'loop_state': 'ready'})
        self.assertEqual(len(self.confirm_calls), 2)

    def test_wrong_token_changes_nothing(self):
        self.pause_on('test_confirm')
        token = self.session.resume_token

        result = self.resume(self.channel, {'kind': 'confirmation', 'value': 'confirm_once'}, self.user,
                             token='not-the-token')

        self.assertEqual(result, {'interactionConsumed': False, 'loop_state': 'waiting_confirmation'})
        self.assertEqual(self.session.resume_token, token)
        self.assertEqual(len(self.confirm_calls), 1)

    def test_wrong_kind_for_state_is_not_consumed(self):
        self.pause_on('test_confirm')

        result = self.resume(self.channel, {'kind': 'client_result', 'value': {}}, self.user)

        self.assertFalse(result['interactionConsumed'])
        self.assertEqual(self.session.loop_state, 'waiting_confirmation')

    def test_invalid_confirmation_value(self):
        self.pause_on('test_confirm')

        with self.assertRaises(UserError):
            self.resume(self.channel, {'kind': 'confirmation', 'value': 'maybe'}, self.user)

        self.assertEqual(self.session.loop_state, 'waiting_confirmation')
        self.assertTrue(self.session.resume_token)

    def test_only_the_requester_can_resume(self):
        self.pause_on('test_confirm')
        other = new_test_user(self.env, login='ow_ai_resume_other', groups='base.group_user')

        with self.assertRaises(AccessError):
            self.resume(self.channel, {'kind': 'confirmation', 'value': 'confirm_once'}, other)

    def test_decline_reports_to_the_model_and_continues(self):
        self.pause_on('test_confirm')
        self.queue_text('Ok, I did not create it.')

        result = self.resume(self.channel, {'kind': 'confirmation', 'value': 'decline'}, self.user)

        self.assertTrue(result['interactionConsumed'])
        self.assertEqual(self.confirm_calls, [(False, 'call_1')])
        self.assertEqual(self.tool_messages(1)['call_1'], 'Error: The user declined this action.')
        self.assertEqual(self.last_answer(self.channel), 'Ok, I did not create it.')
        self.assertEqual(self.last_user_text(), 'No')

    def test_skip_on_confirmation_declines(self):
        self.pause_on('test_confirm')
        self.queue_text('Skipped.')

        self.resume(self.channel, {'kind': 'skip'}, self.user)

        self.assertEqual(self.tool_messages(1)['call_1'], 'Error: The user declined this action.')

    def test_auto_confirm_enables_auto_approval(self):
        self.pause_on('test_confirm', ('test_confirm', {}, 'call_2'))
        self.queue_text('Both created.')

        self.resume(self.channel, {'kind': 'confirmation', 'value': 'auto_confirm'}, self.user)

        self.assertTrue(self.session.auto_confirm)
        # The second call runs confirmed too, without a new card.
        self.assertEqual(self.confirm_calls, [(False, 'call_1'), (True, 'call_1'), (True, 'call_2')])
        notes = self.agent_messages(self.channel).filtered(
            lambda message: 'data-oe-type="ow_ai_note"' in (message.body or ''))
        self.assertEqual(len(notes), 1)
        self.assertIn('Auto-approval enabled for this chat', notes.body)
        self.assertEqual(self.session.loop_state, 'ready')

    def test_confirm_once_does_not_confirm_following_calls(self):
        self.pause_on('test_confirm', ('test_confirm', {}, 'call_2'))

        self.resume(self.channel, {'kind': 'confirmation', 'value': 'confirm_once'}, self.user)

        self.assertEqual(self.confirm_calls, [(False, 'call_1'), (True, 'call_1'), (False, 'call_2')])
        self.assertEqual(self.session.loop_state, 'waiting_confirmation')
        self.assertEqual(self.session.pending_tool_call['index'], 1)

    def test_new_message_aborts_the_pending_card(self):
        self.pause_on('test_confirm', ('test_question', {}, 'call_2'))
        self.queue_text('Fresh answer')

        self.say(self.channel, 'Never mind, something else', self.user)

        self.assertEqual(self.session.loop_state, 'ready')
        self.assertEqual(self.last_answer(self.channel), 'Fresh answer')
        tool_messages = self.tool_messages(1)
        self.assertEqual(tool_messages['call_1'], 'Error: Aborted: the user sent a new message.')
        self.assertEqual(tool_messages['call_2'], 'Error: Aborted: the user sent a new message.')
        roles = self.session.event_ids.sorted('sequence').mapped('role')
        self.assertEqual(roles, ['user', 'assistant', 'user', 'user', 'assistant'])
        self.assertEqual(len(self.confirm_calls), 1)


class TestQuestionPause(ResumeCase):

    def test_question_answer(self):
        self.pause_on('test_question')
        self.assertEqual(self.session.loop_state, 'waiting_answer')
        self.queue_text('You chose B.')

        result = self.resume(self.channel, {'kind': 'question', 'value': {'choices': ['B'], 'text': ''}}, self.user)

        self.assertTrue(result['interactionConsumed'])
        self.assertEqual(json.loads(self.tool_messages(1)['call_1']), {'answer': {'choices': ['B'], 'text': ''}})
        self.assertEqual(self.last_user_text(), 'B')
        self.assertEqual(self.last_answer(self.channel), 'You chose B.')

    def test_invalid_choice(self):
        self.pause_on('test_question')

        with self.assertRaises(UserError):
            self.resume(self.channel, {'kind': 'question', 'value': {'choices': ['Z'], 'text': ''}}, self.user)
        with self.assertRaises(UserError):
            self.resume(self.channel, {'kind': 'question', 'value': {'choices': ['A', 'B'], 'text': ''}}, self.user)
        with self.assertRaises(UserError):
            self.resume(self.channel, {'kind': 'question', 'value': {'choices': [], 'text': 'free'}}, self.user)
        with self.assertRaises(UserError):
            self.resume(self.channel, {'kind': 'question', 'value': {'choices': [], 'text': ''}}, self.user)

        self.assertEqual(self.session.loop_state, 'waiting_answer')

    def test_question_answer_as_plain_list(self):
        self.pause_on('test_question')
        self.queue_text('C it is.')

        self.resume(self.channel, {'kind': 'question', 'value': ['C']}, self.user)

        self.assertEqual(json.loads(self.tool_messages(1)['call_1']), {'answer': {'choices': ['C'], 'text': ''}})

    def test_free_text_answer_when_allowed(self):
        def open_question(ctx):
            ctx.user_input_request = dict(_QUESTION, multi_select=True, allow_free_text=True)
            return "Waiting."

        tool = self.register_builtin('test.open_question', open_question, 'test_open_question')
        self.make_available(tool)
        self.pause_on('test_open_question')
        self.queue_text('Noted.')

        self.resume(self.channel, {'kind': 'question', 'value': {'choices': ['A', 'C'], 'text': 'and D'}}, self.user)

        self.assertEqual(
            json.loads(self.tool_messages(1)['call_1']), {'answer': {'choices': ['A', 'C'], 'text': 'and D'}})
        self.assertEqual(self.last_user_text(), 'A, C — and D')

    def test_skip_question(self):
        self.pause_on('test_question')
        self.queue_text('Skipped then.')

        self.resume(self.channel, {'kind': 'skip'}, self.user)

        self.assertEqual(self.tool_messages(1)['call_1'], 'The user skipped the question.')
        self.assertEqual(self.last_user_text(), '(skipped)')


class TestClientToolPause(ResumeCase):

    def test_client_tool_pause_and_result(self):
        self.env['bus.bus'].sudo().search([]).unlink()

        self.pause_on('test_client')

        session = self.session
        self.assertEqual(session.loop_state, 'waiting_client_result')
        self.assertEqual(session.pending_tool_call['client_tool'], {'name': 'reload', 'params': {'full': True}})
        self.env.cr.precommit.run()
        notifications = [
            json.loads(bus.message) for bus in self.env['bus.bus'].sudo().search([])
            if 'ow_ai.session/client_tools' in bus.message
        ]
        self.assertEqual(len(notifications), 1)
        payload = notifications[0]['payload']
        self.assertEqual(payload['session_id'], session.id)
        self.assertEqual(payload['resume_token'], session.resume_token)
        self.assertEqual(payload['client_tools'], [{'name': 'reload', 'params': {'full': True}}])

        self.queue_text('Reloaded.')
        result = self.resume(self.channel, {'kind': 'client_result', 'value': {'reloaded': True}}, self.user)

        self.assertTrue(result['interactionConsumed'])
        self.assertEqual(json.loads(self.tool_messages(1)['call_1']), {'reloaded': True})
        self.assertEqual(self.last_answer(self.channel), 'Reloaded.')

    def test_client_tools_notification_names_the_requesting_tab(self):
        """The browser tab that sent the message runs the tools (`client_identifier`)."""
        self.env['bus.bus'].sudo().search([]).unlink()
        self.queue_tool_calls([('test_client', {}, 'call_1')])

        self.say(self.channel, 'Please do it', self.user, extra_context={'client_identifier': 'tab-1'})

        self.assertEqual(self.session.request_context['client_identifier'], 'tab-1')
        payload = self.client_tools_payloads()[-1]
        self.assertEqual(payload['client_identifier'], 'tab-1')
        self.assertEqual(payload['resume_token'], self.session.resume_token)

    def test_client_tools_notification_without_tab_lets_any_tab_act(self):
        self.env['bus.bus'].sudo().search([]).unlink()

        self.pause_on('test_client')

        self.assertIs(self.client_tools_payloads()[-1]['client_identifier'], False)

    def test_non_blocking_client_tools_name_the_requesting_tab(self):
        def notify_reload(ctx):
            ctx.client_notifications.append({'name': 'reload', 'args': {}})
            return "Reload requested."

        tool = self.register_builtin('test.notify_reload', notify_reload, 'test_notify_reload')
        self.make_available(tool)
        self.env['bus.bus'].sudo().search([]).unlink()
        self.queue_tool_calls([('test_notify_reload', {}, 'call_1')])
        self.queue_text('Reloaded.')

        self.say(self.channel, 'Please reload', self.user, extra_context={'client_identifier': 'tab-2'})

        payload = self.client_tools_payloads()[-1]
        self.assertIs(payload['blocking'], False)
        self.assertEqual(payload['client_identifier'], 'tab-2')

    def test_resume_names_the_answering_tab(self):
        """A card answered in another tab moves the turn's client tools to that tab."""
        self.env['bus.bus'].sudo().search([]).unlink()
        self.queue_tool_calls([('test_confirm', {}, 'call_1'), ('test_client', {}, 'call_2')])
        self.say(self.channel, 'Please do it', self.user, extra_context={'client_identifier': 'tab-1'})
        self.assertEqual(self.session.loop_state, 'waiting_confirmation')

        self.resume(self.channel, {'kind': 'confirmation', 'value': 'confirm_once'}, self.user,
                    client_identifier='tab-2')

        self.assertEqual(self.session.loop_state, 'waiting_client_result')
        self.assertEqual(self.session.request_context['client_identifier'], 'tab-2')
        payload = self.client_tools_payloads()[-1]
        self.assertEqual(payload['client_identifier'], 'tab-2')
        self.assertEqual(payload['resume_token'], self.session.resume_token)

    def test_resume_without_tab_keeps_the_requesting_tab(self):
        self.env['bus.bus'].sudo().search([]).unlink()
        self.queue_tool_calls([('test_confirm', {}, 'call_1'), ('test_client', {}, 'call_2')])
        self.say(self.channel, 'Please do it', self.user, extra_context={'client_identifier': 'tab-1'})

        self.resume(self.channel, {'kind': 'confirmation', 'value': 'confirm_once'}, self.user)

        self.assertEqual(self.client_tools_payloads()[-1]['client_identifier'], 'tab-1')

    def test_unconsumed_resume_does_not_change_the_tab(self):
        self.queue_tool_calls([('test_confirm', {}, 'call_1')])
        self.say(self.channel, 'Please do it', self.user, extra_context={'client_identifier': 'tab-1'})

        result = self.resume(self.channel, {'kind': 'confirmation', 'value': 'confirm_once'}, self.user,
                             token='not-the-token', client_identifier='tab-2')

        self.assertFalse(result['interactionConsumed'])
        self.assertEqual(self.session.request_context['client_identifier'], 'tab-1')

    def client_tools_payloads(self):
        self.env.cr.precommit.run()
        return [
            json.loads(bus.message)['payload'] for bus in self.env['bus.bus'].sudo().search([], order='id asc')
            if 'ow_ai.session/client_tools' in bus.message
        ]

    def test_client_error(self):
        self.pause_on('test_client')
        self.queue_text('It failed.')

        self.resume(self.channel, {'kind': 'client_error', 'value': 'no view'}, self.user)

        self.assertIn('no view', self.tool_messages(1)['call_1'])
        self.assertEqual(self.session.loop_state, 'ready')
