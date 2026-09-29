# -*- coding: utf-8 -*-
"""Shared TransactionCase base for engine/session tests: installs a
``FakeTransport`` (and fakes for web search, page reads and DNS) so no
test ever makes a real HTTP call, offers small
helpers to queue scripted provider responses and inspect what was sent,
and helpers to drive a whole chat turn (``open_chat``/``say``/``resume``).

``self.env`` carries ``ow_ai_inline_jobs=True``: every ``ow.ai.job`` the
engine enqueues runs immediately, in the test transaction, instead of
waiting for the cron worker. A turn that needs several model rounds runs
them all inside one ``say()`` call, consuming the queued responses in
order.
"""
from __future__ import annotations

import json

from odoo.tests import TransactionCase
from odoo.tools.mail import html2plaintext

from ..engine import loop
from ..provider.transport import FakeTransport
from ..utils import params

DEFAULT_CHAT_TITLE = 'Test chat'
PUBLIC_ADDRESS = '93.184.216.34'
_FAKE_REGISTRY_ATTRS = ('ow_ai_transport', 'ow_ai_web_transport', 'ow_ai_page_session', 'ow_ai_web_resolver')


class FakeWebTransport:
    """Scripted transport for `utils/websearch.py` tests.

    ``queue`` holds a list of ``(status, body_bytes, content_type)``
    tuples, consumed in order for every ``get()`` call; a queued exception
    instance (e.g. ``requests.Timeout()``) is raised instead. ``requests``
    records every call made, as ``{'url', 'params', 'headers'}``.
    """

    def __init__(self, queue=None):
        self.queue = list(queue or [])
        self.requests = []

    def get(self, url, *, params, headers, timeout):
        self.requests.append({'url': url, 'params': params, 'headers': headers})
        if not self.queue:
            raise AssertionError('FakeWebTransport queue is empty')
        item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def fake_resolver(*addresses):
    """A ``socket.getaddrinfo`` stand-in resolving every host to ``addresses``."""
    def resolve(host, port, *args, **kwargs):
        return [(None, None, None, None, (address, port)) for address in addresses]
    return resolve


class FakePageResponse:
    """The part of a streamed ``requests.Response`` that ``webfetch.fetch`` uses."""

    def __init__(self, status, headers, body):
        self.status_code = status
        self.headers = headers
        self.body = body
        self.closed = False

    def iter_content(self, chunk_size):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start:start + chunk_size]

    def close(self):
        self.closed = True


class FakePageSession:
    """Scripted ``requests.Session`` stand-in for page reads (``webfetch.fetch``).

    ``queue`` holds ``FakePageResponse`` objects, consumed in order; a
    queued exception instance (e.g. ``requests.ConnectionError()``) is
    raised instead. ``requests`` records every call made, as
    ``{'url', 'headers'}``: the URL names the address the guard checked,
    the ``Host`` header the host (see ``webfetch._guarded_get``).
    ``adapters`` holds what ``mount`` was given, by prefix.
    """

    def __init__(self):
        self.queue = []
        self.requests = []
        self.adapters = {}

    def mount(self, prefix, adapter):
        self.adapters[prefix] = adapter

    def get(self, url, *, headers, timeout, stream, allow_redirects):
        self.requests.append({'url': url, 'headers': headers})
        if not self.queue:
            raise AssertionError('FakePageSession queue is empty')
        item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class OwAiCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.env = self.env(context=dict(self.env.context, ow_ai_inline_jobs=True))
        self.transport = FakeTransport()
        # Web search/page reads are faked too: no test ever resolves a host
        # or opens a page for real (every host resolves to a public address).
        self.web_transport = FakeWebTransport()
        self.page_session = FakePageSession()
        registry = self.env.registry
        registry.ow_ai_transport = self.transport
        registry.ow_ai_web_transport = self.web_transport
        registry.ow_ai_page_session = self.page_session
        registry.ow_ai_web_resolver = fake_resolver(PUBLIC_ADDRESS)
        self.addCleanup(self._clear_transport)
        params.set_str(self.env, 'ow_ai.api_key', 'sk-or-test')

    def _clear_transport(self):
        for attr in _FAKE_REGISTRY_ATTRS:
            if hasattr(self.env.registry, attr):
                delattr(self.env.registry, attr)

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

    def queue_search(self, results, unresponsive=()):
        """Queue one SearXNG JSON answer: ``results`` in SearXNG's own shape
        (``title``, ``url``, ``content``, ...), ``unresponsive`` as
        ``(engine, reason)`` pairs."""
        body = json.dumps({
            'query': '', 'results': list(results),
            'unresponsive_engines': [list(engine) for engine in unresponsive],
        }).encode()
        self.web_transport.queue.append((200, body, 'application/json'))

    def queue_page(self, html, status=200, content_type='text/html; charset=utf-8'):
        """Queue one page answer for the next page read."""
        body = html.encode('utf-8') if isinstance(html, str) else html
        self.page_session.queue.append(FakePageResponse(status, {'Content-Type': content_type}, body))

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
