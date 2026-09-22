"""M4: when odoo.conf has `proxy_mode = True`, the controller must read the
real client IP from `X-Forwarded-For` for throttling, IP allowlisting, and
audit — otherwise a single bad actor behind a reverse proxy trips the
throttle for every legitimate client sharing the proxy.
"""
from unittest.mock import patch, MagicMock

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.ow_mcp_server.controllers import mcp_controller as ctrl


def _fake_request(remote_addr='10.0.0.1', xff=None):
    req = MagicMock()
    req.httprequest.remote_addr = remote_addr
    headers = {}
    if xff is not None:
        headers['X-Forwarded-For'] = xff
    req.httprequest.headers = headers
    return req


@tagged('post_install', '-at_install')
class TestEffectiveClientIp(TransactionCase):

    def _run(self, request_obj, proxy_mode):
        # `_effective_client_ip` reads the module-level `request` and
        # `odoo_config`. Patch both for the call.
        with patch.object(ctrl, 'request', request_obj), \
                patch.object(ctrl.odoo_config, 'get', lambda k: proxy_mode):
            return ctrl._effective_client_ip()

    def test_no_proxy_mode_uses_remote_addr(self):
        req = _fake_request(
            remote_addr='10.0.0.1', xff='203.0.113.9, 10.0.0.1',
        )
        self.assertEqual(self._run(req, proxy_mode=False), '10.0.0.1')

    def test_proxy_mode_uses_first_xff_entry(self):
        req = _fake_request(
            remote_addr='10.0.0.1',
            xff='203.0.113.9, 198.51.100.2, 10.0.0.1',
        )
        self.assertEqual(self._run(req, proxy_mode=True), '203.0.113.9')

    def test_proxy_mode_without_xff_falls_back_to_remote_addr(self):
        req = _fake_request(remote_addr='10.0.0.1', xff=None)
        self.assertEqual(self._run(req, proxy_mode=True), '10.0.0.1')

    def test_empty_xff_falls_back_to_remote_addr(self):
        req = _fake_request(remote_addr='10.0.0.1', xff='')
        self.assertEqual(self._run(req, proxy_mode=True), '10.0.0.1')

    def test_xff_whitespace_stripped(self):
        req = _fake_request(
            remote_addr='10.0.0.1',
            xff='  203.0.113.9  , 10.0.0.1',
        )
        self.assertEqual(self._run(req, proxy_mode=True), '203.0.113.9')

    def test_missing_remote_addr_with_proxy_off(self):
        req = _fake_request(remote_addr=None, xff='203.0.113.9')
        self.assertEqual(self._run(req, proxy_mode=False), '')
