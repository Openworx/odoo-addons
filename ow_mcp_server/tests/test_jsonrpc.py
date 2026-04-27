from odoo.tests.common import BaseCase
from odoo.addons.ow_mcp_server.protocol import jsonrpc


class TestJsonRpc(BaseCase):
    def test_parse_request(self):
        req = jsonrpc.parse_message(
            '{"jsonrpc":"2.0","id":1,"method":"x","params":{"a":1}}'
        )
        self.assertEqual(req.method, 'x')
        self.assertEqual(req.id, 1)
        self.assertEqual(req.params, {'a': 1})
        self.assertFalse(req.is_notification)

    def test_parse_notification(self):
        msg = jsonrpc.parse_message('{"jsonrpc":"2.0","method":"initialized"}')
        self.assertTrue(msg.is_notification)
        self.assertIsNone(msg.id)

    def test_parse_rejects_missing_version(self):
        with self.assertRaises(ValueError):
            jsonrpc.parse_message('{"id":1,"method":"x"}')

    def test_parse_rejects_bad_json(self):
        with self.assertRaises(ValueError):
            jsonrpc.parse_message('{not json')

    def test_result_envelope(self):
        out = jsonrpc.result(id=1, result={'ok': True})
        self.assertEqual(out, {'jsonrpc': '2.0', 'id': 1, 'result': {'ok': True}})

    def test_error_envelope_has_code(self):
        out = jsonrpc.error(id=1, code=-32602, message='bad params')
        self.assertEqual(out['error']['code'], -32602)
        self.assertEqual(out['error']['message'], 'bad params')
        self.assertNotIn('data', out['error'])

    def test_error_envelope_with_data(self):
        out = jsonrpc.error(id=1, code=-32001, message='x', data={'k': 'v'})
        self.assertEqual(out['error']['data'], {'k': 'v'})

    def test_parse_batch(self):
        msgs = jsonrpc.parse_message(
            '[{"jsonrpc":"2.0","id":1,"method":"a"},'
            '{"jsonrpc":"2.0","id":2,"method":"b"}]'
        )
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0].method, 'a')
        self.assertEqual(msgs[1].id, 2)
