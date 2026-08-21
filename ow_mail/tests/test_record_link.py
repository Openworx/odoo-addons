"""Unit tests for ``ow.mail.record.link`` — the create-record-from-email
helper (curated menu, generic prefill, attach + Message-ID stamping, and
the linked-records lookup).

Pure ORM: the IMAP fetch inside ``attach_email`` goes through the
``_fetch_raw`` seam, patched here with an in-memory RFC 822 message —
no GreenMail required.
"""
from datetime import datetime, timezone
from email.message import EmailMessage
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


def _build_raw(subject="Help with order", from_addr="John Doe <john@acme.test>",
               message_id="<orig-1@acme.test>", html="<p>Please help.</p>",
               attachments=None):
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = "demo@ow.test"
    msg["Subject"] = subject
    msg["Date"] = "Tue, 19 Aug 2026 10:00:00 +0000"
    msg["Message-ID"] = message_id
    msg.set_content("plain fallback")
    msg.add_alternative(html, subtype="html")
    for att in attachments or []:
        msg.add_attachment(att["content"], maintype="application",
                           subtype="octet-stream", filename=att["filename"])
    return msg.as_bytes()


@tagged("post_install", "-at_install", "ow_mail")
class TestRecordLink(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = cls.env.ref("base.user_admin")
        cls.employee = cls.env["res.users"].create({
            "name": "RL Employee", "login": "ow_rl_employee",
            "groups_id": [(6, 0, [cls.env.ref("base.group_user").id])],
        })
        cls.account = cls.env["ow.mail.account"].create({
            "name": "RL Account",
            "email": "demo@ow.test",
            "user_id": cls.admin.id,
            "imap_host": "greenmail", "imap_port": 3143, "imap_ssl": False,
            "imap_login": "demo", "imap_password": "demo",
            "smtp_host": "greenmail", "smtp_port": 3025,
            "smtp_encryption": "none", "smtp_same_as_imap": True,
        })
        cls.folder = cls.env["ow.mail.folder"].create({
            "account_id": cls.account.id, "name": "INBOX",
            "full_path": "INBOX", "kind": "inbox",
        })
        cls.Link = cls.env["ow.mail.record.link"]

    # -- helpers ----------------------------------------------------------

    def _envelope(self, **overrides):
        env = {
            "subject": "Help with order",
            "from_name": "John Doe",
            "from_email": "john@acme.test",
            "to": "demo@ow.test", "cc": "", "reply_to": "",
            "message_id": "<orig-1@acme.test>",
            "in_reply_to": "", "references": "",
            "date": datetime(2026, 8, 19, 10, 0, tzinfo=timezone.utc),
        }
        env.update(overrides)
        return env

    # -- curated menu ------------------------------------------------------

    def test_menu_admin_sees_installed_models(self):
        menu = self.Link.with_user(self.admin).get_create_menu()
        models = [e["model"] for e in menu]
        self.assertIn("res.partner", models)
        self.assertIn("calendar.event", models)
        if "crm.lead" in self.env:
            self.assertIn("crm.lead", models)
            keys = [e["key"] for e in menu]
            self.assertIn("lead", keys)
            self.assertIn("opportunity", keys)
        # every entry is JSON-serializable: label forced to str
        for entry in menu:
            self.assertIsInstance(entry["label"], str)
            self.assertTrue(entry["key"] and entry["icon"])

    def test_menu_hides_models_without_create_access(self):
        menu = self.Link.with_user(self.employee).get_create_menu()
        models = [e["model"] for e in menu]
        # employees can create calendar events (and, per Odoo 18 base
        # ACLs, project tasks)...
        self.assertIn("calendar.event", models)
        # ...but not leads/orders without the app groups
        self.assertNotIn("crm.lead", models)
        self.assertNotIn("sale.order", models)

    def test_menu_never_lists_uninstalled_models(self):
        menu = self.Link.with_user(self.admin).get_create_menu()
        for entry in menu:
            self.assertIn(entry["model"], self.env)

    # -- "Other…" picker list ---------------------------------------------

    def test_creatable_models_excludes_blocklisted_and_includes_partner(self):
        rows = self.Link.with_user(self.admin).get_creatable_models()
        models = [r["model"] for r in rows]
        self.assertIn("res.partner", models)
        self.assertNotIn("res.users", models)          # blocklisted
        self.assertNotIn("mail.mail", models)          # not a thread + blocklisted
        self.assertFalse([m for m in models if m.startswith("ir.")])
        self.assertFalse([m for m in models if m.startswith("ow.mail")])
        for row in rows:
            self.assertTrue(row["name"])

    def test_creatable_models_respects_create_access(self):
        if "crm.lead" not in self.env:
            self.skipTest("crm not installed")
        rows = self.Link.with_user(self.employee).get_creatable_models()
        self.assertNotIn("crm.lead", [r["model"] for r in rows])

    # -- prefill ------------------------------------------------------------

    def test_prefill_res_partner_uses_sender_not_subject(self):
        ctx = self.Link.prefill_context(
            "res.partner", self._envelope(), "<p>Body</p>")
        self.assertEqual(ctx.get("default_name"), "John Doe")
        self.assertEqual(ctx.get("default_email"), "john@acme.test")
        self.assertNotIn("default_comment", ctx)

    def test_prefill_crm_lead_generic_mapping(self):
        if "crm.lead" not in self.env:
            self.skipTest("crm not installed")
        partner = self.env["res.partner"].create(
            {"name": "John Doe", "email": "john@acme.test"})
        ctx = self.Link.prefill_context(
            "crm.lead", self._envelope(), "<p>Please help.</p>",
            extra_context={"default_type": "opportunity"})
        self.assertEqual(ctx.get("default_name"), "Help with order")
        self.assertEqual(ctx.get("default_email_from"), "john@acme.test")
        self.assertEqual(ctx.get("default_partner_id"), partner.id)
        self.assertIn("Please help.", ctx.get("default_description") or "")
        self.assertEqual(ctx.get("default_type"), "opportunity")

    def test_prefill_skips_sequence_name(self):
        if "sale.order" not in self.env:
            self.skipTest("sale not installed")
        ctx = self.Link.prefill_context(
            "sale.order", self._envelope(), "<p>Body</p>")
        self.assertNotIn("default_name", ctx)

    def test_prefill_helpdesk_ticket_title_and_email(self):
        """OCA helpdesk.ticket: _rec_name is the readonly sequence field
        ``number``, so the subject must fall back to the ``name`` (Title)
        field; the sender lands in ``partner_email`` even though the model
        declares no ``_primary_email``."""
        if "helpdesk.ticket" not in self.env:
            self.skipTest("helpdesk not installed")
        ctx = self.Link.prefill_context(
            "helpdesk.ticket", self._envelope(), "<p>Please help.</p>")
        self.assertEqual(ctx.get("default_name"), "Help with order")
        self.assertEqual(ctx.get("default_partner_email"), "john@acme.test")
        self.assertNotIn("default_number", ctx)
        self.assertIn("Please help.", ctx.get("default_description") or "")

    def test_prefill_calendar_event_start_stop(self):
        ctx = self.Link.prefill_context(
            "calendar.event", self._envelope(), "<p>Body</p>")
        start = ctx.get("default_start")
        stop = ctx.get("default_stop")
        self.assertTrue(start and stop)
        dt_start = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
        dt_stop = datetime.strptime(stop, "%Y-%m-%d %H:%M:%S")
        self.assertGreater(dt_stop, dt_start)

    # -- sender lookup helper ------------------------------------------------

    def test_find_by_email_normalized_hit(self):
        partner = self.env["res.partner"].create(
            {"name": "Jane", "email": "jane@acme.test"})
        found = self.env["res.partner"]._ow_find_by_email("Jane@Acme.TEST")
        self.assertEqual(found, partner)

    def test_find_by_email_ilike_fallback(self):
        partner = self.env["res.partner"].create(
            {"name": "Old", "email": "old@acme.test"})
        # simulate a legacy row without a normalized value
        self.env.cr.execute(
            "UPDATE res_partner SET email_normalized = NULL WHERE id = %s",
            (partner.id,))
        self.env["res.partner"].invalidate_model(["email_normalized"])
        found = self.env["res.partner"]._ow_find_by_email("OLD@acme.test")
        self.assertEqual(found, partner)

    def test_find_by_email_miss(self):
        self.assertFalse(
            self.env["res.partner"]._ow_find_by_email("nobody@nowhere.test"))
        self.assertFalse(self.env["res.partner"]._ow_find_by_email(""))

    # -- attach_email ---------------------------------------------------------

    def _attach(self, target, raw=None, uid=101, include_attachments=True):
        raw = raw or _build_raw()
        with patch.object(type(self.Link), "_fetch_raw", return_value=raw):
            return self.Link.with_user(self.admin).attach_email(
                self.folder, uid, target._name, target.id,
                include_attachments=include_attachments)

    def test_attach_posts_message_and_stamps(self):
        sender = self.env["res.partner"].create(
            {"name": "John Doe", "email": "john@acme.test"})
        target = self.env["res.partner"].create({"name": "Target Co"})
        message = self._attach(target)
        self.assertEqual(message.model, "res.partner")
        self.assertEqual(message.res_id, target.id)
        self.assertEqual(message.ow_mail_uid, 101)
        self.assertEqual(message.ow_mail_folder_id, self.folder)
        self.assertEqual(message.ow_mail_message_id, "<orig-1@acme.test>")
        self.assertIn("Help with order", message.body)
        self.assertEqual(message.author_id, sender)
        self.assertIn(sender, target.message_partner_ids)

    def test_attach_includes_attachments(self):
        target = self.env["res.partner"].create({"name": "Attach Co"})
        raw = _build_raw(attachments=[
            {"filename": "quote.pdf", "content": b"%PDF-1.4 fake"}])
        message = self._attach(target, raw=raw)
        self.assertEqual(len(message.attachment_ids), 1)
        self.assertEqual(message.attachment_ids.name, "quote.pdf")
        self.assertEqual(message.attachment_ids.res_model, "res.partner")
        self.assertEqual(message.attachment_ids.res_id, target.id)

    def test_attach_duplicate_message_id_blocked(self):
        target = self.env["res.partner"].create({"name": "Dup Co"})
        self._attach(target)
        with self.assertRaises(UserError):
            # same Message-ID, different uid — still a duplicate
            self._attach(target, uid=202)

    def test_attach_duplicate_legacy_stamp_blocked(self):
        """Pre-upgrade rows have uid/folder stamps but no Message-ID."""
        target = self.env["res.partner"].create({"name": "Legacy Co"})
        target.message_post(body="old import")
        target.message_ids[0].write({
            "ow_mail_uid": 101, "ow_mail_folder_id": self.folder.id})
        raw = _build_raw(message_id="<fresh@acme.test>")
        with self.assertRaises(UserError):
            self._attach(target, raw=raw, uid=101)

    # -- linked_records -----------------------------------------------------

    def _stamp(self, target, message_id="", uid=0):
        message = target.message_post(body="stamped")
        message.write({
            "ow_mail_message_id": message_id,
            "ow_mail_uid": uid,
            "ow_mail_folder_id": self.folder.id if uid else False,
        })
        return message

    def test_linked_records_by_message_id(self):
        target = self.env["res.partner"].create({"name": "Linked Co"})
        self._stamp(target, message_id="<orig-9@acme.test>")
        rows = self.Link.with_user(self.admin).linked_records(
            self.folder, 999, "<orig-9@acme.test>")
        self.assertEqual(
            [(r["model"], r["res_id"]) for r in rows],
            [("res.partner", target.id)])
        self.assertEqual(rows[0]["display_name"], "Linked Co")

    def test_linked_records_uid_fallback(self):
        """Pre-upgrade stamps (no Message-ID) resolve via uid + folder."""
        target = self.env["res.partner"].create({"name": "Fallback Co"})
        self._stamp(target, uid=333)
        rows = self.Link.with_user(self.admin).linked_records(
            self.folder, 333, "")
        self.assertEqual(
            [(r["model"], r["res_id"]) for r in rows],
            [("res.partner", target.id)])

    def test_linked_records_dedupes(self):
        target = self.env["res.partner"].create({"name": "Dedupe Co"})
        self._stamp(target, message_id="<orig-7@acme.test>")
        self._stamp(target, message_id="<orig-7@acme.test>", uid=444)
        rows = self.Link.with_user(self.admin).linked_records(
            self.folder, 444, "<orig-7@acme.test>")
        self.assertEqual(len(rows), 1)

    def test_linked_records_filters_unreadable_models(self):
        if "crm.lead" not in self.env:
            self.skipTest("crm not installed")
        lead = self.env["crm.lead"].create({"name": "Secret lead"})
        self._stamp(lead, message_id="<orig-8@acme.test>")
        rows_admin = self.Link.with_user(self.admin).linked_records(
            self.folder, 998, "<orig-8@acme.test>")
        self.assertIn(("crm.lead", lead.id),
                      [(r["model"], r["res_id"]) for r in rows_admin])
        rows_emp = self.Link.with_user(self.employee).linked_records(
            self.folder, 998, "<orig-8@acme.test>")
        self.assertNotIn(("crm.lead", lead.id),
                         [(r["model"], r["res_id"]) for r in rows_emp])
