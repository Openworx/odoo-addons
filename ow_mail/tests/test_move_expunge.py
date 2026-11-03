# Part of ow_mail.
"""Move and delete never expunge originals that were not copied, and expunge
only the messages acted on.

imaplib raises only on ``BAD``; a ``NO`` to ``COPY`` comes back as a return
value, so the COPY fallback for servers without MOVE has to check it before
flagging and expunging the originals. The IMAP connection is a fake that
records commands: no network, no GreenMail.
"""
import imaplib
from unittest.mock import patch

from odoo.tests import HttpCase, TransactionCase, tagged


class FakeImap:
    """Answers OK to everything except the commands given in ``replies``.

    A reply that is an exception is raised, as imaplib raises on ``BAD``.
    """

    def __init__(self, **replies):
        self.replies = replies
        self.calls = []

    def _answer(self, command):
        reply = self.replies.get(command, ("OK", [b"done"]))
        if isinstance(reply, Exception):
            raise reply
        return reply

    def select(self, mailbox, readonly=False):
        return "OK", [b"3"]

    def uid(self, command, *args):
        self.calls.append(("UID " + command, *args))
        return self._answer("UID_" + command)

    def expunge(self):
        self.calls.append(("EXPUNGE",))
        return self._answer("EXPUNGE")

    def logout(self):
        return "BYE", [b""]

    def commands(self):
        return [call[0] for call in self.calls]


NO_MOVE = imaplib.IMAP4.error("UID command error: BAD [b'Unknown command MOVE']")
NO_UIDPLUS = imaplib.IMAP4.error("UID command error: BAD [b'Unknown command EXPUNGE']")


def _account_vals(user):
    return {
        "name": "Move Account", "email": "move@ow.test", "user_id": user.id,
        "imap_host": "imap.ow.test", "imap_port": 993, "imap_ssl": True,
        "imap_login": "move", "imap_password": "move",
        "smtp_host": "smtp.ow.test", "smtp_port": 465,
        "smtp_encryption": "ssl", "smtp_same_as_imap": True,
        "notify_new_mail": False, "auto_link_replies": False,
    }


@tagged("post_install", "-at_install", "ow_mail")
class TestMessageMoveDelete(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        admin = cls.env.ref("base.user_admin")
        cls.account = cls.env["ow.mail.account"].create(_account_vals(admin))
        Folder = cls.env["ow.mail.folder"]
        cls.inbox, cls.archive, cls.trash = (
            Folder.create({"account_id": cls.account.id, "name": name,
                           "full_path": name, "kind": kind})
            for name, kind in (("INBOX", "inbox"), ("Archive", "archive"),
                               ("Trash", "trash"))
        )

    def _act(self, conn, action, payload=None, trash=True):
        self.account.trash_folder_id = self.trash if trash else False
        Account = type(self.env["ow.mail.account"])
        Folder = type(self.env["ow.mail.folder"])
        self.authenticate("admin", "admin")
        with patch.object(Account, "_imap_connect", lambda acc: conn), \
                patch.object(Folder, "refresh_counts", lambda folder: None):
            return self.make_jsonrpc_request("/ow_mail/message/action", {
                "folder_id": self.inbox.id, "uids": [7, 9],
                "action": action, "payload": payload or {},
            })

    def test_move_uses_move(self):
        conn = FakeImap()
        result = self._act(conn, "move", {"folder_id": self.archive.id})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(conn.commands(), ["UID MOVE"])

    def test_move_fallback_expunges_only_moved_messages(self):
        conn = FakeImap(UID_MOVE=NO_MOVE)
        result = self._act(conn, "move", {"folder_id": self.archive.id})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(conn.commands(),
                         ["UID MOVE", "UID COPY", "UID STORE", "UID EXPUNGE"])
        self.assertEqual(conn.calls[-1], ("UID EXPUNGE", "7,9"))

    def test_move_fallback_keeps_originals_when_copy_fails(self):
        conn = FakeImap(UID_MOVE=NO_MOVE,
                        UID_COPY=("NO", [b"[OVERQUOTA] Quota exceeded"]))
        result = self._act(conn, "move", {"folder_id": self.archive.id})
        self.assertIn("Quota exceeded", result.get("error", ""))
        self.assertEqual(conn.commands(), ["UID MOVE", "UID COPY"],
                         "nothing may be flagged or expunged after a failed COPY")

    def test_refused_move_is_reported(self):
        conn = FakeImap(UID_MOVE=("NO", [b"[TRYCREATE] Mailbox does not exist"]))
        result = self._act(conn, "move", {"folder_id": self.archive.id})
        self.assertIn("Mailbox does not exist", result.get("error", ""))
        self.assertEqual(conn.commands(), ["UID MOVE"])

    def test_delete_to_trash_keeps_originals_when_copy_fails(self):
        conn = FakeImap(UID_MOVE=NO_MOVE,
                        UID_COPY=("NO", [b"[OVERQUOTA] Quota exceeded"]))
        result = self._act(conn, "delete")
        self.assertIn("error", result)
        self.assertEqual(conn.commands(), ["UID MOVE", "UID COPY"])

    def test_delete_without_trash_expunges_only_deleted_messages(self):
        conn = FakeImap()
        result = self._act(conn, "delete", trash=False)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(conn.commands(), ["UID STORE", "UID EXPUNGE"])
        self.assertEqual(conn.calls[-1], ("UID EXPUNGE", "7,9"))

    def test_delete_without_uidplus_falls_back_to_expunge(self):
        conn = FakeImap(UID_EXPUNGE=NO_UIDPLUS)
        result = self._act(conn, "delete", trash=False)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(conn.commands(), ["UID STORE", "UID EXPUNGE", "EXPUNGE"])


@tagged("post_install", "-at_install", "ow_mail")
class TestDiscardDraft(TransactionCase):

    def test_discard_expunges_only_the_draft(self):
        account = self.env["ow.mail.account"].create(
            _account_vals(self.env.ref("base.user_admin")))
        account.drafts_folder_id = self.env["ow.mail.folder"].create({
            "account_id": account.id, "name": "Drafts",
            "full_path": "Drafts", "kind": "drafts"})
        conn = FakeImap()
        with patch.object(type(account), "_imap_connect", lambda acc: conn):
            self.assertTrue(account.discard_draft(42))
        self.assertEqual(conn.calls, [
            ("UID STORE", "42", "+FLAGS", r"(\Deleted)"),
            ("UID EXPUNGE", "42"),
        ])
