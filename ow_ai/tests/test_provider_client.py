# -*- coding: utf-8 -*-
import logging
import os
import traceback
from unittest import mock

import requests
from odoo.tests import TransactionCase
from odoo.tools import config

from ..provider.client import DEFAULT_BASE_URL, ProviderClient
from ..provider.errors import (
    AuthError,
    BadRequest,
    ContentModerated,
    PaymentRequired,
    ProviderError,
    RateLimited,
    TransportTimeout,
)
from ..provider.transport import FakeTransport, RequestsTransport, get_transport


class TestProviderClientBase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.transport = FakeTransport()
        self.env.registry.ow_ai_transport = self.transport
        self.addCleanup(self._clear_transport)
        self.env['ir.config_parameter'].sudo().set_str('ow_ai.api_key', 'sk-or-test-key-abc123')

    def _clear_transport(self):
        if hasattr(self.env.registry, 'ow_ai_transport'):
            del self.env.registry.ow_ai_transport


class TestGetTransport(TestProviderClientBase):

    def test_get_transport_returns_registry_transport(self):
        self.assertIs(get_transport(self.env), self.transport)

    def test_get_transport_defaults_to_requests(self):
        del self.env.registry.ow_ai_transport
        self.assertIsInstance(get_transport(self.env), RequestsTransport)
        self.assertIsInstance(get_transport(None), RequestsTransport)


class TestClientBodyConstruction(TestProviderClientBase):

    def test_body_has_no_usage_and_no_tool_choice(self):
        self.transport.queue.append(FakeTransport.text('hi'))
        client = ProviderClient.from_env(self.env)
        client.chat_completion(model='openai/gpt-4.1-mini', messages=[{'role': 'user', 'content': 'hi'}])

        sent = self.transport.requests[0]['json']
        self.assertNotIn('usage', sent)
        self.assertNotIn('tool_choice', sent)
        self.assertNotIn('tools', sent)

    def test_body_has_only_standard_fields(self):
        self.transport.queue.append(FakeTransport.text('hi'))
        ProviderClient.from_env(self.env).chat_completion(model='m', messages=[{'role': 'user', 'content': 'x'}])
        sent = self.transport.requests[0]['json']
        self.assertEqual(set(sent), {'model', 'messages'})

    def test_body_tool_choice_present_with_tools(self):
        self.transport.queue.append(FakeTransport.text('hi'))
        client = ProviderClient.from_env(self.env)
        tools = [{'type': 'function', 'function': {'name': 'foo', 'parameters': {}}}]
        client.chat_completion(model='openai/gpt-4.1-mini', messages=[{'role': 'user', 'content': 'hi'}], tools=tools)

        sent = self.transport.requests[0]['json']
        self.assertEqual(sent['tool_choice'], 'auto')
        self.assertEqual(sent['tools'], tools)

    def test_body_response_format_passthrough(self):
        self.transport.queue.append(FakeTransport.text('hi'))
        client = ProviderClient.from_env(self.env)
        response_format = {'type': 'json_object'}
        client.chat_completion(
            model='openai/gpt-4.1-mini',
            messages=[{'role': 'user', 'content': 'hi'}],
            response_format=response_format,
        )
        sent = self.transport.requests[0]['json']
        self.assertEqual(sent['response_format'], response_format)

    def test_reasoning_is_passed_through(self):
        self.transport.queue.append(FakeTransport.text('hi'))
        ProviderClient.from_env(self.env).chat_completion(
            model='m', messages=[{'role': 'user', 'content': 'x'}], reasoning={'effort': 'low'})
        sent = self.transport.requests[0]['json']
        self.assertEqual(sent['reasoning'], {'effort': 'low'})

    def test_headers_have_bearer_only(self):
        self.transport.queue.append(FakeTransport.text('hi'))
        ProviderClient.from_env(self.env).chat_completion(model='m', messages=[{'role': 'user', 'content': 'x'}])
        headers = self.transport.requests[0]['headers']
        self.assertEqual(set(headers), {'Authorization', 'Content-Type'})
        self.assertEqual(headers['Authorization'], 'Bearer sk-or-test-key-abc123')

    def test_base_url_trailing_slash_is_stripped(self):
        self.env['ir.config_parameter'].sudo().set_str('ow_ai.base_url', 'https://hostyourai.com/api/v1/')
        self.transport.queue.append(FakeTransport.text('hi'))
        ProviderClient.from_env(self.env).chat_completion(model='m', messages=[{'role': 'user', 'content': 'x'}])
        self.assertEqual(self.transport.requests[0]['url'], 'https://hostyourai.com/api/v1/chat/completions')

    def test_api_key_never_logged(self):
        self.transport.queue.append(FakeTransport.text('hi'))
        client = ProviderClient.from_env(self.env)
        logger = logging.getLogger('odoo.addons.ow_ai.provider')
        with self.assertLogs(logger, level='DEBUG') as capture:
            client.chat_completion(model='m', messages=[{'role': 'user', 'content': 'hi'}])
        for line in capture.output:
            self.assertNotIn('sk-or-test-key-abc123', line)


class TestKeyResolution(TestProviderClientBase):

    def test_param_key_wins(self):
        with mock.patch.dict(os.environ, {'OW_AI_API_KEY': 'sk-env-key', 'OPENROUTER_API_KEY': 'sk-or-env-key'}):
            with mock.patch.dict(config.options, {'ow_ai_api_key': 'sk-or-config-key'}):
                client = ProviderClient.from_env(self.env)
                self.assertEqual(client._api_key, 'sk-or-test-key-abc123')

    def test_env_key_used_when_no_param(self):
        self.env['ir.config_parameter'].sudo().set_str('ow_ai.api_key', '')
        with mock.patch.dict(os.environ, {'OW_AI_API_KEY': '', 'OPENROUTER_API_KEY': 'sk-or-env-key'}):
            with mock.patch.dict(config.options, {'ow_ai_api_key': 'sk-or-config-key'}):
                client = ProviderClient.from_env(self.env)
                self.assertEqual(client._api_key, 'sk-or-env-key')

    def test_config_key_used_as_last_resort(self):
        self.env['ir.config_parameter'].sudo().set_str('ow_ai.api_key', '')
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('OW_AI_API_KEY', None)
            os.environ.pop('OPENROUTER_API_KEY', None)
            with mock.patch.dict(config.options, {'ow_ai_api_key': 'sk-or-config-key'}):
                client = ProviderClient.from_env(self.env)
                self.assertEqual(client._api_key, 'sk-or-config-key')

    def test_every_key_source_is_stripped(self):
        """A trailing newline (e.g. an environment variable read from a file)
        is not part of the key; a whitespace-only value is no key."""
        self.env['ir.config_parameter'].sudo().set_str('ow_ai.api_key', ' sk-param-key\n')
        with mock.patch.dict(os.environ, {'OW_AI_API_KEY': ' sk-env-key\r\n', 'OPENROUTER_API_KEY': ''}), \
             mock.patch.dict(config.options, {'ow_ai_api_key': 'sk-conf-key\n'}):
            self.assertEqual(ProviderClient.resolve_api_key(self.env), ('sk-param-key', 'param'))
            self.env['ir.config_parameter'].sudo().set_str('ow_ai.api_key', ' \n')
            self.assertEqual(ProviderClient.resolve_api_key(self.env), ('sk-env-key', 'env'))
            with mock.patch.dict(os.environ, {'OW_AI_API_KEY': '\n', 'OPENROUTER_API_KEY': 'sk-old-key\n'}):
                self.assertEqual(ProviderClient.resolve_api_key(self.env), ('sk-old-key', 'env'))
            with mock.patch.dict(os.environ, {'OW_AI_API_KEY': ' ', 'OPENROUTER_API_KEY': '\n'}):
                self.assertEqual(ProviderClient.resolve_api_key(self.env), ('sk-conf-key', 'conf'))
                with mock.patch.dict(config.options, {'ow_ai_api_key': ' \n'}):
                    self.assertEqual(ProviderClient.resolve_api_key(self.env), ('', None))
                    self.assertNotIn('Authorization', ProviderClient.from_env(self.env)._headers())

    def test_ow_ai_api_key_env_wins_over_openrouter_env(self):
        self.env['ir.config_parameter'].sudo().set_str('ow_ai.api_key', '')
        with mock.patch.dict('os.environ', {'OW_AI_API_KEY': 'k-new', 'OPENROUTER_API_KEY': 'k-old'}):
            client = ProviderClient.from_env(self.env)
        self.assertEqual(client._api_key, 'k-new')

    def test_no_key_sends_no_authorization_header_and_401_is_auth_error(self):
        self.env['ir.config_parameter'].sudo().set_str('ow_ai.api_key', '')
        with mock.patch.dict('os.environ', {'OW_AI_API_KEY': '', 'OPENROUTER_API_KEY': ''}), \
             mock.patch.dict(config.options, {'ow_ai_api_key': ''}):
            client = ProviderClient.from_env(self.env)            # no MissingApiKey any more
            self.transport.queue.append(FakeTransport.error(401, 'no key'))
            with self.assertRaises(AuthError) as ctx:
                client.chat_completion(model='m', messages=[{'role': 'user', 'content': 'x'}])
        self.assertNotIn('Authorization', self.transport.requests[0]['headers'])
        self.assertEqual(ctx.exception.user_message, "The AI provider refused the API key.")


class TestErrorClassification(TestProviderClientBase):

    def _client(self):
        return ProviderClient.from_env(self.env)

    def _run(self, response):
        self.transport.queue.append(response)
        client = self._client()
        with self.assertRaises(Exception) as capture:
            client.chat_completion(model='m', messages=[{'role': 'user', 'content': 'hi'}])
        return capture.exception

    def test_400_bad_request(self):
        exc = self._run(FakeTransport.error(400, 'bad'))
        self.assertIsInstance(exc, BadRequest)

    def test_401_auth_error(self):
        exc = self._run(FakeTransport.error(401, 'bad key'))
        self.assertIsInstance(exc, AuthError)

    def test_402_payment_required(self):
        exc = self._run(FakeTransport.error(402, 'no credits'))
        self.assertIsInstance(exc, PaymentRequired)

    def test_403_plain_auth_error(self):
        exc = self._run(FakeTransport.error(403, 'forbidden'))
        self.assertIsInstance(exc, AuthError)
        self.assertNotIsInstance(exc, ContentModerated)

    def test_403_moderation_via_metadata_reasons(self):
        exc = self._run(FakeTransport.error(403, 'flagged', metadata={'reasons': ['hate']}))
        self.assertIsInstance(exc, ContentModerated)

    def test_403_moderation_via_code(self):
        exc = self._run(FakeTransport.error(403, 'flagged', code='moderation'))
        self.assertIsInstance(exc, ContentModerated)

    def test_404_bad_request(self):
        exc = self._run(FakeTransport.error(404, 'not found'))
        self.assertIsInstance(exc, BadRequest)

    def test_405_bad_request(self):
        """A POST to a web page (a wrong Base URL) is not worth a retry."""
        exc = self._run(('response', 405, None, {'Content-Type': 'text/html'}))
        self.assertIsInstance(exc, BadRequest)
        self.assertEqual(str(exc), "HTTP 405")

    def test_408_provider_error(self):
        exc = self._run(FakeTransport.error(408, 'timeout'))
        self.assertIsInstance(exc, ProviderError)

    def test_413_bad_request(self):
        exc = self._run(FakeTransport.error(413, 'too large'))
        self.assertIsInstance(exc, BadRequest)

    def test_422_bad_request(self):
        exc = self._run(FakeTransport.error(422, 'unprocessable'))
        self.assertIsInstance(exc, BadRequest)

    def test_429_rate_limited_with_retry_after(self):
        exc = self._run(FakeTransport.error(429, 'slow down', headers={'Retry-After': '7'}))
        self.assertIsInstance(exc, RateLimited)
        self.assertEqual(exc.retry_after, 7.0)

    def test_429_insufficient_quota_is_payment_required(self):
        """An exhausted quota (OpenAI's 429 code) is no rate limit: not worth a retry."""
        exc = self._run(FakeTransport.error(429, 'You exceeded your current quota.', code='insufficient_quota'))
        self.assertIsInstance(exc, PaymentRequired)
        self.assertEqual(str(exc), 'You exceeded your current quota.')

    def test_500_provider_error(self):
        exc = self._run(FakeTransport.error(500, 'boom'))
        self.assertIsInstance(exc, ProviderError)

    def test_502_provider_error(self):
        exc = self._run(FakeTransport.error(502, 'boom'))
        self.assertIsInstance(exc, ProviderError)

    def test_503_provider_error(self):
        exc = self._run(FakeTransport.error(503, 'boom'))
        self.assertIsInstance(exc, ProviderError)

    def test_524_provider_error(self):
        exc = self._run(FakeTransport.error(524, 'boom'))
        self.assertIsInstance(exc, ProviderError)

    def test_200_with_error_body(self):
        exc = self._run(('response', 200, {'error': {'message': 'weird'}}, {}))
        self.assertIsInstance(exc, ProviderError)
        self.assertEqual(str(exc), 'weird')
        exc = self._run(('response', 200, {'error': {'code': 502}}, {}))
        self.assertIsInstance(exc, ProviderError)
        self.assertEqual(str(exc), 'HTTP 200 with an error body')

    def test_200_with_a_string_error_is_a_bad_request(self):
        """LM Studio answers an unknown model or route with 200 and a string error."""
        exc = self._run(('response', 200, {'error': 'Model not found'}, {}))
        self.assertIsInstance(exc, BadRequest)
        self.assertEqual(str(exc), 'Model not found')

    def test_200_with_a_null_error_is_a_success(self):
        response = FakeTransport.text('hi')
        response[2]['error'] = None
        self.transport.queue.append(response)
        body = self._client().chat_completion(model='m', messages=[{'role': 'user', 'content': 'hi'}])
        self.assertEqual(body['choices'][0]['message']['content'], 'hi')

    def test_200_with_empty_choices(self):
        exc = self._run(('response', 200, {'choices': []}, {}))
        self.assertIsInstance(exc, ProviderError)
        self.assertEqual(str(exc), 'HTTP 200 without choices')

    def test_error_text_of_other_servers(self):
        """The detail comes from `error.message`, a string `error`, `detail`
        (FastAPI, e.g. vLLM) or a top-level `message`; else "HTTP <status>"."""
        cases = (
            (('response', 404, None, {'Content-Type': 'text/plain'}), BadRequest, 'HTTP 404'),  # Ollama
            (('response', 404, {'detail': 'Not Found'}, {}), BadRequest, 'Not Found'),  # vLLM
            (('response', 400, {'error': 'Unknown model'}, {}), BadRequest, 'Unknown model'),
            (('response', 500, {'message': 'Internal error'}, {}), ProviderError, 'Internal error'),
            (('response', 503, {'detail': [{'msg': 'x'}]}, {}), ProviderError, 'HTTP 503'),
        )
        for response, error_class, message in cases:
            with self.subTest(response=response):
                exc = self._run(response)
                self.assertIsInstance(exc, error_class)
                self.assertEqual(str(exc), message)

    def test_transport_timeout(self):
        self.transport.queue.append(FakeTransport.timeout())
        client = self._client()
        with self.assertRaises(TransportTimeout):
            client.chat_completion(model='m', messages=[{'role': 'user', 'content': 'hi'}])


class TestListModelsAndEmbeddings(TestProviderClientBase):

    def test_list_models_returns_data(self):
        self.transport.queue.append(('response', 200, {'data': [{'id': 'a'}, {'id': 'b'}]}, {}))
        client = ProviderClient.from_env(self.env)
        result = client.list_models()
        self.assertEqual(result, [{'id': 'a'}, {'id': 'b'}])
        self.assertEqual(self.transport.requests[0]['url'], f'{DEFAULT_BASE_URL}/models')

    def test_list_models_non_json_200_is_a_bad_request(self):
        """A wrong Base URL can answer 200 with an HTML page: RequestsTransport
        parses no body (None). A misconfiguration: not worth a retry."""
        self.transport.queue.append(('response', 200, None, {'Content-Type': 'text/html'}))
        with self.assertRaises(BadRequest) as ctx:
            ProviderClient.from_env(self.env).list_models()
        self.assertEqual(str(ctx.exception), "The endpoint did not return JSON.")

    def test_list_models_json_that_is_not_an_object_is_a_bad_request(self):
        self.transport.queue.append(('response', 200, ['a', 'b'], {}))
        with self.assertRaises(BadRequest) as ctx:
            ProviderClient.from_env(self.env).list_models()
        self.assertEqual(str(ctx.exception), "The endpoint did not return JSON.")

    def test_list_models_error_text_of_a_200(self):
        """LM Studio answers an unknown route with 200 and a string error."""
        message = "Unexpected endpoint or method. (GET /models). Returning 200 anyway"
        self.transport.queue.append(('response', 200, {'error': message}, {}))
        with self.assertRaises(BadRequest) as ctx:
            ProviderClient.from_env(self.env).list_models()
        self.assertEqual(str(ctx.exception), message)

    def test_list_models_without_a_data_list_is_empty(self):
        for body in ({'object': 'list'}, {'data': None}):
            with self.subTest(body=body):
                self.transport.queue.append(('response', 200, body, {}))
                self.assertEqual(ProviderClient.from_env(self.env).list_models(), [])

    def test_chat_non_json_200_is_a_bad_request(self):
        self.transport.queue.append(('response', 200, None, {}))
        with self.assertRaises(BadRequest) as ctx:
            ProviderClient.from_env(self.env).chat_completion(model='m', messages=[{'role': 'user', 'content': 'x'}])
        self.assertEqual(str(ctx.exception), "The endpoint did not return JSON.")

    def test_embeddings_posts_input_list(self):
        self.transport.queue.append(FakeTransport.embeddings([[0.1, 0.2], [0.3, 0.4]]))
        client = ProviderClient.from_env(self.env)
        result = client.embeddings(model='openai/text-embedding-3-small', inputs=['a', 'b'])
        self.assertEqual(len(result['data']), 2)
        sent = self.transport.requests[0]['json']
        self.assertEqual(sent['input'], ['a', 'b'])
        self.assertEqual(self.transport.requests[0]['url'], f'{DEFAULT_BASE_URL}/embeddings')


class TestRequestsTransportTimeout(TestProviderClientBase):

    def test_requests_timeout_wrapped(self):
        transport = RequestsTransport()
        with mock.patch('requests.Session.post', side_effect=requests.Timeout()):
            with self.assertRaises(TransportTimeout):
                transport.post('http://example.test/x', json={}, headers={}, timeout=1.0)

    def test_requests_connection_error_wrapped(self):
        transport = RequestsTransport()
        with mock.patch('requests.Session.get', side_effect=requests.ConnectionError()):
            with self.assertRaises(TransportTimeout):
                transport.get('http://example.test/x', headers={}, timeout=1.0)


class TestWrongBaseUrl(TestProviderClientBase):
    """A Base URL typo must end as an AIProviderError (a danger notification,
    an apology in the chat), never as a server error."""

    # what requests 2.31 raises (before any connection) for, in this order,
    # `ollama/v1`, `ollama:11434/v1` and `http://`
    URL_ERRORS = (
        requests.exceptions.MissingSchema(
            "Invalid URL 'ollama/v1/models': No scheme supplied. Perhaps you meant https://ollama/v1/models?"),
        requests.exceptions.InvalidSchema("No connection adapters were found for 'ollama:11434/v1/models'"),
        requests.exceptions.InvalidURL("Invalid URL 'http:///models': No host supplied"),
    )

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_str('ow_ai.base_url', 'ollama:11434/v1')
        del self.env.registry.ow_ai_transport  # the real RequestsTransport; requests itself is patched

    def test_base_url_without_scheme_is_a_bad_request(self):
        for error in self.URL_ERRORS:
            with self.subTest(error=type(error).__name__):
                with mock.patch('requests.Session.get', side_effect=error) as get:
                    with self.assertRaises(BadRequest) as ctx:
                        ProviderClient.from_env(self.env).list_models()
                self.assertEqual(get.call_args.args[0], 'ollama:11434/v1/models')
                self.assertEqual(str(ctx.exception), f"Invalid Base URL: {error}")
                with mock.patch('requests.Session.post', side_effect=error):
                    with self.assertRaises(BadRequest):
                        ProviderClient.from_env(self.env).chat_completion(
                            model='m', messages=[{'role': 'user', 'content': 'x'}])

    def test_test_connection_on_a_base_url_without_scheme(self):
        with mock.patch('requests.Session.get', side_effect=self.URL_ERRORS[1]):
            result = self.env['res.config.settings'].create({}).action_ow_ai_test_connection()
        self.assertEqual(result['params']['type'], 'danger')
        self.assertEqual(
            result['params']['message'],
            "The AI provider rejected the request. "
            "Invalid Base URL: No connection adapters were found for 'ollama:11434/v1/models'")

    def test_redirect_loops_are_bad_requests(self):
        with mock.patch('requests.Session.get', side_effect=requests.exceptions.TooManyRedirects(
                "Exceeded 30 redirects.")):
            with self.assertRaises(BadRequest) as ctx:
                ProviderClient.from_env(self.env).list_models()
        self.assertEqual(str(ctx.exception), "Exceeded 30 redirects.")

    def test_a_body_that_is_not_valid_json_is_a_bad_request(self):
        # requests refuses NaN (InvalidJSONError) before any connection
        client = ProviderClient('', base_url='http://127.0.0.1:9/v1', transport=RequestsTransport())
        with self.assertRaises(BadRequest):
            client.chat_completion(model='m', messages=[{'role': 'user', 'content': 'x'}], temperature=float('nan'))

    def test_request_errors_of_any_transport_are_wrapped(self):
        transport = FakeTransport()
        transport.queue.append(('raise', self.URL_ERRORS[0]))
        transport.queue.append(('raise', requests.exceptions.ChunkedEncodingError('broken')))
        client = ProviderClient('', base_url='ollama/v1', transport=transport)
        with self.assertRaises(BadRequest):
            client.list_models()
        with self.assertRaises(ProviderError):
            client.chat_completion(model='m', messages=[{'role': 'user', 'content': 'x'}])


class TestKeyThatCannotBeSent(TestProviderClientBase):
    """A key with a line break or a non-Latin-1 character cannot go into the
    Authorization header. requests (InvalidHeader) and http.client (a plain
    ValueError, or UnicodeEncodeError) quote the whole header in their error:
    it must end as a BadRequest with a fixed text that holds no part of the key,
    in its message and in any traceback logged for it."""

    MESSAGE = "The API key contains characters that cannot be sent in a header."
    # the shapes 'k\n', 'k\r\n' and 'a\nb' (and a non-Latin-1 key), with a made-up
    # key long enough to search for: a one-letter 'k' is part of the word "key"
    KEY_PARTS = ('sk-fake-key', '5c1e9a')
    KEYS = ('sk-fake-key-5c1e9a\n', 'sk-fake-key-5c1e9a\r\n', 'sk-fake-key\n5c1e9a', 'sk-fake-key-5c1e9a\u20ac')

    def assert_no_key(self, exc):
        texts = (str(exc), repr(exc), exc.user_message, ''.join(traceback.format_exception(exc)))
        for text in texts:
            for part in self.KEY_PARTS:
                self.assertNotIn(part, text)

    def test_real_requests_stack(self):
        # requests and http.client check the header values before any socket
        # is opened: nothing ever connects to port 9 (discard)
        for key in self.KEYS:
            client = ProviderClient(key, base_url='http://127.0.0.1:9/v1', timeout=5.0, transport=RequestsTransport())
            calls = {
                'GET /models': client.list_models,
                'POST /chat/completions': lambda client=client: client.chat_completion(
                    model='m', messages=[{'role': 'user', 'content': 'x'}]),
            }
            for name, call in calls.items():
                with self.subTest(key=key, call=name):
                    with self.assertRaises(BadRequest) as ctx:
                        call()
                    self.assertEqual(str(ctx.exception), self.MESSAGE)
                    self.assert_no_key(ctx.exception)

    def test_test_connection_with_a_key_that_cannot_be_sent(self):
        del self.env.registry.ow_ai_transport  # the real RequestsTransport
        self.env['ir.config_parameter'].sudo().set_str('ow_ai.base_url', 'http://127.0.0.1:9/v1')
        self.env['ir.config_parameter'].sudo().set_str('ow_ai.api_key', 'sk-fake-key\n5c1e9a')
        result = self.env['res.config.settings'].create({}).action_ow_ai_test_connection()
        self.assertEqual(result['params']['type'], 'danger')
        self.assertEqual(result['params']['message'], f"The AI provider rejected the request. {self.MESSAGE}")

    def test_header_errors_of_any_transport_are_wrapped(self):
        """ProviderClient._post/_get map what another transport lets through."""
        transport = FakeTransport()
        transport.queue.append(('raise', ValueError("Invalid header value b'Bearer sk-fake-key-5c1e9a\\n'")))
        transport.queue.append(('raise', requests.exceptions.InvalidHeader(
            "Invalid leading whitespace, reserved character(s), or return character(s) "
            "in header value: 'Bearer sk-fake-key\\n5c1e9a'")))
        client = ProviderClient('sk-fake-key-5c1e9a', base_url='http://127.0.0.1:9/v1', transport=transport)
        for call in (client.list_models,
                     lambda: client.chat_completion(model='m', messages=[{'role': 'user', 'content': 'x'}])):
            with self.assertRaises(BadRequest) as ctx:
                call()
            self.assertEqual(str(ctx.exception), self.MESSAGE)
            self.assert_no_key(ctx.exception)


class TestTestConnectionAction(TestProviderClientBase):

    def test_success_notification_with_model_count(self):
        self.transport.queue.append(('response', 200, {'data': [{'id': 'a'}, {'id': 'b'}, {'id': 'c'}]}, {}))
        settings = self.env['res.config.settings'].create({})
        result = settings.action_ow_ai_test_connection()
        self.assertEqual(result['params']['type'], 'success')
        self.assertEqual(result['params']['title'], 'AI Provider')
        self.assertEqual(result['params']['message'], 'Connected. 3 models available.')
        self.assertEqual([request['url'] for request in self.transport.requests], [f'{DEFAULT_BASE_URL}/models'])

    def test_danger_notification_on_a_refused_key(self):
        """An endpoint that checks the key on /models answers 401: the neutral message plus the detail."""
        self.transport.queue.append(('response', 401, {'error': {'code': 401, 'message': 'User not found.'}}, {}))
        settings = self.env['res.config.settings'].create({})
        result = settings.action_ow_ai_test_connection()
        self.assertEqual(result['params']['type'], 'danger')
        self.assertEqual(result['params']['title'], 'AI Provider')
        self.assertEqual(result['params']['message'], 'The AI provider refused the API key. User not found.')
        self.assertEqual(len(self.transport.requests), 1)

    def test_danger_notification_on_a_base_url_without_v1(self):
        """Ollama answers /models without /v1 with a plain-text 404."""
        self.transport.queue.append(('response', 404, None, {'Content-Type': 'text/plain; charset=utf-8'}))
        result = self.env['res.config.settings'].create({}).action_ow_ai_test_connection()
        self.assertEqual(result['params']['type'], 'danger')
        self.assertEqual(result['params']['message'], "The AI provider rejected the request. HTTP 404")

    def test_danger_notification_on_a_401_without_key(self):
        self.env['ir.config_parameter'].sudo().set_str('ow_ai.api_key', '')
        with mock.patch.dict(os.environ, {'OW_AI_API_KEY': '', 'OPENROUTER_API_KEY': ''}), \
             mock.patch.dict(config.options, {'ow_ai_api_key': ''}):
            self.transport.queue.append(FakeTransport.error(401, 'Missing Authentication header'))
            settings = self.env['res.config.settings'].create({})
            result = settings.action_ow_ai_test_connection()
        self.assertEqual(result['params']['type'], 'danger')
        self.assertIn('refused the API key', result['params']['message'])
        self.assertNotIn('Authorization', self.transport.requests[0]['headers'])
