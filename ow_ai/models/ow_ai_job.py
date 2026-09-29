# -*- coding: utf-8 -*-
"""``ow.ai.job``: the queue that runs model rounds on cron workers.

The HTTP request that submits a chat message (or resumes a paused tool
batch) only stores the turn and enqueues a job; the ``ow_ai.ir_cron_job_runner``
cron claims jobs one by one (``_claim_one``: ``FOR UPDATE SKIP LOCKED``,
committed at once so a crashed worker leaves a ``running`` row for the
sweeper) and runs each (``_run_one``) as the job's own user -- never as
the cron's superuser -- inside a savepoint, committing after every job
through ``ir.cron._commit_progress``.

Inline mode (context key ``ow_ai_inline_jobs``, or a registry attribute of
the same name for HTTP tests): a job runs as soon as it is enqueued, in the
caller's transaction, followed by any job it enqueued for the same session
(a loop, not a recursion). Tests use it, and they must never commit: in
test mode (``config['test_enable']``) the queue never commits either. The
context keys ``ow_ai_inline_jobs`` / ``ow_ai_inline_nested`` only count in
test mode: any client can send a context key, and an inline job would run
the model inside that client's HTTP request.

Other modules add job kinds: extend ``kind`` with ``selection_add`` and
define ``_run_kind_<kind>(env, session)`` (see :meth:`OwAiJob._run_kind`);
a handler raises :class:`JobFailed` for an expected failure with a short,
user-facing message.
"""
from __future__ import annotations

import logging
import os
import socket
import traceback
from datetime import timedelta

import psycopg2.extensions
from odoo import SUPERUSER_ID, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import clean_context, config

from ..engine import loop
from ..provider.errors import AIProviderError

_logger = logging.getLogger(__name__)

_BACKOFF_SECONDS = (5, 20, 60)
_MAX_ATTEMPTS = 3
_MAX_RETRY_AFTER_SECONDS = 300
_MAX_JOBS_PER_RUN = 50
_TIMEOUT_MARGIN_SECONDS = 15
_INVALID_USER_TEXT = "Invalid job user"
_STALE_ROUND_TEXT = "The session is no longer waiting for this round."
_SUPERSEDED_TEXT = "Superseded by the turn-id upgrade"
_CLAIM_SQL = """
UPDATE ow_ai_job SET state='running', claimed_at=(now() at time zone 'UTC'), claimed_by=%s, attempt=attempt+1
 WHERE id = (SELECT id FROM ow_ai_job WHERE state='pending' AND scheduled_at <= (now() at time zone 'UTC')
             ORDER BY scheduled_at, id LIMIT 1 FOR UPDATE SKIP LOCKED)
 RETURNING id
"""


def _test_context_flag(env, key) -> bool:
    """The context flag ``key``, honoured in test mode only (a client can send any context key)."""
    return bool(config['test_enable'] and env.context.get(key))


def is_inline(env) -> bool:
    """True when jobs run immediately in the caller's transaction."""
    return bool(getattr(env.registry, 'ow_ai_inline_jobs', False) or _test_context_flag(env, 'ow_ai_inline_jobs'))


def _may_commit(env) -> bool:
    return not is_inline(env) and not config['test_enable']


class JobFailed(Exception):
    """Raised by a job handler to fail its job with a short, user-facing message.

    Everything the handler did is rolled back with ``_run_one``'s savepoint;
    ``user_message`` becomes the job's failure message (``_fail``), ``detail``
    (optional) goes into ``error`` for administrators instead.
    """

    def __init__(self, user_message, detail=None):
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail


class OwAiJob(models.Model):
    _name = 'ow.ai.job'
    _description = 'AI Job'
    _order = 'id desc'

    session_id = fields.Many2one('ow.ai.session', required=True, ondelete='cascade', index=True)
    kind = fields.Selection(
        [('agent_round', "Agent round"), ('channel_title', "Channel title")], required=True)
    state = fields.Selection([
        ('pending', "Pending"),
        ('running', "Running"),
        ('done', "Done"),
        ('failed', "Failed"),
        ('cancelled', "Cancelled"),
    ], required=True, default='pending', index=True)
    user_id = fields.Many2one('res.users', required=True, ondelete='cascade')
    # Request context, job input and failure detail (provider messages,
    # tracebacks) are system-only: AI managers keep the job list, states
    # and the Retry/Cancel buttons, not the data. The queue itself always
    # works on `sudo()` jobs.
    context = fields.Json(default=dict, groups='base.group_system')
    payload = fields.Json(default=dict, groups='base.group_system')
    scheduled_at = fields.Datetime(required=True, default=fields.Datetime.now, index=True)
    claimed_at = fields.Datetime()
    claimed_by = fields.Char()
    finished_at = fields.Datetime()
    attempt = fields.Integer(default=0)
    error = fields.Text(groups='base.group_system')
    can_retry = fields.Boolean(compute='_compute_can_retry')

    _state_scheduled_at_idx = models.Index('(state, scheduled_at)')

    # -- enqueueing -------------------------------------------------------------

    @api.model
    def _enqueue(self, session, kind, *, user, context, payload=None, at=None, values=None, wake=True):
        """Queue a ``kind`` job for ``session``, to run as ``user`` with ``context``.

        Wakes the runner cron (at ``at``, default now), or runs the job right
        away in inline mode. Returns the job. An ``agent_round`` job records
        the round, requesting user and turn it was queued for (see
        :meth:`_is_current_round`). ``values`` are extra field values of the
        new job (e.g. lookup fields another module adds); they never
        override the arguments above. With ``wake=False`` the caller wakes
        the runner itself (:meth:`_wake_runner`), e.g. once after queuing
        many jobs. The caller's ``default_*`` context keys are ignored.
        """
        payload = dict(payload or {})
        if kind == 'agent_round':
            payload.setdefault('request_round', session.request_round)
            payload.setdefault('request_user_id', session.request_user_id.id)
            payload.setdefault('request_turn', session.request_turn)
        job = self.sudo().with_context(clean_context(self.env.context)).create({
            # first: extra values never override the job's own keys
            **(values or {}),
            'session_id': session.id,
            'kind': kind,
            'user_id': user.id,
            'context': dict(context or {}),
            'payload': payload,
            # The transaction's own timestamp: the same clock the claim SQL
            # compares against, so the job is claimable at once.
            'scheduled_at': at or self.env.cr.now(),
        })
        if is_inline(self.env):
            if not _test_context_flag(self.env, 'ow_ai_inline_nested'):
                job._run_inline()
            return job
        if wake:
            self._wake_runner(at)
        return job

    @api.model
    def _wake_runner(self, at=None):
        """Schedule the runner cron (``ir.cron._trigger``) now or at ``at``."""
        cron = self.sudo().env.ref('ow_ai.ir_cron_job_runner', raise_if_not_found=False)
        if cron:
            cron._trigger(at)

    def _run_inline(self):
        """Run this job now, then each new pending job it left for its session."""
        self.ensure_one()
        seen = set()
        job = self
        for _iteration in range(max(self.session_id.request_round_limit, 1) + 2):
            seen.add(job.id)
            job._run_one()
            job = self.search([
                ('session_id', '=', self.session_id.id),
                ('state', '=', 'pending'),
                ('id', 'not in', list(seen)),
            ], order='id', limit=1)
            if not job:
                return

    # -- cron: claim and run ----------------------------------------------------

    @api.model
    def _cron_run_jobs(self):
        """Runner cron: claim and run ready jobs until none is left or time runs out.

        After each job, ``ir.cron._commit_progress`` commits and returns the
        seconds left in this cron run; with less than one more model call
        (``ow_ai.timeout``) plus a margin left -- or after 50 jobs -- the
        run stops and re-triggers itself for the jobs still waiting, so a
        run never overstays its time budget and no ready job waits for the
        cron's fallback interval.
        """
        timeout = self.env['ir.config_parameter'].sudo().get_int('ow_ai.timeout', 120)
        for _index in range(_MAX_JOBS_PER_RUN):
            job_id = self._claim_one()
            if not job_id:
                return
            self.browse(job_id)._run_claimed()
            time_left = self._commit_job_progress()
            if time_left < timeout + _TIMEOUT_MARGIN_SECONDS:
                break
        if self._count_ready_jobs():
            # Committed by the cron framework right after this method returns.
            self._wake_runner()

    @api.model
    def _claim_one(self):
        """Atomically mark the oldest ready job ``running``; return its id or None.

        Committed at once (outside tests/inline mode) so the claim survives
        a crash of the round itself: the sweeper finds the ``running`` row.
        """
        self.flush_model()
        claimed_by = f"{socket.gethostname()}:{os.getpid()}"
        self.env.cr.execute(_CLAIM_SQL, (claimed_by,))
        row = self.env.cr.fetchone()
        if _may_commit(self.env):
            self.env.cr.commit()
            self.env.invalidate_all()
        else:
            self.invalidate_model()
        return row[0] if row else None

    @api.model
    def _count_ready_jobs(self):
        return self.search_count([('state', '=', 'pending'), ('scheduled_at', '<=', self.env.cr.now())])

    @api.model
    def _commit_job_progress(self):
        """Commit one finished job; return the seconds left in this cron run.

        ``ir.cron._commit_progress(processed)`` commits, and returns
        ``cron_end_time - now`` inside a cron run (``inf`` outside one). No
        ``remaining`` count is reported: leftover jobs are handed to a fresh
        run through a trigger instead (see :meth:`_cron_run_jobs`).
        """
        if not _may_commit(self.env):
            self.env.flush_all()
            return float('inf')
        return self.env['ir.cron']._commit_progress(1)

    def _run_claimed(self):
        """``_run_one`` for the cron: a crash outside the job's savepoint only fails this job.

        The claim is already committed; everything else this job did is
        rolled back, then the job (and, if possible, its turn) is failed.
        """
        try:
            self._run_one()
            # Flush here, still inside the try: a flush conflict on the
            # job's own final-state write is a crash like any other and
            # must go through the same recovery below, not fail the whole
            # cron run.
            self.env.flush_all()
        except Exception:
            if not _may_commit(self.env):
                raise
            detail = traceback.format_exc()
            _logger.exception("ow_ai job %s crashed", self.id)
            self.env.cr.rollback()
            try:
                with self.env.cr.savepoint():
                    self._fail(loop.GENERIC_ERROR_TEXT, detail=detail)
            except Exception:
                _logger.exception("ow_ai job %s: could not end its turn; the sweeper will", self.id)
                self.sudo().write({'state': 'failed', 'finished_at': fields.Datetime.now(), 'error': detail})

    def _run_one(self):
        """Run this job as its own user; record success, retry or failure."""
        self.ensure_one()
        job = self.sudo()
        if job.state not in ('pending', 'running'):
            return
        if job.state == 'pending':
            # Inline mode (or a direct call): nobody claimed it through SQL.
            job.write({
                'state': 'running', 'claimed_at': fields.Datetime.now(), 'claimed_by': 'inline',
                'attempt': job.attempt + 1,
            })
        env = job._get_user_env()
        if env is None:
            job._fail(_INVALID_USER_TEXT)
            return
        session = job.session_id
        if job.kind == 'agent_round' and not job._is_current_round():
            # Stale job (the turn already ended or moved on): never touch the session.
            job._fail(_STALE_ROUND_TEXT)
            return

        try:
            with self.env.cr.savepoint():
                job._run_kind(env, session)
        except AIProviderError as exc:
            job._handle_provider_error(exc)
        except psycopg2.extensions.TransactionRollbackError as exc:
            job._handle_conflict(exc)
        except JobFailed as exc:
            job._fail(exc.user_message, detail=exc.detail)
        except Exception:
            _logger.exception("ow_ai job %s (%s) failed", job.id, job.kind)
            job._fail(loop.GENERIC_ERROR_TEXT, detail=traceback.format_exc())
        else:
            job.write({'state': 'done', 'finished_at': fields.Datetime.now(), 'error': False})

    def _run_kind(self, env, session):
        """Run this job's work as ``env``'s user (inside ``_run_one``'s savepoint).

        Dispatches to ``_run_kind_<kind>(env, session)``: a module adding a
        job kind extends the ``kind`` selection and defines that method.
        """
        handler = getattr(self, f'_run_kind_{self.kind}', None)
        if handler is None:
            raise JobFailed(loop.GENERIC_ERROR_TEXT, detail=f"No handler for AI job kind {self.kind!r}")
        handler(env, session)

    def _run_kind_agent_round(self, env, session):
        loop.run_round(env, session, self)

    def _run_kind_channel_title(self, env, session):
        loop.run_channel_title(env, session, self)

    def _is_current_round(self):
        """True while this round job is the one the session is waiting for.

        The session must still wait on the model, for this job's user, and --
        when the job recorded them at enqueue time -- for the same round,
        requesting user and turn: a leftover job of an earlier round (or
        turn) of the same user must never run against the session's current
        one. The turn id catches what the round number alone cannot: every
        turn's round numbering restarts at 1, so a stale job from a
        finished turn A could otherwise pass as round 1 of a new turn B for
        the same user. A job without a turn id (enqueued before jobs carried
        one) is therefore never current once the session has one; only when
        neither has a turn id (a turn from before the upgrade still going on)
        does the round/user check decide alone.
        """
        session = self.session_id
        if session.loop_state != 'waiting_model' or session.request_user_id != self.user_id:
            return False
        payload = self.payload or {}
        if 'request_round' in payload and payload['request_round'] != session.request_round:
            return False
        if 'request_user_id' in payload and payload['request_user_id'] != session.request_user_id.id:
            return False
        if (payload.get('request_turn') or False) != (session.request_turn or False):
            return False
        return True

    @api.model
    def _ow_ai_supersede_turnless_rounds(self):
        """Fail every pending or failed round job without a turn id (queued
        before jobs carried one) that can no longer run (not
        :meth:`_is_current_round`), keeping a failed job's own error below.

        Run once by the ``20.0.1.2.1`` upgrade; a round of a turn from before
        the upgrade that the session still waits for is left to finish.
        """
        jobs = self.sudo().search([('kind', '=', 'agent_round'), ('state', 'in', ('pending', 'failed'))])
        now = fields.Datetime.now()
        for job in jobs.filtered(lambda job: not (job.payload or {}).get('request_turn')):
            if job._is_current_round():
                continue
            job.write({
                'state': 'failed',
                'finished_at': job.finished_at or now,
                'error': '\n\n'.join(filter(None, [_SUPERSEDED_TEXT, job.error])),
            })

    def _get_user_env(self):
        """The job user's own environment (``su=False``), or None when not allowed.

        Companies the user can no longer access are dropped from the stored
        context instead of failing the environment.
        """
        self.ensure_one()
        user = self.user_id
        if not user or not user.active or user.id == SUPERUSER_ID or not user._is_internal():
            return None
        ctx = dict(self.context or {})
        ctx.pop('uid', None)
        user_company_ids = set(user._get_company_ids())
        allowed = [company_id for company_id in ctx.get('allowed_company_ids') or [] if company_id in user_company_ids]
        if allowed:
            ctx['allowed_company_ids'] = allowed
        else:
            ctx.pop('allowed_company_ids', None)
        if is_inline(self.env):
            ctx.update(ow_ai_inline_jobs=True, ow_ai_inline_nested=True)
        env = api.Environment(self.env.cr, user.id, ctx)
        if env.su or env.uid == SUPERUSER_ID:
            return None
        return env

    # -- outcomes ---------------------------------------------------------------

    def _handle_provider_error(self, exc):
        """Retry a transient provider error with backoff, else fail the job."""
        self.ensure_one()
        if exc.retryable and self.attempt < _MAX_ATTEMPTS:
            retry_after = getattr(exc, 'retry_after', None)
            if retry_after:
                delay = min(retry_after, _MAX_RETRY_AFTER_SECONDS)
            else:
                delay = _BACKOFF_SECONDS[min(self.attempt, len(_BACKOFF_SECONDS)) - 1]
            self._requeue(fields.Datetime.now() + timedelta(seconds=delay), error=str(exc) or exc.code)
            return
        self._fail(exc.user_message, detail=str(exc) or exc.code)

    def _handle_conflict(self, exc):
        """A concurrent update rolled the round back: requeue it once, then give up."""
        self.ensure_one()
        payload = dict(self.payload or {})
        if payload.get('conflict_retries', 0) >= 1:
            _logger.warning("ow_ai job %s: concurrent update again, giving up", self.id)
            self._fail(loop.GENERIC_ERROR_TEXT, detail=str(exc))
            return
        payload['conflict_retries'] = payload.get('conflict_retries', 0) + 1
        self.payload = payload
        self._requeue(fields.Datetime.now(), error=str(exc))

    def _requeue(self, at, *, error):
        self.write({'state': 'pending', 'scheduled_at': at, 'claimed_at': False, 'claimed_by': False, 'error': error})
        if not is_inline(self.env):
            self._wake_runner(at)

    def _fail(self, user_message, *, detail=None):
        """Mark the jobs failed; failing the round a session waits for also ends that turn.

        ``user_message`` is short and user-facing (it is posted in the chat
        and stored as the session's ``last_error``); ``detail`` (a provider
        message, a traceback, ...) only ever goes into ``error``.
        """
        now = fields.Datetime.now()
        for job in self.sudo():
            is_current_round = job.kind == 'agent_round' and job._is_current_round()
            job.write({'state': 'failed', 'finished_at': now, 'error': detail or user_message})
            if is_current_round:
                job.session_id._finish_exchange('failed', error_text=user_message)

    # -- admin views (Reports › Jobs) --------------------------------------------

    def _check_manager(self):
        if not self.env.user.has_group('ow_ai.group_ai_manager'):
            raise AccessError(self.env._("Only AI managers can do this."))

    @api.depends('state', 'kind', 'session_id.loop_state', 'session_id.request_round',
                 'session_id.request_user_id', 'session_id.request_turn')
    def _compute_can_retry(self):
        for job in self:
            # sudo: `payload` is system-only; only the yes/no answer is exposed.
            job.can_retry = job.state == 'failed' and (
                job.kind != 'agent_round' or job.sudo()._is_current_round())

    def action_retry(self):
        """Manager button: requeue a failed job and wake the runner cron.

        A failed round is only retried while its session still waits on the
        model for that very round; once the turn ended or moved on,
        requeueing it would only fail again as stale, so it is refused.
        """
        self._check_manager()
        jobs = self.sudo().filtered(lambda job: job.state == 'failed')
        if any(job.kind == 'agent_round' and not job._is_current_round() for job in jobs):
            raise UserError(self.env._("This job can no longer be retried"))
        jobs.write({
            'state': 'pending', 'scheduled_at': fields.Datetime.now(),
            'claimed_at': False, 'claimed_by': False, 'error': False,
        })
        if jobs:
            self._wake_runner()

    def action_cancel(self):
        """Manager button: cancel a job still pending or already failed."""
        self._check_manager()
        jobs = self.sudo().filtered(lambda job: job.state in ('pending', 'failed'))
        jobs.write({'state': 'cancelled', 'finished_at': fields.Datetime.now()})
