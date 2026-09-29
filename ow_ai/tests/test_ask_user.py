# -*- coding: utf-8 -*-
"""The real ``ask_user_question``/``load_skills`` built-in tools
(``tools/interaction.py``): the question-pause/resume flow end to end, the
tools' own argument validation, and where each tool is (not) offered.

The engine's generic pause/resume mechanics (confirmation cards, wrong
token, single-use token, ...) are already covered against fake builtins in
``test_engine_resume.py``; this file exercises the same flow through the
production ``ask_user_question``/``load_skills`` implementations.
"""
from __future__ import annotations

import json

from odoo.exceptions import UserError
from odoo.tools.mail import html2plaintext

from .test_engine_loop import EngineLoopCase


class AskUserCase(EngineLoopCase):

    def setUp(self):
        super().setUp()
        self.make_available('ow_ai.tool_ask_user_question')

    def ask(self, choices=('A', 'B', 'C'), *, multi_select=False, allow_free_text=False, call_id='call_1'):
        self.queue_tool_calls([(
            'ask_user_question',
            {
                'question': 'Which one do you mean?',
                'choices': list(choices),
                'multi_select': multi_select,
                'allow_free_text': allow_free_text,
            },
            call_id,
        )])
        self.say(self.channel, 'Please pick', self.user)

    def user_messages(self):
        return self.env['mail.message'].sudo().search([
            ('model', '=', 'discuss.channel'), ('res_id', '=', self.channel.id),
            ('author_id', '=', self.user.partner_id.id),
        ], order='id asc')

    def last_user_text(self):
        return html2plaintext(self.user_messages()[-1].body or '')


class TestQuestionPause(AskUserCase):

    def test_pause_state_card_and_store(self):
        self.ask()

        session = self.session
        self.assertEqual(session.loop_state, 'waiting_answer')
        self.assertTrue(session.resume_token)
        pending = session.pending_tool_call
        self.assertEqual(pending['user_input_request']['type'], 'question')
        self.assertEqual(pending['user_input_request']['choices'], ['A', 'B', 'C'])

        card = self.agent_messages(self.channel)[-1]
        self.assertIn('o_ow_ai_input_card', card.body)
        self.assertIn('Which one do you mean?', card.body)
        self.assertEqual(self.typing[-1], (session.id, False))

        store_request = session._ow_ai_user_input_request()
        self.assertEqual(store_request['type'], 'question')
        self.assertEqual(store_request['choices'], ['A', 'B', 'C'])
        self.assertFalse(store_request['multiSelect'])
        self.assertFalse(store_request['allowFreeText'])
        self.assertEqual(store_request['resumeToken'], session.resume_token)

    def test_valid_single_choice_answer(self):
        self.ask()
        self.queue_text('Got it, B it is.')

        result = self.resume(self.channel, {'kind': 'question', 'value': {'choices': ['B'], 'text': ''}}, self.user)

        self.assertEqual(result, {'interactionConsumed': True, 'loop_state': 'ready'})
        self.assertEqual(self.last_user_text(), 'B')
        tool_message = self.tool_messages(1)['call_1']
        self.assertEqual(json.loads(tool_message), {'answer': {'choices': ['B'], 'text': ''}})
        self.assertEqual(self.last_answer(self.channel), 'Got it, B it is.')
        self.assertEqual(self.session.loop_state, 'ready')

    def test_multi_select_two_choices(self):
        self.ask(multi_select=True)
        self.queue_text('Noted.')

        result = self.resume(
            self.channel, {'kind': 'question', 'value': {'choices': ['A', 'C'], 'text': ''}}, self.user)

        self.assertTrue(result['interactionConsumed'])
        self.assertEqual(
            json.loads(self.tool_messages(1)['call_1']), {'answer': {'choices': ['A', 'C'], 'text': ''}})

    def test_invalid_choice_raises_and_leaves_session_unchanged(self):
        self.ask()
        token = self.session.resume_token

        with self.assertRaises(UserError):
            self.resume(self.channel, {'kind': 'question', 'value': {'choices': ['Nope'], 'text': ''}}, self.user)

        self.assertEqual(self.session.loop_state, 'waiting_answer')
        self.assertEqual(self.session.resume_token, token)

    def test_free_text_refused_when_not_allowed(self):
        self.ask()

        with self.assertRaises(UserError):
            self.resume(self.channel, {'kind': 'question', 'value': {'choices': [], 'text': 'something else'}},
                        self.user)

        self.assertEqual(self.session.loop_state, 'waiting_answer')

    def test_free_text_accepted_when_allowed(self):
        self.ask(allow_free_text=True)
        self.queue_text('Understood.')

        self.resume(self.channel, {'kind': 'question', 'value': {'choices': [], 'text': 'my own answer'}}, self.user)

        self.assertEqual(
            json.loads(self.tool_messages(1)['call_1']), {'answer': {'choices': [], 'text': 'my own answer'}})
        self.assertEqual(self.last_user_text(), 'my own answer')

    def test_empty_answer_raises(self):
        self.ask()

        with self.assertRaises(UserError):
            self.resume(self.channel, {'kind': 'question', 'value': {'choices': [], 'text': ''}}, self.user)

    def test_skip_reports_skipped_and_continues(self):
        self.ask()
        self.queue_text('No problem.')

        result = self.resume(self.channel, {'kind': 'skip'}, self.user)

        self.assertTrue(result['interactionConsumed'])
        self.assertEqual(self.tool_messages(1)['call_1'], 'The user skipped the question.')
        self.assertEqual(self.last_user_text(), '(skipped)')
        self.assertEqual(self.session.loop_state, 'ready')

    def test_wrong_resume_token_not_consumed(self):
        self.ask()
        token = self.session.resume_token

        result = self.resume(
            self.channel, {'kind': 'question', 'value': {'choices': ['A'], 'text': ''}}, self.user,
            token='definitely-not-it')

        self.assertEqual(result, {'interactionConsumed': False, 'loop_state': 'waiting_answer'})
        self.assertEqual(self.session.resume_token, token)

    def test_reusing_the_token_after_success_is_not_consumed(self):
        self.ask()
        token = self.session.resume_token
        self.queue_text('Ok.')
        self.resume(self.channel, {'kind': 'question', 'value': {'choices': ['A'], 'text': ''}}, self.user,
                    token=token)

        again = self.resume(
            self.channel, {'kind': 'question', 'value': {'choices': ['B'], 'text': ''}}, self.user, token=token)

        self.assertEqual(again, {'interactionConsumed': False, 'loop_state': 'ready'})


class TestQuestionArgumentValidation(AskUserCase):

    def test_too_few_choices_is_a_tool_error_not_a_pause(self):
        self.queue_tool_calls([(
            'ask_user_question',
            {'question': 'Sure?', 'choices': ['Only one'], 'multi_select': False, 'allow_free_text': False},
            'call_1',
        )])
        self.queue_text('Never mind.')

        self.say(self.channel, 'Please pick', self.user)

        self.assertIn('Invalid arguments', self.tool_messages(1)['call_1'])
        self.assertEqual(self.session.loop_state, 'ready')
        self.assertEqual(self.last_answer(self.channel), 'Never mind.')

    def test_too_many_choices_is_a_tool_error_not_a_pause(self):
        self.queue_tool_calls([(
            'ask_user_question',
            {'question': 'Sure?', 'choices': ['A', 'B', 'C', 'D', 'E'],
             'multi_select': False, 'allow_free_text': False},
            'call_1',
        )])
        self.queue_text('Never mind.')

        self.say(self.channel, 'Please pick', self.user)

        self.assertIn('Invalid arguments', self.tool_messages(1)['call_1'])
        self.assertEqual(self.session.loop_state, 'ready')

    def test_duplicate_choices_collapse_below_the_minimum(self):
        self.queue_tool_calls([(
            'ask_user_question',
            {'question': 'Sure?', 'choices': ['A', 'A'], 'multi_select': False, 'allow_free_text': False},
            'call_1',
        )])
        self.queue_text('Never mind.')

        self.say(self.channel, 'Please pick', self.user)

        self.assertIn('Tool call failed', self.tool_messages(1)['call_1'])
        self.assertEqual(self.session.loop_state, 'ready')


class TestLoadSkills(EngineLoopCase):

    def setUp(self):
        super().setUp()
        self.make_available('ow_ai.tool_load_skills')
        self.skill = self.env.ref('ow_ai.skill_search_database')

    def test_loads_allowed_skill_and_ignores_unknown_id(self):
        unknown_id = self.skill.id + 10 ** 6
        self.queue_tool_calls([('load_skills', {'skill_ids': [self.skill.id, unknown_id]}, 'call_1')])
        self.queue_text('Ready.')

        self.say(self.channel, 'Load what you need', self.user)

        tool_message = self.tool_messages(1)["call_1"]
        self.assertIn(f'## Skill: {self.skill.name}', tool_message)
        self.assertIn('Tools now available:', tool_message)
        self.assertIn('not available', tool_message.lower())
        self.assertIn(str(unknown_id), tool_message)

        self.assertEqual(self.session.state.get('loaded_skills'), [self.skill.id])
        # The skill's tools are added on top of whatever was already
        # available (here: the agent's own default tools, plus load_skills
        # itself, made available in setUp).
        self.assertTrue(set(self.skill.tool_ids.ids) <= set(self.session.state.get('available_tools') or []))

    def test_next_round_tools_and_instructions_include_the_loaded_skill(self):
        self.queue_tool_calls([('load_skills', {'skill_ids': [self.skill.id]}, 'call_1')])
        self.queue_text('Ready.')

        self.say(self.channel, 'Load what you need', self.user)

        request = self.request_json(1)
        instructions = request['messages'][0]['content']
        self.assertIn(f'### Loaded skill: {self.skill.name}', instructions)
        tool_names = self.request_tool_names(1)
        for tool in self.skill.tool_ids:
            self.assertIn(tool.tool_name, tool_names)

    def test_skill_not_linked_to_the_agent_cannot_be_loaded(self):
        outside_skill = self.env['ow.ai.skill'].create({
            'name': "Outside Skill", 'description': "Not offered to this agent",
        })
        self.queue_tool_calls([('load_skills', {'skill_ids': [outside_skill.id]}, 'call_1')])
        self.queue_text('Ok.')

        self.say(self.channel, 'Load it', self.user)

        tool_message = self.tool_messages(1)["call_1"]
        self.assertIn('not available', tool_message.lower())
        self.assertIn(str(outside_skill.id), tool_message)
        self.assertEqual(self.session.state.get('loaded_skills'), [])


class TestAskUserQuestionAvailability(EngineLoopCase):

    def test_present_in_chat_mode(self):
        self.make_available('ow_ai.tool_ask_user_question')

        names = {tool['name'] for tool in self.session._prepare_tools()}

        self.assertIn('ask_user_question', names)

    def test_absent_in_direct_automation_mode(self):
        session = self.env['ow.ai.session'].create({'agent_id': self.env.ref('ow_ai.agent_default').id})
        state = dict(session.state or {})
        state['available_tools'] = [self.env.ref('ow_ai.tool_ask_user_question').id]
        session.state = state

        names = {tool['name'] for tool in session._prepare_tools()}

        self.assertNotIn('ask_user_question', names)
        self.assertFalse(session.channel_id)
