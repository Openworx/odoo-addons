# Part of ow_mail.
"""A user cannot hand their own records to another user.

The "own only" record rules are checked before a write, against the
record's current owner, and Odoo does not check them again afterwards. So
without a check after the write, a user could write ``user_id`` (or a
folder's ``account_id``) on a record of their own over RPC and move it into
a colleague's client: an account on a server they control, shown as the
colleague's own mailbox; a trusted sender that turns off Safe Mode for
their address; contacts in the colleague's autocomplete.
"""
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "ow_mail")
class TestOwnerReassign(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        group_user = cls.env.ref("base.group_user").id
        cls.user_a, cls.user_b = (
            cls.env["res.users"].create({
                "name": f"Owner {x}", "login": f"ow_owner_{x}",
                "group_ids": [(4, group_user)],
            })
            for x in ("a", "b")
        )
        cls.account_a, cls.account_b = (
            cls.env["ow.mail.account"].create({
                "name": f"Account {user.name}", "email": f"{user.login}@ow.test",
                "user_id": user.id,
                "imap_host": "imap.ow.test", "imap_port": 993, "imap_ssl": True,
                "imap_login": user.login, "imap_password": "x",
                "smtp_host": "smtp.ow.test", "smtp_port": 465,
                "smtp_encryption": "ssl", "smtp_same_as_imap": True,
            })
            for user in (cls.user_a, cls.user_b)
        )
        cls.folder_a = cls.env["ow.mail.folder"].create({
            "account_id": cls.account_a.id, "name": "INBOX",
            "full_path": "INBOX", "kind": "inbox",
        })

    def _as_a(self, record):
        self.env.invalidate_all()
        return record.with_user(self.user_a)

    def _assert_cannot_give_away(self, record, vals):
        with self.assertRaises(AccessError):
            self._as_a(record).write(vals)

    def _own(self, model, vals):
        return self.env[model].with_user(self.user_a).create(vals)

    def test_account(self):
        self._assert_cannot_give_away(self.account_a, {"user_id": self.user_b.id})

    def test_folder_to_another_users_account(self):
        self._assert_cannot_give_away(self.folder_a, {"account_id": self.account_b.id})

    def test_folder_related_owner_does_not_move_the_folder(self):
        # A folder's user_id is a read-only related copy of its account's,
        # and nothing decides access by it; writing it changes no owner.
        self._as_a(self.folder_a).write({"user_id": self.user_b.id})
        self.env.invalidate_all()
        self.assertEqual(self.account_a.user_id, self.user_a)
        visible_to_b = self.env["ow.mail.folder"].with_user(self.user_b).search([])
        self.assertNotIn(self.folder_a, visible_to_b)

    def test_tag(self):
        tag = self._own("ow.mail.tag", {"name": "Mine"})
        self._assert_cannot_give_away(tag, {"user_id": self.user_b.id})

    def test_tag_renamed_and_given_away(self):
        tag = self._own("ow.mail.tag", {"name": "Mine", "imap_keyword": "Mine"})
        self._assert_cannot_give_away(tag, {"name": "Theirs", "user_id": self.user_b.id})

    def test_contact(self):
        contact = self._own("ow.mail.contact", {"name": "Eve", "email": "eve@evil.test"})
        self._assert_cannot_give_away(contact, {"user_id": self.user_b.id})

    def test_trusted_sender(self):
        sender = self._own("ow.mail.trusted.sender", {"email": "evil.test"})
        self._assert_cannot_give_away(sender, {"user_id": self.user_b.id})

    def test_preferences(self):
        prefs = self._own("ow.mail.preferences", {})
        self._assert_cannot_give_away(prefs, {"user_id": self.user_b.id})

    def test_owner_may_still_edit_and_keep_ownership(self):
        account = self._as_a(self.account_a)
        account.write({"name": "Renamed", "user_id": self.user_a.id})
        self.assertEqual(account.name, "Renamed")
        folder = self._as_a(self.folder_a)
        folder.write({"name": "Inbox", "account_id": self.account_a.id})
        self.assertEqual(folder.name, "Inbox")

    def test_superuser_may_reassign(self):
        self.account_a.sudo().write({"user_id": self.user_b.id})
        self.assertEqual(self.account_a.user_id, self.user_b)
        self.assertEqual(self.folder_a.user_id, self.user_b)
