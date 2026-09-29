# -*- coding: utf-8 -*-
"""``ow.ai.job`` maintenance: the sweeper cron and garbage collection.

Split out of ``ow_ai_job.py`` (kept < 300 lines) as a second
``_inherit='ow.ai.job'`` class.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from odoo import api, fields, models

from ..engine import loop

_logger = logging.getLogger(__name__)

_STALE_TEXT = "The AI took too long to answer."
_PENDING_MAX_MINUTES = 60
_INTERACTION_MAX_AGE = timedelta(hours=24)
_GC_AGE = timedelta(days=7)


class OwAiJobSweep(models.Model):
    _inherit = 'ow.ai.job'

    @api.model
    def _sweep_limits(self, kind):
        """``(pending_max_minutes, running_stale_minutes)`` of jobs of ``kind``.

        The sweeper fails a job still pending that long after its scheduled
        time, or running that long after it was claimed. Chat jobs (one model
        call each) get an hour and ``ow_ai.job_stale_minutes`` (10); a module
        adding a slower kind overrides this for it.
        """
        stale_minutes = self.env['ir.config_parameter'].sudo().get_int('ow_ai.job_stale_minutes', 10) or 10
        return _PENDING_MAX_MINUTES, stale_minutes

    @api.model
    def _cron_sweep(self):
        """Fail jobs/turns a dead worker left behind, expire forgotten cards."""
        now = fields.Datetime.now()
        jobs = self.sudo()
        stale = jobs.browse()
        for kind in self._fields['kind'].get_values(self.env):
            pending_minutes, running_minutes = self._sweep_limits(kind)
            stale |= jobs.search([
                ('kind', '=', kind), '|',
                '&', ('state', '=', 'running'), ('claimed_at', '<', now - timedelta(minutes=running_minutes)),
                '&', ('state', '=', 'pending'), ('scheduled_at', '<', now - timedelta(minutes=pending_minutes)),
            ])
        for job in stale:
            self._sweep_step(job._fail, _STALE_TEXT)

        sessions = self.env['ow.ai.session'].sudo()
        waiting = sessions.search([('loop_state', '=', 'waiting_model')])
        busy = jobs.search([
            ('session_id', 'in', waiting.ids), ('kind', '=', 'agent_round'), ('state', 'in', ('pending', 'running')),
        ]).session_id
        for session in waiting - busy:
            self._sweep_step(session._finish_exchange, 'failed', error_text=_STALE_TEXT)

        expired = sessions.search([
            ('loop_state', 'in', loop.INTERACTION_STATES), ('write_date', '<', now - _INTERACTION_MAX_AGE),
        ])
        for session in expired:
            self._sweep_step(session._abort_pending, reason='expired')

    def _sweep_step(self, method, *args, **kwargs):
        try:
            with self.env.cr.savepoint():
                method(*args, **kwargs)
        except Exception:
            _logger.exception("ow_ai sweeper: %s failed", getattr(method, '__name__', method))

    @api.autovacuum
    def _gc_jobs(self):
        """Delete finished jobs older than a week."""
        cutoff = fields.Datetime.now() - _GC_AGE
        self.sudo().search([
            ('state', 'in', ('done', 'failed', 'cancelled')),
            '|', ('finished_at', '<', cutoff), '&', ('finished_at', '=', False), ('create_date', '<', cutoff),
        ]).unlink()
