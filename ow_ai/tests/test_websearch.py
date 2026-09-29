# -*- coding: utf-8 -*-
"""``utils/websearch.py``: SearXNG JSON parsing edge cases and transport failures.

Uses a `TransactionCase` (not `BaseCase`, unlike `test_webfetch.py`):
`websearch.search` reads its settings through `utils/params.py`, which needs a
real ``env``. No real HTTP call is made: every test injects a `FakeWebTransport`
or a stub whose `get` raises directly.
"""
from __future__ import annotations

import requests
from odoo.tests import TransactionCase

from ..utils import params, websearch
from .common import FakeWebTransport


class TestWebSearchUnexpectedJson(TransactionCase):

    def setUp(self):
        super().setUp()
        params.set_str(self.env, 'ow_ai.web_search_url', 'https://search.example.org')

    def _search_with_body(self, body):
        transport = FakeWebTransport([(200, body, 'application/json')])
        with self.assertRaises(websearch.WebSearchError) as ctx:
            websearch.search(self.env, 'Odoo', transport=transport)
        self.assertEqual(ctx.exception.code, 'not_json')

    def test_top_level_list_is_not_json(self):
        self._search_with_body(b'[]')

    def test_top_level_string_is_not_json(self):
        self._search_with_body(b'"x"')

    def test_results_not_a_list_is_not_json(self):
        self._search_with_body(b'{"results": "oops"}')

    def test_non_dict_items_are_skipped_not_fatal(self):
        transport = FakeWebTransport([(
            200,
            b'{"results": [1, {"url": "https://a.example/", "title": "A"}]}',
            'application/json',
        )])
        result = websearch.search(self.env, 'Odoo', transport=transport)
        self.assertEqual(len(result['results']), 1)
        self.assertEqual(result['results'][0]['url'], 'https://a.example/')


class TestWebSearchTransportFailures(TransactionCase):

    def setUp(self):
        super().setUp()
        params.set_str(self.env, 'ow_ai.web_search_url', 'https://search.example.org')

    def test_timeout_is_timeout(self):
        class RaisingTransport:
            def get(self, url, *, params, headers, timeout):
                raise requests.Timeout('simulated timeout')

        with self.assertRaises(websearch.WebSearchError) as ctx:
            websearch.search(self.env, 'Odoo', transport=RaisingTransport())
        self.assertEqual(ctx.exception.code, 'timeout')

    def test_connection_error_is_http_error(self):
        class RaisingTransport:
            def get(self, url, *, params, headers, timeout):
                raise requests.ConnectionError('simulated connection refused')

        with self.assertRaises(websearch.WebSearchError) as ctx:
            websearch.search(self.env, 'Odoo', transport=RaisingTransport())
        self.assertEqual(ctx.exception.code, 'http_error')
        self.assertEqual(ctx.exception.message, "The search service could not be reached.")
