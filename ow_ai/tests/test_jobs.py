# -*- coding: utf-8 -*-
"""The ``ow.ai.job`` queue: claiming, running as the job's user, retrying,
failing, the sweeper and garbage collection.

Most tests run jobs "async" (``ow_ai_inline_jobs=False``): the turn is
submitted, which leaves a pending job and a cron trigger, and the test then
runs that job explicitly with ``_run_one()``/``_cron_run_jobs()``. In test
mode (``config['test_enable']``) the queue never commits.
"""
from datetime import timedelta
from unittest.mock import patch

import psycopg2.errors
from odoo import SUPERUSER_ID, api, fields
from odoo.exceptions import UserError
from odoo.modules.module import load_script
from odoo.tests import TransactionCase, new_test_user
from odoo.tools import config, mute_logger

from ..engine import loop
from ..engine.types import text_part
from ..models.ow_ai_job import JobFailed
from ..provider.errors import ProviderError
from ..utils import params
from .test_engine_loop import EngineLoopCase

_JOB_LOGGER = 'odoo.addons.ow_ai.models.ow_ai_job'


class JobCase(EngineLoopCase):

    def async_env(self, user):
        return self.env(user=user, context=dict(self.env.context, ow_ai_inline_jobs=False))

    def submit_async(self, text='Hi', user=None, channel=None):
        """Submit a user message without running its job; returns the pending job."""
        user = user or self.user
        channel = channel or self.channel
        return loop.submit_user_message(self.chat_session(channel), self.async_env(user), [text_part(text)])

    def runner_triggers(self):
        cron = self.env.ref('ow_ai.ir_cron_job_runner')
        return self.env['ir.cron.trigger'].sudo().search([('cron_id', '=', cron.id)])


class TestEnqueue(JobCase):

    def test_async_enqueue_creates_pending_job_and_cron_trigger(self):
        triggers_before = self.runner_triggers()

        job = self.submit_async()

        self.assertEqual(job.state, 'pending')
        self.assertEqual(job.kind, 'agent_round')
        self.assertEqual(job.user_id, self.user)
        self.assertEqual(job.session_id, self.session)
        self.assertEqual(job.context['uid'], self.user.id)
        self.assertEqual(job.attempt, 0)
        self.assertEqual(len(self.runner_triggers() - triggers_before), 1)
        self.assertEqual(self.session.loop_state, 'waiting_model')
        self.assertEqual(self.session.request_user_id, self.user)
        self.assertEqual(self.session.request_round, 1)
        self.assertEqual(self.session.request_round_limit, 30)
        self.assertEqual(self.typing[-1], (self.session.id, True))
        self.assertEqual(self.transport.requests, [])

    def test_extra_context_is_merged_into_request_context(self):
        job = self.submit_async()
        self.assertNotIn('current_view_info', job.context)

        session = self.session
        session.write({'loop_state': 'ready', 'request_round': 0, 'request_round_limit': 0})
        user_env = self.async_env(self.user)
        extra_context = {'current_view_info': {'view_type': 'list'}, 'client_identifier': None}
        job = loop.submit_user_message(session, user_env, [text_part('Again')], extra_context=extra_context)

        self.assertEqual(session.request_context['current_view_info'], {'view_type': 'list'})
        self.assertNotIn('client_identifier', session.request_context)
        self.assertEqual(job.context['current_view_info'], {'view_type': 'list'})

    def test_cron_run_jobs_processes_every_round(self):
        self.make_available('ow_ai.tool_search')
        self.queue_tool_calls([('search', {'model_name': 'res.partner'}, 'call_1')])
        self.queue_text('Done.')
        self.submit_async()

        self.env['ow.ai.job'].with_context(ow_ai_inline_jobs=False)._cron_run_jobs()

        jobs = self.session.job_ids
        self.assertEqual(len(jobs), 2)
        self.assertEqual(set(jobs.mapped('state')), {'done'})
        self.assertTrue(all(job.claimed_by and job.claimed_at and job.attempt == 1 for job in jobs))
        self.assertEqual(self.session.loop_state, 'ready')
        self.assertEqual(self.last_answer(self.channel), 'Done.')

    def test_cron_run_stops_when_out_of_time_and_retriggers(self):
        self.make_available('ow_ai.tool_search')
        self.queue_tool_calls([('search', {'model_name': 'res.partner'}, 'call_1')])
        first = self.submit_async()
        Job = self.env['ow.ai.job'].with_context(ow_ai_inline_jobs=False)
        triggers_before = self.runner_triggers()

        # Less time left than one model call (ow_ai.timeout) + margin.
        with patch.object(type(Job), '_commit_job_progress', return_value=10.0):
            Job._cron_run_jobs()

        self.assertEqual(first.state, 'done')
        second = self.session.job_ids - first
        self.assertEqual(second.state, 'pending')
        self.assertEqual(self.session.loop_state, 'waiting_model')
        # One trigger from enqueueing the second round, one to resume the run.
        self.assertEqual(len(self.runner_triggers() - triggers_before), 2)
        self.assertEqual(len(self.transport.requests), 1)

    def test_enqueue_stores_extra_values(self):
        job = self.env['ow.ai.job'].with_context(ow_ai_inline_jobs=False)._enqueue(
            self.session, 'channel_title', user=self.user, context={}, values={'claimed_by': 'test-values'})
        self.assertEqual(job.claimed_by, 'test-values')
        self.assertEqual(job.state, 'pending')

    def test_the_inline_context_flag_only_counts_in_test_mode(self):
        # a client can send any context key: outside tests the job is queued
        triggers_before = self.runner_triggers()
        with patch.dict(config._runtime_options, {'test_enable': False}):
            job = self.env['ow.ai.job'].with_context(ow_ai_inline_jobs=True)._enqueue(
                self.session, 'channel_title', user=self.user, context={})
        self.assertEqual(job.state, 'pending')
        self.assertEqual(len(self.runner_triggers() - triggers_before), 1)
        self.assertEqual(self.transport.requests, [])

    def test_default_keys_of_the_context_are_not_job_values(self):
        job = self.env['ow.ai.job'].with_context(
            ow_ai_inline_jobs=False, default_state='done', default_attempt=3, default_claimed_by='x',
        )._enqueue(self.session, 'channel_title', user=self.user, context={})
        self.assertEqual((job.state, job.attempt, job.claimed_by), ('pending', 0, False))

    def test_extra_values_never_override_the_job_arguments(self):
        admin = self.env.ref('base.user_admin')
        job = self.env['ow.ai.job'].with_context(ow_ai_inline_jobs=False)._enqueue(
            self.session, 'channel_title', user=self.user, context={},
            values={'user_id': admin.id, 'kind': 'agent_round', 'session_id': False})
        self.assertEqual((job.user_id, job.kind, job.session_id), (self.user, 'channel_title', self.session))


class TestRunOne(JobCase):

    def test_tools_run_as_the_job_user(self):
        seen = []

        def record_env(ctx):
            seen.append((ctx.env.uid, ctx.env.su, ctx.env.context.get('lang'), ctx.record))
            return "ok"

        tool = self.register_builtin('test.record_env', record_env, 'test_record_env')
        channel = self.open_chat(self.user, res_model='res.partner', res_id=self.partner.id)
        session = self.chat_session(channel)
        self.make_available(tool, session=session)
        self.queue_tool_calls([('test_record_env', {}, 'call_1')])
        self.queue_text('Done.')
        job = self.submit_async(channel=channel)

        job._run_one()
        session.job_ids.filtered(lambda other: other.state == 'pending')._run_one()

        self.assertEqual(len(seen), 1)
        uid, su, lang, record = seen[0]
        self.assertEqual(uid, self.user.id)
        self.assertFalse(su)
        self.assertEqual(lang, self.user.lang)
        self.assertEqual(record, self.partner)
        self.assertEqual(record.env.uid, self.user.id)
        self.assertFalse(record.env.su)
        self.assertEqual(session.loop_state, 'ready')

    def test_unauthorised_companies_are_dropped_from_the_job_context(self):
        other_company = self.env['res.company'].create({'name': "Other Co"})
        seen = []

        def record_companies(ctx):
            seen.append(ctx.env.companies.ids)
            return "ok"

        tool = self.register_builtin('test.companies', record_companies, 'test_companies')
        self.make_available(tool)
        self.queue_tool_calls([('test_companies', {}, 'call_1')])
        job = self.submit_async()
        job.context = dict(job.context, allowed_company_ids=[self.user.company_id.id, other_company.id])

        job._run_one()

        self.assertEqual(job.state, 'done')
        self.assertEqual(seen, [[self.user.company_id.id]])

    def test_inactive_user_fails_without_request(self):
        job = self.submit_async()
        self.user.active = False

        job._run_one()

        self.assertEqual(job.state, 'failed')
        self.assertEqual(self.transport.requests, [])
        self.assertEqual(self.session.loop_state, 'ready')
        self.assertEqual(self.session.last_error, 'Invalid job user')

    def test_portal_user_fails_without_request(self):
        portal = new_test_user(self.env, login='ow_ai_job_portal', groups='base.group_portal')
        channel = self.open_chat(portal)
        job = self.submit_async(user=portal, channel=channel)

        job._run_one()

        self.assertEqual(job.state, 'failed')
        self.assertEqual(self.transport.requests, [])
        self.assertEqual(self.chat_session(channel).loop_state, 'ready')

    def test_superuser_job_fails_closed(self):
        job = self.submit_async()
        job.user_id = SUPERUSER_ID
        self.session.request_user_id = SUPERUSER_ID

        job._run_one()

        self.assertEqual(job.state, 'failed')
        self.assertEqual(self.transport.requests, [])

    def test_round_for_a_session_no_longer_waiting_fails_quietly(self):
        job = self.submit_async()
        self.session.write({'loop_state': 'ready', 'request_round': 0, 'request_round_limit': 0})

        job._run_one()

        self.assertEqual(job.state, 'failed')
        self.assertEqual(job.error, "The session is no longer waiting for this round.")
        self.assertFalse(self.session.last_error)
        self.assertEqual(self.transport.requests, [])
        self.assertEqual(self.session.loop_state, 'ready')
        self.assertFalse(self.agent_messages(self.channel))

    def test_round_job_is_pinned_to_its_round(self):
        job = self.submit_async()
        self.assertEqual(job.payload['request_round'], 1)
        self.assertEqual(job.payload['request_user_id'], self.user.id)
        # The same user's turn moved on to a later round: this leftover job
        # must not run against it.
        self.session.request_round = 2

        job._run_one()

        self.assertEqual(job.state, 'failed')
        self.assertEqual(job.error, "The session is no longer waiting for this round.")
        self.assertEqual(self.transport.requests, [])
        self.assertEqual(self.session.loop_state, 'waiting_model')

    def test_next_round_job_records_its_round(self):
        self.make_available('ow_ai.tool_search')
        self.queue_tool_calls([('search', {'model_name': 'res.partner'}, 'call_1')])
        first = self.submit_async()

        first._run_one()

        second = self.session.job_ids - first
        self.assertEqual(second.payload['request_round'], 2)
        self.assertEqual(self.session.request_round, 2)

    def test_finished_job_is_not_run_again(self):
        self.queue_text('Hello.')
        job = self.submit_async()
        job._run_one()

        job._run_one()

        self.assertEqual(len(self.transport.requests), 1)
        self.assertEqual(job.attempt, 1)

    def test_job_failed_fails_the_job_with_its_message_and_rolls_back(self):
        job = self.submit_async()

        def failing_round(env, session, job):
            self.partner.sudo().write({'city': 'Nowhere'})
            raise JobFailed("Nothing to do", detail="handler detail")

        with patch.object(loop, 'run_round', side_effect=failing_round):
            job._run_one()

        self.assertEqual(job.state, 'failed')
        self.assertEqual(job.sudo().error, 'handler detail')
        self.assertEqual(self.partner.city, 'Utrecht')

    def test_job_of_an_unknown_kind_fails_without_a_request(self):
        # e.g. a kind whose module was uninstalled while the job waited
        job = self.submit_async()
        self.env.cr.execute("UPDATE ow_ai_job SET kind = 'removed_kind' WHERE id = %s", [job.id])
        job.invalidate_recordset(['kind'])

        job._run_one()

        self.assertEqual(job.state, 'failed')
        self.assertIn("No handler for AI job kind 'removed_kind'", job.sudo().error)
        self.assertEqual(self.transport.requests, [])


class TestRetryAndFailure(JobCase):

    def test_rate_limited_job_is_retried_after_retry_after(self):
        self.queue_error(429, 'slow down', headers={'Retry-After': '7'})
        job = self.submit_async()
        triggers_before = self.runner_triggers()

        job._run_one()

        self.assertEqual(job.state, 'pending')
        self.assertEqual(job.attempt, 1)
        self.assertIn('slow down', job.error)
        delay = (job.scheduled_at - fields.Datetime.now()).total_seconds()
        self.assertTrue(4 <= delay <= 8, delay)
        self.assertEqual(self.session.loop_state, 'waiting_model')
        self.assertFalse(self.agent_messages(self.channel))
        new_triggers = self.runner_triggers() - triggers_before
        self.assertEqual(new_triggers.mapped('call_at'), [job.scheduled_at])

    def test_inline_retry_does_not_loop(self):
        self.queue_error(429, 'slow down', headers={'Retry-After': '7'})

        job = self.say(self.channel, 'Hi', self.user)

        self.assertEqual(job.state, 'pending')
        self.assertEqual(job.attempt, 1)
        self.assertEqual(len(self.transport.requests), 1)

    def test_three_provider_errors_fail_the_job(self):
        for _attempt in range(3):
            self.queue_error(500, 'boom')
        job = self.submit_async()

        job._run_one()
        self.assertEqual((job.state, job.attempt), ('pending', 1))
        first_delay = (job.scheduled_at - fields.Datetime.now()).total_seconds()
        self.assertTrue(3 <= first_delay <= 6, first_delay)
        job._run_one()
        self.assertEqual((job.state, job.attempt), ('pending', 2))
        second_delay = (job.scheduled_at - fields.Datetime.now()).total_seconds()
        self.assertTrue(18 <= second_delay <= 21, second_delay)
        job._run_one()

        self.assertEqual((job.state, job.attempt), ('failed', 3))
        self.assertEqual(len(self.transport.requests), 3)
        self.assertEqual(self.session.loop_state, 'ready')
        self.assertEqual(self.session.last_error, ProviderError.user_message)
        self.assertEqual(
            self.last_answer(self.channel), f"I could not complete this request: {ProviderError.user_message}")
        self.assertEqual(self.typing[-1], (self.session.id, False))

    def test_non_retryable_error_fails_immediately(self):
        self.queue_error(402, 'no credits')
        job = self.submit_async()

        job._run_one()

        self.assertEqual(job.state, 'failed')
        self.assertIn('no credits', job.error)
        self.assertEqual(self.session.last_error, "The AI provider account has insufficient credits.")

    def test_a_key_that_cannot_be_sent_stays_out_of_the_job(self):
        """requests quotes the Authorization header (the key) in its error on a
        key with a line break: the job fails with a fixed text instead."""
        del self.env.registry.ow_ai_transport  # the real RequestsTransport; nothing connects
        params.set_str(self.env, 'ow_ai.base_url', 'http://127.0.0.1:9/v1')
        params.set_str(self.env, 'ow_ai.api_key', 'sk-fake-key\n5c1e9a')
        job = self.submit_async()

        job._run_one()

        self.assertEqual(job.state, 'failed')
        self.assertEqual(job.sudo().error, "The API key contains characters that cannot be sent in a header.")
        self.assertEqual(self.session.last_error, "The AI provider rejected the request.")
        for part in ('sk-fake-key', '5c1e9a'):
            self.assertNotIn(part, self.last_answer(self.channel))

    def test_unexpected_error_never_reaches_the_chat(self):
        job = self.submit_async()

        with patch.object(loop, 'run_round', side_effect=RuntimeError('secret internal detail')), \
                mute_logger(_JOB_LOGGER):
            job._run_one()

        self.assertEqual(job.state, 'failed')
        self.assertIn('Traceback', job.error)
        self.assertIn('secret internal detail', job.error)
        answer = self.last_answer(self.channel)
        self.assertEqual(answer, "I could not complete this request: Something went wrong while answering.")
        self.assertEqual(self.session.last_error, "Something went wrong while answering.")

    def test_serialization_failure_is_requeued_once(self):
        job = self.submit_async()
        conflict = psycopg2.errors.SerializationFailure('could not serialize access')

        with patch.object(loop, 'run_round', side_effect=conflict), mute_logger(_JOB_LOGGER):
            job._run_one()
            self.assertEqual((job.state, job.attempt), ('pending', 1))
            self.assertEqual(self.session.loop_state, 'waiting_model')
            job._run_one()

        self.assertEqual((job.state, job.attempt), ('failed', 2))
        self.assertEqual(self.session.loop_state, 'ready')

    def test_work_done_before_a_failure_is_rolled_back(self):
        def failing_round(env, session, job):
            session._append_event('assistant', {'role': 'assistant', 'content': [{'type': 'text', 'text': 'x'}]})
            raise RuntimeError('late failure')

        job = self.submit_async()

        with patch.object(loop, 'run_round', side_effect=failing_round), mute_logger(_JOB_LOGGER):
            job._run_one()

        self.assertEqual(self.session.event_ids.mapped('role'), ['user'])

    def test_retry_from_an_earlier_turn_is_refused_once_a_new_turn_started(self):
        """A job's round number alone is not enough: every turn restarts at
        round 1, so the round check would otherwise pass a leftover job of a
        finished turn A as round 1 of a fresh turn B of the same user."""
        manager = new_test_user(self.env, login='ow_ai_turn_manager', groups='ow_ai.group_ai_manager')
        job_a = self.submit_async()
        turn_a = self.session.sudo().request_turn
        job_a._fail('boom')  # ends turn A; the session goes back to 'ready'
        self.assertEqual(self.session.loop_state, 'ready')

        job_b = self.submit_async()  # turn B: round 1 again, same user, a new turn id

        self.assertEqual(self.session.request_round, 1)
        self.assertEqual(self.session.request_user_id, self.user)
        self.assertNotEqual(self.session.sudo().request_turn, turn_a)
        self.assertFalse(job_a.with_user(manager).can_retry)

        with self.assertRaises(UserError):
            job_a.with_user(manager).action_retry()

        self.assertEqual(job_a.state, 'failed')
        pending = self.session.job_ids.filtered(lambda job: job.state == 'pending')
        self.assertEqual(pending, job_b)

    def test_a_job_without_a_turn_id_is_stale_once_the_session_has_one(self):
        """A job queued before jobs carried a turn id (no ``request_turn`` in
        its payload) cannot prove which turn it belongs to: once a turn with
        an id started, it is refused, whatever its round and user."""
        manager = new_test_user(self.env, login='ow_ai_legacy_turn_manager', groups='ow_ai.group_ai_manager')
        job_a = self.submit_async()   # turn A, from before the upgrade: no turn id on either side
        payload = dict(job_a.sudo().payload)
        del payload['request_turn']
        job_a.sudo().payload = payload
        self.session.sudo().request_turn = False
        job_a._fail('boom')   # ends turn A
        self.assertEqual(self.session.loop_state, 'ready')

        job_b = self.submit_async()   # turn B: round 1 again, same user

        self.assertEqual((self.session.request_round, self.session.request_user_id), (1, self.user))
        self.assertTrue(self.session.sudo().request_turn)
        self.assertFalse(job_a._is_current_round())
        self.assertFalse(job_a.with_user(manager).can_retry)
        with self.assertRaises(UserError):
            job_a.with_user(manager).action_retry()
        self.assertEqual(job_a.state, 'failed')
        self.assertEqual(self.session.job_ids.filtered(lambda job: job.state == 'pending'), job_b)

    def test_without_turn_ids_on_either_side_the_round_decides(self):
        """A turn started before the upgrade (the session has no turn id either):
        its own job still runs, one of another round does not."""
        job = self.submit_async()
        payload = dict(job.sudo().payload)
        del payload['request_turn']
        job.sudo().payload = payload
        self.session.sudo().request_turn = False

        self.assertTrue(job._is_current_round())
        job.sudo().payload = dict(payload, request_round=2)
        self.assertFalse(job._is_current_round())

    def test_upgrade_supersedes_jobs_without_a_turn_id(self):
        """``19.0.2.1.1``: every pending or failed round without a turn id that
        can no longer run is failed; a round of a turn still waiting on the
        model from before the upgrade is left to finish."""
        def legacy(job):
            payload = dict(job.sudo().payload)
            del payload['request_turn']
            job.sudo().payload = payload
            return job

        failed = legacy(self.submit_async())
        self.session.sudo().request_turn = False   # a turn from before the upgrade
        failed._fail('boom')
        self.assertEqual(self.session.loop_state, 'ready')
        pending = legacy(self.submit_async('Again'))
        self.session.sudo().write({'loop_state': 'ready', 'request_user_id': False, 'request_turn': False})
        other_channel = self.open_chat(self.user)
        current = legacy(self.submit_async(channel=other_channel))
        self.chat_session(other_channel).sudo().request_turn = False   # a turn from before the upgrade
        modern = self.submit_async('Modern', channel=self.open_chat(self.user))
        failed_error = failed.sudo().error

        script = load_script('ow_ai/migrations/19.0.2.1.1/post-repair_agent_partner_flags.py',
                             'odoo.upgrade.ow_ai.test_supersede_legacy_rounds')
        script.migrate(self.env.cr, '19.0.2.1.0')

        for job in (failed, pending):
            with self.subTest(job=job):
                self.assertEqual(job.state, 'failed')
                self.assertTrue(job.sudo().error.startswith("Superseded by the turn-id upgrade"))
                self.assertTrue(job.finished_at)
        self.assertIn(failed_error, failed.sudo().error)
        self.assertEqual((current.state, modern.state), ('pending', 'pending'))

    def test_stale_job_of_a_finished_turn_never_touches_the_new_turn(self):
        """``job_a`` never ran (still pending) when turn A ended some other
        way; turn B then starts for the same user. Running ``job_a`` must
        fail quietly and leave turn B's session state untouched."""
        job_a = self.submit_async()
        # Simulate turn A ending without job_a ever running (e.g. aborted).
        self.session.sudo().write({
            'loop_state': 'ready', 'request_round': 0, 'request_round_limit': 0,
            'request_user_id': False, 'request_turn': False,
        })
        job_b = self.submit_async()
        turn_b = self.session.sudo().request_turn

        job_a._run_one()

        self.assertEqual(job_a.state, 'failed')
        self.assertEqual(job_a.sudo().error, "The session is no longer waiting for this round.")
        self.assertEqual(self.session.loop_state, 'waiting_model')
        self.assertEqual(self.session.request_round, 1)
        self.assertEqual(self.session.sudo().request_turn, turn_b)
        self.assertEqual(job_b.state, 'pending')


class TestSweeper(JobCase):

    def sweep(self):
        self.env['ow.ai.job'].with_context(ow_ai_inline_jobs=False)._cron_sweep()

    def test_stale_running_job_fails_the_session(self):
        job = self.submit_async()
        job.write({'state': 'running', 'attempt': 1, 'claimed_at': fields.Datetime.now() - timedelta(minutes=20)})

        self.sweep()

        self.assertEqual(job.state, 'failed')
        self.assertEqual(self.session.loop_state, 'ready')
        self.assertEqual(self.session.last_error, 'The AI took too long to answer.')
        self.assertEqual(self.typing[-1], (self.session.id, False))

    def test_recent_running_job_is_left_alone(self):
        job = self.submit_async()
        job.write({'state': 'running', 'attempt': 1, 'claimed_at': fields.Datetime.now() - timedelta(minutes=2)})

        self.sweep()

        self.assertEqual(job.state, 'running')
        self.assertEqual(self.session.loop_state, 'waiting_model')

    def test_old_pending_job_fails(self):
        job = self.submit_async()
        job.scheduled_at = fields.Datetime.now() - timedelta(minutes=61)

        self.sweep()

        self.assertEqual(job.state, 'failed')
        self.assertEqual(self.session.loop_state, 'ready')

    def test_the_limits_per_kind_default_to_the_chat_limits(self):
        Job = self.env['ow.ai.job']
        self.assertEqual(Job._sweep_limits('agent_round'), (60, 10))
        params.set_int(self.env, 'ow_ai.job_stale_minutes', 15)
        self.assertEqual(Job._sweep_limits('channel_title'), (60, 15))

    def test_the_sweeper_asks_each_kind_for_its_limits(self):
        # modules with slower job kinds override the hook
        job = self.submit_async()
        job.scheduled_at = fields.Datetime.now() - timedelta(minutes=61)

        with patch.object(type(self.env['ow.ai.job']), '_sweep_limits', lambda self, kind: (120, 10)):
            self.sweep()

        self.assertEqual(job.state, 'pending')

    def test_waiting_model_session_without_job_fails(self):
        job = self.submit_async()
        job.state = 'cancelled'

        self.sweep()

        self.assertEqual(self.session.loop_state, 'ready')
        self.assertTrue(self.session.last_error)
        self.assertEqual(self.typing[-1], (self.session.id, False))

    def test_old_interaction_is_aborted(self):
        call = {'type': 'tool_call', 'name': 'search', 'args': {}, 'call_id': 'call_1'}
        self.session._append_event('user', {'role': 'user', 'content': [{'type': 'text', 'text': 'Hi'}]})
        self.session._append_event('assistant', {'role': 'assistant', 'content': [call]})
        self.session.write({
            'loop_state': 'waiting_confirmation', 'request_round': 1, 'request_round_limit': 30,
            'request_user_id': self.user.id, 'resume_token': 'tok',
            'pending_tool_call': {
                'calls': [call], 'results': [], 'index': 0,
                'user_input_request': {'type': 'confirmation', 'body': 'Sure?'},
            },
        })
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE ow_ai_session SET write_date = %s WHERE id = %s",
            (fields.Datetime.now() - timedelta(hours=25), self.session.id))
        self.session.invalidate_recordset(['write_date'])

        self.sweep()

        self.assertEqual(self.session.loop_state, 'ready')
        self.assertFalse(self.session.resume_token)
        last_event = self.session.event_ids.sorted('sequence')[-1]
        result = last_event.metadata['content'][0]
        self.assertEqual(result['type'], 'tool_result')
        self.assertIn('Aborted', result['result'][0]['text'])

    def test_recent_interaction_is_kept(self):
        self.session.write({
            'loop_state': 'waiting_answer', 'request_round': 1, 'request_round_limit': 30,
            'request_user_id': self.user.id, 'resume_token': 'tok',
            'pending_tool_call': {'calls': [], 'results': [], 'index': 0},
        })

        self.sweep()

        self.assertEqual(self.session.loop_state, 'waiting_answer')


class TestGarbageCollection(JobCase):

    def test_gc_removes_old_finished_jobs(self):
        now = fields.Datetime.now()
        Job = self.env['ow.ai.job'].sudo()
        common = {'session_id': self.session.id, 'kind': 'agent_round', 'user_id': self.user.id}
        old_done = Job.create(dict(common, state='done', finished_at=now - timedelta(days=8)))
        old_failed = Job.create(dict(common, state='failed', finished_at=now - timedelta(days=8)))
        recent_done = Job.create(dict(common, state='done', finished_at=now - timedelta(days=1)))
        old_pending = Job.create(dict(common, state='pending', scheduled_at=now - timedelta(days=8)))

        Job._gc_jobs()

        self.assertFalse(old_done.exists())
        self.assertFalse(old_failed.exists())
        self.assertTrue(recent_done.exists())
        self.assertTrue(old_pending.exists())


class TestClaimConcurrency(TransactionCase):
    """Two real, independent cursors claiming from the same queue.

    ``TransactionCase`` data lives in the test's own uncommitted
    transaction, invisible to other connections, so the fixtures are
    committed through a third, separate cursor (the test cursor itself
    never commits) and deleted again afterwards. The two claiming cursors
    are rolled back, never committed: ``_claim_one`` does not commit in
    test mode, so each keeps its row lock until then, exactly like a
    worker between its claim and its commit.
    """

    def setUp(self):
        super().setUp()
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            session = env['ow.ai.session'].create({'agent_id': env.ref('ow_ai.agent_default').id})
            past = fields.Datetime.now() - timedelta(minutes=1)
            jobs = env['ow.ai.job'].create([{
                'session_id': session.id, 'kind': 'agent_round', 'user_id': env.ref('base.user_admin').id,
                'scheduled_at': past,
            } for _index in range(2)])
            self.session_id = session.id
            self.job_ids = jobs.ids
        self.addCleanup(self._delete_fixtures)

    def _delete_fixtures(self):
        with self.registry.cursor() as cr:
            cr.execute("DELETE FROM ow_ai_session WHERE id = %s", (self.session_id,))

    def test_each_job_is_claimed_once(self):
        cr1, cr2 = self.registry.cursor(), self.registry.cursor()
        try:
            jobs1 = api.Environment(cr1, SUPERUSER_ID, {})['ow.ai.job']
            jobs2 = api.Environment(cr2, SUPERUSER_ID, {})['ow.ai.job']

            first = jobs1._claim_one()
            second = jobs2._claim_one()
            third = jobs1._claim_one()
            fourth = jobs2._claim_one()

            self.assertEqual({first, second}, set(self.job_ids))
            self.assertIsNone(third)
            self.assertIsNone(fourth)
            claimed = jobs1.browse(first)
            self.assertEqual(claimed.state, 'running')
            self.assertEqual(claimed.attempt, 1)
            self.assertTrue(claimed.claimed_by)
            self.assertTrue(claimed.claimed_at)
        finally:
            for cr in (cr1, cr2):
                cr.rollback()
                cr.close()
