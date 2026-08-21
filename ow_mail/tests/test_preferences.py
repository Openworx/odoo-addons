"""Unit tests for ``ow.mail.preferences`` and the tag keyword helpers.

No IMAP: ``_sync_keyword_on_server`` only contacts accounts in state
``confirmed``, and these tests create none, so tag rename/delete stays
a pure ORM affair here (the IMAP round-trip is covered by the GreenMail
integration tests).
"""
import json

from odoo.tests import HttpCase, TransactionCase, tagged


@tagged("post_install", "-at_install", "ow_mail")
class TestPreferences(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user_a = cls.env["res.users"].create({
            "name": "Prefs A", "login": "ow_prefs_a",
            "groups_id": [(4, cls.env.ref("base.group_user").id)],
        })
        cls.user_b = cls.env["res.users"].create({
            "name": "Prefs B", "login": "ow_prefs_b",
            "groups_id": [(4, cls.env.ref("base.group_user").id)],
        })

    def test_get_for_user_creates_defaults(self):
        Prefs = self.env["ow.mail.preferences"].with_user(self.user_a)
        prefs = Prefs._get_for_user()
        self.assertEqual(prefs.user_id, self.user_a)
        self.assertEqual(prefs.mark_read_delay, 0)
        self.assertFalse(prefs.thread_view_default)

    def test_get_for_user_idempotent(self):
        Prefs = self.env["ow.mail.preferences"].with_user(self.user_a)
        first = Prefs._get_for_user()
        second = Prefs._get_for_user()
        self.assertEqual(first, second)

    def test_ir_rule_isolation(self):
        prefs_a = self.env["ow.mail.preferences"].with_user(
            self.user_a)._get_for_user()
        prefs_a.write({"mark_read_delay": 5})
        # user B sees only an own (fresh) record, never A's.
        visible_to_b = self.env["ow.mail.preferences"].with_user(
            self.user_b).search([])
        self.assertNotIn(prefs_a.id, visible_to_b.ids)

    def test_wire_shape(self):
        prefs = self.env["ow.mail.preferences"].with_user(
            self.user_a)._get_for_user()
        prefs.write({"mark_read_delay": -1, "thread_view_default": True})
        self.assertEqual(prefs._to_wire(), {
            "mark_read_delay": -1, "thread_view_default": True,
            "stacked_threads": True, "theme": "system",
        })

    def test_theme_default_system(self):
        prefs = self.env["ow.mail.preferences"].with_user(
            self.user_a)._get_for_user()
        self.assertEqual(prefs.theme, "system")

    def test_theme_roundtrip(self):
        prefs = self.env["ow.mail.preferences"].with_user(
            self.user_a)._get_for_user()
        prefs.write({"theme": "dark"})
        self.assertEqual(prefs._to_wire()["theme"], "dark")

    def test_theme_invalid_rejected(self):
        prefs = self.env["ow.mail.preferences"].with_user(
            self.user_a)._get_for_user()
        with self.assertRaises(ValueError):
            prefs.write({"theme": "midnight"})

    def test_stacked_threads_default_on(self):
        prefs = self.env["ow.mail.preferences"].with_user(
            self.user_a)._get_for_user()
        self.assertTrue(prefs.stacked_threads)


@tagged("post_install", "-at_install", "ow_mail")
class TestTagKeywordRename(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = cls.env["res.users"].create({
            "name": "Tagger", "login": "ow_tagger",
            "groups_id": [(4, cls.env.ref("base.group_user").id)],
        })
        cls.Tag = cls.env["ow.mail.tag"].with_user(cls.user)

    def test_rename_regenerates_keyword(self):
        tag = self.Tag.create({"name": "Urgent", "user_id": self.user.id})
        self.assertEqual(tag.imap_keyword, "OwTag_Urgent")
        tag.write({"name": "Vandaag"})
        self.assertEqual(tag.imap_keyword, "OwTag_Vandaag")

    def test_rename_collision_gets_suffix(self):
        self.Tag.create({"name": "PlanB", "user_id": self.user.id})
        other = self.Tag.create({"name": "Soon", "user_id": self.user.id})
        # "Plan B" slugs to "PlanB" — colliding with the first tag's
        # keyword, so the rename must pick a suffixed keyword.
        other.write({"name": "Plan B"})
        self.assertEqual(other.imap_keyword, "OwTag_PlanB-2")

    def test_manual_keyword_untouched_by_rename(self):
        tag = self.Tag.create({
            "name": "Custom", "user_id": self.user.id,
            "imap_keyword": "MyOwnFlag",
        })
        tag.write({"name": "Renamed"})
        self.assertEqual(tag.imap_keyword, "MyOwnFlag")

    def test_unlink_without_confirmed_accounts(self):
        tag = self.Tag.create({"name": "Weg", "user_id": self.user.id})
        tag.unlink()
        self.assertFalse(tag.exists())


@tagged("post_install", "-at_install", "ow_mail")
class TestThemePrefsRoute(HttpCase):
    """The prefs_save whitelist must validate the theme value."""

    def _save(self, vals):
        self.authenticate("admin", "admin")
        response = self.url_open(
            "/ow_mail/prefs/save",
            data=json.dumps({"jsonrpc": "2.0", "method": "call",
                             "params": {"vals": vals}}),
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        return response.json()["result"]

    def test_valid_theme_accepted(self):
        res = self._save({"theme": "dark"})
        self.assertTrue(res.get("ok"))
        self.assertEqual(res["prefs"]["theme"], "dark")

    def test_invalid_theme_rejected(self):
        res = self._save({"theme": "midnight"})
        self.assertTrue(res.get("error"))
