"""Integration tests for /ow_mail/messages against the GreenMail sandbox.

Self-skips if GreenMail isn't reachable — pure unit tests still run
in that case. When the Compose stack is up, these exercise the full
stack: IMAP SEARCH, envelope fetch, and post-filters.
"""
import json
import unittest
from datetime import datetime, timedelta, timezone

from odoo.tests import HttpCase, tagged

from .helpers import (
    GREENMAIL_HOST,
    GREENMAIL_IMAP_PORT,
    GREENMAIL_PASS,
    GREENMAIL_SMTP_PORT,
    GREENMAIL_USER,
    clear_inbox,
    greenmail_append,
    greenmail_reachable,
)


@unittest.skipUnless(greenmail_reachable(),
                     "GreenMail not reachable at greenmail:3143")
@tagged("post_install", "-at_install", "ow_mail")
class TestMessagesRoute(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Don't let _cron_refresh_counts interfere with our test account.
        cron = cls.env.ref(
            "ow_mail.ir_cron_ow_mail_counts", raise_if_not_found=False)
        if cron:
            cron.active = False
        cls.admin_user = cls.env.ref("base.user_admin")
        cls.env = cls.env(user=cls.admin_user)
        cls.account = cls.env["ow.mail.account"].create({
            "name": "Test Greenmail",
            "email": f"{GREENMAIL_USER}@ow.test",
            "user_id": cls.admin_user.id,
            "imap_host": GREENMAIL_HOST,
            "imap_port": GREENMAIL_IMAP_PORT,
            "imap_ssl": False,
            "imap_login": GREENMAIL_USER,
            "imap_password": GREENMAIL_PASS,
            "smtp_host": GREENMAIL_HOST,
            "smtp_port": GREENMAIL_SMTP_PORT,
            "smtp_encryption": "none",
            "smtp_same_as_imap": True,
        })
        # Reset the GreenMail inbox to a known state and append fixtures.
        clear_inbox()
        now = datetime.now(timezone.utc)
        # Tag `work` for label: tests.
        cls.tag_work = cls.env["ow.mail.tag"].create({
            "name": "work",
            "user_id": cls.admin_user.id,
        })
        # 1. Plain from Alice, subject 'Weekly report', dated recent.
        greenmail_append(
            frm="Alice <alice@ow.test>",
            subject="Weekly report — Q2",
            html="<p>Numbers attached next time.</p>",
            date_utc=now - timedelta(hours=1),
        )
        # 2. From Bob, subject about lunch, dated recent.
        greenmail_append(
            frm="Bob <bob@ow.test>",
            subject="Lunch tomorrow?",
            html="<p>Place on 5th?</p>",
            date_utc=now - timedelta(hours=2),
        )
        # 3. From Alice WITH attachment.
        greenmail_append(
            frm="Alice <alice@ow.test>",
            subject="Report with spreadsheet",
            html="<p>See xlsx</p>",
            attachments=[{
                "content": b"col1,col2\n1,2\n",
                "filename": "data.csv",
                "maintype": "text",
                "subtype": "csv",
            }],
            date_utc=now - timedelta(hours=3),
        )
        # 4. Older message (2 months back) for date filtering.
        cls.old_date = now - timedelta(days=60)
        greenmail_append(
            frm="Old <old@ow.test>",
            subject="Ancient history",
            html="<p>Months ago</p>",
            date_utc=cls.old_date,
        )
        # Pull the folder tree + special folder mapping.
        cls.account._refresh_folders()

    # ------------------------------------------------------------------
    # Helpers

    def _rpc(self, url, params):
        response = self.url_open(
            url,
            data=json.dumps({
                "jsonrpc": "2.0", "method": "call", "params": params,
            }),
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        return response.json()["result"]

    def _search(self, query, filter="all"):
        self.authenticate("admin", "admin")
        inbox = self.account.inbox_folder_id
        self.assertTrue(inbox, "Inbox folder not mapped")
        result = self._rpc("/ow_mail/messages", {
            "folder_id": inbox.id,
            "filter": filter,
            "search": query,
            "limit": 100,
        })
        return result

    def _subjects(self, result):
        return sorted(m["subject"] for m in result.get("messages", []))

    # ------------------------------------------------------------------
    # Tests

    def test_from_filters_sender(self):
        res = self._search("from:alice@ow.test")
        subjects = self._subjects(res)
        self.assertIn("Weekly report — Q2", subjects)
        self.assertIn("Report with spreadsheet", subjects)
        self.assertNotIn("Lunch tomorrow?", subjects)
        self.assertNotIn("Ancient history", subjects)

    def test_subject_substring(self):
        res = self._search('subject:"weekly report"')
        subjects = self._subjects(res)
        self.assertEqual(subjects, ["Weekly report — Q2"])

    def test_has_attachment(self):
        res = self._search("has:attachment")
        subjects = self._subjects(res)
        # Only message 3 carries an attachment part.
        self.assertEqual(subjects, ["Report with spreadsheet"])

    def test_since_filters_recent_only(self):
        # Everything in the last 30 days, excluding the 60-day-old one.
        thirty_days_ago = (datetime.now(timezone.utc) -
                           timedelta(days=30)).strftime("%Y-%m-%d")
        res = self._search(f"since:{thirty_days_ago}")
        subjects = self._subjects(res)
        self.assertNotIn("Ancient history", subjects)
        self.assertIn("Weekly report — Q2", subjects)

    def test_and_composition(self):
        res = self._search("from:alice@ow.test has:attachment")
        subjects = self._subjects(res)
        self.assertEqual(subjects, ["Report with spreadsheet"])

    def test_free_text_fallback_still_works(self):
        # No operators → legacy broad OR + substring fallback path.
        res = self._search("Lunch")
        subjects = self._subjects(res)
        self.assertIn("Lunch tomorrow?", subjects)

    def test_preview_snippets_populated(self):
        # helpers._build_message stores the subject as the text/plain
        # alternative, so the preview of each fixture equals its subject.
        res = self._search(None)
        by_subject = {m["subject"]: m for m in res.get("messages", [])}
        self.assertIn("Lunch tomorrow?", by_subject)
        self.assertEqual(by_subject["Lunch tomorrow?"]["preview"],
                         "Lunch tomorrow?")
        self.assertEqual(by_subject["Weekly report — Q2"]["preview"],
                         "Weekly report — Q2")
