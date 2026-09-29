# -*- coding: utf-8 -*-
"""``/ow_ai/session/*`` HTTP controllers.

An ``HttpCase`` runs its HTTP requests in another thread, sharing the test
cursor with the main thread but starting from a *fresh* environment (its own
context, no ``ow_ai_inline_jobs`` unless set at the registry level -- see
``ow.ai.job.is_inline``). ``setUp`` therefore installs the ``FakeTransport``
and flips inline jobs on directly on ``self.env.registry`` instead of (only)
the context, so the request thread's job runs synchronously too and the
turn is over by the time ``make_jsonrpc_request`` returns.
"""
import json
from unittest.mock import patch

from odoo.tests import HttpCase, JsonRpcException, new_test_user, tagged

from ..engine import tools_registry
from ..provider.transport import FakeTransport
from ..utils import params
from .common import DEFAULT_CHAT_TITLE

_PASSWORD = 'ow-ai-http-test-pwd'


def _confirm_tool(ctx):
    if not ctx.tool_request_confirmed:
        ctx.user_input_request = {
            'type': 'confirmation',
            'body': '<p>Proceed?</p>',
            'choices': ['confirm_once', 'decline'],
        }
        return "Waiting for confirmation."
    return "Done."


@tagged('post_install', '-at_install')
class OwAiControllerCase(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = new_test_user(
            cls.env, login='ow_ai_http_user', password=_PASSWORD, groups='base.group_user')
        cls.other_user = new_test_user(
            cls.env, login='ow_ai_http_other', password=_PASSWORD, groups='base.group_user')
        cls.manager = new_test_user(
            cls.env, login='ow_ai_http_manager', password=_PASSWORD, groups='base.group_system')
        cls.portal_user = new_test_user(
            cls.env, login='ow_ai_http_portal', password=_PASSWORD, groups='base.group_portal')
        cls.agent = cls.env.ref('ow_ai.agent_default')

    def setUp(self):
        super().setUp()
        params.set_str(self.env, 'ow_ai.api_key', 'sk-or-test')
        self.transport = FakeTransport()
        self.env.registry.ow_ai_transport = self.transport
        self.env.registry.ow_ai_inline_jobs = True
        self.addCleanup(self._clear_registry_flags)

    def _clear_registry_flags(self):
        for attr in ('ow_ai_transport', 'ow_ai_inline_jobs'):
            if hasattr(self.env.registry, attr):
                delattr(self.env.registry, attr)

    # -- helpers --------------------------------------------------------------

    def _open_channel(self, user=None, title=DEFAULT_CHAT_TITLE):
        return self.agent._create_chat_channel(user or self.user, title=title)

    def _session(self, channel):
        return channel.sudo().ow_ai_session_ids[:1]

    def _pause_for_confirmation(self, channel, user):
        """Drive one real turn (through the HTTP route) up to a confirmation card."""
        key = 'test.http_security_confirm'
        spec = tools_registry.ToolSpec(key=key, func=_confirm_tool, is_write=True)
        self.startPatcher(patch.dict(tools_registry._REGISTRY, {key: spec}))
        tool = self.env['ow.ai.tool'].create({
            'name': 'HTTP Security Confirm',
            'tool_name': 'http_security_confirm',
            'kind': 'builtin',
            'builtin_key': key,
            'is_write': True,
            'description': "Test tool.",
            'schema': json.dumps({'type': 'object', 'properties': {}}),
        })
        session = self._session(channel)
        state = dict(session.state or {})
        state['available_tools'] = list(state.get('available_tools') or []) + [tool.id]
        session.state = state

        self.transport.queue.append(FakeTransport.tool_calls([('http_security_confirm', {}, 'call_1')]))
        message = channel.with_user(user).message_post(body="Please do it", message_type='comment')
        self.authenticate(user.login, _PASSWORD)
        self.make_jsonrpc_request('/ow_ai/session/advance', {
            'channel_id': channel.id, 'message_id': message.id,
        })
        session.invalidate_recordset()
        return session


class TestAdvance(OwAiControllerCase):

    def test_advance_as_author_runs_the_turn(self):
        channel = self._open_channel()
        message = channel.with_user(self.user).message_post(body="Hello", message_type='comment')
        self.transport.queue.append(FakeTransport.text("Hi there!"))
        self.authenticate(self.user.login, _PASSWORD)

        result = self.make_jsonrpc_request('/ow_ai/session/advance', {
            'channel_id': channel.id, 'message_id': message.id,
        })

        session = self._session(channel)
        self.assertEqual(result['session_id'], session.id)
        self.assertEqual(result['loop_state'], 'ready')
        answers = self.env['mail.message'].sudo().search([
            ('model', '=', 'discuss.channel'), ('res_id', '=', channel.id),
            ('author_id', '=', self.agent.partner_id.id), ('message_type', '=', 'comment'),
        ])
        self.assertTrue(answers)

    def test_advance_ignores_an_invalid_tab(self):
        channel = self._open_channel()
        message = channel.with_user(self.user).message_post(body="Hello", message_type='comment')
        self.transport.queue.append(FakeTransport.text("Hi there!"))
        self.authenticate(self.user.login, _PASSWORD)

        self.make_jsonrpc_request('/ow_ai/session/advance', {
            'channel_id': channel.id, 'message_id': message.id, 'client_identifier': ['tab-1'],
        })

        self.assertNotIn('client_identifier', self._session(channel).request_context)

    def test_advance_with_someone_elses_message_errors(self):
        channel = self._open_channel()
        # The agent's own message belongs to the channel but was not authored
        # by the (member) user about to call the route.
        agent_message = channel.sudo().message_post(
            body="An earlier answer", author_id=self.agent.partner_id.id, message_type='comment')
        self.authenticate(self.user.login, _PASSWORD)

        with self.assertRaises(JsonRpcException):
            self.make_jsonrpc_request('/ow_ai/session/advance', {
                'channel_id': channel.id, 'message_id': agent_message.id,
            })

    def test_advance_on_a_channel_not_a_member_of_errors(self):
        channel = self._open_channel(user=self.other_user)
        message = channel.with_user(self.other_user).message_post(body="Hi", message_type='comment')
        self.authenticate(self.user.login, _PASSWORD)

        with self.assertRaises(JsonRpcException):
            self.make_jsonrpc_request('/ow_ai/session/advance', {
                'channel_id': channel.id, 'message_id': message.id,
            })

    def test_advance_as_portal_user_errors(self):
        self.authenticate(self.portal_user.login, _PASSWORD)
        with self.assertRaises(JsonRpcException):
            self.make_jsonrpc_request('/ow_ai/session/advance', {
                'channel_id': 999999, 'message_id': 999999,
            })


class TestResume(OwAiControllerCase):

    def test_resume_with_correct_token(self):
        channel = self._open_channel()
        session = self._pause_for_confirmation(channel, self.user)
        self.assertEqual(session.loop_state, 'waiting_confirmation')
        token = session.resume_token
        self.transport.queue.append(FakeTransport.text("All done."))

        result = self.make_jsonrpc_request('/ow_ai/session/resume', {
            'channel_id': channel.id, 'session_id': session.id, 'resume_token': token,
            'response': {'kind': 'confirmation', 'value': 'confirm_once'},
        })

        self.assertTrue(result['interactionConsumed'])
        session.invalidate_recordset()
        self.assertEqual(session.loop_state, 'ready')

    def test_resume_with_wrong_token(self):
        channel = self._open_channel()
        session = self._pause_for_confirmation(channel, self.user)

        result = self.make_jsonrpc_request('/ow_ai/session/resume', {
            'channel_id': channel.id, 'session_id': session.id, 'resume_token': 'not-the-token',
            'response': {'kind': 'confirmation', 'value': 'confirm_once'},
        })

        self.assertFalse(result['interactionConsumed'])
        session.invalidate_recordset()
        self.assertEqual(session.loop_state, 'waiting_confirmation')

    def test_resume_stores_the_answering_tab(self):
        channel = self._open_channel()
        session = self._pause_for_confirmation(channel, self.user)
        token = session.resume_token
        self.transport.queue.append(FakeTransport.text("All done."))

        result = self.make_jsonrpc_request('/ow_ai/session/resume', {
            'channel_id': channel.id, 'session_id': session.id, 'resume_token': token,
            'response': {'kind': 'confirmation', 'value': 'confirm_once'},
            'client_identifier': 'tab-9',
        })

        self.assertTrue(result['interactionConsumed'])
        session.invalidate_recordset()
        self.assertEqual(session.request_context['client_identifier'], 'tab-9')

    def _resume_confirmed_from_tab(self, client_identifier):
        channel = self._open_channel()
        session = self._pause_for_confirmation(channel, self.user)
        token = session.resume_token
        self.transport.queue.append(FakeTransport.text("All done."))
        result = self.make_jsonrpc_request('/ow_ai/session/resume', {
            'channel_id': channel.id, 'session_id': session.id, 'resume_token': token,
            'response': {'kind': 'confirmation', 'value': 'confirm_once'},
            'client_identifier': client_identifier,
        })
        self.assertTrue(result['interactionConsumed'])
        session.invalidate_recordset()
        return session

    def test_resume_ignores_a_too_long_tab(self):
        session = self._resume_confirmed_from_tab('x' * 65)
        self.assertNotIn('client_identifier', session.request_context)

    def test_resume_ignores_a_tab_that_is_not_a_string(self):
        session = self._resume_confirmed_from_tab({'tab': 1})
        self.assertNotIn('client_identifier', session.request_context)

    def test_resume_as_non_requester_errors(self):
        channel = self._open_channel()
        session = self._pause_for_confirmation(channel, self.user)
        token = session.resume_token
        # The manager can see the channel (admin channel rule) but is not
        # the user who made the request.
        self.authenticate(self.manager.login, _PASSWORD)

        with self.assertRaises(JsonRpcException):
            self.make_jsonrpc_request('/ow_ai/session/resume', {
                'channel_id': channel.id, 'session_id': session.id, 'resume_token': token,
                'response': {'kind': 'confirmation', 'value': 'confirm_once'},
            })


class TestConfig(OwAiControllerCase):

    def test_config_toggle_posts_note_and_updates_session(self):
        channel = self._open_channel()
        self.authenticate(self.user.login, _PASSWORD)

        result = self.make_jsonrpc_request('/ow_ai/session/config', {
            'channel_id': channel.id, 'config': {'auto_confirm': True},
        })

        self.assertTrue(result['auto_confirm'])
        session = self._session(channel)
        self.assertTrue(session.auto_confirm)
        note = self.env['mail.message'].sudo().search([
            ('model', '=', 'discuss.channel'), ('res_id', '=', channel.id),
            ('body', 'like', 'Auto-approval enabled'),
        ])
        self.assertTrue(note)

        result = self.make_jsonrpc_request('/ow_ai/session/config', {
            'channel_id': channel.id, 'config': {'auto_confirm': False},
        })
        self.assertFalse(result['auto_confirm'])
        note_off = self.env['mail.message'].sudo().search([
            ('model', '=', 'discuss.channel'), ('res_id', '=', channel.id),
            ('body', 'like', 'Auto-approval disabled'),
        ])
        self.assertTrue(note_off)

    def test_config_switches_web_search(self):
        params.set_str(self.env, 'ow_ai.web_search_url', 'https://search.example.org')
        params.set_bool(self.env, 'ow_ai.web_search_default', True)  # these tests want the switch on
        channel = self._open_channel()
        session = self._session(channel)
        self.assertTrue(session.web_search)
        self.authenticate(self.user.login, _PASSWORD)

        result = self.make_jsonrpc_request('/ow_ai/session/config', {
            'channel_id': channel.id, 'config': {'web_search': False},
        })

        self.assertEqual(result, {'auto_confirm': False, 'show_agent_steps': False, 'web_search': False})
        session.invalidate_recordset()
        self.assertFalse(session.web_search)

        result = self.make_jsonrpc_request('/ow_ai/session/config', {
            'channel_id': channel.id, 'config': {'web_search': True},
        })
        self.assertTrue(result['web_search'])
        session.invalidate_recordset()
        self.assertTrue(session.web_search)

    def test_config_of_a_chat_not_a_member_of_errors(self):
        params.set_str(self.env, 'ow_ai.web_search_url', 'https://search.example.org')
        params.set_bool(self.env, 'ow_ai.web_search_default', True)  # these tests want the switch on
        channel = self._open_channel(user=self.other_user)
        self.authenticate(self.user.login, _PASSWORD)

        with self.assertRaises(JsonRpcException):
            self.make_jsonrpc_request('/ow_ai/session/config', {
                'channel_id': channel.id, 'config': {'web_search': False, 'auto_confirm': True},
            })

        session = self._session(channel)
        session.invalidate_recordset()
        self.assertEqual((session.web_search, session.auto_confirm), (True, False))


class TestDeleteChat(OwAiControllerCase):

    def test_delete_chat_removes_channel_for_member(self):
        channel = self._open_channel()
        channel_id = channel.id
        self.authenticate(self.user.login, _PASSWORD)

        result = self.make_jsonrpc_request('/ow_ai/session/delete_chat', {'channel_id': channel_id})

        self.assertTrue(result)
        self.assertFalse(self.env['discuss.channel'].sudo().browse(channel_id).exists())

    def test_delete_chat_as_non_member_errors(self):
        channel = self._open_channel()
        self.authenticate(self.other_user.login, _PASSWORD)

        with self.assertRaises(JsonRpcException):
            self.make_jsonrpc_request('/ow_ai/session/delete_chat', {'channel_id': channel.id})

        self.assertTrue(channel.exists())


class TestLaunchChatRpc(OwAiControllerCase):

    def test_action_launch_chat_over_rpc(self):
        """The web client gets the launched chat's Store payload as plain JSON."""
        self.authenticate(self.user.login, _PASSWORD)

        result = self.make_jsonrpc_request('/web/dataset/call_kw/ow.ai.agent/action_launch_chat', {
            'model': 'ow.ai.agent', 'method': 'action_launch_chat', 'args': [[]],
            'kwargs': {'interface_key': 'systray'},
        })

        channel = self.env['discuss.channel'].browse(result['channel_id'])
        self.assertEqual(channel.channel_type, 'ow_ai_chat')
        self.assertEqual(result['session_id'], self._session(channel).id)
        channel_ids = [data['id'] for data in result['store_data']['discuss.channel']]
        self.assertIn(channel.id, channel_ids)
        session_ids = [data['id'] for data in result['store_data']['ow.ai.session']]
        self.assertIn(result['session_id'], session_ids)
        self.assertTrue(result['prompt_buttons'])
        self.assertTrue(all(button['prompt'] for button in result['prompt_buttons']))

    def test_launched_chat_publishes_its_web_search_switch(self):
        params.set_str(self.env, 'ow_ai.web_search_url', 'https://search.example.org')
        params.set_bool(self.env, 'ow_ai.web_search_default', True)  # these tests want the switch on
        self.authenticate(self.user.login, _PASSWORD)

        result = self.make_jsonrpc_request('/web/dataset/call_kw/ow.ai.agent/action_launch_chat', {
            'model': 'ow.ai.agent', 'method': 'action_launch_chat', 'args': [[]],
            'kwargs': {'interface_key': 'systray'},
        })

        session_data = next(
            data for data in result['store_data']['ow.ai.session'] if data['id'] == result['session_id'])
        self.assertEqual(session_data['config'], {'auto_confirm': False, 'show_agent_steps': False, 'web_search': True})

