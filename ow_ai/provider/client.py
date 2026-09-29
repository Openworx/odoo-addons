# -*- coding: utf-8 -*-
"""HTTP client for an OpenAI-compatible Chat Completions endpoint.

Sends only standard fields; the endpoint (OpenRouter, HostYourAI, OpenAI,
Ollama, …) is whatever ``ow_ai.base_url`` points at. Never log the API
key or the full request/response headers.
"""
from __future__ import annotations

import logging
import os
import time

import requests

from .errors import BadRequest, classify
from .transport import get_transport, request_error

_logger = logging.getLogger('odoo.addons.ow_ai.provider')

DEFAULT_BASE_URL = 'https://openrouter.ai/api/v1'


class ProviderClient:

    def __init__(self, api_key, *, base_url=DEFAULT_BASE_URL, timeout=120.0, transport=None):
        self._api_key = api_key or ''
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.transport = transport or get_transport(None)

    @staticmethod
    def resolve_api_key(env) -> tuple:
        """Return (key, source): the first key set in the ``ow_ai.api_key``
        parameter ('param'), the ``OW_AI_API_KEY``/``OPENROUTER_API_KEY``
        environment variables ('env') or odoo.conf's ``ow_ai_api_key``
        ('conf'); ('', None) when there is none (keyless endpoints). Every
        source is stripped: a trailing newline (an environment variable read
        from a file) is not part of the key, a blank value is no key."""
        api_key = env['ir.config_parameter'].sudo().get_str('ow_ai.api_key').strip()
        if api_key:
            return api_key, 'param'
        api_key = ((os.environ.get('OW_AI_API_KEY') or '').strip()
                   or (os.environ.get('OPENROUTER_API_KEY') or '').strip())
        if api_key:
            return api_key, 'env'
        from odoo.tools import config  # local import: keep this module Odoo-optional
        api_key = (config.get('ow_ai_api_key') or '').strip()
        if api_key:
            return api_key, 'conf'
        return '', None

    @classmethod
    def from_env(cls, env, *, timeout=None) -> 'ProviderClient':
        api_key, _source = cls.resolve_api_key(env)
        params = env['ir.config_parameter'].sudo()
        base_url = params.get_str('ow_ai.base_url') or DEFAULT_BASE_URL
        if timeout is None:
            timeout = float(params.get_int('ow_ai.timeout', 120))
        return cls(api_key, base_url=base_url, timeout=timeout, transport=get_transport(env))

    def _headers(self) -> dict:
        headers = {'Content-Type': 'application/json'}
        if self._api_key:
            headers['Authorization'] = f'Bearer {self._api_key}'
        return headers

    # RequestsTransport already maps requests' exceptions and the ValueError of
    # a header that cannot be sent; these wrappers cover any other transport
    # that lets one through (``from None``: its text can quote the key).
    def _post(self, path: str, payload: dict) -> tuple:
        url = f'{self.base_url}{path}'
        headers = self._headers()
        try:
            return self.transport.post(url, json=payload, headers=headers, timeout=self.timeout)
        except (requests.RequestException, ValueError) as exc:
            raise request_error(exc) from None

    def _get(self, path: str) -> tuple:
        url = f'{self.base_url}{path}'
        headers = self._headers()
        try:
            return self.transport.get(url, headers=headers, timeout=self.timeout)
        except (requests.RequestException, ValueError) as exc:
            raise request_error(exc) from None

    @staticmethod
    def _check_response(status: int, body, headers, *, is_chat: bool):
        if status != 200:
            raise classify(status, body, headers)
        if not isinstance(body, dict):
            # e.g. a wrong Base URL answering with an HTML page: a
            # misconfiguration, not worth a retry
            raise BadRequest("The endpoint did not return JSON.", status=status)
        if body.get('error'):
            raise classify(200, body, headers)
        if is_chat and not body.get('choices'):
            raise classify(200, body, headers)

    def chat_completion(self, *, model, messages, tools=None, response_format=None,
                         temperature=None, max_tokens=None, reasoning=None, parallel_tool_calls=None,
                         user=None, extra=None) -> dict:
        payload = {'model': model, 'messages': messages}
        if tools:
            payload['tools'] = tools
            payload['tool_choice'] = 'auto'
        if response_format is not None:
            payload['response_format'] = response_format
        if temperature is not None:
            payload['temperature'] = temperature
        if max_tokens is not None:
            payload['max_tokens'] = max_tokens
        if reasoning is not None:
            payload['reasoning'] = reasoning
        if parallel_tool_calls is not None:
            payload['parallel_tool_calls'] = parallel_tool_calls
        if user is not None:
            payload['user'] = user
        if extra:
            payload.update(extra)

        start = time.monotonic()
        status, body, headers = self._post('/chat/completions', payload)
        latency_ms = int((time.monotonic() - start) * 1000)

        _logger.debug(
            'chat_completion model=%s messages=%d tools=%d status=%s latency_ms=%d',
            model, len(messages), len(tools) if tools else 0, status, latency_ms,
        )

        self._check_response(status, body, headers, is_chat=True)

        body['_ow_ai_latency_ms'] = latency_ms
        return body

    def embeddings(self, *, model, inputs: list) -> dict:
        payload = {'model': model, 'input': inputs}

        start = time.monotonic()
        status, body, headers = self._post('/embeddings', payload)
        latency_ms = int((time.monotonic() - start) * 1000)

        _logger.debug(
            'embeddings model=%s inputs=%d status=%s latency_ms=%d',
            model, len(inputs), status, latency_ms,
        )

        self._check_response(status, body, headers, is_chat=False)

        body['_ow_ai_latency_ms'] = latency_ms
        return body

    def list_models(self) -> list:
        start = time.monotonic()
        status, body, headers = self._get('/models')
        latency_ms = int((time.monotonic() - start) * 1000)

        _logger.debug('list_models status=%s latency_ms=%d', status, latency_ms)

        self._check_response(status, body, headers, is_chat=False)

        models_list = body.get('data')
        return models_list if isinstance(models_list, list) else []
