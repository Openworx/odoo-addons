"""Sender lookups go through the mail app's own partner matcher.

A module that extends ``mail.thread._mail_find_partner_from_emails`` —
several addresses per contact, for instance — must be honoured by the
"Customer" badge and by every ``_ow_find_by_email`` caller, exactly as it is
for incoming mail. The extension is simulated here by wrapping the matcher,
so the tests need no such module installed.
"""
from types import SimpleNamespace
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from ..controllers import main as controller


@tagged("post_install", "-at_install", "ow_mail")
class TestPartnerMatch(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.jane = cls.env["res.partner"].create(
            {"name": "Jane", "email": "jane@acme.test"})

    def _matcher_knows(self, address, partner):
        """Extend the matcher so ``address`` resolves to ``partner``."""
        MailThread = type(self.env["mail.thread"])
        original = MailThread._mail_find_partner_from_emails

        def extended(thread, emails, *args, **kwargs):
            results = original(thread, emails, *args, **kwargs)
            return [partner if email == address and not found else found
                    for email, found in zip(emails, results)]

        return patch.object(MailThread, "_mail_find_partner_from_emails", extended)

    def _annotate(self, *senders):
        msgs = [{"from_email": sender} for sender in senders]
        with patch.object(controller, "request", SimpleNamespace(env=self.env)):
            return controller._annotate_known_partners(msgs)

    def test_find_by_email_uses_the_matcher(self):
        with self._matcher_knows("jane.home@other.test", self.jane):
            found = self.env["res.partner"]._ow_find_by_email("Jane.Home@Other.TEST")
        self.assertEqual(found, self.jane)

    def test_badge_uses_the_matcher(self):
        with self._matcher_knows("jane.home@other.test", self.jane):
            msgs = self._annotate(
                "jane.home@other.test", "JANE@acme.test", "nobody@nowhere.test", "")
        self.assertEqual(
            [m["partner_id"] for m in msgs],
            [self.jane.id, self.jane.id, False, False])

    def test_badge_without_extension(self):
        msgs = self._annotate("jane@acme.test", "jane.home@other.test")
        self.assertEqual([m["partner_id"] for m in msgs], [self.jane.id, False])
