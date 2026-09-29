# -*- coding: utf-8 -*-
import copy
import json
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests import new_test_user

from ..engine import direct, tools_registry
from ..engine.history import truncate_history
from ..engine.types import (
    assistant_message,
    inline_data_part,
    text_part,
    tool_call_part,
    tool_result_part,
    user_message,
)
from ..provider.errors import ProviderError
from ..provider.transport import FakeTransport
from ..utils import params
from ..utils.prompts import ROUND_LIMIT_WARNING, build_instructions
from .common import OwAiCase


class DirectResponseCase(OwAiCase):

    def setUp(self):
        super().setUp()
        self.agent = self.env.ref('ow_ai.agent_default')
        self.instructions = self.agent._get_instructions()
        self.search_tool = self.env.ref('ow_ai.tool_search')
        # TransactionCase.env defaults to the superuser; run_tool refuses to
        # run under a superuser environment (tools must always act as a real
        # user, so record rules/ACLs are actually exercised), so anything
        # that may execute a tool call needs a real, restricted user.
        self.call_env = self.env

    def get_response(self, message_parts, **kw):
        return self.call_env['ow.ai.session']._get_direct_response(self.instructions, message_parts, **kw)


class TestTextOnly(DirectResponseCase):

    def test_instructions_include_protocol_agent_skills(self):
        self.assertIn('## Protocol', self.instructions)
        self.assertIn('## Agent', self.instructions)
        self.assertIn('## Skills', self.instructions)

    def test_last_user_message_has_odoo_context(self):
        self.queue_text('Hello there')
        self.get_response([text_part('Hi')])

        sent = self.last_request()['json']
        self.assertEqual(sent['messages'][0]['content'], self.instructions)
        user_message = sent['messages'][-1]
        self.assertEqual(user_message['role'], 'user')
        context_text = user_message['content'][-1]['text']
        self.assertIn('<odoo_context>', context_text)
        self.assertIn('</odoo_context>', context_text)
        self.assertIn(self.env.user.name, context_text)
        self.assertIn(self.env.user.tz or 'UTC', context_text)

    def test_returns_text_part(self):
        self.queue_text('Hello there')
        parts = self.get_response([text_part('Hi')])
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]['type'], 'text')
        self.assertEqual(parts[0]['text'], 'Hello there')

    def test_usage_row_logged(self):
        self.queue_text(
            'Hello there', model='test/model', request_id='gen-123',
            usage={'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15, 'cost': 0.01})
        self.get_response([text_part('Hi')])

        rows = self.env['ow.ai.usage'].search([('kind', '=', 'direct')])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.model, 'test/model')
        self.assertEqual(row.request_id, 'gen-123')
        self.assertEqual(row.prompt_tokens, 10)
        self.assertEqual(row.completion_tokens, 5)
        self.assertEqual(row.total_tokens, 15)
        self.assertEqual(row.cost, 0.01)
        self.assertGreaterEqual(row.latency_ms, 0)
        self.assertEqual(row.status, 'ok')


class TestToolLoop(DirectResponseCase):

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({'name': 'Alpha Testing'})
        self.tool_user = new_test_user(self.env, login='ow_ai_direct_tool_user', groups='base.group_user')
        self.call_env = self.env(user=self.tool_user)

    def test_tool_executed_and_history_preserved(self):
        steps = []
        self.queue_tool_calls([
            ('search', {'model_name': 'res.partner', 'domain': [['id', '=', self.partner.id]]}, 'call_1'),
        ])
        self.queue_text('done')

        parts = self.get_response(
            [text_part('Find Alpha Testing')], tools=self.search_tool,
            on_step=lambda call, result: steps.append((call, result)))

        self.assertEqual(parts[0]['text'], 'done')
        self.assertEqual(len(steps), 1)
        self.assertTrue(steps[0][1].success)

        second_request = self.transport.requests[1]['json']
        messages = second_request['messages']

        assistant_message = next(m for m in messages if m['role'] == 'assistant' and m.get('tool_calls'))
        arguments = assistant_message['tool_calls'][0]['function']['arguments']
        self.assertIsInstance(arguments, str)
        self.assertEqual(json.loads(arguments)['model_name'], 'res.partner')

        tool_message = next(m for m in messages if m['role'] == 'tool' and m['tool_call_id'] == 'call_1')
        self.assertIn('Alpha Testing', tool_message['content'])

    def test_unknown_tool_name(self):
        self.queue_tool_calls([('does_not_exist', {}, 'call_1')])
        self.queue_text('done')

        self.get_response([text_part('Do it')], tools=self.search_tool)

        second_request = self.transport.requests[1]['json']
        tool_message = next(m for m in second_request['messages'] if m['role'] == 'tool')
        self.assertIn('Unknown tool', tool_message['content'])

    def test_too_many_tool_calls_in_one_round(self):
        params.set_int(self.env, 'ow_ai.max_tool_calls_per_round', 1)
        self.queue_tool_calls([
            ('search', {'model_name': 'res.partner'}, 'call_1'),
            ('search', {'model_name': 'res.partner'}, 'call_2'),
        ])
        self.queue_text('done')

        self.get_response([text_part('Do it')], tools=self.search_tool)

        second_request = self.transport.requests[1]['json']
        tool_messages = {m['tool_call_id']: m['content'] for m in second_request['messages'] if m['role'] == 'tool'}
        self.assertNotIn('Too many tool calls', tool_messages['call_1'])
        self.assertIn('Too many tool calls', tool_messages['call_2'])

    def test_tool_requiring_confirmation_in_direct_mode(self):
        def _confirm(ctx, **args):
            ctx.user_input_request = {'type': 'confirmation'}
            return "would do the thing"

        tools_registry._REGISTRY['test.confirm'] = tools_registry.ToolSpec(
            key='test.confirm', func=_confirm, is_write=True)
        self.addCleanup(tools_registry._REGISTRY.pop, 'test.confirm', None)

        confirm_tool = self.env['ow.ai.tool'].create({
            'name': 'Confirm Tool',
            'tool_name': 'confirm_tool',
            'kind': 'builtin',
            'builtin_key': 'test.confirm',
            'description': "A tool that always needs confirmation.",
            'schema': '{"type": "object", "properties": {}}',
        })

        self.queue_tool_calls([('confirm_tool', {}, 'call_1')])
        self.queue_text('done')

        self.get_response([text_part('Do the thing')], tools=confirm_tool)

        second_request = self.transport.requests[1]['json']
        tool_message = next(m for m in second_request['messages'] if m['role'] == 'tool')
        self.assertIn('needs user confirmation', tool_message['content'])

    def test_round_limit_reached(self):
        self.queue_tool_calls([('search', {'model_name': 'res.partner'}, 'call_1')])
        self.queue_tool_calls([('search', {'model_name': 'res.partner'}, 'call_1')])

        parts = self.get_response([text_part('Do it forever')], tools=self.search_tool, max_rounds=2)

        self.assertEqual(len(self.transport.requests), 2)
        self.assertIn('could not finish', parts[0]['text'])

    def test_tool_state_reaches_each_call_as_a_fresh_copy(self):
        seen = []
        tool_state = {'ow_ai_action': {'action_id': 7}}

        def fake_run_tool(tool_record, args, ctx):
            seen.append(copy.deepcopy(ctx.state))
            ctx.state['touched'] = True
            ctx.state['ow_ai_action']['action_id'] = 99
            return tools_registry.ToolResult(response='ok')

        self.queue_tool_calls([
            ('search', {'model_name': 'res.partner'}, 'call_1'),
            ('search', {'model_name': 'res.partner'}, 'call_2'),
        ])
        self.queue_text('done')
        with patch.object(direct, 'run_tool', side_effect=fake_run_tool):
            self.get_response(
                [text_part('Do it')], tools=self.search_tool, tool_state=tool_state)

        self.assertEqual(seen, [{'ow_ai_action': {'action_id': 7}}] * 2)
        self.assertEqual(tool_state, {'ow_ai_action': {'action_id': 7}})


class TestUsageLogging(DirectResponseCase):

    def test_usage_row_without_usage_block(self):
        item = FakeTransport.text('Hi')
        item[2].pop('usage', None)          # ('response', status, body, headers)
        self.transport.queue.append(item)
        self.get_response([text_part('Hi')])
        row = self.env['ow.ai.usage'].search([], order='id desc', limit=1)
        self.assertEqual((row.prompt_tokens, row.completion_tokens, row.cost), (0, 0, 0.0))


class TestErrorLogging(DirectResponseCase):

    def test_provider_error_logged(self):
        # NB: deliberately not `self.assertRaises` here -- Odoo's own
        # TransactionCase wraps that helper in a savepoint that is rolled
        # back whenever the expected exception is raised (to avoid ORM
        # cache pollution from a call that is expected to fail), which
        # would also silently undo the usage row this test wants to see.
        self.queue_error(500, 'boom')

        try:
            self.get_response([text_part('Hi')])
            self.fail("ProviderError was not raised")
        except ProviderError:
            pass

        rows = self.env['ow.ai.usage'].search([('status', '=', 'error')])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.error_code, 'provider_error')


class TestStructuredResponse(DirectResponseCase):

    def setUp(self):
        super().setUp()
        self.schema = {
            'type': 'object',
            'properties': {'a': {'type': 'integer'}},
            'required': ['a'],
            'additionalProperties': False,
        }

    def test_valid_json_first_try(self):
        self.queue_json({'a': 1})
        result = self.env['ow.ai.session']._get_structured_response(
            self.instructions, [text_part('Give me a')], self.schema)
        self.assertEqual(result, {'a': 1})
        self.assertEqual(len(self.transport.requests), 1)

    def test_retries_once_on_invalid_json(self):
        self.queue_text('not json at all')
        self.queue_json({'a': 2})
        result = self.env['ow.ai.session']._get_structured_response(
            self.instructions, [text_part('Give me a')], self.schema)
        self.assertEqual(result, {'a': 2})
        self.assertEqual(len(self.transport.requests), 2)

    def test_raises_after_two_invalid_responses(self):
        self.queue_text('nope')
        self.queue_text('still nope')
        with self.assertRaises(ValueError):
            self.env['ow.ai.session']._get_structured_response(
                self.instructions, [text_part('Give me a')], self.schema)
        self.assertEqual(len(self.transport.requests), 2)


class TestSessionEvents(OwAiCase):

    def setUp(self):
        super().setUp()
        self.agent = self.env.ref('ow_ai.agent_default')
        self.session = self.env['ow.ai.session'].create({'agent_id': self.agent.id})

    def test_append_event_sequences_and_history_order(self):
        first = self.session._append_event('user', {'role': 'user', 'content': [{'type': 'text', 'text': 'one'}]})
        second = self.session._append_event(
            'assistant', {'role': 'assistant', 'content': [{'type': 'text', 'text': 'two'}]})

        self.assertEqual(first.sequence, 1)
        self.assertEqual(second.sequence, 2)

        messages = self.session._get_history_messages()
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]['content'][0]['text'], 'one')
        self.assertEqual(messages[1]['content'][0]['text'], 'two')

    def test_get_tool_params_strips_tool_status_and_explanation(self):
        event = self.session._append_event('assistant', {
            'role': 'assistant',
            'content': [{
                'type': 'tool_call', 'name': 'search', 'call_id': 'c1',
                'args': {'model_name': 'res.partner', 'tool_status': 'Looking things up', 'explanation': 'because'},
            }],
        })

        admin = self.env.ref('base.user_admin')
        params = event.with_user(admin).get_tool_params('c1')
        self.assertEqual(params, {'model_name': 'res.partner'})

    def test_get_tool_params_denied_for_plain_ai_user(self):
        event = self.session._append_event('assistant', {
            'role': 'assistant',
            'content': [{'type': 'tool_call', 'name': 'search', 'call_id': 'c1', 'args': {}}],
        })
        plain_user = new_test_user(self.env, login='ow_ai_plain_direct', groups='base.group_user')

        with self.assertRaises(AccessError):
            event.with_user(plain_user).get_tool_params('c1')


class TestPrompts(OwAiCase):

    def test_build_instructions_loaded_skill_not_in_available_list(self):
        agent = self.env.ref('ow_ai.agent_default')
        skill = self.env.ref('ow_ai.skill_search_database')
        skills = agent._get_available_skills()

        instructions = build_instructions(agent, skills=skills, loaded_skill_ids=[skill.id])

        self.assertIn(f'### Loaded skill: {skill.name}', instructions)
        self.assertNotIn(f'- {skill.id}: {skill.name}', instructions)

    def test_round_limit_warning_text(self):
        self.assertEqual(ROUND_LIMIT_WARNING(3), "You have 3 steps left; wrap up.")


class TestHistoryTruncation(OwAiCase):

    def test_drops_oldest_first_keeps_newest(self):
        messages = [
            {'role': 'user', 'content': [{'type': 'text', 'text': 'a' * 100}]},
            {'role': 'assistant', 'content': [{'type': 'text', 'text': 'b' * 100}]},
            {'role': 'user', 'content': [{'type': 'text', 'text': 'c' * 100}]},
        ]
        truncated = truncate_history(messages, max_chars=150)
        self.assertEqual(truncated[-1], messages[-1])
        self.assertLess(len(truncated), len(messages))

    def test_never_starts_with_orphaned_tool_results(self):
        # A tool round is assistant(tool_calls) -> user(tool_results): the
        # tool_result parts refer back to the call id on the assistant
        # message right before them. Truncation must drop that whole unit
        # together, never leave the user(tool_results) message as the new
        # first message (it would become an orphan `role: tool` message
        # once mapped to OpenAI format).
        messages = [
            user_message(text_part('a' * 200)),
            assistant_message([tool_call_part('search', {'q': 'x' * 200}, 'call_1')]),
            user_message(tool_result_part('search', 'call_1', [text_part('y' * 200)])),
            assistant_message([text_part('z' * 200)]),
            user_message(text_part('final question')),
        ]
        truncated = truncate_history(messages, max_chars=250)

        self.assertEqual(truncated[-1], messages[-1])
        first = truncated[0]
        self.assertEqual(first['role'], 'user')
        self.assertFalse(any(part.get('type') == 'tool_result' for part in first['content']))

    def test_newest_user_message_always_kept_even_if_over_budget(self):
        messages = [
            user_message(text_part('a' * 100)),
            user_message(text_part('b' * 5000)),
        ]
        truncated = truncate_history(messages, max_chars=10)
        self.assertEqual(truncated, [messages[-1]])

    def test_large_trailing_tool_result_keeps_question_and_pair(self):
        # The current turn (question + its tool call/result pair) is never
        # cut, however large its latest tool result is: only old turns go.
        messages = [
            user_message(text_part('old question')),
            assistant_message([text_part('old answer')]),
            user_message(text_part('current question')),
            assistant_message([tool_call_part('search', {'q': 'x'}, 'call_1')]),
            user_message(tool_result_part('search', 'call_1', [text_part('y' * 5000)])),
        ]
        truncated = truncate_history(messages, max_chars=500)
        self.assertEqual(truncated, messages[2:])

    def test_old_tool_results_shrink_before_old_turns_are_dropped(self):
        messages = [
            user_message(text_part('first question')),
            assistant_message([tool_call_part('search', {'q': 'x'}, 'call_1')]),
            user_message(tool_result_part('search', 'call_1', [text_part('y' * 5000)])),
            assistant_message([text_part('first answer')]),
            user_message(text_part('what about this one?'), inline_data_part('image/png', 'A' * 50000)),
            assistant_message([tool_call_part('search', {'q': 'z'}, 'call_2')]),
            user_message(tool_result_part('search', 'call_2', [text_part('w' * 400)])),
        ]
        truncated = truncate_history(messages, max_chars=1500)

        # Shrinking the old result was enough: no turn was dropped, the old
        # call/result pair is still whole, and the newest turn (with its
        # image, which does not count toward the budget) is intact.
        self.assertEqual(len(truncated), len(messages))
        old_result = truncated[2]['content'][0]
        self.assertEqual(old_result['tool_call_id'], 'call_1')
        self.assertEqual(old_result['result'], [text_part('[result truncated]')])
        self.assertEqual(truncated[4:], messages[4:])
        self.assertEqual(messages[2]['content'][0]['result'], [text_part('y' * 5000)])

    def test_old_images_and_files_replaced_by_placeholders(self):
        messages = [
            user_message(
                text_part('what is on this picture?'),
                inline_data_part('image/png', 'A' * 100000),
                inline_data_part('application/pdf', 'B' * 1000, filename='terms.pdf'),
            ),
            assistant_message([tool_call_part('read_records', {'record_ids': [1]}, 'call_1')]),
            user_message(tool_result_part('read_records', 'call_1', [
                text_part('{"records": []}'), inline_data_part('image/jpeg', 'C' * 1000)])),
            assistant_message([text_part('a cat')]),
            user_message(text_part('and its colour?'), inline_data_part('image/png', 'D' * 100000)),
        ]
        truncated = truncate_history(messages, max_chars=2000)

        self.assertEqual(len(truncated), len(messages))
        self.assertEqual(
            [part.get('text') for part in truncated[0]['content']],
            ['what is on this picture?', '[image omitted]', '[file omitted]'])
        self.assertEqual(truncated[2]['content'][0]['result'][1], text_part('[image omitted]'))
        # The newest turn keeps its own image; the input is left untouched.
        self.assertEqual(truncated[-1], messages[-1])
        self.assertEqual(messages[0]['content'][1]['type'], 'inline_data')
