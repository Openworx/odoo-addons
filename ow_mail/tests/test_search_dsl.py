"""Unit tests for the search DSL parser.

Pure-Python — no IMAP, no DB queries for the parser side. The
``TransactionCase`` base still gives us an Odoo env in case a future
test wants to exercise ``_tag_keyword_lookup``; current tests pass a
stub callable.
"""
from datetime import date
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from ..models.ow_mail_search import parse_query, has_operators


@tagged("post_install", "-at_install", "ow_mail")
class TestSearchDSL(TransactionCase):

    # ------------------------------------------------------------------
    # Single operators

    def test_from(self):
        self.assertEqual(
            parse_query("from:alice@ow.test"),
            ('FROM "alice@ow.test"', {}),
        )

    def test_to_and_cc(self):
        self.assertEqual(
            parse_query("to:alice@ow.test cc:bob@ow.test")[0],
            'TO "alice@ow.test" CC "bob@ow.test"',
        )

    def test_subject_quoted(self):
        criteria, _ = parse_query('subject:"weekly report"')
        self.assertEqual(criteria, 'SUBJECT "weekly report"')

    def test_body(self):
        criteria, _ = parse_query("body:budget")
        self.assertEqual(criteria, 'BODY "budget"')

    def test_is_unread(self):
        self.assertEqual(parse_query("is:unread")[0], "UNSEEN")

    def test_is_starred(self):
        self.assertEqual(parse_query("is:starred")[0], "FLAGGED")

    def test_is_unknown_value_dropped(self):
        # Unknown is: values don't blow up, just drop.
        self.assertEqual(parse_query("is:bogus")[0], "")

    def test_before_date(self):
        criteria, _ = parse_query("before:2026-04-01")
        self.assertEqual(criteria, "BEFORE 01-Apr-2026")

    def test_since_date(self):
        criteria, _ = parse_query("since:2026-01-15")
        self.assertEqual(criteria, "SINCE 15-Jan-2026")

    def test_on_date(self):
        criteria, _ = parse_query("on:2026-12-31")
        self.assertEqual(criteria, "ON 31-Dec-2026")

    def test_older_than_days(self):
        criteria, _ = parse_query(
            "older_than:7d", today=date(2026, 4, 20))
        self.assertEqual(criteria, "BEFORE 13-Apr-2026")

    def test_newer_than_days(self):
        criteria, _ = parse_query(
            "newer_than:30d", today=date(2026, 4, 20))
        self.assertEqual(criteria, "SINCE 21-Mar-2026")

    def test_invalid_date_keeps_as_text(self):
        # Gracefully degrades — don't crash, keep as free-text so the
        # user sees results (broad match) rather than an empty list.
        criteria, _ = parse_query("before:notadate")
        self.assertEqual(criteria, 'TEXT "before:notadate"')

    def test_invalid_days_keeps_as_text(self):
        criteria, _ = parse_query("older_than:garbage")
        self.assertEqual(criteria, 'TEXT "older_than:garbage"')

    # ------------------------------------------------------------------
    # Labels

    def test_label_resolved(self):
        lookup = lambda name: "OwTag_work" if name == "work" else None
        criteria, _ = parse_query("label:work", tag_lookup=lookup)
        self.assertEqual(criteria, "KEYWORD OwTag_work")

    def test_label_unknown_dropped(self):
        lookup = lambda name: None
        criteria, _ = parse_query("label:ghost", tag_lookup=lookup)
        self.assertEqual(criteria, "")

    # ------------------------------------------------------------------
    # has: → post filters

    def test_has_attachment(self):
        criteria, post = parse_query("has:attachment")
        self.assertEqual(criteria, "")
        self.assertEqual(post, {"has_attachment": True})

    def test_has_att_short(self):
        _, post = parse_query("has:att")
        self.assertEqual(post, {"has_attachment": True})

    def test_has_unknown_dropped(self):
        criteria, post = parse_query("has:wings")
        self.assertEqual(criteria, "")
        self.assertEqual(post, {})

    # ------------------------------------------------------------------
    # Composition / free text

    def test_and_composition(self):
        criteria, post = parse_query(
            'from:boss@x.com subject:"weekly report" has:attachment is:unread')
        self.assertEqual(
            criteria,
            'FROM "boss@x.com" SUBJECT "weekly report" UNSEEN',
        )
        self.assertEqual(post, {"has_attachment": True})

    def test_free_text_joined_and(self):
        criteria, _ = parse_query("overleg agenda")
        self.assertEqual(criteria, 'TEXT "overleg" TEXT "agenda"')

    def test_free_text_mixed_with_operators(self):
        criteria, _ = parse_query("from:alice@ow.test vergadering")
        self.assertEqual(
            criteria, 'FROM "alice@ow.test" TEXT "vergadering"')

    def test_empty_query(self):
        self.assertEqual(parse_query(""), ("", {}))

    def test_whitespace_only(self):
        self.assertEqual(parse_query("   \t  "), ("", {}))

    def test_quote_escaping_in_value(self):
        # Users shouldn't craft quotes inside quoted values but the
        # parser shouldn't crash if they do.
        criteria, _ = parse_query('from:"a\\"b@x.com"')
        self.assertIn('FROM ', criteria)

    def test_unterminated_quote_falls_back(self):
        # A single quote becomes a literal text token on the fallback
        # whitespace split — must not raise.
        criteria, _ = parse_query('subject:"unterminated')
        self.assertIn("SUBJECT", criteria)

    # ------------------------------------------------------------------
    # has_operators

    def test_has_operators_true(self):
        self.assertTrue(has_operators("from:alice"))
        self.assertTrue(has_operators("hello from:alice"))
        self.assertTrue(has_operators("is:unread"))

    def test_has_operators_false(self):
        self.assertFalse(has_operators("hello world"))
        self.assertFalse(has_operators(""))
        self.assertFalse(has_operators(None))
