"""Regression tests for the audit findings H1, M1 and M3.

* H1 — transient wizards are per-creator. Odoo's ORM gives TransientModel
  no creator isolation of its own (``TransientModel`` has no
  ``_check_access`` override, so ``ir.model.access`` + ``ir.rule`` decide),
  and these wizards hold a mailbox password and full message bodies.
* M1 — ``auth="ow_mail_user"`` keeps logged-in portal users out of the
  routes; stock ``auth="user"`` only rejects the *public* user.
* M3 — client-supplied Message-IDs are IMAP-quoted before they reach the
  wire, so neither a quote nor a CRLF can alter the command.

M2 (sandboxed print frame) is frontend-only and has no test harness here;
it is verified in the browser.
"""
import json
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests import HttpCase, TransactionCase, tagged

from ..models.ow_mail_imap import search_thread_uids
from ..models.ow_mail_search import _imap_quote, parse_query


@tagged("post_install", "-at_install", "ow_mail")
class TestTransientWizardIsolation(TransactionCase):
    """One user's wizard rows must be invisible to every other user."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        group_user = cls.env.ref("base.group_user").id
        cls.user_a = cls.env["res.users"].create({
            "name": "Sec A", "login": "ow_sec_a",
            "group_ids": [(4, group_user)],
        })
        cls.user_b = cls.env["res.users"].create({
            "name": "Sec B", "login": "ow_sec_b",
            "group_ids": [(4, group_user)],
        })

    def _assert_hidden_from_b(self, record):
        model = record._name
        visible = self.env[model].with_user(self.user_b).search([])
        self.assertNotIn(record.id, visible.ids,
                         f"{model} row leaked into user B's search")
        with self.assertRaises(AccessError):
            self.env[model].with_user(self.user_b).browse(record.id).read()

    def test_connect_wizard_password_not_readable_by_others(self):
        wizard = self.env["ow.mail.connect.wizard"].with_user(self.user_a).create({
            "name": "Work", "email": "a@example.test", "password": "s3cret",
            "imap_host": "imap.example.test", "smtp_host": "smtp.example.test",
        })
        self.assertEqual(wizard.password, "s3cret")
        self._assert_hidden_from_b(wizard)

    def test_attach_wizard_body_not_readable_by_others(self):
        model_id = self.env["ir.model"]._get_id("res.partner")
        wizard = self.env["ow.mail.attach.record"].with_user(self.user_a).create({
            "folder_id": 1, "uid": 42,
            "subject": "Quarterly numbers",
            "body_html": "<p>confidential</p>",
            "res_model_id": model_id,
            "res_id": self.env.user.partner_id.id,
        })
        self._assert_hidden_from_b(wizard)

    def test_compose_wizard_not_readable_by_others(self):
        account = self.env["ow.mail.account"].with_user(self.user_a).create({
            "name": "Sec A box", "email": "a@example.test",
            "imap_host": "imap.example.test", "imap_login": "a",
            "smtp_host": "smtp.example.test",
        })
        wizard = self.env["ow.mail.compose"].with_user(self.user_a).create({
            "account_id": account.id, "to": "b@example.test",
            "subject": "Draft", "body_html": "<p>confidential</p>",
        })
        self._assert_hidden_from_b(wizard)

    def test_own_wizard_still_readable(self):
        """The rule must not lock users out of their own rows."""
        wizard = self.env["ow.mail.connect.wizard"].with_user(self.user_a).create({
            "name": "Work", "email": "a@example.test", "password": "s3cret",
        })
        again = self.env["ow.mail.connect.wizard"].with_user(
            self.user_a).search([("id", "=", wizard.id)])
        self.assertEqual(again, wizard)


@tagged("post_install", "-at_install", "ow_mail")
class TestConnectWizardPasswordLifetime(TransactionCase):
    """The plain-text password must not outlive the wizard."""

    def test_max_hours_is_short(self):
        self.assertLessEqual(
            self.env["ow.mail.connect.wizard"]._transient_max_hours, 0.1)

    def test_row_dropped_after_successful_connect(self):
        wizard = self.env["ow.mail.connect.wizard"].create({
            "name": "Work", "email": "demo@example.test", "password": "s3cret",
            "imap_host": "imap.example.test", "imap_port": 993,
            "smtp_host": "smtp.example.test", "smtp_port": 465,
        })
        Wizard = type(self.env["ow.mail.connect.wizard"])
        Account = type(self.env["ow.mail.account"])
        with patch.object(Wizard, "_probe_imap", lambda self, login: None), \
             patch.object(Wizard, "_probe_smtp", lambda self, login: None), \
             patch.object(Account, "action_test_connection", lambda self: True), \
             patch.object(Account, "action_sync", lambda self: True):
            wizard.action_connect()
        self.assertFalse(wizard.exists(),
                         "wizard row with the plain-text password survived")


@tagged("post_install", "-at_install", "ow_mail")
class TestRouteAuthGate(HttpCase):
    """Portal users authenticate fine — they must still not reach the mail."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.portal_user = cls.env["res.users"].create({
            "name": "Portal Peer", "login": "ow_sec_portal",
            "password": "ow_sec_portal_pw",
            "group_ids": [(6, 0, [cls.env.ref("base.group_portal").id])],
        })

    def _bootstrap(self):
        response = self.url_open(
            "/ow_mail/bootstrap",
            data=json.dumps({"jsonrpc": "2.0", "method": "call", "params": {}}),
            headers={"Content-Type": "application/json"},
        )
        return response.json()

    def test_portal_user_refused(self):
        self.authenticate("ow_sec_portal", "ow_sec_portal_pw")
        payload = self._bootstrap()
        self.assertIn("error", payload)
        self.assertNotIn("result", payload)

    def test_internal_user_allowed(self):
        self.authenticate("admin", "admin")
        payload = self._bootstrap()
        self.assertNotIn("error", payload)
        self.assertIn("accounts", payload["result"])


@tagged("post_install", "-at_install", "ow_mail")
class TestImapQuoting(TransactionCase):
    """No client-supplied value may alter the IMAP command being sent."""

    def test_quote_escapes_quote_and_backslash(self):
        self.assertEqual(_imap_quote('a"b'), '"a\\"b"')
        self.assertEqual(_imap_quote("a\\b"), '"a\\\\b"')

    def test_quote_drops_control_characters(self):
        self.assertEqual(_imap_quote("a\r\nA1 NOOP"), '"aA1 NOOP"')
        self.assertEqual(_imap_quote("a\x00b"), '"ab"')

    def test_dsl_output_has_no_control_characters(self):
        criteria, _post = parse_query('subject:"weekly\r\nA1 NOOP"')
        self.assertNotIn("\r", criteria)
        self.assertNotIn("\n", criteria)

    def _thread_criteria(self, message_ids):
        captured = []

        class FakeConn:
            @staticmethod
            def uid(command, charset, criteria):
                captured.append(criteria)
                return "OK", [b""]

        search_thread_uids(FakeConn(), message_ids)
        self.assertTrue(captured, "no SEARCH was issued")
        return captured[0]

    def test_thread_ids_are_quoted(self):
        criteria = self._thread_criteria(["<plain@example.test>"])
        self.assertIn('HEADER Message-ID "plain@example.test"', criteria)

    def test_thread_id_cannot_break_out_of_the_quoted_string(self):
        criteria = self._thread_criteria(['a" TEXT "b'])
        self.assertIn('HEADER Message-ID "a\\" TEXT \\"b"', criteria)

    def test_thread_id_cannot_inject_a_second_command(self):
        criteria = self._thread_criteria(["x\r\nA1 LOGOUT"])
        self.assertNotIn("\r", criteria)
        self.assertNotIn("\n", criteria)
