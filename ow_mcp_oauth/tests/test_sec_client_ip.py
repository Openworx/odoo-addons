"""Effective_client_ip() ignores a
client-supplied X-Forwarded-For (the DCR per-IP limit keyed on it)."""
from unittest.mock import MagicMock, patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.ow_mcp_oauth.controllers import _utils as u


@tagged('post_install', '-at_install')
class TestOauthClientIp(TransactionCase):
    def test_spoofed_xff_ignored(self):
        req = MagicMock()
        req.httprequest.remote_addr = '198.51.100.2'
        req.httprequest.headers = {'X-Forwarded-For': '203.0.113.9, 198.51.100.2'}
        with patch.object(u, 'request', req):
            self.assertEqual(u.effective_client_ip(), '198.51.100.2')
