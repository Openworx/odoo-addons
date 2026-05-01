"""Regression: with ow_mcp_oauth installed, an existing res.users.apikeys
(scope=mcp) keeps authenticating /mcp calls — backward compatibility.
"""
import json
from datetime import datetime, timedelta

from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestApiKeyFallback(HttpCase):
    def setUp(self):
        super().setUp()
        self.user = self.env['res.users'].create({
            'login': 'legacy',
            'name': 'Legacy User',
            'password': 'pass',
            'groups_id': [
                (4, self.env.ref('ow_mcp_server.group_mcp_user').id),
                (4, self.env.ref('base.group_user').id),
            ],
        })
        self.key = (
            self.env['res.users.apikeys'].with_user(self.user)
            ._generate('mcp', 'legacy', datetime.now() + timedelta(days=1))
        )

    def test_apikey_still_works(self):
        r = self.url_open(
            '/mcp',
            data=json.dumps(
                {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'}
            ).encode(),
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {self.key}',
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['result']['protocolVersion'], '2025-06-18')

    def test_invalid_bearer_returns_401_with_resource_metadata(self):
        r = self.url_open(
            '/mcp',
            data=json.dumps(
                {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'}
            ).encode(),
            headers={
                'Content-Type': 'application/json',
                'Authorization': 'Bearer not-a-real-token',
            },
        )
        self.assertEqual(r.status_code, 401)
        challenge = r.headers.get('WWW-Authenticate', '')
        self.assertIn('Bearer', challenge)
        self.assertIn('resource_metadata=', challenge)
