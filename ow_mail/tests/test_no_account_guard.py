# Part of ow_mail.
"""Saving/sending with no account must return a clean error, not a 500 TypeError."""
import json
from odoo.tests import HttpCase, tagged


@tagged("post_install", "-at_install", "ow_mail")
class TestNoAccountGuard(HttpCase):
    def _call(self, route, params):
        self.authenticate("admin", "admin")
        resp = self.url_open(
            route,
            data=json.dumps({"jsonrpc": "2.0", "method": "call", "params": params}),
            headers={"Content-Type": "application/json"},
        )
        return resp

    def test_save_draft_without_account_is_clean_error(self):
        # No account_id at all (user without a connected account).
        resp = self._call("/ow_mail/draft", {"to": "x@y.z", "subject": "s"})
        self.assertEqual(resp.status_code, 200,
                         "a missing account must not 500")
        payload = resp.json()
        self.assertNotIn("error", payload,
                         "must not be a JSON-RPC server error (TypeError)")
        self.assertFalse(payload["result"].get("ok"),
                         "the call must report failure cleanly")

    def test_send_without_account_is_clean_error(self):
        resp = self._call("/ow_mail/send", {"to": "x@y.z", "subject": "s"})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("error", resp.json())
