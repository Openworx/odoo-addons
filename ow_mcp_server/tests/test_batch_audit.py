"""JSON-RPC batches.

Local patch 2 made each batch sub-call audit its real tool/model/outcome
with a redacted payload. Local patch 5 (M7) goes further and rejects
batches outright (removed in MCP 2025-06-18; they bypassed the per-user
rate limit). The rejection is audited as ONE failed row that carries no
part of the submitted payload.
"""
import json
from datetime import datetime, timedelta

from odoo.tests import HttpCase, tagged

from odoo.addons.ow_mcp_server.protocol import authfail, ratelimit


@tagged('post_install', '-at_install')
class TestBatchAudit(HttpCase):
    def setUp(self):
        super().setUp()
        authfail.reset()
        ratelimit.reset()
        user = self.env['res.users'].create({
            'login': 'mcpbatch', 'name': 'MCP Batch', 'password': 'mcpbatch',
            'groups_id': [
                (4, self.env.ref('ow_mcp_server.group_mcp_user').id),
                (4, self.env.ref('base.group_user').id),
            ],
        })
        self.key = self.env['res.users.apikeys'].with_user(user)._generate(
            'mcp', 'test', datetime.now() + timedelta(days=1),
        )
        partner = self.env.ref('base.model_res_partner').id
        self.env['ow.mcp.model.access'].sudo().search(
            [('model_id', '=', partner)]
        ).unlink()
        self.env['ow.mcp.model.access'].create({
            'model_id': partner, 'allow_read': True,
        })
        self.env['ow.mcp.config'].sudo().get_singleton().audit_log = True

    def _batch(self, n=2):
        body = [
            {'jsonrpc': '2.0', 'id': i, 'method': 'tools/call', 'params': {
                'name': 'search_count',
                'arguments': {'model': 'res.partner', 'password': 's3cr3t'},
            }} for i in range(n)
        ]
        return self.url_open('/mcp', data=json.dumps(body).encode(), headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.key}',
        })

    def test_batch_rejected_and_audited_without_payload(self):
        Log = self.env['ow.mcp.audit.log'].sudo()
        before = Log.search([]).ids
        r = self._batch()
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()['error']['code'], -32600)
        rows = Log.search([('id', 'not in', before)])
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows.success)
        dump = json.dumps(rows.read(['method', 'request_payload',
                                     'response_summary', 'error_message']))
        self.assertNotIn('s3cr3t', dump)

    def test_batch_cannot_dodge_rate_limit(self):
        self.env['ow.mcp.config'].sudo().get_singleton().rate_limit_per_minute = 3
        self.env.flush_all()
        self.addCleanup(ratelimit.reset)
        r = self._batch(200)
        self.assertNotEqual(r.status_code, 200, r.text)
