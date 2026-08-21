"""Integration tests for the record-integration routes against GreenMail.

Covers the full HTTP round-trip of the create-record flow:
``/ow_mail/record/prefill`` → ``/ow_mail/record/attach`` → the
``linked_records`` key of ``/ow_mail/message``. Self-skips when GreenMail
isn't reachable (pure unit coverage lives in test_record_link.py).
"""
import json
import unittest

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
class TestRecordRoutes(HttpCase):

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
            "name": "Record Routes",
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
        clear_inbox()
        cls.message_id = greenmail_append(
            frm="Carol Customer <carol@acme.test>",
            subject="Need a quote for 10 licenses",
            html="<p>Can you send a quote?</p>",
            attachments=[{"content": b"specs", "filename": "specs.txt",
                          "maintype": "text", "subtype": "plain"}],
        )
        cls.account._refresh_folders()
        cls.inbox = cls.account.inbox_folder_id

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

    def _inbox_uid(self):
        result = self._rpc("/ow_mail/messages", {
            "folder_id": self.inbox.id, "filter": "all", "limit": 10})
        messages = result.get("messages", [])
        self.assertTrue(messages, "fixture message not found in INBOX")
        return messages[0]["uid"]

    def test_bootstrap_carries_create_menu(self):
        self.authenticate("admin", "admin")
        result = self._rpc("/ow_mail/bootstrap", {})
        menu = result.get("create_menu")
        self.assertTrue(menu)
        self.assertIn("res.partner", [e["model"] for e in menu])

    def test_prefill_attach_and_linked_records(self):
        self.authenticate("admin", "admin")
        uid = self._inbox_uid()

        prefill = self._rpc("/ow_mail/record/prefill", {
            "folder_id": self.inbox.id, "uid": uid, "key": "contact"})
        self.assertEqual(prefill.get("model"), "res.partner")
        ctx = prefill.get("context") or {}
        self.assertEqual(ctx.get("default_name"), "Carol Customer")
        self.assertEqual(ctx.get("default_email"), "carol@acme.test")

        partner = self.env["res.partner"].create(
            {"name": "Carol Customer", "email": "carol@acme.test"})
        attach = self._rpc("/ow_mail/record/attach", {
            "folder_id": self.inbox.id, "uid": uid,
            "model": "res.partner", "res_id": partner.id})
        self.assertTrue(attach.get("ok"))
        self.assertEqual(attach.get("display_name"), "Carol Customer")

        stamped = self.env["mail.message"].sudo().search([
            ("model", "=", "res.partner"), ("res_id", "=", partner.id),
            ("ow_mail_message_id", "!=", False)])
        self.assertEqual(len(stamped), 1)
        self.assertEqual(stamped.ow_mail_message_id, self.message_id)
        self.assertEqual(stamped.attachment_ids.name, "specs.txt")

        message = self._rpc("/ow_mail/message", {
            "folder_id": self.inbox.id, "uid": uid, "peek": True})
        linked = message.get("linked_records") or []
        self.assertIn(("res.partner", partner.id),
                      [(r["model"], r["res_id"]) for r in linked])

    def test_thread_carries_linked_records(self):
        """The stacked view needs the linked records too — the /ow_mail/thread
        wire dict must carry them per message."""
        self.authenticate("admin", "admin")
        uid = self._inbox_uid()
        partner = self.env["res.partner"].create({"name": "Thread Co"})
        attach = self._rpc("/ow_mail/record/attach", {
            "folder_id": self.inbox.id, "uid": uid,
            "model": "res.partner", "res_id": partner.id})
        self.assertTrue(attach.get("ok"))
        thread = self._rpc("/ow_mail/thread", {
            "account_id": self.account.id, "message_ids": [self.message_id]})
        messages = thread.get("messages") or []
        self.assertTrue(messages, "thread search returned no messages")
        linked = [(r["model"], r["res_id"]) for m in messages
                  for r in (m.get("linked_records") or [])]
        self.assertIn(("res.partner", partner.id), linked)

    def test_prefill_rejects_unknown_key_and_model(self):
        self.authenticate("admin", "admin")
        uid = self._inbox_uid()
        result = self._rpc("/ow_mail/record/prefill", {
            "folder_id": self.inbox.id, "uid": uid, "key": "nope"})
        self.assertTrue(result.get("error"))
        result = self._rpc("/ow_mail/record/prefill", {
            "folder_id": self.inbox.id, "uid": uid, "model": "res.users"})
        self.assertTrue(result.get("error"))

    def test_record_models_route(self):
        self.authenticate("admin", "admin")
        result = self._rpc("/ow_mail/record/models", {})
        models = [r["model"] for r in result.get("models") or []]
        self.assertIn("res.partner", models)
        self.assertNotIn("res.users", models)
