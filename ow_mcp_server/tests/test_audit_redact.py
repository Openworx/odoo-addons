"""C2: the audit log must never store raw secrets (passwords, tokens,
API keys, IBANs, etc.). The controller's `_redact` / `_request_payload`
/ `_response_summary` helpers walk the payload recursively and replace
sensitive values before they are persisted.
"""
import json

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.ow_mcp_server.controllers import mcp_controller as ctrl


@tagged('post_install', '-at_install')
class TestRedact(TransactionCase):
    def test_password_scrubbed(self):
        out = ctrl._redact({'login': 'a', 'password': 'hunter2'})
        self.assertEqual(out['login'], 'a')
        self.assertEqual(out['password'], ctrl._REDACTED)

    def test_nested_dict_scrubbed(self):
        out = ctrl._redact({
            'values': {'name': 'X', 'new_password': 'secret'},
        })
        self.assertEqual(out['values']['new_password'], ctrl._REDACTED)
        self.assertEqual(out['values']['name'], 'X')

    def test_list_of_dicts_scrubbed(self):
        out = ctrl._redact([
            {'api_key': 'abc', 'label': 'k1'},
            {'api_key': 'def', 'label': 'k2'},
        ])
        self.assertEqual(out[0]['api_key'], ctrl._REDACTED)
        self.assertEqual(out[1]['api_key'], ctrl._REDACTED)
        self.assertEqual(out[0]['label'], 'k1')

    def test_x2many_command_preserved_but_inner_values_scrubbed(self):
        out = ctrl._redact({
            'child_ids': [(0, 0, {'name': 'c', 'totp_secret': 'T'})],
        })
        # Outer tuple shape must survive — only the secret leaf is replaced.
        cmd = out['child_ids'][0]
        self.assertEqual(cmd[0], 0)
        self.assertEqual(cmd[1], 0)
        self.assertEqual(cmd[2]['name'], 'c')
        self.assertEqual(cmd[2]['totp_secret'], ctrl._REDACTED)

    def test_key_matching_is_case_insensitive(self):
        out = ctrl._redact({'PASSWORD': 'x', 'Api_Key': 'y'})
        self.assertEqual(out['PASSWORD'], ctrl._REDACTED)
        self.assertEqual(out['Api_Key'], ctrl._REDACTED)

    def test_bank_fields_scrubbed(self):
        out = ctrl._redact({
            'acc_number': 'NL00BANK0000000000',
            'iban': 'NL00BANK0000000000',
            'bic': 'BANKNL2A',
        })
        self.assertEqual(out['acc_number'], ctrl._REDACTED)
        self.assertEqual(out['iban'], ctrl._REDACTED)
        self.assertEqual(out['bic'], ctrl._REDACTED)

    def test_non_sensitive_values_untouched(self):
        data = {'name': 'John', 'age': 42, 'tags': ['x', 'y']}
        self.assertEqual(ctrl._redact(data), data)


@tagged('post_install', '-at_install')
class TestRequestPayloadHelper(TransactionCase):
    def test_not_a_dict_returns_none(self):
        self.assertIsNone(ctrl._request_payload('not a dict'))
        self.assertIsNone(ctrl._request_payload(None))

    def test_non_tool_call_returns_none(self):
        self.assertIsNone(ctrl._request_payload({
            'jsonrpc': '2.0', 'id': 1, 'method': 'ping',
        }))

    def test_tool_call_payload_is_redacted(self):
        text = ctrl._request_payload({
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {
                'name': 'create_record',
                'arguments': {
                    'model': 'res.users',
                    'values': {'login': 'a', 'password': 'secret'},
                },
            },
        })
        self.assertIsNotNone(text)
        parsed = json.loads(text)
        self.assertEqual(parsed['values']['password'], ctrl._REDACTED)
        self.assertEqual(parsed['values']['login'], 'a')

    def test_payload_truncated_to_limit(self):
        big = {'model': 'res.partner', 'values': {'name': 'x' * 10000}}
        text = ctrl._request_payload({
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'create_record', 'arguments': big},
        })
        self.assertLessEqual(len(text), ctrl._PAYLOAD_LIMIT)


@tagged('post_install', '-at_install')
class TestResponseSummaryHelper(TransactionCase):
    def test_error_response_summarised(self):
        summary = ctrl._response_summary({
            'jsonrpc': '2.0', 'id': 1,
            'error': {'code': -32001, 'message': 'boom'},
        })
        parsed = json.loads(summary)
        self.assertEqual(parsed['error'], 'boom')
        self.assertEqual(parsed['code'], -32001)

    def test_structured_content_redacted(self):
        summary = ctrl._response_summary({
            'jsonrpc': '2.0', 'id': 1,
            'result': {
                'structuredContent': {
                    'record': {'login': 'a', 'password': 'leak'},
                },
            },
        })
        self.assertIn(ctrl._REDACTED, summary)
        self.assertNotIn('leak', summary)

    def test_missing_structured_content_returns_none(self):
        self.assertIsNone(ctrl._response_summary({
            'jsonrpc': '2.0', 'id': 1, 'result': {},
        }))
