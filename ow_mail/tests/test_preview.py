"""Unit tests for the list-view preview snippet helpers.

Pure-Python — no IMAP round-trips. ``snippet_from_bytes`` must survive
truncated transfer encodings (the payload comes from a partial
``BODY.PEEK[..]<0.N>`` fetch) and ``_parse_preview_fetch`` must group
imaplib's two-literals-per-message response shape on both Dovecot- and
GreenMail-style item ordering.
"""
import base64

from odoo.tests import TransactionCase, tagged

from ..models.ow_mail_imap import (
    _parse_preview_fetch,
    preview_plan_from_bodystructure,
    snippet_from_bytes,
)


@tagged("post_install", "-at_install", "ow_mail")
class TestPreviewSnippet(TransactionCase):

    def test_plain_utf8(self):
        self.assertEqual(
            snippet_from_bytes("Hallo  wereld\r\n".encode(), "text/plain",
                               "7bit", "utf-8"),
            "Hallo wereld")

    def test_empty_payload(self):
        self.assertEqual(
            snippet_from_bytes(b"", "text/plain", "7bit", "utf-8"), "")

    def test_truncation(self):
        out = snippet_from_bytes(b"x" * 500, "text/plain", "7bit", "utf-8",
                                 max_len=120)
        self.assertEqual(len(out), 120)

    def test_quoted_printable_truncated_escape(self):
        # "café" in qp with the final escape cut mid-sequence by the
        # partial fetch: decoding must not raise.
        payload = b"caf=C3=A9 en meer =C"
        out = snippet_from_bytes(payload, "text/plain",
                                 "quoted-printable", "utf-8")
        self.assertTrue(out.startswith("café"))

    def test_base64_truncated(self):
        full = base64.b64encode("Dit is een base64 voorbeeldtekst".encode())
        # Chop off 3 bytes so the length is no longer a multiple of 4.
        out = snippet_from_bytes(full[:-3], "text/plain", "base64", "utf-8")
        self.assertTrue(out.startswith("Dit is een base64"))

    def test_html_stripped(self):
        html = (b"<html><head><style>p{color:red}</style></head>"
                b"<body><p>Hello <b>world</b> &amp; you</p></body></html>")
        self.assertEqual(
            snippet_from_bytes(html, "text/html", "7bit", "utf-8"),
            "Hello world & you")

    def test_latin1_fallback(self):
        payload = "prijs €5".encode("utf-8")
        # A bogus charset label falls through to utf-8.
        self.assertEqual(
            snippet_from_bytes(payload, "text/plain", "7bit", "x-bogus"),
            "prijs €5")

    def test_non_text_part_yields_empty(self):
        self.assertEqual(
            snippet_from_bytes(b"\x89PNG...", "image/png", "base64", None), "")


@tagged("post_install", "-at_install", "ow_mail")
class TestPreviewFetchParser(TransactionCase):

    def test_dovecot_order(self):
        # Dovecot: "seq (UID n BODY[1.MIME] {len}" then " BODY[1]<0> {len}".
        data = [
            (b"1 (UID 7 BODY[1.MIME] {10}", b"Content-Type: text/plain\r\n\r\n"),
            (b" BODY[1]<0> {5}", b"hello"),
            b")",
            (b"2 (UID 9 BODY[1.MIME] {10}", b"Content-Type: text/plain\r\n\r\n"),
            (b" BODY[1]<0> {5}", b"world"),
            b")",
        ]
        grouped = _parse_preview_fetch(data)
        self.assertEqual(grouped[7]["1"], b"hello")
        self.assertEqual(grouped[9]["1"], b"world")

    def test_greenmail_order_uid_last(self):
        # Some servers emit the UID in a later item of the group.
        data = [
            (b"1 (BODY[1.MIME] {10} UID 3", b"mimehdr"),
            (b" BODY[1]<0> {4}", b"body"),
            b")",
        ]
        grouped = _parse_preview_fetch(data)
        self.assertEqual(grouped[3]["1.MIME"], b"mimehdr")
        self.assertEqual(grouped[3]["1"], b"body")

    def test_garbage_items_ignored(self):
        self.assertEqual(_parse_preview_fetch([b")", None, (b"junk",)]), {})


@tagged("post_install", "-at_install", "ow_mail")
class TestPreviewPlan(TransactionCase):
    """BODYSTRUCTURE → (section, ctype, charset, cte) plan derivation.

    Fixture lines mirror real server output; the single-part one is
    verbatim GreenMail (which rejects ``BODY[1.MIME]`` on such messages —
    the reason this parser exists at all).
    """

    def test_single_part_plain(self):
        line = ('5 (BODYSTRUCTURE ("text" "plain" ("charset" "utf-8") '
                'NIL NIL "7bit" 50 1 NIL NIL NIL) UID 10)')
        self.assertEqual(preview_plan_from_bodystructure(line),
                         ("1", "text/plain", "utf-8", "7bit"))

    def test_multipart_alternative(self):
        line = ('7 (UID 12 BODYSTRUCTURE (("text" "plain" ("charset" "utf-8") '
                'NIL NIL "quoted-printable" 60 2 NIL NIL NIL)'
                '("text" "html" ("charset" "utf-8") NIL NIL "base64" 120 3 '
                'NIL NIL NIL) "alternative" ("boundary" "b1") NIL NIL))')
        self.assertEqual(preview_plan_from_bodystructure(line),
                         ("1", "text/plain", "utf-8", "quoted-printable"))

    def test_mixed_wrapping_alternative(self):
        line = ('9 (UID 14 BODYSTRUCTURE ((("text" "plain" ("charset" "utf-8") '
                'NIL NIL "7bit" 30 1 NIL NIL NIL)'
                '("text" "html" ("charset" "utf-8") NIL NIL "7bit" 60 1 NIL '
                'NIL NIL) "alternative" ("boundary" "b2") NIL NIL)'
                '("text" "csv" ("charset" "us-ascii") NIL NIL "base64" 20 1 '
                'NIL ("attachment" ("filename" "d.csv")) NIL) "mixed" '
                '("boundary" "b3") NIL NIL))')
        self.assertEqual(preview_plan_from_bodystructure(line),
                         ("1.1", "text/plain", "utf-8", "7bit"))

    def test_non_text_first_leaf_skipped(self):
        line = ('3 (BODYSTRUCTURE ("image" "png" NIL NIL NIL "base64" 500 '
                'NIL NIL NIL) UID 4)')
        self.assertIsNone(preview_plan_from_bodystructure(line))

    def test_no_bodystructure(self):
        self.assertIsNone(preview_plan_from_bodystructure("5 (UID 10 FLAGS ())"))
