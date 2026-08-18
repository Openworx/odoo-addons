"""Unit tests: hyperlinks are navigation, not remote content.

Regression guard for the "links in mail don't work" report: an ``<a
href="https://…">`` must not trip the safe-mode banner (which also made
the client neuter the link), while genuinely auto-fetched remote
resources and dangerous schemes keep being detected/stripped.
"""
from odoo.tests import TransactionCase, tagged

from ..models.ow_mail_imap import sanitize_and_detect


@tagged("post_install", "-at_install", "ow_mail")
class TestSanitizeLinks(TransactionCase):

    def test_plain_hyperlink_is_not_remote_content(self):
        html, has_remote = sanitize_and_detect(
            '<p>Zie <a href="https://example.com/actie">deze pagina</a>.</p>')
        self.assertFalse(has_remote)
        self.assertIn('href="https://example.com/actie"', html)

    def test_remote_image_still_detected(self):
        _html, has_remote = sanitize_and_detect(
            '<p><img src="https://tracker.example/p.gif"/></p>')
        self.assertTrue(has_remote)

    def test_link_plus_image_detected_link_kept(self):
        html, has_remote = sanitize_and_detect(
            '<a href="https://example.com"><img src="https://cdn.example/x.png"/></a>')
        self.assertTrue(has_remote)  # de afbeelding, niet de link
        self.assertIn('href="https://example.com"', html)

    def test_dangerous_scheme_still_stripped_from_links(self):
        html, has_remote = sanitize_and_detect(
            '<a href="javascript:alert(1)">klik</a>')
        self.assertFalse(has_remote)
        self.assertNotIn("javascript:", html)

    def test_mailto_untouched(self):
        html, has_remote = sanitize_and_detect(
            '<a href="mailto:info@example.com?subject=Hoi">mail ons</a>')
        self.assertFalse(has_remote)
        self.assertIn('href="mailto:info@example.com?subject=Hoi"', html)
