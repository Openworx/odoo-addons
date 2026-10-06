# Part of ow_mail.
"""Folder names in IMAP modified-UTF-7 must be stored decoded for display."""
from unittest.mock import patch, MagicMock
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "ow_mail")
class TestFolderNames(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account = cls.env["ow.mail.account"].create({
            "name": "Acc", "email": "demo@ow.test",
            "user_id": cls.env.ref("base.user_admin").id,
            "imap_host": "h", "imap_port": 993, "imap_ssl": True,
            "imap_login": "demo", "imap_password": "demo",
            "smtp_host": "h", "smtp_port": 465, "smtp_encryption": "ssl",
            "smtp_same_as_imap": True,
        })
        cls.account.state = "confirmed"

    def test_modified_utf7_folder_name_is_decoded(self):
        conn = MagicMock()
        conn.list.return_value = ("OK", [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren) "/" "INBOX/cumplea&APE-os"',
        ])
        conn.lsub.return_value = ("OK", [])
        _STATUS = "odoo.addons.ow_mail.models.ow_mail_imap.status_counts"
        with patch.object(type(self.account), "_imap_connect", return_value=conn), \
             patch(_STATUS, return_value=(0, 0)):
            self.account._refresh_folders()
        folder = self.env["ow.mail.folder"].search([
            ("account_id", "=", self.account.id),
            ("full_path", "=", "INBOX/cumplea&APE-os"),
        ])
        self.assertTrue(folder, "the folder must exist with its RAW full_path")
        self.assertEqual(folder.name, "cumpleaños",
                         "the display name must be decoded from modified UTF-7")
