"""Integration tests for /ow_mail/thread — full conversation from any member.

Regression guard for the "stacked replies missing" report: the thread
search must return the complete chain whether it is queried from the
root, a middle message, or the newest reply. Requires GreenMail
(self-skips otherwise); the chain spans INBOX and Sent to cover the
own-reply case.
"""
import json
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import make_msgid

from odoo.tests import HttpCase, tagged

from .helpers import (
    GREENMAIL_HOST,
    GREENMAIL_IMAP_PORT,
    GREENMAIL_PASS,
    GREENMAIL_SMTP_PORT,
    GREENMAIL_USER,
    greenmail_reachable,
)

SUBJ = "ThreadRoute onderwerp"


@unittest.skipUnless(greenmail_reachable(),
                     "GreenMail not reachable at greenmail:3143")
@tagged("post_install", "-at_install", "ow_mail")
class TestThreadRoute(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cron = cls.env.ref(
            "ow_mail.ir_cron_ow_mail_counts", raise_if_not_found=False)
        if cron:
            cron.active = False
        cls.admin_user = cls.env.ref("base.user_admin")
        cls.env = cls.env(user=cls.admin_user)
        cls.account = cls.env["ow.mail.account"].create({
            "name": "Thread Greenmail",
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
        cls.account._refresh_folders()
        if not cls.account.sent_folder_id:
            cls.account.imap_create_folder("", "Sent")
        assert cls.account.sent_folder_id, "Sent folder not mapped"

        now = datetime.now(timezone.utc)
        cls.root_id = make_msgid(domain="thread.ow.test")
        cls.mid_id = make_msgid(domain="thread.ow.test")
        cls.leaf_id = make_msgid(domain="thread.ow.test")
        # root (INBOX) -> eigen reply (Sent) -> nieuwste reply (INBOX);
        # met expliciete Message-IDs zodat de keten deterministisch is.
        cls._append_with_ids(
            "INBOX", "Alice <alice@ow.test>", SUBJ, "root",
            cls.root_id, None, None, now - timedelta(hours=3))
        cls._append_with_ids(
            "Sent", f"Demo <{GREENMAIL_USER}@ow.test>", f"Re: {SUBJ}",
            "eigen antwoord", cls.mid_id, cls.root_id, cls.root_id,
            now - timedelta(hours=2))
        cls._append_with_ids(
            "INBOX", "Alice <alice@ow.test>", f"Re: {SUBJ}",
            "nieuwste reply", cls.leaf_id, cls.mid_id,
            f"{cls.root_id} {cls.mid_id}", now - timedelta(hours=1))

    @staticmethod
    def _append_with_ids(folder, frm, subject, text, msgid,
                         in_reply_to, references, date_utc):
        import imaplib
        from email.message import EmailMessage
        from email.utils import formatdate
        m = EmailMessage()
        m["From"] = frm
        m["To"] = f"{GREENMAIL_USER}@ow.test"
        m["Subject"] = subject
        m["Date"] = formatdate(date_utc.timestamp(), localtime=False)
        m["Message-ID"] = msgid
        if in_reply_to:
            m["In-Reply-To"] = in_reply_to
        if references:
            m["References"] = references
        m.set_content(text)
        conn = imaplib.IMAP4(GREENMAIL_HOST, GREENMAIL_IMAP_PORT)
        conn.login(GREENMAIL_USER, GREENMAIL_PASS)
        try:
            typ, _ = conn.append(
                folder, "", imaplib.Time2Internaldate(date_utc), m.as_bytes())
            assert typ == "OK"
        finally:
            conn.logout()

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

    def _thread_subjects(self, message_ids):
        self.authenticate("admin", "admin")
        res = self._rpc("/ow_mail/thread", {
            "account_id": self.account.id, "message_ids": message_ids})
        return [m["subject"] for m in res.get("messages", [])
                if SUBJ in m["subject"]]

    def test_full_thread_from_root(self):
        # Alleen de root-id (zoals de client die voor een rootbericht heeft):
        # nakomelingen moeten via hun References gevonden worden.
        subjects = self._thread_subjects([self.root_id])
        self.assertEqual(len(subjects), 3,
                         f"root gaf {subjects} i.p.v. de volledige keten")

    def test_full_thread_from_middle(self):
        subjects = self._thread_subjects([self.mid_id, self.root_id])
        self.assertEqual(len(subjects), 3)

    def test_full_thread_from_leaf(self):
        subjects = self._thread_subjects(
            [self.leaf_id, self.mid_id, self.root_id])
        self.assertEqual(len(subjects), 3)
