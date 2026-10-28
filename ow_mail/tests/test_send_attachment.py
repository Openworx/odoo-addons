"""Attachments on outgoing mail and drafts.

Since Odoo 20 ``ir.attachment.raw`` is a ``BinaryValue`` (``LocalBinaryFile``
for filestore attachments), not ``bytes``; ``EmailMessage.add_attachment``
only accepts bytes, so the content must be coerced before building the MIME
message.
"""
from unittest.mock import MagicMock, patch

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestSendAttachment(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account = cls.env["ow.mail.account"].create({
            "name": "Attachment Account",
            "email": "demo@ow.test",
            "user_id": cls.env.ref("base.user_admin").id,
            "imap_host": "greenmail", "imap_port": 3143, "imap_ssl": False,
            "imap_login": "demo", "imap_password": "demo",
            "smtp_host": "greenmail", "smtp_port": 3025,
            "smtp_encryption": "none", "smtp_same_as_imap": True,
        })

    def _attachment(self):
        att = self.env["ir.attachment"].create({
            "name": "hello.txt",
            "raw": b"hello world",
            "mimetype": "text/plain",
            "res_model": "ow.mail.compose",
            "res_id": 0,
        })
        # Read back from storage as the send route does in a fresh request
        att.invalidate_recordset()
        return att

    def test_send_mail_with_attachment(self):
        att = self._attachment()
        smtp = MagicMock()
        with patch.object(type(self.account), "_smtp_connect", return_value=smtp):
            self.account.send_mail(to="customer@ow.test", subject="Attachment",
                                   body_html="<p>Hello</p>", attachment_ids=[att.id])
        msg = smtp.send_message.call_args.args[0]
        parts = list(msg.iter_attachments())
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].get_filename(), "hello.txt")
        self.assertEqual(parts[0].get_content(), "hello world")

    def test_save_draft_with_attachment(self):
        att = self._attachment()
        imap = MagicMock()
        imap.append.return_value = ("OK", [b"[APPENDUID 1 7] done"])
        self.account.drafts_folder_id = self.env["ow.mail.folder"].create({
            "account_id": self.account.id, "name": "Drafts",
            "full_path": "Drafts", "kind": "drafts",
        })
        with patch.object(type(self.account), "_imap_connect", return_value=imap):
            self.account.save_draft(to="customer@ow.test", subject="Attachment",
                                    body_html="<p>Hello</p>", attachment_ids=[att.id])
        raw = imap.append.call_args.args[3]
        self.assertIn(b'filename="hello.txt"', raw)
