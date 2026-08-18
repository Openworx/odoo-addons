"""Draft autosave: APPENDUID parsing (unit) + replace/discard round-trip
against GreenMail (integration, self-skips without the container)."""
import imaplib
import json
import unittest

from odoo.tests import HttpCase, TransactionCase, tagged

from .helpers import (
    GREENMAIL_HOST,
    GREENMAIL_IMAP_PORT,
    GREENMAIL_PASS,
    GREENMAIL_SMTP_PORT,
    GREENMAIL_USER,
    greenmail_reachable,
)


@tagged("post_install", "-at_install", "ow_mail")
class TestParseAppenduid(TransactionCase):

    def _parse(self, data):
        return self.env["ow.mail.account"]._parse_appenduid(data)

    def test_typical_response(self):
        self.assertEqual(
            self._parse([b"[APPENDUID 1721321 42] APPEND completed."]), 42)

    def test_no_uidplus(self):
        self.assertIsNone(self._parse([b"APPEND completed."]))

    def test_empty_and_garbage(self):
        self.assertIsNone(self._parse(None))
        self.assertIsNone(self._parse([None, b""]))


@unittest.skipUnless(greenmail_reachable(),
                     "GreenMail not reachable at greenmail:3143")
@tagged("post_install", "-at_install", "ow_mail")
class TestDraftAutosave(HttpCase):

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
            "name": "Draft Greenmail",
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
        # GreenMail starts with INBOX only — make a Drafts folder so the
        # name-hint heuristic maps drafts_folder_id.
        cls.account._refresh_folders()
        if not cls.account.drafts_folder_id:
            cls.account.imap_create_folder("", "Drafts")
        assert cls.account.drafts_folder_id, "Drafts folder not mapped"

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

    def _drafts_subjects(self):
        conn = imaplib.IMAP4(GREENMAIL_HOST, GREENMAIL_IMAP_PORT)
        conn.login(GREENMAIL_USER, GREENMAIL_PASS)
        try:
            typ, _sel = conn.select(
                f'"{self.account.drafts_folder_id.full_path}"')
            self.assertEqual(typ, "OK")
            typ, data = conn.uid("SEARCH", None, "ALL")
            uids = data[0].split() if data and data[0] else []
            subjects = []
            for uid in uids:
                typ, fetched = conn.uid(
                    "FETCH", uid.decode(),
                    "(BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
                for item in fetched or []:
                    if isinstance(item, tuple) and len(item) == 2:
                        line = (item[1] or b"").decode(errors="replace")
                        subjects.append(
                            line.replace("Subject:", "").strip())
            return subjects
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def _save(self, subject, replace_uid=None):
        return self._rpc("/ow_mail/draft", {
            "account_id": self.account.id,
            "to": "someone@ow.test",
            "subject": subject,
            "body_html": f"<p>{subject}</p>",
            "replace_uid": replace_uid,
        })

    def test_autosave_replace_keeps_single_copy(self):
        self.authenticate("admin", "admin")
        baseline = len(self._drafts_subjects())
        first = self._save("Autosave v1")
        self.assertTrue(first.get("ok"))
        self.assertTrue(first.get("uid"),
                        "APPENDUID/Message-ID fallback returned no uid")
        second = self._save("Autosave v2", replace_uid=first["uid"])
        self.assertTrue(second.get("ok"))
        subjects = self._drafts_subjects()
        self.assertEqual(len(subjects), baseline + 1,
                         "replace must not leave two draft copies")
        self.assertIn("Autosave v2", subjects)
        self.assertNotIn("Autosave v1", subjects)
        # Cleanup for other tests in this class.
        self._rpc("/ow_mail/draft/discard", {
            "account_id": self.account.id, "uid": second["uid"]})

    def test_discard_removes_draft(self):
        self.authenticate("admin", "admin")
        baseline = len(self._drafts_subjects())
        res = self._save("To be discarded")
        self.assertTrue(res.get("uid"))
        out = self._rpc("/ow_mail/draft/discard", {
            "account_id": self.account.id, "uid": res["uid"]})
        self.assertTrue(out.get("ok"))
        self.assertEqual(len(self._drafts_subjects()), baseline)
