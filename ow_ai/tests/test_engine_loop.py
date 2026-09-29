# -*- coding: utf-8 -*-
"""The stateful chat loop (``engine/loop.py`` + ``engine/tool_batch.py``),
driven end-to-end through ``OwAiCase.say()`` with inline jobs."""
import json
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import new_test_user
from odoo.tools import mute_logger

from ..engine import tools_registry
from ..provider.errors import AuthError
from ..utils.prompts import CHANNEL_TITLE_INSTRUCTIONS
from .common import OwAiCase


class EngineLoopCase(OwAiCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = new_test_user(cls.env, login='ow_ai_loop_user', groups='base.group_user')
        cls.partner = cls.env['res.partner'].create({'name': "Loop Testing BV", 'city': "Utrecht"})

    def setUp(self):
        super().setUp()
        self.typing = []
        session_cls = self.registry['ow.ai.session']
        original_notify_typing = session_cls._notify_typing

        def record_typing(session, is_typing):
            self.typing.append((session.id, is_typing))
            return original_notify_typing(session, is_typing)

        self.startPatcher(patch.object(session_cls, '_notify_typing', record_typing))
        self.channel = self.open_chat(self.user)
        self.session = self.chat_session(self.channel)

    # -- helpers ------------------------------------------------------------

    def register_builtin(self, key, func, tool_name, *, is_write=False, schema=None):
        """Register a temporary builtin under ``key`` and an ``ow.ai.tool`` for it."""
        spec = tools_registry.ToolSpec(key=key, func=func, is_write=is_write)
        self.startPatcher(patch.dict(tools_registry._REGISTRY, {key: spec}))
        return self.env['ow.ai.tool'].create({
            'name': tool_name.replace('_', ' ').title(),
            'tool_name': tool_name,
            'kind': 'builtin',
            'builtin_key': key,
            'is_write': is_write,
            'description': f"Test tool {tool_name}.",
            'schema': json.dumps(schema or {'type': 'object', 'properties': {}}),
        })

    def make_available(self, *tools, session=None):
        """Add ``tools`` (records or xmlids) to the session's available tools."""
        session = session or self.session
        ids = [self.env.ref(tool).id if isinstance(tool, str) else tool.id for tool in tools]
        state = dict(session.state or {})
        state['available_tools'] = list(state.get('available_tools') or []) + ids
        session.state = state

    def request_json(self, index):
        return self.transport.requests[index]['json']

    def tool_messages(self, request_index):
        messages = self.request_json(request_index)['messages']
        return {message['tool_call_id']: message['content'] for message in messages if message['role'] == 'tool'}

    def user_texts(self, request_index):
        """All text sent in ``role: user`` messages of a request, joined."""
        texts = []
        for message in self.request_json(request_index)['messages']:
            if message['role'] != 'user':
                continue
            content = message['content']
            if isinstance(content, str):
                texts.append(content)
            else:
                texts.extend(part.get('text', '') for part in content if part.get('type') == 'text')
        return '\n'.join(texts)

    def request_tool_names(self, request_index):
        return {tool['function']['name'] for tool in self.request_json(request_index).get('tools') or []}


class TestTextTurn(EngineLoopCase):

    def test_text_only_turn(self):
        self.queue_text('Hello **there**')

        job = self.say(self.channel, 'Hi', self.user)

        self.assertEqual(job.state, 'done')
        answers = self.agent_messages(self.channel)
        self.assertEqual(len(answers), 1)
        self.assertIn('<p>', answers.body)
        self.assertIn('<strong>there</strong>', answers.body)
        self.assertEqual(answers.message_type, 'comment')
        self.assertFalse(answers.is_internal)
        self.assertEqual(self.last_answer(self.channel), 'Hello *there*')

        self.assertEqual(self.session.loop_state, 'ready')
        self.assertEqual(self.session.request_round, 0)
        self.assertFalse(self.session.request_user_id)
        self.assertFalse(self.session.pending_tool_call)
        self.assertFalse(self.session.last_error)

        usage = self.env['ow.ai.usage'].search([('session_id', '=', self.session.id)])
        self.assertEqual(usage.mapped('kind'), ['chat'])
        self.assertEqual(usage.user_id, self.user)

        self.assertIn((self.session.id, True), self.typing)
        self.assertEqual(self.typing[-1], (self.session.id, False))

        events = self.session.event_ids.sorted('sequence')
        self.assertEqual(events.mapped('role'), ['user', 'assistant'])
        self.assertEqual(events[1].usage_id, usage)
        # The context block is added to the request only, never persisted.
        self.assertEqual(events[0].metadata['content'], [{'type': 'text', 'text': 'Hi'}])

    def test_request_carries_context_block_and_default_tools(self):
        self.queue_text('Hello')

        self.say(self.channel, 'Hi', self.user)

        texts = self.user_texts(0)
        self.assertIn('Hi', texts)
        self.assertIn('<odoo_context>', texts)
        self.assertIn(self.user.name, texts)
        system = self.request_json(0)['messages'][0]
        self.assertEqual(system['role'], 'system')
        self.assertIn('## Protocol', system['content'])
        self.assertEqual(self.request_tool_names(0), {'load_skills', 'ask_user_question'})
        state = self.session.state
        self.assertEqual(state['loaded_skills'], [])
        self.assertEqual(
            set(state['available_tools']), set(self.env.ref('ow_ai.agent_default')._get_default_tools().ids))

    def test_record_bound_chat_sends_record_snapshot(self):
        channel = self.open_chat(self.user, res_model='res.partner', res_id=self.partner.id)
        self.queue_text('It is a contact.')

        self.say(channel, 'What is this?', self.user)

        texts = self.user_texts(0)
        self.assertIn(f'record: res.partner/{self.partner.id}/Loop Testing BV', texts)
        self.assertIn('Utrecht', texts)
        self.assertIn('## Record', self.request_json(0)['messages'][0]['content'])

    def test_session_config_applied(self):
        self.queue_text('ok')

        self.say(self.channel, 'Hi', self.user, session_config={'auto_confirm': True, 'show_agent_steps': False})

        self.assertTrue(self.session.auto_confirm)
        self.assertFalse(self.session.show_agent_steps)

    def test_busy_session_refuses_new_message(self):
        self.session.write({
            'loop_state': 'waiting_model', 'request_round': 1, 'request_round_limit': 5,
            'request_user_id': self.user.id,
        })

        with self.assertRaises(UserError):
            self.say(self.channel, 'Hello?', self.user)

    def test_failure_sets_last_error_and_apologises(self):
        self.queue_error(401, 'bad key')

        job = self.say(self.channel, 'Hi', self.user, expect_failure=True)

        self.assertEqual(job.state, 'failed')
        self.assertEqual(self.session.loop_state, 'ready')
        self.assertEqual(self.session.last_error, AuthError.user_message)
        self.assertEqual(
            self.last_answer(self.channel), f"I could not complete this request: {AuthError.user_message}")
        self.assertEqual(self.typing[-1], (self.session.id, False))


class TestToolRounds(EngineLoopCase):

    def test_two_tool_calls_then_answer(self):
        self.make_available('ow_ai.tool_search', 'ow_ai.tool_read_group')
        self.queue_tool_calls([
            ('search', {'model_name': 'res.partner', 'domain': [['id', '=', self.partner.id]]}, 'call_1'),
            ('read_group', {'model_name': 'res.partner', 'domain': [], 'groupby': ['city']}, 'call_2'),
        ], text='Let me look that up.')
        self.queue_text('Found it.')

        self.say(self.channel, 'Find Loop Testing', self.user)

        self.assertEqual(len(self.transport.requests), 2)
        tool_messages = self.tool_messages(1)
        self.assertIn('Loop Testing BV', tool_messages['call_1'])
        self.assertIn('call_2', tool_messages)

        messages = self.agent_messages(self.channel)
        summaries = messages.filtered(lambda message: 'o_ow_ai_tool_summary' in (message.body or ''))
        self.assertEqual(len(summaries), 2)
        self.assertEqual(set(summaries.mapped('message_type')), {'notification'})
        self.assertIn('data-id="call_1"', summaries[0].body)
        self.assertIn('data-id="call_2"', summaries[1].body)
        steps = messages.filtered(lambda message: 'o_ow_ai_agent_step' in (message.body or ''))
        self.assertEqual(len(steps), 1)
        self.assertTrue(steps.is_internal)
        self.assertIn('Let me look that up.', steps.body)
        self.assertEqual(self.last_answer(self.channel), 'Found it.')

        events = self.session.event_ids.sorted('sequence')
        self.assertEqual(events.mapped('role'), ['user', 'assistant', 'user', 'assistant'])
        results = [part for part in events[2].metadata['content'] if part['type'] == 'tool_result']
        self.assertEqual([part['tool_call_id'] for part in results], ['call_1', 'call_2'])
        self.assertIn(f'data-oe-id="{events[1].id}"', summaries[0].body)

        jobs = self.session.job_ids
        self.assertEqual(jobs.mapped('state'), ['done', 'done'])
        self.assertEqual(self.session.loop_state, 'ready')
        self.assertEqual(self.typing[-1], (self.session.id, False))

    def test_tool_error_reaches_the_model(self):
        self.make_available('ow_ai.tool_search')
        self.queue_tool_calls([('search', {'model_name': 'no.such.model'}, 'call_1')])
        self.queue_text('Sorry.')

        self.say(self.channel, 'Search nonsense', self.user)

        self.assertTrue(self.tool_messages(1)['call_1'].startswith('Error: Tool call failed'))
        self.assertEqual(self.last_answer(self.channel), 'Sorry.')

    def test_unknown_tool(self):
        self.queue_tool_calls([('does_not_exist', {}, 'call_1')])
        self.queue_text('Hmm.')

        self.say(self.channel, 'Do it', self.user)

        self.assertIn('Unknown tool', self.tool_messages(1)['call_1'])

    def test_skill_tool_not_loaded_is_unknown(self):
        # `search` belongs to a skill: it is not usable before load_skills.
        self.queue_tool_calls([('search', {'model_name': 'res.partner'}, 'call_1')])
        self.queue_text('Hmm.')

        self.say(self.channel, 'Search', self.user)

        self.assertIn('Unknown tool', self.tool_messages(1)['call_1'])

    def test_too_many_tool_calls(self):
        self.env['ir.config_parameter'].sudo().set_int('ow_ai.max_tool_calls_per_round', 1)
        self.make_available('ow_ai.tool_search')
        self.queue_tool_calls([
            ('search', {'model_name': 'res.partner'}, 'call_1'),
            ('search', {'model_name': 'res.partner'}, 'call_2'),
        ])
        self.queue_text('done')

        self.say(self.channel, 'Do it', self.user)

        tool_messages = self.tool_messages(1)
        self.assertNotIn('Too many tool calls', tool_messages['call_1'])
        self.assertEqual(tool_messages['call_2'], "Error: Too many tool calls in one step; call at most 1.")

    def test_round_limit(self):
        self.env['ir.config_parameter'].sudo().set_int('ow_ai.max_rounds', 2)
        self.make_available('ow_ai.tool_search')
        self.queue_tool_calls([('search', {'model_name': 'res.partner'}, 'call_1')])
        self.queue_tool_calls([('search', {'model_name': 'res.partner'}, 'call_2')])

        self.say(self.channel, 'Search forever', self.user)

        self.assertEqual(len(self.transport.requests), 2)
        self.assertIn('You have 1 steps left; wrap up.', self.user_texts(1))
        self.assertIn('maximum number of steps', self.last_answer(self.channel))
        self.assertEqual(self.session.loop_state, 'ready')
        # The last round's tool call (not `ask_user_question`) is refused
        # outright -- there is no further round for the model to read its
        # result -- so no tool-results event is appended for it.
        events = self.session.event_ids.sorted('sequence')
        self.assertEqual(events.mapped('role'), ['user', 'assistant', 'user', 'assistant'])

    def test_round_limit_still_runs_ask_user_question(self):
        self.env['ir.config_parameter'].sudo().set_int('ow_ai.max_rounds', 1)
        self.make_available('ow_ai.tool_ask_user_question')
        self.queue_tool_calls([(
            'ask_user_question',
            {'question': 'Which one?', 'choices': ['A', 'B'], 'multi_select': False, 'allow_free_text': False},
            'call_1',
        )])

        self.say(self.channel, 'Do it', self.user)

        self.assertEqual(len(self.transport.requests), 1)
        self.assertEqual(self.session.loop_state, 'waiting_answer')

    def test_loaded_skills_persist_across_rounds_and_turns(self):
        def fake_load_skills(ctx, skill_ids):
            skills = ctx.env['ow.ai.skill'].sudo().browse(skill_ids)
            ctx.state['loaded_skills'] = list(dict.fromkeys((ctx.state.get('loaded_skills') or []) + skill_ids))
            ctx.state['available_tools'] = list(dict.fromkeys(
                (ctx.state.get('available_tools') or []) + skills.tool_ids.ids))
            return "Loaded."

        spec = tools_registry.ToolSpec(key='interaction.load_skills', func=fake_load_skills)
        self.startPatcher(patch.dict(tools_registry._REGISTRY, {'interaction.load_skills': spec}))
        skill = self.env.ref('ow_ai.skill_search_database')
        self.queue_tool_calls([('load_skills', {'skill_ids': [skill.id]}, 'call_1')])
        self.queue_text('Ready.')
        self.queue_text('Still ready.')

        self.say(self.channel, 'Load what you need', self.user)
        self.say(self.channel, 'And again', self.user)

        self.assertNotIn('search', self.request_tool_names(0))
        for index in (1, 2):
            self.assertIn('search', self.request_tool_names(index))
            self.assertIn(f'### Loaded skill: {skill.name}', self.request_json(index)['messages'][0]['content'])
        self.assertEqual(self.session.state['loaded_skills'], [skill.id])

    def test_tool_final_message_ends_the_turn(self):
        def finisher(ctx):
            ctx.final_message = 'All done.'
            return "Finished."

        tool = self.register_builtin('test.finisher', finisher, 'test_finisher')
        self.make_available(tool)
        self.queue_tool_calls([('test_finisher', {}, 'call_1')])

        self.say(self.channel, 'Finish it', self.user)

        self.assertEqual(len(self.transport.requests), 1)
        self.assertEqual(self.last_answer(self.channel), 'All done.')
        self.assertEqual(self.session.loop_state, 'ready')
        self.assertEqual(self.session.event_ids.sorted('sequence').mapped('role'), ['user', 'assistant', 'user'])

    def test_turn_notifications_posted_after_answer(self):
        def notifier(ctx):
            ctx.notifications.append({'kind': 'preview', 'body': '<p>Created <a href="/odoo/res.partner/1">X</a></p>'})
            return "Created."

        tool = self.register_builtin('test.notifier', notifier, 'test_notifier')
        self.make_available(tool)
        self.queue_tool_calls([('test_notifier', {}, 'call_1')])
        self.queue_text('Created X for you.')

        self.say(self.channel, 'Create X', self.user)

        last = self.agent_messages(self.channel)[-1]
        self.assertIn('data-oe-type="ow_ai_preview"', last.body)
        self.assertIn('href="/odoo/res.partner/1"', last.body)
        self.assertEqual(self.last_answer(self.channel), 'Created X for you.')
        self.assertFalse(self.session.turn_notifications)


class TestChannelTitle(EngineLoopCase):

    def test_title_job_renames_the_channel(self):
        channel = self.open_chat(self.user, title=None)
        agent = self.env.ref('ow_ai.agent_default')
        self.assertEqual(channel.name, agent.name)
        self.queue_text('Here they are.')
        self.queue_text('**Top customers** report')
        self.env['bus.bus'].sudo().search([]).unlink()

        self.say(channel, 'Who are my top customers?', self.user)

        self.assertEqual(channel.name, 'Top customers report')
        # discuss.channel syncs `name` to open clients on write (bus.sync.mixin).
        self.env.cr.precommit.run()
        self.assertTrue(any(
            'Top customers report' in bus.message for bus in self.env['bus.bus'].sudo().search([])))
        title_request = self.request_json(1)
        self.assertEqual(title_request['messages'][0]['content'], CHANNEL_TITLE_INSTRUCTIONS)
        self.assertEqual(title_request['max_tokens'], 400)
        self.assertNotIn('tools', title_request)
        session = self.chat_session(channel)
        self.assertEqual(
            sorted(self.env['ow.ai.usage'].search([('session_id', '=', session.id)]).mapped('kind')),
            ['chat', 'title'])
        self.assertEqual(sorted(session.job_ids.mapped('kind')), ['agent_round', 'channel_title'])

        # Only the first message of a chat asks for a title.
        self.queue_text('More.')
        self.say(channel, 'And more?', self.user)
        self.assertEqual(len(self.transport.requests), 3)

    def test_no_title_job_when_user_named_the_chat(self):
        self.queue_text('Hello.')

        self.say(self.channel, 'Hi', self.user)

        self.assertEqual(len(self.transport.requests), 1)
        self.assertEqual(self.channel.name, 'Test chat')

    def test_title_is_asked_again_without_max_tokens_on_a_bad_request(self):
        """Some models refuse `max_tokens` (400): the title request is retried once without it."""
        channel = self.open_chat(self.user, title=None)
        self.queue_text('Hello.')
        self.queue_error(400, "Unsupported parameter: 'max_tokens' is not supported with this model.")
        self.queue_text('Greeting')

        self.say(channel, 'Hi', self.user)

        self.assertEqual(len(self.transport.requests), 3)
        self.assertEqual(self.request_json(1)['max_tokens'], 400)
        self.assertNotIn('max_tokens', self.request_json(2))
        self.assertEqual(self.request_json(2)['messages'], self.request_json(1)['messages'])
        self.assertEqual(channel.name, 'Greeting')
        usage = self.env['ow.ai.usage'].search([('session_id', '=', self.chat_session(channel).id)], order='id')
        self.assertEqual(
            [(row.kind, row.status, row.error_code or False) for row in usage],
            [('chat', 'ok', False), ('title', 'error', 'bad_request'), ('title', 'ok', False)])

    def test_title_gives_up_after_a_second_bad_request(self):
        channel = self.open_chat(self.user, title=None)
        self.queue_text('Hello.')
        self.queue_error(400, 'bad')
        self.queue_error(400, 'still bad')

        with mute_logger('odoo.addons.ow_ai.engine.loop'):
            self.say(channel, 'Hi', self.user)

        self.assertEqual(len(self.transport.requests), 3)
        self.assertEqual(self.chat_session(channel).loop_state, 'ready')
        self.assertEqual(self.last_answer(channel), 'Hello.')
        self.assertEqual(channel.name, self.env.ref('ow_ai.agent_default').name)

    def test_title_failure_never_fails_the_chat(self):
        channel = self.open_chat(self.user, title=None)
        self.queue_text('Hello.')
        self.queue_error(500, 'boom')

        with mute_logger('odoo.addons.ow_ai.engine.loop'):
            self.say(channel, 'Hi', self.user)

        session = self.chat_session(channel)
        self.assertEqual(session.loop_state, 'ready')
        self.assertFalse(session.last_error)
        self.assertEqual(self.last_answer(channel), 'Hello.')
        self.assertEqual(channel.name, self.env.ref('ow_ai.agent_default').name)


class TestAnswerLinks(EngineLoopCase):

    def test_same_host_absolute_link_is_kept_in_the_answer(self):
        self.env['ir.config_parameter'].sudo().set_str('web.base.url', 'https://erp.example.com')
        self.queue_text(
            'See [Acme](https://erp.example.com/odoo/res.partner/1) or [this](https://evil.example/?d=1).')

        self.say(self.channel, 'Hi', self.user)

        body = self.agent_messages(self.channel)[-1].body
        self.assertIn('href="https://erp.example.com/odoo/res.partner/1"', body)
        self.assertNotIn('href="https://evil.example', body)
        self.assertIn('o_ow_ai_extlink', body)
