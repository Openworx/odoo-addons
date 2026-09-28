# Part of ow_mail.
"""The 5-minute counts cron must only push ow_mail/refresh on a real change."""

from unittest.mock import MagicMock, patch

from odoo.tests import TransactionCase, tagged

_STATUS = "odoo.addons.ow_mail.models.ow_mail_imap.status_counts"


@tagged("post_install", "-at_install", "ow_mail")
class TestRefreshCountsNotify(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account = cls.env["ow.mail.account"].create(
            {
                "name": "Acc",
                "email": "demo@ow.test",
                "user_id": cls.env.ref("base.user_admin").id,
                "imap_host": "greenmail",
                "imap_port": 3143,
                "imap_ssl": False,
                "imap_login": "demo",
                "imap_password": "demo",
                "smtp_host": "greenmail",
                "smtp_port": 3025,
                "smtp_encryption": "none",
                "smtp_same_as_imap": True,
                "notify_new_mail": False,
                "auto_link_replies": False,
            }
        )
        cls.account.state = "confirmed"
        cls.folder = cls.env["ow.mail.folder"].create(
            {
                "account_id": cls.account.id,
                "name": "INBOX",
                "full_path": "INBOX",
                "kind": "inbox",
                "total_count": 5,
                "unread_count": 1,
            }
        )
        cls.account.inbox_folder_id = cls.folder

    def _run_cron_refreshes(self, status_return):
        """Run the counts cron with a mocked IMAP edge; return the list of
        `ow_mail/refresh` bus notifications it pushed.

        We spy on `bus.bus._sendone` (the notify boundary) rather than search
        `bus.bus`: the row is inserted on commit, which is neutralised in tests.
        The IMAP STATUS call is the only external edge that is mocked.
        """
        sent = []
        BusCls = type(self.env["bus.bus"])
        original_sendone = BusCls._sendone

        def _spy(bus_self, target, notification_type, message, *args, **kwargs):
            sent.append(notification_type)
            return original_sendone(
                bus_self, target, notification_type, message, *args, **kwargs
            )

        # The cron commits once per account (so a later failure keeps earlier
        # work). That per-account commit is orthogonal to the notify decision
        # under test, and a real commit mid-test is neutralised by the test
        # cursor -> no-op it so the loop is not interrupted.
        with (
            patch.object(type(self.account), "_imap_connect", return_value=MagicMock()),
            patch(_STATUS, return_value=status_return),
            patch.object(BusCls, "_sendone", _spy),
            patch.object(self.env.cr, "commit", lambda: None),
        ):
            self.env["ow.mail.account"]._cron_refresh_counts()
        return [t for t in sent if t == "ow_mail/refresh"]

    def test_no_refresh_when_counts_unchanged(self):
        """No ow_mail/refresh when the IMAP STATUS matches the stored counts.

        An unconditional refresh every run piles up in bus.bus and is replayed
        in bulk to a tab that reconnects after sleeping, where the client runs
        one bootstrap() per event -> request flood ("Connection lost" loop).
        """
        refresh = self._run_cron_refreshes((5, 1))
        self.assertFalse(
            refresh, "the cron must not push ow_mail/refresh when nothing changed"
        )

    def test_refresh_when_counts_change(self):
        """A real count change must still notify the user."""
        refresh = self._run_cron_refreshes((9, 3))
        self.assertTrue(refresh, "a real count change must still push ow_mail/refresh")
