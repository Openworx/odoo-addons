# -*- coding: utf-8 -*-
"""Single choke point for turning agent-authored text into safe HTML.

Every ``markupsafe.Markup`` object in ``ow_ai`` is built here (a repo-guard
test in ``tests/test_html_output.py`` enforces this): LLM output is never
trusted, so it always goes through Markdown rendering (or plain sanitizing,
for HTML already built by qweb) followed by an "exfiltration filter" that
strips anything capable of leaking data through a GET request (tracking
pixels, external images/iframes, ``javascript:``/``url(...)`` attribute
values, etc.) before the result is wrapped in ``Markup`` and handed to
``mail.thread.message_post``.
"""
from __future__ import annotations

import logging
import re
import secrets
import urllib.parse

import lxml.html
from markupsafe import Markup, escape
from odoo.tools import html_sanitize

_logger = logging.getLogger(__name__)

try:
    import markdown2
except ImportError:  # pragma: no cover - exercised via monkeypatch in tests
    markdown2 = None
    _logger.warning(
        "markdown2 is not installed; ow_ai falls back to a minimal Markdown renderer.")

# `highlightjs-lang` keeps a fence's language as `class="<lang> language-<lang>"`
# on its `<code>`: the chat highlights code blocks from it (Prism).
_MARKDOWN_EXTRAS = [
    'fenced-code-blocks', 'highlightjs-lang', 'tables', 'strike', 'cuddled-lists', 'break-on-newline',
    'code-friendly',
]

_REMOVE_TAGS = {
    'img', 'picture', 'source', 'video', 'audio', 'iframe', 'object', 'embed',
    'form', 'input', 'button', 'link', 'meta', 'svg', 'use', 'base',
}
_STRIP_ATTR_NAMES = {'style', 'background', 'poster', 'srcset', 'ping', 'formaction'}
# What a browser removes from a URL before resolving it: every tab/newline,
# and C0 control characters or spaces at either end.
_URL_TAB_NEWLINE = re.compile(r'[\t\n\r]')
_URL_TRIMMED_CHARS = ''.join(chr(code) for code in range(0x21))

# Icon names (Material Symbols names, as on the 20.0 branch) -> the Font
# Awesome 4 classes Odoo 19 renders (``fa fa-…``; ``data-icon`` means
# nothing on 19). The tool names are what ``ToolResult.summary['icon']``
# carries (``tools/*.py``, ``engine/tool_batch.py``'s fallbacks); the
# template names are listed so the whole translation lives in one place,
# the templates (``static/src/**/*.xml``) write their classes directly.
ICON_CLASSES = {
    # tool summaries
    'search': 'fa-search',
    'view_list': 'fa-list',
    'table': 'fa-table',
    'bar_chart': 'fa-bar-chart',
    'visibility': 'fa-eye',
    'article': 'fa-file-text-o',
    'edit': 'fa-pencil',
    'add': 'fa-plus',
    'check': 'fa-check',
    'help': 'fa-question-circle',
    'settings': 'fa-cog',
    'warning': 'fa-exclamation-triangle',
    # templates
    'hourglass_empty': 'fa-hourglass-o',
    'autorenew': 'fa-refresh fa-spin',
    'wand_stars': 'fa-magic',
    'expand_more': 'fa-chevron-down',
    'expand_less': 'fa-chevron-up',
    'chevron_right': 'fa-chevron-right',
}
# An icon name missing from ``ICON_CLASSES`` (e.g. a third-party tool's).
DEFAULT_ICON_CLASS = 'fa-cog'

# ``[WEB_SOURCE:1]`` / ``[WEB_SOURCE:1,3]``: the model citing results of the
# web tools' source table (``tools/web.py``, ``{"1": {"url", "title", "host"}}``).
_WEB_SOURCE_RE = re.compile(r'\[WEB_SOURCE:([0-9]+(?:\s*,\s*[0-9]+)*)\]')
# A citation crosses Markdown and the sanitiser as a placeholder
# ``\ue000WS:<nonce>:1,3\ue001``: two private-use characters (plain text to
# markdown2, lxml and ``html_sanitize``) around a random nonce, new for every
# ``render_ai_markdown`` call, and the numbers. The model's own raw copies of
# the two characters are dropped first; copies it writes as entities
# (``&#xE000;``) only turn into the characters once the HTML is parsed, and
# lack the nonce, so they never match and are dropped at the end. Each
# placeholder substituted is therefore one this module inserted.
_CITE_OPEN = '\ue000'
_CITE_CLOSE = '\ue001'
_CITE_MARKS_RE = re.compile(f'[{_CITE_OPEN}{_CITE_CLOSE}]')


# -- public API --------------------------------------------------------

def render_ai_markdown(text: str, *, base_url: str | None = None, web_sources: dict | None = None) -> Markup:
    """Render agent/LLM Markdown ``text`` to sanitised, exfiltration-safe HTML.

    Every class the model wrote itself (e.g. raw ``<div class="position-fixed
    ...">`` meant to overlay the UI or fake a card) is dropped; only the
    ``language-*`` class of a code block survives (syntax highlighting),
    plus the classes this module adds itself.

    ``[WEB_SOURCE:n]`` citations become numbered links to the URLs of
    ``web_sources`` (the turn's source table, see ``tools/web.py``) plus a
    list of the cited sources under the text; a number missing from the
    table stays ``[n]`` (``o_ow_ai_cite_missing``), and without a table
    every tag is plain ``[n]`` text. The links are inserted after
    :func:`strip_exfiltration`: they are server data, not model text, so
    every link the model writes itself is still filtered as before.
    """
    if not text:
        return Markup('')
    nonce = secrets.token_hex(8)
    text = _mark_citations(text, nonce)
    if markdown2 is not None:
        raw_html = markdown2.markdown(text, extras=_MARKDOWN_EXTRAS)
    else:
        raw_html = _minimal_markdown(text)
    raw_html = _add_semantic_classes(_strip_model_classes(raw_html))
    sanitized = html_sanitize(
        raw_html, sanitize_tags=True, sanitize_attributes=True, sanitize_style=True, strip_classes=False)
    cleaned = strip_exfiltration(sanitized, base_url)
    return Markup(_render_citations(cleaned, web_sources, nonce))


def sanitize_ai_html(html: str, *, base_url: str | None = None) -> Markup:
    """Sanitize+filter ``html`` already built elsewhere (e.g. qweb card bodies).

    Unlike :func:`render_ai_markdown` this does not run Markdown parsing:
    ``html`` is assumed to already be HTML.
    """
    if not html:
        return Markup('')
    sanitized = html_sanitize(
        html, sanitize_tags=True, sanitize_attributes=True, sanitize_style=True, strip_classes=False)
    cleaned = strip_exfiltration(sanitized, base_url)
    return Markup(cleaned)


def tool_summary_markup(icon: str, text: str, call_id: str, event_id: int | None = None) -> Markup:
    """``<div class="o_ow_ai_tool_summary" data-id="<call id>" data-oe-id="<event id>">
    <i class="fa fa-…" aria-hidden="true"></i>text</div>``

    ``icon`` is a tool icon name (a Material Symbols name, e.g. ``search``):
    Odoo 19 renders Font Awesome classes, so it becomes its
    ``ICON_CLASSES`` class (``DEFAULT_ICON_CLASS`` for an unknown name);
    ``class`` survives the sanitiser, the chat reads the icon from it. The
    event id (the assistant event holding the call) goes into
    ``data-oe-id``: ``mail.message`` bodies are sanitised on write and only
    a fixed list of ``data-*`` attributes (``odoo.tools.mail.safe_attrs``)
    survives -- ``data-id`` and ``data-oe-id`` do, an ad-hoc
    ``data-event-id`` would be stripped.
    """
    attrs = Markup('data-id="{}"').format(call_id)
    if event_id is not None:
        attrs += Markup(' data-oe-id="{}"').format(event_id)
    icon_class = ICON_CLASSES.get(icon, DEFAULT_ICON_CLASS)
    return Markup(
        '<div class="o_ow_ai_tool_summary" {attrs}><i class="fa {icon_class}" aria-hidden="true"></i>{text}</div>'
    ).format(attrs=attrs, icon_class=icon_class, text=text)


def agent_step_markup(inner_html: Markup, event_id: int) -> Markup:
    """``<div class="o_ow_ai_agent_step" data-id="…">…</div>``"""
    return Markup('<div class="o_ow_ai_agent_step" data-id="{event_id}">{inner_html}</div>').format(
        event_id=event_id, inner_html=inner_html)


def input_card_markup(body: Markup) -> Markup:
    """``<div class="o_ow_ai_input_card">…</div>``: a confirmation/question card's body."""
    return Markup('<div class="o_ow_ai_input_card">{body}</div>').format(body=body)


def notification_markup(kind: str, body: Markup) -> Markup:
    """``<div class="o_mail_notification" data-oe-type="ow_ai_note|ow_ai_preview">…</div>``"""
    return Markup('<div class="o_mail_notification" data-oe-type="{kind}">{body}</div>').format(
        kind=kind, body=body)


def markdown_to_plaintext_title(text: str) -> str:
    """Strip Markdown from ``text`` for use as a chat title.

    Uses the rendered HTML's bare text content: ``html2plaintext`` would
    turn ``<strong>`` back into ``*…*`` markers.
    """
    if not text:
        return ''
    wrapper = _parse_wrapped(str(render_ai_markdown(text)))
    plain = wrapper.text_content() if wrapper is not None else ''
    return ' '.join(plain.split())


def strip_exfiltration(html: str, base_url: str | None = None) -> str:
    """Remove anything able to leak data through a request (see module docstring)."""
    wrapper = _parse_wrapped(html)
    if wrapper is None:
        return ''
    base_host = urllib.parse.urlsplit(base_url).netloc.lower() if base_url else None
    base_web_prefix = f"{base_url.rstrip('/')}/web/" if base_url else None
    join_base = base_url or 'http://localhost'
    join_host = urllib.parse.urlsplit(join_base).netloc.lower()

    def internal_href(href):
        """``href`` normalised when it stays on this host (``#...`` or a
        root-relative ``/path``), else None.

        Browsers read a backslash as ``/`` and drop tabs/newlines inside a
        URL, so a backslash (or a tab) right after the leading slash is the
        protocol-relative ``//other-host`` in disguise: normalise first,
        then accept only a single leading ``/`` that still resolves to this
        host.
        """
        normalised = _URL_TAB_NEWLINE.sub('', href).strip(_URL_TRIMMED_CHARS).replace('\\', '/')
        if normalised.startswith('#'):
            return normalised
        if not normalised.startswith('/') or normalised.startswith('//'):
            return None
        if urllib.parse.urlsplit(normalised).netloc:
            return None
        joined = urllib.parse.urlsplit(urllib.parse.urljoin(join_base, normalised))
        if joined.netloc.lower() != join_host:
            return None
        return normalised

    def is_kept_img(el):
        src = (el.get('src') or '').strip()
        if not src:
            return False
        if src.startswith('/web/') or src.startswith('/ow_ai/'):
            return True
        if base_web_prefix and src.startswith(base_web_prefix):
            return True
        return False

    for anchor in list(wrapper.iter('a')):
        href = (anchor.get('href') or '').strip()
        if not href or href.startswith(('mailto:', 'tel:')):
            continue
        internal = internal_href(href)
        if internal is not None:
            anchor.set('href', internal)
            continue
        parsed = urllib.parse.urlsplit(href)
        if base_host and parsed.netloc.lower() == base_host:
            anchor.set('rel', 'noopener noreferrer')
            anchor.set('target', '_blank')
            continue
        _replace_external_link(anchor, parsed)

    for tag in _REMOVE_TAGS:
        for el in list(wrapper.iter(tag)):
            if tag == 'img' and is_kept_img(el):
                continue
            _drop_element(el)

    for el in wrapper.iter():
        if el is wrapper:
            continue
        for attr_name, attr_value in list(el.attrib.items()):
            lower_value = (attr_value or '').strip().lower()
            if attr_name.lower() in _STRIP_ATTR_NAMES:
                del el.attrib[attr_name]
            elif 'url(' in lower_value or lower_value.startswith('javascript:'):
                del el.attrib[attr_name]

    return _serialize_wrapped(wrapper)


# -- web citations -------------------------------------------------------

def _mark_citations(text: str, nonce: str) -> str:
    """``text`` with each ``[WEB_SOURCE:…]`` tag replaced by its placeholder
    (after dropping every placeholder character the model wrote itself)."""
    def placeholder(match):
        numbers = ','.join(_citation_numbers(match.group(1)))
        return f'{_CITE_OPEN}WS:{nonce}:{numbers}{_CITE_CLOSE}'
    return _WEB_SOURCE_RE.sub(placeholder, _CITE_MARKS_RE.sub('', text))


def _placeholder_re(nonce: str) -> re.Pattern:
    """This call's placeholders (see ``_mark_citations``); group 1: the numbers."""
    return re.compile(f'{_CITE_OPEN}WS:{re.escape(nonce)}:([0-9,]+){_CITE_CLOSE}')


def _citation_numbers(numbers: str) -> list[str]:
    """The distinct numbers of a tag, in order, as the source table's keys
    (``str(int)``: no leading zeros; no ``int()`` either, a huge number
    would raise)."""
    result = []
    for number in numbers.split(','):
        number = number.strip().lstrip('0') or '0'
        if number not in result:
            result.append(number)
    return result


def _source_link(source) -> tuple[str, str, str] | None:
    """``(url, title, host)`` of a source table entry that may become a link
    (an ``http(s)://`` URL with a host), else None."""
    if not isinstance(source, dict):
        return None
    url = source.get('url')
    if not isinstance(url, str) or not url.lower().startswith(('http://', 'https://')):
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if not parsed.netloc:
        return None
    host = str(source.get('host') or parsed.hostname or '')
    title = str(source.get('title') or '').strip() or host or url
    return url, title, host


def _render_citations(html: str, web_sources: dict | None, nonce: str) -> str:
    """Replace the citation placeholders (``nonce``: this call's) of
    sanitised ``html`` by the trusted links (``o_ow_ai_cite``) and append the
    list of cited sources (``o_ow_ai_sources``, in order of first citation).

    URL, title and host come from ``web_sources`` only and are escaped
    (``Markup.format``); a placeholder that ended up in an attribute value
    (a tag in a model-written ``title="…"``) becomes plain ``[n]`` text
    there. Any other placeholder character (written as an entity by the
    model) is dropped.
    """
    if _CITE_OPEN not in html and _CITE_CLOSE not in html:
        return html
    placeholder_re = _placeholder_re(nonce)
    wrapper = _parse_wrapped(html)
    if wrapper is None:
        return ''
    for el in wrapper.iter():
        for name, value in el.attrib.items():
            if _CITE_OPEN in value or _CITE_CLOSE in value:
                value = placeholder_re.sub(lambda match: _plain_citation(match.group(1)), value)
                el.set(name, _CITE_MARKS_RE.sub('', value))
    html = _serialize_wrapped(wrapper)

    table = {str(key): source for key, source in (web_sources or {}).items()}
    links = {}
    cited = []

    def citation(match):
        if not table:
            return _plain_citation(match.group(1))
        parts = []
        for number in match.group(1).split(','):
            if number not in links:
                links[number] = _source_link(table.get(number))
            link = links[number]
            if link is None:
                parts.append(Markup('<span class="o_ow_ai_cite_missing">[{}]</span>').format(number))
                continue
            if number not in cited:
                cited.append(number)
            parts.append(Markup(
                '<sup class="o_ow_ai_cite"><a href="{url}" rel="noopener noreferrer nofollow" target="_blank">'
                '{number}</a></sup>').format(url=link[0], number=number))
        return str(Markup('').join(parts))

    body = _CITE_MARKS_RE.sub('', placeholder_re.sub(citation, html))
    if cited:
        body += str(_sources_markup([(number, *links[number]) for number in cited]))
    return body


def _plain_citation(numbers: str) -> str:
    return ''.join(f'[{number}]' for number in numbers.split(','))


def _sources_markup(sources: list[tuple[str, str, str, str]]) -> Markup:
    """``<div class="o_ow_ai_sources"><ol><li value="n"><a …>title</a>
    <span class="text-muted">host</span></li>…</ol></div>``; ``value`` keeps
    each item's number equal to its citations'."""
    items = Markup('').join(
        Markup(
            '<li value="{number}"><a href="{url}" rel="noopener noreferrer nofollow" target="_blank">{title}</a> '
            '<span class="text-muted">{host}</span></li>'
        ).format(number=number, url=url, title=title, host=host)
        for number, url, title, host in sources)
    return Markup('<div class="o_ow_ai_sources"><ol>{items}</ol></div>').format(items=items)


# -- fallback renderer (markdown2 unavailable) --------------------------

def _minimal_markdown(text: str) -> str:
    paragraphs = re.split(r'\n\s*\n', text.strip())
    html_parts = []
    for paragraph in paragraphs:
        lines = [line for line in paragraph.split('\n')]
        stripped_lines = [line.strip() for line in lines if line.strip()]
        if stripped_lines and all(line.startswith('- ') for line in stripped_lines):
            items = ''.join(f'<li>{_inline_minimal(line[2:])}</li>' for line in stripped_lines)
            html_parts.append(f'<ul>{items}</ul>')
        elif stripped_lines:
            content = '<br/>'.join(_inline_minimal(line) for line in lines if line.strip())
            html_parts.append(f'<p>{content}</p>')
    return ''.join(html_parts)


def _inline_minimal(line: str) -> str:
    result = str(escape(line))
    result = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', result)
    result = re.sub(r'`(.+?)`', r'<code>\1</code>', result)
    return result


# -- lxml helpers --------------------------------------------------------

def _parse_wrapped(html: str):
    if not html or not html.strip():
        return None
    return lxml.html.fragment_fromstring(html, create_parent='div')


def _serialize_wrapped(wrapper) -> str:
    """The HTML of ``wrapper``'s content. Its leading text (before the first
    child) is decoded text like any other: escaped again here, or every
    parse/serialise round would decode one more level of entities (an
    ``&amp;lt;img …&amp;gt;`` left over from an unwrapped ``<form>`` would
    become a live ``<img>``). The children's text and tails are escaped by
    ``lxml.html.tostring``."""
    parts = [str(escape(wrapper.text or ''))]
    for child in wrapper:
        parts.append(lxml.html.tostring(child, encoding='unicode'))
    return ''.join(parts)


def _strip_model_classes(html: str) -> str:
    """Drop every ``class`` attribute of model-written HTML, except the
    ``language-*`` class(es) of a ``<code>`` element."""
    wrapper = _parse_wrapped(html)
    if wrapper is None:
        return ''
    for el in wrapper.iter():
        if el is wrapper or 'class' not in el.attrib:
            continue
        kept = []
        if el.tag == 'code':
            kept = [name for name in el.get('class').split() if name.startswith('language-')]
        if kept:
            el.set('class', ' '.join(kept))
        else:
            del el.attrib['class']
    return _serialize_wrapped(wrapper)


def _add_semantic_classes(html: str) -> str:
    wrapper = _parse_wrapped(html)
    if wrapper is None:
        return ''
    for table in wrapper.iter('table'):
        _append_class(table, 'table table-sm table-bordered o_ow_ai_table')
    for pre in wrapper.iter('pre'):
        _append_class(pre, 'o_ow_ai_pre')
    for quote in wrapper.iter('blockquote'):
        _append_class(quote, 'o_ow_ai_quote')
    return _serialize_wrapped(wrapper)


def _append_class(el, classes: str):
    existing = el.get('class') or ''
    el.set('class', ' '.join(part for part in (existing, classes) if part))


def _drop_element(el):
    parent = el.getparent()
    if parent is None:
        return
    tail = el.tail or ''
    previous = el.getprevious()
    parent.remove(el)
    if previous is not None:
        previous.tail = (previous.tail or '') + tail
    else:
        parent.text = (parent.text or '') + tail


def _replace_external_link(anchor, parsed):
    text = anchor.text_content()
    host = parsed.netloc or (anchor.get('href') or '')
    span = anchor.makeelement('span', {'class': 'o_ow_ai_extlink'})
    span.text = f"{text} ({host})" if host else text
    span.tail = anchor.tail
    parent = anchor.getparent()
    if parent is not None:
        parent.replace(anchor, span)
