from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.ow_mcp_server.protocol.dispatcher import (
    dispatch, PROTOCOL_VERSION, TOOL_SCHEMAS,
)
from odoo.addons.ow_mcp_server.protocol import jsonrpc


@tagged('post_install', '-at_install')
class TestDispatcher(TransactionCase):
    def setUp(self):
        super().setUp()
        partner = self.env.ref('base.model_res_partner').id
        self.env['ow.mcp.model.access'].sudo().search(
            [('model_id', '=', partner)]
        ).unlink()
        self.env['ow.mcp.model.access'].create({
            'model_id': partner,
            'allow_read': True,
        })

    def test_initialize(self):
        out = dispatch(self.env, {
            'method': 'initialize', 'id': 1, 'params': {},
        })
        self.assertEqual(out['id'], 1)
        self.assertEqual(out['result']['protocolVersion'], PROTOCOL_VERSION)
        self.assertIn('capabilities', out['result'])
        self.assertIn('tools', out['result']['capabilities'])
        self.assertEqual(out['result']['serverInfo']['name'], 'ow_mcp_server')

    def test_ping(self):
        out = dispatch(self.env, {'method': 'ping', 'id': 2})
        self.assertEqual(out['result'], {})

    def test_initialized_is_notification(self):
        self.assertIsNone(
            dispatch(self.env, {'method': 'notifications/initialized'})
        )

    def test_tools_list_has_six(self):
        out = dispatch(self.env, {
            'method': 'tools/list', 'id': 3, 'params': {},
        })
        names = {t['name'] for t in out['result']['tools']}
        self.assertEqual(names, {
            'list_models', 'search_records', 'get_record',
            'create_record', 'update_record', 'delete_record',
        })

    def test_schemas_validate_required(self):
        search = next(t for t in TOOL_SCHEMAS if t['name'] == 'search_records')
        self.assertIn('model', search['inputSchema']['required'])

    def test_method_not_found(self):
        out = dispatch(self.env, {
            'method': 'nope.nothere', 'id': 4, 'params': {},
        })
        self.assertEqual(out['error']['code'], jsonrpc.METHOD_NOT_FOUND)

    def test_unknown_tool_returns_method_not_found(self):
        out = dispatch(self.env, {
            'method': 'tools/call', 'id': 5,
            'params': {'name': 'imaginary_tool', 'arguments': {}},
        })
        self.assertEqual(out['error']['code'], jsonrpc.METHOD_NOT_FOUND)

    def test_tools_call_routes_to_handler(self):
        out = dispatch(self.env, {
            'method': 'tools/call', 'id': 6,
            'params': {'name': 'list_models', 'arguments': {}},
        })
        self.assertIn('content', out['result'])
        self.assertIn('structuredContent', out['result'])
        self.assertFalse(out['result']['isError'])
        names = {m['model'] for m in out['result']['structuredContent']['models']}
        self.assertIn('res.partner', names)

    def test_access_error_becomes_permission_denied(self):
        out = dispatch(self.env, {
            'method': 'tools/call', 'id': 7,
            'params': {'name': 'search_records',
                       'arguments': {'model': 'ir.config_parameter'}},
        })
        self.assertEqual(out['error']['code'], jsonrpc.PERMISSION_DENIED)

    # ---- C3: notifications never receive a response ----

    def test_notification_unknown_method_returns_none(self):
        # Per JSON-RPC 2.0 §4.1, a notification (no 'id') MUST NOT receive a
        # response — even on error.
        self.assertIsNone(dispatch(self.env, {'method': 'nope.nothere'}))

    def test_notification_tool_error_returns_none(self):
        self.assertIsNone(dispatch(self.env, {
            'method': 'tools/call',
            'params': {'name': 'search_records',
                       'arguments': {'model': 'ir.config_parameter'}},
        }))

    def test_notification_successful_call_returns_none(self):
        self.assertIsNone(dispatch(self.env, {
            'method': 'tools/call',
            'params': {'name': 'list_models', 'arguments': {}},
        }))

    def test_null_id_still_gets_response(self):
        # id: null is present, so NOT a notification — response required.
        out = dispatch(self.env, {'method': 'ping', 'id': None})
        self.assertIsNotNone(out)
        self.assertIsNone(out['id'])
