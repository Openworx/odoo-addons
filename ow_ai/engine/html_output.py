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


# -- public API --------------------------------------------------------

def render_ai_markdown(text: str, *, base_url: str | None = None) -> Markup:
    """Render agent/LLM Markdown ``text`` to sanitised, exfiltration-safe HTML.

    Every class the model wrote itself (e.g. raw ``<div class="position-fixed
    ...">`` meant to overlay the UI or fake a card) is dropped; only the
    ``language-*`` class of a code block survives (syntax highlighting),
    plus the classes this module adds itself.
    """
    if not text:
        return Markup('')
    if markdown2 is not None:
        raw_html = markdown2.markdown(text, extras=_MARKDOWN_EXTRAS)
    else:
        raw_html = _minimal_markdown(text)
    raw_html = _add_semantic_classes(_strip_model_classes(raw_html))
    sanitized = html_sanitize(
        raw_html, sanitize_tags=True, sanitize_attributes=True, sanitize_style=True, strip_classes=False)
    cleaned = strip_exfiltration(sanitized, base_url)
    return Markup(cleaned)


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
    <i class="oi" data-icon="…"/>text</div>``

    ``icon`` is an Odoo 20 icon name (a Material Symbols name from
    ``web/tooling/icons/icons_wishlist.txt``, e.g. ``search``): Odoo 20 no
    longer renders Font Awesome (``fa-*``) classes, so the icon is carried
    as the ``data-icon`` attribute of an ``<i class="oi">`` element, the
    same convention ``view_button.js`` uses for a button's ``icon=``. The
    event id (the assistant event holding the call) goes into
    ``data-oe-id``: ``mail.message`` bodies are sanitised on write and only
    a fixed list of ``data-*`` attributes (``odoo.tools.mail.safe_attrs``)
    survives -- ``data-id``, ``data-oe-id`` and ``data-icon`` do, an ad-hoc
    ``data-event-id`` would be stripped.
    """
    attrs = Markup('data-id="{}"').format(call_id)
    if event_id is not None:
        attrs += Markup(' data-oe-id="{}"').format(event_id)
    return Markup('<div class="o_ow_ai_tool_summary" {attrs}><i class="oi" data-icon="{icon}"/>{text}</div>').format(
        attrs=attrs, icon=icon, text=text)


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
    ``&lt;img …&gt;`` left over from an unwrapped ``<form>`` would become a
    live ``<img>``). The children's text and tails are escaped by
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
