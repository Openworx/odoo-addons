# -*- coding: utf-8 -*-
"""HTTP transport abstraction for the provider client.

Kept separate from client.py so tests can inject a FakeTransport instead of
making real HTTP calls.
"""
from __future__ import annotations

import json as json_module
from typing import Protocol

import requests

from .errors import AIProviderError, BadRequest, ProviderError, TransportTimeout

_session = requests.Session()

# requests raises these before any connection when the URL is malformed,
# e.g. a Base URL without its scheme (`ollama:11434/v1`, `localhost/v1`).
_URL_ERRORS = (requests.exceptions.MissingSchema, requests.exceptions.InvalidSchema,
               requests.exceptions.InvalidURL)
# A redirect loop (a wrong Base URL) or a request body that is not valid JSON
# (e.g. a NaN option): a retry would fail the same way.
_REQUEST_ERRORS = (requests.exceptions.TooManyRedirects, requests.exceptions.InvalidJSONError)


# A header value that cannot be sent (a line break or a non-Latin-1 character
# in the API key) makes requests (InvalidHeader) or http.client (a plain
# ValueError, or UnicodeEncodeError) quote the whole header -- the key -- in
# their message: such an error only ever becomes this fixed text.
HEADER_ERROR = "The API key contains characters that cannot be sent in a header."


def request_error(exc: Exception) -> AIProviderError:
    """Map an exception raised before any HTTP status to an AIProviderError.

    ``exc`` is a `requests` exception or a ValueError. A header value that
    cannot be sent (InvalidHeader, any other ValueError) -> BadRequest
    (HEADER_ERROR, never the exception's text), timeouts and connection
    failures -> TransportTimeout, a malformed URL -> BadRequest ("Invalid
    Base URL: ..."), too many redirects or a body that is not valid JSON ->
    BadRequest, anything else -> ProviderError. Callers raise the result
    ``from None``: the original exception must not show up in a traceback.
    """
    if isinstance(exc, requests.exceptions.InvalidHeader) or not isinstance(exc, requests.RequestException):
        return BadRequest(HEADER_ERROR)
    detail = str(exc)
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return TransportTimeout(detail)
    if isinstance(exc, _URL_ERRORS):
        return BadRequest(f"Invalid Base URL: {detail}")
    if isinstance(exc, _REQUEST_ERRORS):
        return BadRequest(detail)
    return ProviderError(detail)


class Transport(Protocol):

    def post(self, url: str, *, json: dict, headers: dict, timeout: float) -> tuple:
        """POST json to url. Returns (status, parsed JSON body or None, response headers)."""
        ...

    def get(self, url: str, *, headers: dict, timeout: float) -> tuple:
        """GET url. Returns (status, parsed JSON body or None, response headers)."""
        ...


class RequestsTransport:
    """Transport backed by the `requests` library."""

    def post(self, url: str, *, json: dict, headers: dict, timeout: float) -> tuple:
        try:
            response = _session.post(url, json=json, headers=headers, timeout=timeout)
        except (requests.RequestException, ValueError) as exc:
            raise request_error(exc) from None
        return self._parse(response)

    def get(self, url: str, *, headers: dict, timeout: float) -> tuple:
        try:
            response = _session.get(url, headers=headers, timeout=timeout)
        except (requests.RequestException, ValueError) as exc:
            raise request_error(exc) from None
        return self._parse(response)

    @staticmethod
    def _parse(response) -> tuple:
        try:
            body = response.json()
        except ValueError:
            body = None
        return response.status_code, body, dict(response.headers)


class FakeTransport:
    """Scripted transport for tests.

    ``queue`` holds scripted responses, consumed in order for every
    post()/get() call. Each item is either:
      - ('response', status, body, headers)
      - ('raise', exc)
    ``requests`` records every call made, as {'url', 'json', 'headers'}
    (json is None for GET calls).
    """

    def __init__(self):
        self.queue: list = []
        self.requests: list = []

    def _next(self, url: str, json_body: dict | None, headers: dict) -> tuple:
        self.requests.append({'url': url, 'json': json_body, 'headers': headers})
        if not self.queue:
            raise AssertionError('FakeTransport queue is empty')
        item = self.queue.pop(0)
        kind = item[0]
        if kind == 'raise':
            raise item[1]
        _, status, body, resp_headers = item
        return status, body, resp_headers or {}

    def post(self, url: str, *, json: dict, headers: dict, timeout: float) -> tuple:
        return self._next(url, json, headers)

    def get(self, url: str, *, headers: dict, timeout: float) -> tuple:
        return self._next(url, None, headers)

    # -- helper constructors -------------------------------------------------

    @staticmethod
    def text(text: str, *, model: str = 'fake/model', usage: dict | None = None,
             finish_reason: str = 'stop', request_id: str = 'gen-fake-id') -> tuple:
        body = {
            'id': request_id,
            'model': model,
            'choices': [{
                'index': 0,
                'finish_reason': finish_reason,
                'message': {'role': 'assistant', 'content': text, 'tool_calls': None},
            }],
            'usage': usage or {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2, 'cost': 0.0},
        }
        return ('response', 200, body, {})

    @staticmethod
    def tool_calls(calls: list, *, text: str | None = None, model: str = 'fake/model',
                    usage: dict | None = None, finish_reason: str = 'tool_calls',
                    request_id: str = 'gen-fake-id') -> tuple:
        tool_call_entries = []
        for index, call in enumerate(calls):
            name, args = call[0], call[1]
            call_id = call[2] if len(call) > 2 else f'call_{index}'
            tool_call_entries.append({
                'id': call_id,
                'type': 'function',
                'function': {'name': name, 'arguments': json_module.dumps(args)},
            })
        body = {
            'id': request_id,
            'model': model,
            'choices': [{
                'index': 0,
                'finish_reason': finish_reason,
                'message': {'role': 'assistant', 'content': text, 'tool_calls': tool_call_entries},
            }],
            'usage': usage or {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2, 'cost': 0.0},
        }
        return ('response', 200, body, {})

    @staticmethod
    def json_text(obj) -> tuple:
        return FakeTransport.text(json_module.dumps(obj))

    @staticmethod
    def error(status: int, message: str, *, headers: dict | None = None, code=None,
              metadata: dict | None = None) -> tuple:
        error_body = {'message': message}
        if code is not None:
            error_body['code'] = code
        if metadata is not None:
            error_body['metadata'] = metadata
        return ('response', status, {'error': error_body}, headers or {})

    @staticmethod
    def timeout() -> tuple:
        return ('raise', requests.Timeout('simulated timeout'))

    @staticmethod
    def embeddings(vectors: list, *, model: str = 'fake/embed', usage: dict | None = None) -> tuple:
        body = {
            'model': model,
            'data': [{'index': i, 'embedding': vector} for i, vector in enumerate(vectors)],
            'usage': usage or {'prompt_tokens': 1, 'completion_tokens': 0, 'total_tokens': 1, 'cost': 0.0},
        }
        return ('response', 200, body, {})


def get_transport(env=None) -> Transport:
    """Return the transport to use: the test-injected fake if present, else RequestsTransport()."""
    if env is not None:
        registry = getattr(env, 'registry', None)
        transport = getattr(registry, 'ow_ai_transport', None)
        if transport is not None:
            return transport
    return RequestsTransport()
