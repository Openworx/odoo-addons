# Part of ow_mail.
"""fetch_envelopes must parse a message whose FETCH response is split by IMAP
literals (non-ASCII attachment filename), not take the first literal as headers."""

from odoo.tests import TransactionCase, tagged

from odoo.addons.ow_mail.models.ow_mail_imap import fetch_envelopes

# Real Dovecot 2.3 UID FETCH (UID FLAGS RFC822.SIZE BODYSTRUCTURE
# BODY.PEEK[HEADER.FIELDS (...)]) response for a message with a non-ASCII
# attachment filename ("261006 Flächen.pdf"): the server sends the filename as
# an IMAP literal {19}, so imaplib splits the single message across three
# tuples. The real headers are the LAST literal; the first two literals are the
# attachment name/filename and carry no UID.
_FETCH_SPLIT = [
    (b'1 (UID 1 FLAGS (\\Recent) RFC822.SIZE 503 BODYSTRUCTURE (("text" "plain" '
     b'("charset" "utf-8") NIL NIL "7bit" 27 0 NIL NIL NIL NIL)("application" '
     b'"pdf" ("name" {19}', b'261006 Fl\xc3\xa4chen.pdf'),
    (b') NIL NIL "base64" 12 NIL ("attachment" ("filename" {19}',
     b'261006 Fl\xc3\xa4chen.pdf'),
    (b')) NIL NIL) "mixed" ("boundary" "BB") NIL NIL NIL) '
     b'BODY[HEADER.FIELDS (SUBJECT FROM TO DATE MESSAGE-ID IN-REPLY-TO '
     b'REFERENCES)] {171}',
     b'From: Jutta Beispiel <jutta@example.com>\r\nTo: me@example.com\r\n'
     b'Subject: Re: Grundriss Wohnung 2\r\n'
     b'Date: Mon, 06 Oct 2026 10:00:00 +0000\r\n'
     b'Message-ID: <a1test@example.com>\r\n\r\n'),
    b')',
]

# Ordinary single-tuple response (ASCII, one literal = the header block).
_FETCH_SIMPLE = [
    (b'2 (UID 2 FLAGS (\\Seen) RFC822.SIZE 120 BODYSTRUCTURE ("text" "plain" '
     b'("charset" "us-ascii") NIL NIL "7bit" 10 1 NIL NIL NIL NIL) '
     b'BODY[HEADER.FIELDS (SUBJECT FROM TO DATE MESSAGE-ID IN-REPLY-TO '
     b'REFERENCES)] {64}',
     b'From: Bob <bob@example.com>\r\nSubject: Hello\r\n\r\n'),
    b')',
]


class _FakeConn:
    def __init__(self, data):
        self._data = data

    def uid(self, *args, **kwargs):
        return ("OK", self._data)


@tagged("post_install", "-at_install", "ow_mail")
class TestFetchLiterals(TransactionCase):
    def test_non_ascii_attachment_does_not_blank_the_row(self):
        """A non-ASCII attachment name must not blank subject/sender/date."""
        rows = fetch_envelopes(_FakeConn(_FETCH_SPLIT), [1])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["uid"], 1)
        self.assertEqual(row["subject"], "Re: Grundriss Wohnung 2")
        self.assertEqual(row["from_email"], "jutta@example.com")
        self.assertTrue(row["date"], "the date must be parsed, not None")
        self.assertTrue(row["has_attachments"])

    def test_ordinary_message_still_parses(self):
        """The common single-literal response keeps working."""
        rows = fetch_envelopes(_FakeConn(_FETCH_SIMPLE), [2])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subject"], "Hello")
        self.assertEqual(rows[0]["from_email"], "bob@example.com")
