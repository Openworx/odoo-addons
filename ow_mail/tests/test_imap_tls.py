"""IMAP over SSL verifies the server's certificate and hostname.

Given no ``ssl_context``, ``imaplib.IMAP4_SSL`` falls back to
``ssl._create_stdlib_context()``, which verifies neither, so both
connection paths pass ``ssl.create_default_context()`` as SMTP does.
``IMAP4_SSL`` is mocked: no network, no GreenMail.
"""
import imaplib
import ssl
from unittest.mock import MagicMock, patch

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "ow_mail")
class TestImapTls(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account = cls.env["ow.mail.account"].create({
            "name": "TLS Account", "email": "tls@ow.test",
            "imap_host": "imap.ow.test", "imap_port": 993, "imap_ssl": True,
            "imap_login": "tls", "imap_password": "tls",
            "smtp_host": "smtp.ow.test", "smtp_port": 465,
            "smtp_encryption": "ssl", "smtp_same_as_imap": True,
        })
        cls.wizard = cls.env["ow.mail.connect.wizard"].create({
            "name": "TLS", "email": "tls@ow.test", "password": "tls",
            "imap_host": "imap.ow.test", "imap_port": 993, "imap_ssl": True,
            "smtp_host": "smtp.ow.test",
        })

    def _assert_verifying(self, imap_ssl):
        imap_ssl.assert_called_once()
        ctx = imap_ssl.call_args.kwargs.get("ssl_context")
        self.assertIsNotNone(ctx, "IMAP4_SSL got no ssl_context")
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_account_connect_verifies_certificate(self):
        with patch.object(imaplib, "IMAP4_SSL", MagicMock()) as imap_ssl, \
                patch.object(type(self.account), "_imap_authenticate"):
            self.account._imap_connect()
        self._assert_verifying(imap_ssl)

    def test_wizard_probe_verifies_certificate(self):
        with patch.object(imaplib, "IMAP4_SSL", MagicMock()) as imap_ssl:
            self.wizard._probe_imap("tls")
        self._assert_verifying(imap_ssl)

    def test_plain_imap_gets_no_context(self):
        self.account.imap_ssl = False
        with patch.object(imaplib, "IMAP4", MagicMock()) as imap_plain, \
                patch.object(type(self.account), "_imap_authenticate"):
            self.account._imap_connect()
        imap_plain.assert_called_once_with("imap.ow.test", 993)
