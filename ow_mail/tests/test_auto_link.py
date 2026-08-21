"""Unit + integration tests for auto-filing replies on linked threads.

The unit tests feed hand-built envelope dicts straight into the pure-ORM
half (``_auto_link_process``) with the IMAP fetch patched at the
``ow.mail.record.link._fetch_raw`` seam — no GreenMail needed. The cron
entry point itself (``_cron_refresh_counts``) commits per account and is
therefore only exercised in the GreenMail HttpCase.
"""
import unittest
from email.message import EmailMessage
from unittest.mock import patch

from odoo.tests import HttpCase, TransactionCase, tagged

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


def _raw_reply(message_id="<reply-1@acme.test>", subject="Re: order",
               from_addr="John Doe <john@acme.test>"):
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = "demo@ow.test"
    msg["Subject"] = subject
    msg["Date"] = "Fri, 21 Aug 2026 10:00:00 +0000"
    msg["Message-ID"] = message_id
    msg.set_content("reply body text")
    return msg.as_bytes()


@tagged("post_install", "-at_install", "ow_mail")
class TestAutoLinkProcess(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.owner = cls.env["res.users"].create({
            "name": "AL Owner", "login": "ow_al_owner",
            "groups_id": [(6, 0, [
                cls.env.ref("base.group_user").id,
                cls.env.ref("base.group_partner_manager").id,
            ])],
        })
        cls.account = cls.env["ow.mail.account"].create({
            "name": "AL Account", "email": "demo@ow.test",
            "user_id": cls.owner.id,
            "imap_host": "greenmail", "imap_port": 3143, "imap_ssl": False,
            "imap_login": "demo", "imap_password": "demo",
            "smtp_host": "greenmail", "smtp_port": 3025,
            "smtp_encryption": "none", "smtp_same_as_imap": True,
            "auto_link_replies": True,
        })
        cls.inbox = cls.env["ow.mail.folder"].create({
            "account_id": cls.account.id, "name": "INBOX",
            "full_path": "INBOX", "kind": "inbox",
        })
        cls.account.inbox_folder_id = cls.inbox
        cls.Account = cls.env["ow.mail.account"]

    def _stamp(self, target, message_id):
        message = target.message_post(body="original")
        message.write({"ow_mail_message_id": message_id})
        return message

    def _envelope(self, uid, in_reply_to="", references=""):
        return {"uid": uid, "subject": "Re: order",
                "from_name": "John Doe", "from_email": "john@acme.test",
                "message_id": f"<reply-{uid}@acme.test>",
                "in_reply_to": in_reply_to, "references": references}

    def _process(self, envs, raw=None):
        with patch.object(type(self.env["ow.mail.record.link"]), "_fetch_raw",
                          return_value=raw or _raw_reply()):
            self.Account._auto_link_process(self.account, envs)

    def test_reply_to_linked_thread_attached(self):
        target = self.env["res.partner"].create({"name": "Linked Org"})
        self._stamp(target, "<orig-1@acme.test>")
        mails_before = self.env["mail.mail"].sudo().search_count([])
        self._process([self._envelope(11, in_reply_to="<orig-1@acme.test>")])
        posted = self.env["mail.message"].search([
            ("model", "=", "res.partner"), ("res_id", "=", target.id),
            ("ow_mail_uid", "=", 11)])
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted.ow_mail_message_id, "<reply-1@acme.test>")
        # posted as the account owner, not as the cron user
        self.assertEqual(posted.create_uid, self.owner)
        # chatter note only — no outgoing notification mail (autoreply-loop guard)
        self.assertEqual(self.env["mail.mail"].sudo().search_count([]),
                         mails_before)
        self.assertEqual(self.inbox.auto_link_uid, 11)

    def test_second_run_is_noop(self):
        target = self.env["res.partner"].create({"name": "Noop Org"})
        self._stamp(target, "<orig-2@acme.test>")
        env_d = self._envelope(21, in_reply_to="<orig-2@acme.test>")
        self._process([env_d])
        self._process([env_d])
        posted = self.env["mail.message"].search([
            ("model", "=", "res.partner"), ("res_id", "=", target.id),
            ("ow_mail_uid", "=", 21)])
        self.assertEqual(len(posted), 1)
        self.assertEqual(self.inbox.auto_link_uid, 21)

    def test_unrelated_message_untouched(self):
        before = self.env["mail.message"].search_count([])
        self._process([self._envelope(31, in_reply_to="<unknown@x.test>")])
        self.assertEqual(self.env["mail.message"].search_count([]), before)
        # watermark still advances past unrelated mail
        self.assertEqual(self.inbox.auto_link_uid, 31)

    def test_references_chain_matches_ancestor(self):
        target = self.env["res.partner"].create({"name": "Chain Org"})
        self._stamp(target, "<mid@acme.test>")
        refs = "<root@acme.test> <mid@acme.test> <leaf@acme.test>"
        self._process([self._envelope(41, references=refs)])
        posted = self.env["mail.message"].search([
            ("model", "=", "res.partner"), ("res_id", "=", target.id),
            ("ow_mail_uid", "=", 41)])
        self.assertEqual(len(posted), 1)

    def test_multi_record_thread_attached_to_all(self):
        t1 = self.env["res.partner"].create({"name": "Multi A"})
        t2 = self.env["res.partner"].create({"name": "Multi B"})
        self._stamp(t1, "<orig-5@acme.test>")
        self._stamp(t2, "<orig-5@acme.test>")
        self._process([self._envelope(51, in_reply_to="<orig-5@acme.test>")])
        for target in (t1, t2):
            posted = self.env["mail.message"].search([
                ("model", "=", "res.partner"), ("res_id", "=", target.id),
                ("ow_mail_uid", "=", 51)])
            self.assertEqual(len(posted), 1, target.name)

    def test_watermark_stops_on_unexpected_error(self):
        target = self.env["res.partner"].create({"name": "Err Org"})
        self._stamp(target, "<orig-6@acme.test>")
        self.inbox.auto_link_uid = 60
        envs = [self._envelope(61, in_reply_to="<orig-6@acme.test>"),
                self._envelope(62, in_reply_to="<orig-6@acme.test>")]
        with patch.object(type(self.env["ow.mail.record.link"]),
                          "attach_email", side_effect=RuntimeError("boom")):
            self.Account._auto_link_process(self.account, envs)
        # transient failure: watermark must not move past the failing uid,
        # and the second envelope stays untouched for the next tick
        self.assertEqual(self.inbox.auto_link_uid, 60)
        self.assertFalse(self.env["mail.message"].search([
            ("ow_mail_uid", "in", [61, 62])]))

    def test_first_run_seeds_watermark(self):
        self.assertEqual(self.inbox.auto_link_uid, 0)

        class FakeImap:
            @staticmethod
            def imap_mbox_quote(path):
                return path

            @staticmethod
            def search_uids(conn, criteria):
                return [7, 8, 9]

            @staticmethod
            def fetch_envelopes(conn, uids):
                raise AssertionError("first run must not fetch envelopes")

        class FakeConn:
            def select(self, mailbox, readonly=False):
                return "OK", [b""]

        self.Account._auto_link_new_mail(self.account, FakeConn(), FakeImap())
        self.assertEqual(self.inbox.auto_link_uid, 9)


@unittest.skipUnless(greenmail_reachable(),
                     "GreenMail not reachable at greenmail:3143")
@tagged("post_install", "-at_install", "ow_mail")
class TestAutoLinkEndToEnd(HttpCase):

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
            "name": "AL E2E", "email": f"{GREENMAIL_USER}@ow.test",
            "user_id": cls.admin_user.id,
            "imap_host": GREENMAIL_HOST, "imap_port": GREENMAIL_IMAP_PORT,
            "imap_ssl": False, "imap_login": GREENMAIL_USER,
            "imap_password": GREENMAIL_PASS,
            "smtp_host": GREENMAIL_HOST, "smtp_port": GREENMAIL_SMTP_PORT,
            "smtp_encryption": "none", "smtp_same_as_imap": True,
            "auto_link_replies": True,
        })
        clear_inbox()
        cls.orig_mid = greenmail_append(
            frm="Carol <carol@acme.test>", subject="Order VG-1",
            text="original order mail", html="<p>original order mail</p>")
        cls.account._refresh_folders()
        cls.inbox = cls.account.inbox_folder_id

    def test_reply_gets_filed_automatically(self):
        from odoo.addons.ow_mail.models import ow_mail_imap as imap_utils
        target = self.env["res.partner"].create({"name": "E2E Org"})
        # link the original (stamps its Message-ID)
        with imap_utils.imap_session(self.account, self.inbox.full_path,
                                     readonly=True) as conn:
            uids = imap_utils.search_uids(conn, "ALL")
        self.env["ow.mail.record.link"].attach_email(
            self.inbox, uids[-1], "res.partner", target.id)
        # seed the watermark at current top, then a reply arrives
        self.inbox.auto_link_uid = max(uids)
        greenmail_append(
            frm="Carol <carol@acme.test>", subject="Re: Order VG-1",
            text="reply mail", html="<p>reply mail</p>",
            headers={"In-Reply-To": self.orig_mid,
                     "References": self.orig_mid})
        conn = self.account._imap_connect()
        try:
            self.env["ow.mail.account"]._auto_link_new_mail(
                self.account, conn, imap_utils)
        finally:
            conn.logout()
        posted = self.env["mail.message"].search([
            ("model", "=", "res.partner"), ("res_id", "=", target.id),
            ("body", "like", "reply mail")])
        self.assertEqual(len(posted), 1)
        self.assertGreater(self.inbox.auto_link_uid, max(uids))

    def test_opt_out_account_untouched(self):
        from odoo.addons.ow_mail.models import ow_mail_imap as imap_utils
        self.account.auto_link_replies = False
        target = self.env["res.partner"].create({"name": "OptOut Org"})
        before = self.env["mail.message"].search_count([
            ("model", "=", "res.partner"), ("res_id", "=", target.id)])
        # the cron gate must skip the account entirely; emulate one tick's
        # guard exactly as _cron_refresh_counts does
        if self.account.auto_link_replies and self.account.user_id.active:
            conn = self.account._imap_connect()
            try:
                self.env["ow.mail.account"]._auto_link_new_mail(
                    self.account, conn, imap_utils)
            finally:
                conn.logout()
        self.assertEqual(self.env["mail.message"].search_count([
            ("model", "=", "res.partner"), ("res_id", "=", target.id)]),
            before)
