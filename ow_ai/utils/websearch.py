# -*- coding: utf-8 -*-
"""SearXNG web search client.

Odoo-free apart from `env` (used only to read settings via `utils/params.py`
and to look up a test-injected transport), so the request-building/parsing
logic can be unit tested with a `FakeWebTransport`. Mirrors the shape of
`provider/transport.py`'s `get_transport(env)`: `env.registry.ow_ai_web_transport`
wins when a test set it, else a `requests`-based transport is used. The page
session and resolver `tools/web.py` hands to `webfetch.fetch` are looked up
the same way (`get_page_session`, `get_resolver`).
"""
from __future__ import annotations

import json
from typing import Protocol

import requests

from . import params


class WebSearchError(Exception):
    """Raised by `search` for any failure (`is_configured` never raises).

    `code` is one of `not_configured`, `timeout`, `http_error`, `not_json`.
    The message is always user-readable.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class WebTransport(Protocol):

    def get(self, url: str, *, params: dict, headers: dict, timeout: float) -> tuple:
        """GET url. Returns (status, body bytes, content-type)."""
        ...


class RequestsWebTransport:
    """Transport backed by the `requests` library.

    Does not itself catch `requests.RequestException`: `search()` wraps it
    into a `WebSearchError`, so a `FakeWebTransport` that raises the same
    exception is handled identically.
    """

    def get(self, url: str, *, params: dict, headers: dict, timeout: float) -> tuple:
        response = requests.get(url, params=params, headers=headers, timeout=timeout)
        return response.status_code, response.content, response.headers.get('Content-Type', '')


def get_transport(env=None) -> WebTransport:
    """Return the transport to use: the test-injected fake if present, else RequestsWebTransport()."""
    if env is not None:
        registry = getattr(env, 'registry', None)
        transport = getattr(registry, 'ow_ai_web_transport', None)
        if transport is not None:
            return transport
    return RequestsWebTransport()


def get_page_session(env):
    """The HTTP session for page reads: the test-injected ``registry.ow_ai_page_session``
    if present, else ``None`` (``webfetch.fetch`` then opens, hardens and closes its own)."""
    return getattr(env.registry, 'ow_ai_page_session', None)


def get_resolver(env):
    """The host resolver for ``webfetch.fetch``: the test-injected
    ``registry.ow_ai_web_resolver`` if present, else ``None`` (real DNS)."""
    return getattr(env.registry, 'ow_ai_web_resolver', None)


def is_configured(env) -> bool:
    return bool(params.get_str(env, 'ow_ai.web_search_url', ''))


def search(env, query, *, count=None, time_range=None, site=None, language=None, transport=None) -> dict:
    """Run a SearXNG search for `query` and return `{'results': [...], 'unresponsive_engines': [...]}`.

    Each result is `{'title', 'url', 'snippet', 'published', 'engines'}`,
    deduplicated by URL, capped to `count` (or the `ow_ai.web_search_results`
    setting), itself capped at 10 and floored at 1. Raises `WebSearchError`
    on any failure (`not_configured`, `timeout`, `http_error`, `not_json`).
    """
    base = params.get_str(env, 'ow_ai.web_search_url', '').rstrip('/')
    if not base:
        raise WebSearchError('not_configured', "Web search is not configured.")

    limit = max(1, min(count or params.get_int(env, 'ow_ai.web_search_results', 5), 10))

    q = f"site:{site} {query}" if site else query
    query_params = {'q': q, 'format': 'json', 'safesearch': 1}

    language = language or params.get_str(env, 'ow_ai.web_search_language', '') or (env.lang or '').split('_')[0]
    if language:
        query_params['language'] = language
    if time_range:
        query_params['time_range'] = time_range

    transport = transport or get_transport(env)
    try:
        status, body, _content_type = transport.get(
            f"{base}/search", params=query_params, headers={'Accept': 'application/json'},
            timeout=params.get_int(env, 'ow_ai.web_timeout', 15))
    except requests.Timeout as exc:
        raise WebSearchError('timeout', "The search service did not answer in time.") from exc
    except requests.RequestException as exc:
        raise WebSearchError('http_error', "The search service could not be reached.") from exc

    if status != 200:
        raise WebSearchError('http_error', f"The search service answered HTTP {status}.")

    try:
        data = json.loads(body)
    except ValueError as exc:
        raise WebSearchError(
            'not_json',
            "The search service did not return JSON: enable format 'json' in SearXNG's settings.yml.") from exc

    if not isinstance(data, dict):
        raise WebSearchError('not_json', "The search service returned an unexpected answer.")
    raw_results = data.get('results')
    if raw_results is None:
        raw_results = []
    if not isinstance(raw_results, list):
        raise WebSearchError('not_json', "The search service returned an unexpected answer.")

    results, seen = [], set()
    for item in raw_results:
        if not isinstance(item, dict):
            # A malformed entry among otherwise-good ones: skip it rather than
            # failing the whole search (the list itself is fine).
            continue
        url = (item.get('url') or '').strip()
        if not url or url in seen:
            continue
        seen.add(url)
        results.append({
            'title': (item.get('title') or '').strip()[:200],
            'url': url,
            'snippet': ' '.join((item.get('content') or '').split())[:300],
            'published': (item.get('publishedDate') or '')[:10] or None,
            'engines': list(item.get('engines') or []),
        })
        if len(results) >= limit:
            break

    return {'results': results, 'unresponsive_engines': data.get('unresponsive_engines') or []}
