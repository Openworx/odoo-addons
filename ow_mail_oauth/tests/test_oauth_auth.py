"""Unit tests for the OAuth bridge — no network, no real IMAP/SMTP.

The XOAUTH2 handshake itself can only be verified against real providers
(GreenMail speaks no XOAUTH2); these tests pin down the dispatch logic,
the wizard presets, the CSRF/state contract, and the token exchange
bookkeeping with mocked HTTP.
"""
from unittest.mock import MagicMock, patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, tagged

SASL = "user=x@ow.test\1auth=Bearer tok\1\1"


def _account_vals(**over):
    vals = {
        "name": "OAuth Test",
        "email": "x@ow.test",
        "imap_host": "imap.gmail.com", "imap_port": 993, "imap_ssl": True,
        "imap_login": "x@ow.test",
        "smtp_host": "smtp.gmail.com", "smtp_port": 465,
        "smtp_encryption": "ssl",
        "auth_type": "gmail",
    }
    vals.update(over)
    return vals


@tagged("post_install", "-at_install", "ow_mail_oauth")
class TestOauthDispatch(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = cls.env["res.users"].create({
            "name": "OAuth User", "login": "ow_oauth_user",
            "group_ids": [(4, cls.env.ref("base.group_user").id)],
        })
        cls.Account = cls.env["ow.mail.account"].with_user(cls.user)

    def _gmail_account(self):
        acc = self.Account.create(_account_vals(user_id=self.user.id))
        acc.sudo().google_gmail_refresh_token = "rt"
        return acc

    def test_imap_auth_uses_xoauth2_for_gmail(self):
        acc = self._gmail_account()
        conn = MagicMock()
        with patch.object(type(acc), "_generate_oauth2_string",
                          return_value=SASL) as gen:
            acc._imap_authenticate(conn)
        gen.assert_called_once()
        conn.authenticate.assert_called_once()
        self.assertEqual(conn.authenticate.call_args[0][0], "XOAUTH2")
        # de authobject moet bytes leveren (imaplib b64-encodeert die)
        authobject = conn.authenticate.call_args[0][1]
        self.assertEqual(authobject(b""), SASL.encode())
        conn.login.assert_not_called()

    def test_smtp_auth_uses_xoauth2_for_outlook(self):
        acc = self.Account.create(_account_vals(
            user_id=self.user.id, auth_type="outlook",
            imap_host="outlook.office365.com",
            smtp_host="smtp.office365.com", smtp_port=587,
            smtp_encryption="starttls"))
        acc.sudo().microsoft_outlook_refresh_token = "rt"
        conn = MagicMock()
        with patch.object(type(acc), "_generate_outlook_oauth2_string",
                          return_value=SASL):
            acc._smtp_authenticate(conn)
        conn.auth.assert_called_once()
        self.assertEqual(conn.auth.call_args[0][0], "XOAUTH2")
        conn.login.assert_not_called()

    def test_password_account_still_uses_login(self):
        acc = self.Account.create(_account_vals(
            user_id=self.user.id, auth_type="password",
            imap_password="geheim"))
        conn = MagicMock()
        acc._imap_authenticate(conn)
        conn.login.assert_called_once()
        conn.authenticate.assert_not_called()

    def test_gmail_without_refresh_token_raises(self):
        acc = self.Account.create(_account_vals(user_id=self.user.id))
        with self.assertRaises(UserError):
            acc._ow_oauth_sasl_string()

    def test_csrf_token_binds_account_and_owner(self):
        acc = self._gmail_account()
        token = acc._ow_oauth_csrf_token()
        self.assertTrue(token)
        self.assertEqual(token, acc._ow_oauth_csrf_token())
        other = self.Account.create(_account_vals(
            user_id=self.user.id, email="y@ow.test", name="Other"))
        self.assertNotEqual(token, other._ow_oauth_csrf_token())

    def test_connect_action_owner_only(self):
        acc = self._gmail_account()
        stranger = self.env["res.users"].create({
            "name": "Stranger", "login": "ow_oauth_stranger",
            "group_ids": [(4, self.env.ref("base.group_user").id)],
        })
        # de ir.rule verbergt het record voor een vreemde…
        with self.assertRaises(AccessError):
            acc.with_user(stranger).read(["name"])
        # …de eigenaar zelf mag de connect-action starten…
        Config = self.env["ir.config_parameter"].sudo()
        Config.set_param("google_gmail_client_id", "cid")
        Config.set_param("google_gmail_client_secret", "sec")
        action = acc.action_connect_oauth()
        self.assertEqual(action["type"], "ir.actions.act_url")
        self.assertIn("accounts.google.com", action["url"])

    def test_authorize_uri_requires_configuration(self):
        acc = self._gmail_account()
        self.env["ir.config_parameter"].sudo().set_param(
            "google_gmail_client_id", "")
        with self.assertRaises(UserError):
            acc._ow_oauth_authorize_uri()

    def test_authorize_uri_shape(self):
        Config = self.env["ir.config_parameter"].sudo()
        Config.set_param("google_gmail_client_id", "cid")
        Config.set_param("google_gmail_client_secret", "sec")
        acc = self._gmail_account()
        uri = acc._ow_oauth_authorize_uri()
        self.assertIn("accounts.google.com", uri)
        self.assertIn("ow_mail_oauth%2Fgmail%2Fconfirm", uri)
        self.assertIn("prompt=consent", uri)

    def test_exchange_writes_tokens(self):
        Config = self.env["ir.config_parameter"].sudo()
        Config.set_param("google_gmail_client_id", "cid")
        Config.set_param("google_gmail_client_secret", "sec")
        acc = self._gmail_account().sudo()
        response = MagicMock(ok=True)
        response.json.return_value = {
            "refresh_token": "new-rt", "access_token": "new-at",
            "expires_in": 3600,
        }
        with patch("odoo.addons.ow_mail_oauth.models.ow_mail_account"
                   ".requests.post", return_value=response) as post:
            acc._ow_oauth_exchange_code("the-code")
        self.assertEqual(acc.google_gmail_refresh_token, "new-rt")
        self.assertEqual(acc.google_gmail_access_token, "new-at")
        self.assertTrue(acc.google_gmail_access_token_expiration)
        sent = post.call_args[1]["data"]
        self.assertEqual(sent["grant_type"], "authorization_code")
        self.assertIn("/ow_mail_oauth/gmail/confirm", sent["redirect_uri"])

    def test_wizard_preset_creates_oauth_account(self):
        Config = self.env["ir.config_parameter"].sudo()
        Config.set_param("microsoft_outlook_client_id", "cid")
        Config.set_param("microsoft_outlook_client_secret", "sec")
        wiz = self.env["ow.mail.connect.wizard"].with_user(self.user).create({
            "name": "Werk", "email": "x@ow.test", "provider": "outlook",
        })
        action = wiz.action_connect()
        self.assertEqual(action["type"], "ir.actions.act_url")
        self.assertIn("login.microsoftonline.com", action["url"])
        acc = self.Account.search([("email", "=", "x@ow.test")], limit=1)
        self.assertEqual(acc.auth_type, "outlook")
        self.assertEqual(acc.imap_host, "outlook.office365.com")
        self.assertEqual(acc.smtp_encryption, "starttls")

    def test_wizard_other_still_requires_password(self):
        wiz = self.env["ow.mail.connect.wizard"].with_user(self.user).create({
            "name": "Werk", "email": "x@ow.test", "provider": "other",
            "imap_host": "h", "smtp_host": "h",
        })
        with self.assertRaises(UserError):
            wiz.action_connect()


@tagged("post_install", "-at_install", "ow_mail_oauth")
class TestOauthCallback(HttpCase):

    def test_malformed_state_forbidden(self):
        self.authenticate("admin", "admin")
        response = self.url_open(
            "/ow_mail_oauth/gmail/confirm?code=x&state=not-json")
        self.assertEqual(response.status_code, 403)

    def test_wrong_csrf_forbidden(self):
        import json as _json
        from urllib.parse import urlencode
        self.authenticate("admin", "admin")
        acc = self.env["ow.mail.account"].create(_account_vals(
            user_id=self.env.ref("base.user_admin").id))
        query = urlencode({
            "code": "x",
            "state": _json.dumps(
                {"account_id": acc.id, "csrf_token": "wrong"}),
        })
        response = self.url_open(f"/ow_mail_oauth/gmail/confirm?{query}")
        self.assertEqual(response.status_code, 403)
