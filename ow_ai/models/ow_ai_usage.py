# -*- coding: utf-8 -*-
from odoo import fields, models


class OwAiUsage(models.Model):
    _name = 'ow.ai.usage'
    _description = 'AI Usage Log'
    _order = 'create_date desc'

    agent_id = fields.Many2one('ow.ai.agent', ondelete='set null', index=True)
    session_id = fields.Many2one('ow.ai.session', ondelete='set null', index=True)
    user_id = fields.Many2one('res.users', ondelete='set null', index=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    kind = fields.Selection(
        [('chat', "Chat"), ('title', "Chat title"), ('direct', "Direct response"), ('embedding', "Embedding")],
        required=True)
    model = fields.Char()
    request_id = fields.Char()
    prompt_tokens = fields.Integer()
    completion_tokens = fields.Integer()
    total_tokens = fields.Integer()
    cached_tokens = fields.Integer()
    cost = fields.Float(digits=(16, 6), help="USD as reported by the provider (0 when it reports no cost)")
    latency_ms = fields.Integer()
    status = fields.Selection([('ok', "OK"), ('error', "Error")], required=True, default='ok')
    error_code = fields.Char()

    @classmethod
    def _log(cls, env, *, kind, model, usage, status='ok', error_code=None,
              latency_ms=0, agent=None, session=None, user=None, company=None, request_id=''):
        usage = usage or {}
        vals = {
            'kind': kind,
            'model': model,
            'status': status,
            'error_code': error_code,
            'latency_ms': latency_ms,
            'agent_id': agent.id if agent else False,
            'session_id': session.id if session else False,
            'user_id': user.id if user else env.uid,
            'company_id': company.id if company else env.company.id,
            'request_id': request_id,
            'prompt_tokens': usage.get('prompt_tokens', 0),
            'completion_tokens': usage.get('completion_tokens', 0),
            'total_tokens': usage.get('total_tokens', 0),
            'cached_tokens': usage.get('cached_tokens', 0),
            'cost': usage.get('cost', 0.0),
        }
        return env['ow.ai.usage'].sudo().create(vals)
