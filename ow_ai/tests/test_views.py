# -*- coding: utf-8 -*-
"""Admin views (Reports › Sessions/Jobs/Usage): view archs load, the fields
the brief lists exist and are readable, button icons are icon font classes,
and the manager-only actions (``action_test_chat``, job retry/cancel,
session abort/open-chat) behave.
"""
from __future__ import annotations

from lxml import etree
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, new_test_user

from ..engine import loop
from ..engine.types import text_part
from .test_engine_loop import EngineLoopCase


class ViewsCase(EngineLoopCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = new_test_user(cls.env, login='ow_ai_views_manager', groups='ow_ai.group_ai_manager')

    @staticmethod
    def _model_fields(result, model_name):
        """The field names of ``model_name`` in a ``get_views()`` result.

        ``get_views()`` returns ``result['models'][model] = {'fields': {...}}``
        for every model referenced by the requested view archs (the current
        model, and any related model an inline one2many/many2many form pulls
        in) -- there is no flat, top-level ``fields`` key.
        """
        return result['models'][model_name]['fields']


class TestViewArchsLoad(ViewsCase):
    """Every new/changed view arch loads (``get_views``) with its fields present."""

    def test_session_views_load(self):
        result = self.env['ow.ai.session'].get_views([(False, 'list'), (False, 'form'), (False, 'search')])
        fields = self._model_fields(result, 'ow.ai.session')
        for field in ('create_date', 'channel_id', 'agent_id', 'request_user_id',
                      'loop_state', 'request_round', 'request_round_limit', 'last_error', 'active',
                      'event_ids', 'job_ids', 'usage_ids'):
            self.assertIn(field, fields)

    def test_session_event_summary_field(self):
        # `ow.ai.session.event` has no top-level list view of its own -- its
        # list/form arch is only defined inline inside the session form's
        # `event_ids` field -- so fetch it through that form, the same way
        # the web client does when it renders the "Events" notebook page.
        result = self.env['ow.ai.session'].get_views([(False, 'form')])
        self.assertIn('summary', self._model_fields(result, 'ow.ai.session.event'))

    def test_job_views_load(self):
        result = self.env['ow.ai.job'].get_views([(False, 'list'), (False, 'form'), (False, 'search')])
        fields = self._model_fields(result, 'ow.ai.job')
        for field in ('kind', 'state', 'attempt', 'scheduled_at', 'claimed_at', 'finished_at', 'error'):
            self.assertIn(field, fields)

    def test_usage_views_load(self):
        result = self.env['ow.ai.usage'].get_views(
            [(False, 'list'), (False, 'pivot'), (False, 'graph'), (False, 'search')])
        fields = self._model_fields(result, 'ow.ai.usage')
        self.assertIn('cost', fields)
        self.assertIn('total_tokens', fields)

    def test_agent_views_load(self):
        result = self.env['ow.ai.agent'].get_views([(False, 'form')])
        fields = self._model_fields(result, 'ow.ai.agent')
        self.assertIn('session_count', fields)
        self.assertIn('usage_count', fields)

    def test_tool_views_load(self):
        result = self.env['ow.ai.tool'].get_views([(False, 'form')])
        fields = self._model_fields(result, 'ow.ai.tool')
        self.assertIn('schema', fields)
        self.assertIn('schema_summary', fields)

    def test_skill_views_load(self):
        result = self.env['ow.ai.skill'].get_views([(False, 'form')])
        self.assertIn('tool_ids', self._model_fields(result, 'ow.ai.skill'))


class TestViewButtonIcons(TransactionCase):
    """Odoo 19's ``ViewButton`` renders an ``icon`` starting with ``fa-`` or
    ``oi-`` as an icon font glyph; any other value (the Material Symbols names
    of the 20.0 branch) becomes ``<img src="<value>">``, a broken image
    (``web/static/src/views/view_button/view_button.js``)."""

    def test_every_view_button_icon_is_an_icon_font_class(self):
        view_ids = self.env['ir.model.data'].search([
            ('module', '=', 'ow_ai'),
            ('model', '=', 'ir.ui.view'),
        ]).mapped('res_id')
        views = self.env['ir.ui.view'].browse(view_ids)
        self.assertTrue(views)
        icons = []
        for view in views:
            for button in etree.fromstring(view.arch_db).iter('button'):
                if button.get('icon'):
                    icons.append((view.xml_id, button.get('name'), button.get('icon')))
        self.assertTrue(icons)
        for xml_id, name, icon in icons:
            with self.subTest(view=xml_id, button=name):
                self.assertTrue(icon.startswith(('fa-', 'oi-')), f"{xml_id}: button {name} has icon {icon!r}")


class TestActionTestChat(ViewsCase):

    def test_action_test_chat_returns_open_chat_client_action(self):
        agent = self.env.ref('ow_ai.agent_default')
        action = agent.with_user(self.user).action_test_chat()
        self.assertEqual(action['type'], 'ir.actions.client')
        self.assertEqual(action['tag'], 'ow_ai.open_chat')
        self.assertIn('channel_id', action['params'])
        self.assertTrue(action['params']['channel_id'])


class TestSessionAdminActions(ViewsCase):

    def test_open_chat_returns_discuss_client_action(self):
        action = self.session.with_user(self.manager).action_open_chat()
        self.assertEqual(action['type'], 'ir.actions.client')
        self.assertEqual(action['tag'], 'mail.action_discuss')
        self.assertEqual(action['context']['active_id'], f'discuss.channel_{self.channel.id}')

    def test_open_chat_requires_manager(self):
        with self.assertRaises(AccessError):
            self.session.with_user(self.user).action_open_chat()

    def test_abort_pending_requires_manager(self):
        with self.assertRaises(AccessError):
            self.session.with_user(self.user).action_abort_pending()

    def test_abort_pending_drops_a_paused_confirmation(self):
        self.session.write({
            'loop_state': 'waiting_confirmation',
            'request_round': 1,
            'request_round_limit': 5,
            'request_user_id': self.user.id,
            'resume_token': 'tok-1',
            'pending_tool_call': {
                'calls': [{'name': 'do_thing', 'call_id': 'call_1', 'args': {}}],
                'results': [],
                'index': 0,
                'user_input_request': {'type': 'confirmation', 'body': '<p>Sure?</p>'},
            },
        })
        self.session.with_user(self.manager).action_abort_pending()
        self.assertEqual(self.session.loop_state, 'ready')
        self.assertFalse(self.session.pending_tool_call)

    def test_abort_pending_noop_when_not_waiting(self):
        # 'ready' is not one of the waiting states: no-op, no error.
        self.session.with_user(self.manager).action_abort_pending()
        self.assertEqual(self.session.loop_state, 'ready')


class TestJobAdminActions(ViewsCase):

    def async_env(self, user):
        return self.env(user=user, context=dict(self.env.context, ow_ai_inline_jobs=False))

    def runner_triggers(self):
        cron = self.env.ref('ow_ai.ir_cron_job_runner')
        return self.env['ir.cron.trigger'].sudo().search([('cron_id', '=', cron.id)])

    def _failed_job(self):
        job = loop.submit_user_message(self.session, self.async_env(self.user), [text_part('Hi')])
        job.sudo().write({'state': 'failed', 'error': 'boom'})
        return job

    def test_retry_requeues_a_failed_job_and_triggers_the_cron(self):
        job = self._failed_job()
        self.assertTrue(job.with_user(self.manager).can_retry)
        triggers_before = self.runner_triggers()

        job.with_user(self.manager).action_retry()

        self.assertEqual(job.state, 'pending')
        self.assertFalse(job.error)
        self.assertEqual(len(self.runner_triggers() - triggers_before), 1)

    def test_retry_requires_manager(self):
        job = self._failed_job()
        with self.assertRaises(AccessError):
            job.with_user(self.user).action_retry()

    def test_retry_ignores_a_job_that_is_not_failed(self):
        job = self._failed_job()
        job.sudo().write({'state': 'done'})
        job.with_user(self.manager).action_retry()
        self.assertEqual(job.state, 'done')

    def test_retry_refused_once_the_turn_is_over(self):
        job = loop.submit_user_message(self.session, self.async_env(self.user), [text_part('Hi')])
        job._fail('boom')  # failing the round the session waits for ends the turn
        self.assertEqual(self.session.loop_state, 'ready')
        self.assertFalse(job.with_user(self.manager).can_retry)

        with self.assertRaises(UserError):
            job.with_user(self.manager).action_retry()
        self.assertEqual(job.state, 'failed')

    def test_cancel_a_pending_job(self):
        job = self.submit_async()
        self.assertEqual(job.state, 'pending')
        job.with_user(self.manager).action_cancel()
        self.assertEqual(job.state, 'cancelled')

    def test_cancel_requires_manager(self):
        job = self.submit_async()
        with self.assertRaises(AccessError):
            job.with_user(self.user).action_cancel()

    def submit_async(self, text='Hi', user=None, channel=None):
        user = user or self.user
        channel = channel or self.channel
        return loop.submit_user_message(self.chat_session(channel), self.async_env(user), [text_part(text)])
