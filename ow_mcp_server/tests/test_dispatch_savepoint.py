"""C4: each JSON-RPC message inside a batch must run inside its own
savepoint. A failure in message #1 (e.g. a PG IntegrityError) must not
poison the transaction for message #2.
"""
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.ow_mcp_server.protocol import dispatcher


def _bad_handler(env, args):
    """Trigger a low-level PG error that would normally abort the txn."""
    env.cr.execute('SELECT * FROM this_table_does_not_exist')


def _good_handler(env, args):
    env.cr.execute('SELECT 1')
    return {'ok': True}


@tagged('post_install', '-at_install')
class TestDispatchSavepoint(TransactionCase):
    def setUp(self):
        super().setUp()
        # Register two throwaway handlers. Names must not collide with the
        # real tool list.
        self._saved = dict(dispatcher.TOOL_HANDLERS)
        dispatcher.TOOL_HANDLERS['_test_bad'] = _bad_handler
        dispatcher.TOOL_HANDLERS['_test_good'] = _good_handler
        self.addCleanup(self._restore)

    def _restore(self):
        dispatcher.TOOL_HANDLERS.clear()
        dispatcher.TOOL_HANDLERS.update(self._saved)

    def _call(self, tool, msg_id=1):
        return dispatcher._dispatch(self.env, {
            'jsonrpc': '2.0', 'id': msg_id, 'method': 'tools/call',
            'params': {'name': tool, 'arguments': {}},
        })

    def test_failing_call_returns_error_envelope(self):
        resp = self._call('_test_bad')
        self.assertIn('error', resp)
        self.assertEqual(resp['id'], 1)

    def test_subsequent_call_still_succeeds(self):
        # Without the per-call savepoint, this second call would raise
        # InFailedSqlTransaction because the first call's DB error left
        # the transaction in an aborted state.
        failed = self._call('_test_bad', msg_id=1)
        self.assertIn('error', failed)
        ok = self._call('_test_good', msg_id=2)
        self.assertIn('result', ok)
        self.assertEqual(ok['result']['structuredContent'], {'ok': True})
