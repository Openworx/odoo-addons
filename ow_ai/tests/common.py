# -*- coding: utf-8 -*-
"""Shared TransactionCase base for engine/session tests: installs a
``FakeTransport`` so no test ever makes a real HTTP call, offers small
helpers to queue scripted provider responses and inspect what was sent,
and helpers to drive a whole chat turn (``open_chat``/``say``/``resume``).

``self.env`` carries ``ow_ai_inline_jobs=True``: every ``ow.ai.job`` the
engine enqueues runs immediately, in the test transaction, instead of
waiting for the cron worker. A turn that needs several model rounds runs
them all inside one ``say()`` call, consuming the queued responses in
order.
"""
from __future__ import annotations

from odoo.tests import TransactionCase
from odoo.tools.mail import html2plaintext

from ..engine import loop
from ..provider.transport import FakeTransport

DEFAULT_CHAT_TITLE = 'Test chat'


class OwAiCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.env = self.env(context=dict(self.env.context, ow_ai_inline_jobs=True))
        self.transport = FakeTransport()
        self.env.registry.ow_ai_transport = self.transport
        self.addCleanup(self._clear_transport)
        self.env['ir.config_parameter'].sudo().set_str('ow_ai.api_key', 'sk-or-test')

    def _clear_transport(self):
        if hasattr(self.env.registry, 'ow_ai_transport'):
            del self.env.registry.ow_ai_transport

    # -- scripted responses ---------------------------------------------------

    def queue_text(self, text, **kw):
        self.transport.queue.append(FakeTransport.text(text, **kw))

    def queue_tool_calls(self, calls, **kw):
        self.transport.queue.append(FakeTransport.tool_calls(calls, **kw))

    def queue_json(self, obj, **kw):
        self.transport.queue.append(FakeTransport.json_text(obj))

    def queue_error(self, status, message, **kw):
        self.transport.queue.append(FakeTransport.error(status, message, **kw))

    def last_request(self):
        return self.transport.requests[-1]

    # -- chat helpers -----------------------------------------------------------

    def open_chat(self, user, agent=None, res_model=None, res_id=None, title=DEFAULT_CHAT_TITLE):
        """Create an ``ow_ai_chat`` channel (and its root session) for ``user``.

        The channel gets a custom ``title`` by default, so the first message
        does NOT enqueue a ``channel_title`` job (which would consume a
        queued transport response of its own, after the turn's responses).
        Pass ``title=None`` to get the agent-named channel a real systray
        chat starts with, and queue the title response last.
        """
        agent = agent or self.env.ref('ow_ai.agent_default')
        return agent._create_chat_channel(user, title=title, res_model=res_model, res_id=res_id)

    def chat_session(self, channel):
        """The (sudo) root ``ow.ai.session`` of ``channel``."""
        return channel.sudo().ow_ai_session_ids[:1]

    def say(self, channel, text, user, *, expect_failure=False, **kw):
        """Post ``text`` as ``user`` in ``channel`` and submit it to the agent.

        Returns the ``agent_round`` job; with inline jobs the whole turn has
        already run (up to its answer or its next pause) when this returns.
        Unless ``expect_failure`` is true, asserts that no job of this
        session ended up ``failed`` as a result of this call -- a job
        failure inside an inline run never raises into the caller (see
        ``ow.ai.job._run_inline``), so a test could otherwise pass while
        silently exercising a broken turn instead of the one it meant to.
        """
        session = self.chat_session(channel)
        failed_before = set(session.job_ids.filtered(lambda job: job.state == 'failed').ids)
        user_env = self.env(user=user)
        message = channel.with_env(user_env).message_post(body=text, message_type='comment')
        job = loop.submit_user_message(session, user_env, message._ow_ai_to_parts(), **kw)
        self._assert_no_new_job_failures(session, failed_before, expect_failure)
        return job

    def resume(self, channel, response, user, token=None, *, client_identifier=None, expect_failure=False):
        """Answer the session's pending card/client tool as ``user``.

        ``token`` defaults to the session's current ``resume_token``;
        ``client_identifier`` is the answering browser tab. See ``say()``'s
        docstring for ``expect_failure``.
        """
        session = self.chat_session(channel)
        failed_before = set(session.job_ids.filtered(lambda job: job.state == 'failed').ids)
        token = session.resume_token if token is None else token
        result = loop.resume_pending(
            self.env(user=user), session, response, resume_token=token, client_identifier=client_identifier)
        self._assert_no_new_job_failures(session, failed_before, expect_failure)
        return result

    def _assert_no_new_job_failures(self, session, failed_before, expect_failure):
        if expect_failure:
            return
        newly_failed = session.job_ids.filtered(
            lambda job: job.state == 'failed' and job.id not in failed_before)
        if newly_failed:
            self.fail(
                f"Unexpected job failure(s): {newly_failed.mapped('error')} "
                "(pass expect_failure=True if this is intentional)")

    def agent_messages(self, channel):
        """All messages the agent posted in ``channel``, oldest first."""
        agent_partner = channel.sudo().ow_ai_agent_id.partner_id
        return self.env['mail.message'].sudo().search([
            ('model', '=', 'discuss.channel'),
            ('res_id', '=', channel.id),
            ('author_id', '=', agent_partner.id),
        ], order='id asc')

    def last_answer(self, channel):
        """Plain text of the agent's last visible (non-internal) comment."""
        answers = self.agent_messages(channel).filtered(
            lambda message: message.message_type == 'comment' and not message.is_internal)
        return html2plaintext(answers[-1:].body or '') if answers else ''
