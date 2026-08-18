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
