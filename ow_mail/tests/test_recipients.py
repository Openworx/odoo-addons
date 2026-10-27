# Copyright 2026 Openworx
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo.tests import BaseCase, tagged

from ..models import ow_mail_imap as imap_utils
from ..models.ow_mail_account import _normalize_recipients, _parse_recipients


@tagged("post_install", "-at_install")
class TestParseRecipients(BaseCase):
    def test_comma_separated(self):
        self.assertEqual(
            _parse_recipients('"Smith, John" <j@x.de>, a@b.de', "c@d.de", None),
            (["j@x.de", "a@b.de", "c@d.de"], []),
        )

    def test_space_or_semicolon_is_reported_not_dropped(self):
        # On 19.0 HEAD these went out to the first address only, silently.
        for value in (
            "jw@wv-gmbh-cokg.de vw@wv-gmbh-cokg.de",
            "jw@wv-gmbh-cokg.de; vw@wv-gmbh-cokg.de",
        ):
            _recipients, unreadable = _parse_recipients(value)
            self.assertEqual(
                unreadable, ["jw@wv-gmbh-cokg.de", "vw@wv-gmbh-cokg.de"]
            )

    def test_bare_name_is_reported(self):
        self.assertEqual(
            _parse_recipients("Julian, a@x.de"), (["a@x.de"], ["Julian"])
        )

    def test_empty_elements_are_ignored(self):
        # Autocomplete leaves a trailing ", ".
        self.assertEqual(_parse_recipients("a@x.de, "), (["a@x.de"], []))
        self.assertEqual(
            _parse_recipients(", a@x.de ,, b@y.de"), (["a@x.de", "b@y.de"], [])
        )
        self.assertEqual(_parse_recipients("", None, " , "), ([], []))

    def test_address_inside_display_name(self):
        self.assertEqual(_parse_recipients('"jw@x.de" <jw@x.de>'), (["jw@x.de"], []))


@tagged("post_install", "-at_install")
class TestNormalizeRecipients(BaseCase):
    def test_hand_typed_separators_become_commas(self):
        cases = {
            "jw@wv-gmbh-cokg.de vw@wv-gmbh-cokg.de": (
                "jw@wv-gmbh-cokg.de, vw@wv-gmbh-cokg.de"
            ),
            "a@x.de; b@y.de": "a@x.de, b@y.de",
            "a@x.de b@y.de  c@z.de": "a@x.de, b@y.de, c@z.de",
            '"Schwarzer; Susanne" <s@x.de>; b@y.de': (
                '"Schwarzer; Susanne" <s@x.de>, b@y.de'
            ),
            "Julian Winklbauer <jw@x.de>": "Julian Winklbauer <jw@x.de>",
            "": "",
        }
        for value, expected in cases.items():
            self.assertEqual(_normalize_recipients(value), expected)

    def test_normalized_value_parses_completely(self):
        value = _normalize_recipients("jw@wv-gmbh-cokg.de vw@wv-gmbh-cokg.de; a@b.de")
        self.assertEqual(
            _parse_recipients(value),
            (["jw@wv-gmbh-cokg.de", "vw@wv-gmbh-cokg.de", "a@b.de"], []),
        )


@tagged("post_install", "-at_install")
class TestDisplayAddresses(BaseCase):
    def test_hand_typed_header_is_not_shown_empty(self):
        # A Sent copy whose raw "To" was typed by hand used to render empty.
        self.assertEqual(
            imap_utils._addrs("jw@wv-gmbh-cokg.de vw@wv-gmbh-cokg.de"),
            "jw@wv-gmbh-cokg.de, vw@wv-gmbh-cokg.de",
        )
        self.assertEqual(
            imap_utils._addrs('"Smith, John" <j@x.de>, a@b.de'), "j@x.de, a@b.de"
        )
        self.assertEqual(imap_utils._addrs("Julian"), "Julian")
        self.assertEqual(imap_utils._addrs(None), "")
