# -*- coding: utf-8 -*-
"""Browser tours of the AI chat (``static/tests/tours/``), scripted end to end.

The model is a ``FakeTransport`` installed on the registry
(``registry.ow_ai_transport``, what ``get_transport(env)`` reads in the
HTTP request threads too), answering from a queue in order, whatever the
request. ``registry.ow_ai_inline_jobs`` makes every ``ow.ai.job`` run inline,
inside the HTTP request that enqueued it (see ``ow.ai.job.is_inline``): the
``/ow_ai/session/advance`` (resp. ``/resume``) request only returns once the
whole turn -- every model round, up to the answer or the next pause -- and
then the chat title job have run, and the browser gets the result over the
bus afterwards. Acceptable for tours: they check what the user ends up
seeing, not the cron-driven asynchrony.

Every queue below is therefore in the order the requests happen: the
turn's rounds, then (for a chat still carrying the agent's name, i.e. a
systray chat) the chat title, then the rounds after a resumed pause. The
HTTP requests of a tour share the test cursor, whose lock serialises them:
the queue is never used by two threads at once, no lock needed.

The default agent only offers ``load_skills``/``ask_user_question`` until a
skill is loaded, so each scripted turn first loads the skill that brings
the tools it calls -- as a real model has to.

On failure, Odoo saves a screenshot (and the Chrome log) under
``--screenshots``: ``logs/odoo_tests/ow_ai_test/`` with ``scripts/test.sh``.
"""
import time

from odoo.tests import HttpCase, new_test_user, tagged
from odoo.tools.mail import html2plaintext

from ..provider.transport import FakeTransport
from ..utils import params

_LOGIN = 'ow_ai_tour_user'
# The Contacts list: from any view but Discuss (the first app of the menu,
# where `/odoo` lands in Community), a systray chat opens in a chat window.
_START_URL = '/odoo/action-base.action_partner_form'
# Both also in ow_ai_record_chat_tour.js.
_RECORD_PARTNER_NAME = 'Tour Partner Ltd'
_RECORD_SUMMARY = f'Summary: {_RECORD_PARTNER_NAME} is a company in Utrecht.'
_READ_TITLE = 'Top customers this quarter'


class _TourTransport(FakeTransport):
    """A ``FakeTransport`` that answers after a short, simulated model latency,
    so the chat's "Thinking…" status stays up long enough for a tour to see it."""

    latency = 0.2

    def _next(self, url, json_body, headers):
        time.sleep(self.latency)
        return super()._next(url, json_body, headers)


@tagged('post_install', '-at_install')
class TestOwAiTours(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.transport = _TourTransport()
        cls.env.registry.ow_ai_transport = cls.transport
        cls.env.registry.ow_ai_inline_jobs = True
        cls.addClassCleanup(cls._clear_registry_flags)
        params.set_str(cls.env, 'ow_ai.api_key', 'sk-or-test')

        # Contact creation rights (base.group_partner_manager): the write
        # tour has the assistant create a contact as this user.
        cls.user = new_test_user(
            cls.env, login=_LOGIN, password=_LOGIN, name='Tour User',
            groups='base.group_user,base.group_partner_manager,ow_ai.group_ai_user')
        cls.agent = cls.env.ref('ow_ai.agent_default')
        cls.skill_search = cls.env.ref('ow_ai.skill_search_database')
        cls.skill_create = cls.env.ref('ow_ai.skill_create_records')
        cls.record_partner = cls.env['res.partner'].create({
            'name': _RECORD_PARTNER_NAME, 'is_company': True, 'city': 'Utrecht',
            'email': 'hello@tour-partner.test',
        })
        cls.env['res.partner'].create([
            {'name': 'Globex Tour', 'is_company': True, 'city': 'Almere', 'email': 'sales@globex.test'},
            {'name': 'Initech Tour', 'is_company': True, 'city': 'Utrecht', 'email': 'info@initech.test'},
        ])

    @classmethod
    def _clear_registry_flags(cls):
        for attr in ('ow_ai_transport', 'ow_ai_inline_jobs'):
            if hasattr(cls.env.registry, attr):
                delattr(cls.env.registry, attr)

    def setUp(self):
        super().setUp()
        self.transport.queue.clear()
        self.transport.requests.clear()

    # -- helpers --------------------------------------------------------------

    def _user_chats(self):
        return self.env['discuss.channel'].sudo().search([
            ('channel_type', '=', 'ow_ai_chat'),
            ('channel_member_ids.partner_id', '=', self.user.partner_id.id),
        ])

    def _assert_turns_done(self, channel):
        """All scripted responses consumed, no failed job, the session ``ready``."""
        session = channel.ow_ai_session_ids
        self.assertEqual(len(session), 1)
        self.assertEqual(self.transport.queue, [], "every scripted response was requested")
        self.assertFalse(session.job_ids.filtered(lambda job: job.state == 'failed').mapped('error'))
        self.assertEqual(session.loop_state, 'ready')
        return session

    def _agent_messages(self, channel):
        return self.env['mail.message'].sudo().search([
            ('model', '=', 'discuss.channel'), ('res_id', '=', channel.id),
            ('author_id', '=', self.agent.partner_id.id),
        ], order='id asc')

    # -- tours ----------------------------------------------------------------

    def test_chat_read_tour(self):
        """Scenario A: a question answered with the read tools."""
        self.transport.queue.extend([
            FakeTransport.tool_calls([('load_skills', {'skill_ids': [self.skill_search.id]})]),
            FakeTransport.tool_calls([('get_fields', {'model_name': 'res.partner'})]),
            FakeTransport.tool_calls([('read_group', {
                'model_name': 'res.partner', 'domain': [['is_company', '=', True]],
                'groupby': ['city'], 'aggregates': ['__count'], 'order': '__count desc', 'limit': 5,
            })]),
            FakeTransport.text("## Top customers\n\n| Customer | Revenue |\n|---|---|\n| A | 10 |"),
            FakeTransport.text(_READ_TITLE),  # the chat title
        ])

        self.start_tour(_START_URL, 'ow_ai_chat_read', login=self.user.login)

        channel = self._user_chats()
        self.assertEqual(len(channel), 1)
        self.assertEqual(channel.name, _READ_TITLE)
        session = self._assert_turns_done(channel)
        self.assertEqual(len(self.transport.requests), 5)
        usages = self.env['ow.ai.usage'].search([('session_id', '=', session.id)])
        self.assertEqual(sorted(usages.mapped('kind')), ['chat'] * 4 + ['title'])
        self.assertEqual(usages.user_id, self.user)
        summaries = self._agent_messages(channel).filtered(
            lambda message: 'o_ow_ai_tool_summary' in (message.body or ''))
        self.assertEqual(len(summaries), 3)
        answer = self._agent_messages(channel).filtered(
            lambda message: message.message_type == 'comment' and not message.is_internal)
        self.assertIn('<table', answer.body)
        self.assertIn('Top customers', html2plaintext(answer.body))

    def test_chat_write_tour(self):
        """Scenario B: a creation, confirmed on the card before anything is written."""
        self.transport.queue.extend([
            FakeTransport.tool_calls([('load_skills', {'skill_ids': [self.skill_create.id]})]),
            FakeTransport.tool_calls([('create_records', {
                'explanation': "Creating the contact ACME Corp you asked for.",
                'model_name': 'res.partner',
                'values': [{'field_values': [
                    {'field': 'name', 'value': 'ACME Corp'},
                    {'field': 'email', 'value': 'info@acme.test'},
                ]}],
            })]),
            # The turn pauses on the card: the chat title comes next, the
            # confirmed creation needs no model round of its own.
            FakeTransport.text("New contact ACME Corp"),
            FakeTransport.text("Created the contact ACME Corp."),
        ])

        self.start_tour(_START_URL, 'ow_ai_chat_write', login=self.user.login)

        partner = self.env['res.partner'].search([('name', '=', 'ACME Corp')])
        self.assertEqual(len(partner), 1)
        self.assertEqual(partner.email, 'info@acme.test')
        self.assertEqual(partner.create_uid, self.user)
        channel = self._user_chats()
        self.assertEqual(len(channel), 1)
        self._assert_turns_done(channel)
        self.assertEqual(len(self.transport.requests), 4)
        notification = self._agent_messages(channel).filtered(
            lambda message: 'data-oe-type="ow_ai_preview"' in (message.body or ''))
        self.assertIn(f'/odoo/res.partner/{partner.id}', notification.body)

    def test_record_chat_tour(self):
        """Scenario C: a chat about the open record, its answer logged as a note."""
        self.transport.queue.append(FakeTransport.text(_RECORD_SUMMARY))
        partner = self.record_partner
        notes_before = partner.message_ids

        self.start_tour(f'/odoo/res.partner/{partner.id}', 'ow_ai_record_chat', login=self.user.login)

        channel = self._user_chats()
        self.assertEqual(len(channel), 1)
        self.assertEqual(channel.name, _RECORD_PARTNER_NAME)
        session = self._assert_turns_done(channel)
        self.assertEqual((session.res_model, session.res_id), ('res.partner', partner.id))
        # a record chat keeps the record's name: no title request
        self.assertEqual(len(self.transport.requests), 1)
        # the model was told about the record
        self.assertIn(_RECORD_PARTNER_NAME, str(self.transport.requests[0]['json']['messages']))
        # the full composer was closed without logging the note
        partner.invalidate_recordset(['message_ids'])
        self.assertEqual(partner.message_ids, notes_before)
