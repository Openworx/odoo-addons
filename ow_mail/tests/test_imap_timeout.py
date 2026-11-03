# Part of ow_mail.
"""Every IMAP connection has a timeout, as SMTP does.

Without one, a mail server that stops answering holds the worker until
Odoo's time limit kills it, and in the counts cron it stalls every account
queued behind it. ``IMAP4`` and ``IMAP4_SSL`` are mocked: no network.
"""
import imaplib
import socket
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "ow_mail")
class TestImapTimeout(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account = cls.env["ow.mail.account"].create({
            "name": "Timeout Account", "email": "slow@ow.test",
            "imap_host": "imap.ow.test", "imap_port": 993, "imap_ssl": True,
            "imap_login": "slow", "imap_password": "slow",
            "smtp_host": "smtp.ow.test", "smtp_port": 465,
            "smtp_encryption": "ssl", "smtp_same_as_imap": True,
        })
        cls.wizard = cls.env["ow.mail.connect.wizard"].create({
            "name": "Slow", "email": "slow@ow.test", "password": "slow",
            "imap_host": "imap.ow.test", "imap_port": 993, "imap_ssl": True,
            "smtp_host": "smtp.ow.test",
        })

    def _connect(self, ssl):
        self.account.imap_ssl = ssl
        name = "IMAP4_SSL" if ssl else "IMAP4"
        with patch.object(imaplib, name, MagicMock()) as imap, \
                patch.object(type(self.account), "_imap_authenticate"):
            self.account._imap_connect()
        return imap.call_args.kwargs.get("timeout")

    def test_account_connections_time_out(self):
        self.assertEqual(self._connect(ssl=True), 30)
        self.assertEqual(self._connect(ssl=False), 30)

    def test_wizard_probe_times_out(self):
        for ssl in (True, False):
            self.wizard.imap_ssl = ssl
            name = "IMAP4_SSL" if ssl else "IMAP4"
            with patch.object(imaplib, name, MagicMock()) as imap:
                self.wizard._probe_imap("slow")
            self.assertEqual(imap.call_args.kwargs.get("timeout"), 15)

    def test_wizard_reports_a_login_timeout(self):
        conn = MagicMock()
        conn.login.side_effect = socket.timeout("timed out")
        with patch.object(imaplib, "IMAP4_SSL", MagicMock(return_value=conn)):
            with self.assertRaises(UserError):
                self.wizard._probe_imap("slow")
