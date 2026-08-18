"""OAuth2 (XOAUTH2) authentication for ow.mail.account.

Token storage, refresh and the SASL string come from Odoo's standard
``google.gmail.mixin`` / ``microsoft.outlook.mixin``. What this module adds
on top is a *per-user* connect flow: the standard authorize URL and
``/google_gmail/confirm`` / ``/microsoft_outlook/confirm`` callbacks are
hard-restricted to ``base.group_system`` (mail servers are admin
infrastructure), while an OW Mail account belongs to one regular user. We
therefore build our own authorize URL and exchange the authorization code
on our own callback route (see ``controllers/main.py``), with ownership +
CSRF checks, and only then hand over to the mixins for refresh and SASL.

Note on the token *refresh* path: the mixins send their own (standard)
``redirect_uri`` in the refresh request; both Google and Microsoft ignore
that parameter for the ``refresh_token`` grant, so refresh keeps working
for codes that were exchanged against our callback URL.
"""
import json
import logging
import time

import requests
from werkzeug.urls import url_encode, url_join

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import hmac as hmac_tool

_logger = logging.getLogger(__name__)

_TOKEN_TIMEOUT = 10  # seconds; mirrors the standard mixins


class OwMailAccount(models.Model):
    _name = "ow.mail.account"
    _inherit = ["ow.mail.account", "google.gmail.mixin",
                "microsoft.outlook.mixin"]

    # One token for both protocols — the fetchmail/ir.mail_server subclasses
    # of the mixin each request a single scope; an OW Mail account reads AND
    # sends, so it needs both. The mixin prefixes ``offline_access`` itself.
    _OUTLOOK_SCOPE = ("https://outlook.office.com/IMAP.AccessAsUser.All "
                      "https://outlook.office.com/SMTP.Send")

    auth_type = fields.Selection(
        [("password", "Password"),
         ("gmail", "Gmail (OAuth)"),
         ("outlook", "Microsoft 365 (OAuth)")],
        string="Authentication", default="password", required=True)
    oauth_connected = fields.Boolean(
        compute="_compute_oauth_connected", string="OAuth Connected")

    @api.depends("auth_type", "google_gmail_refresh_token",
                 "microsoft_outlook_refresh_token")
    def _compute_oauth_connected(self):
        for rec in self:
            # Token fields are base.group_system — read via sudo; the record
            # itself is already scoped to the owner by the ir.rule.
            rec_sudo = rec.sudo()
            rec.oauth_connected = bool(
                (rec.auth_type == "gmail"
                 and rec_sudo.google_gmail_refresh_token)
                or (rec.auth_type == "outlook"
                    and rec_sudo.microsoft_outlook_refresh_token))

    # ------------------------------------------------------------------
    # Authentication hooks (see ow_mail's _imap_connect/_smtp_connect)
    # ------------------------------------------------------------------

    def _imap_authenticate(self, conn):
        self.ensure_one()
        if self.auth_type in ("gmail", "outlook"):
            auth_string = self._ow_oauth_sasl_string()
            conn.authenticate("XOAUTH2", lambda challenge: auth_string.encode())
            return
        return super()._imap_authenticate(conn)

    def _smtp_authenticate(self, conn):
        self.ensure_one()
        if self.auth_type in ("gmail", "outlook"):
            auth_string = self._ow_oauth_sasl_string()
            conn.ehlo_or_helo_if_needed()
            conn.auth("XOAUTH2", lambda challenge=None: auth_string,
                      initial_response_ok=True)
            return
        return super()._smtp_authenticate(conn)

    def _ow_oauth_sasl_string(self):
        """Return the XOAUTH2 SASL argument, refreshing the token if needed.

        Delegates to the mixin generators via ``sudo()`` — the token fields
        are ``base.group_system`` while the caller is the account owner
        (controller routes assert ownership before ever reaching this).
        A missing refresh token raises a clear "connect first" error.
        """
        self.ensure_one()
        rec = self.sudo()
        login = self.imap_login or self.email
        if self.auth_type == "gmail":
            if not rec.google_gmail_refresh_token:
                raise UserError(_(
                    "This account is not connected to Google yet. "
                    "Open the account and use 'Connect' first."))
            return rec._generate_oauth2_string(
                login, rec.google_gmail_refresh_token)
        return rec._generate_outlook_oauth2_string(login)

    # ------------------------------------------------------------------
    # Per-user connect flow
    # ------------------------------------------------------------------

    def _ow_oauth_csrf_token(self):
        """State token binding this account AND its owner, HMAC-signed with
        the database secret. Verified in the callback controller."""
        self.ensure_one()
        return hmac_tool(
            self.env(su=True), "ow_mail_oauth",
            ("ow.mail.account", self.id, self.user_id.id))

    def _ow_oauth_redirect_uri(self):
        self.ensure_one()
        path = ("/ow_mail_oauth/gmail/confirm" if self.auth_type == "gmail"
                else "/ow_mail_oauth/outlook/confirm")
        return url_join(self.get_base_url(), path)

    def _ow_oauth_authorize_uri(self):
        """Build the provider authorization URL with OUR callback route.

        Mirrors the mixins' ``_compute_*_uri`` but with a per-user state
        (account id + owner-bound CSRF) and the bridge redirect URI.
        """
        self.ensure_one()
        Config = self.env["ir.config_parameter"].sudo()
        state = json.dumps({
            "account_id": self.id,
            "csrf_token": self._ow_oauth_csrf_token(),
        })
        if self.auth_type == "gmail":
            client_id = Config.get_param("google_gmail_client_id")
            if not client_id or not Config.get_param(
                    "google_gmail_client_secret"):
                raise UserError(_(
                    "Gmail OAuth is not configured. Ask your administrator "
                    "to set the Gmail credentials in the general settings."))
            return "https://accounts.google.com/o/oauth2/v2/auth?%s" % url_encode({
                "client_id": client_id,
                "redirect_uri": self._ow_oauth_redirect_uri(),
                "response_type": "code",
                "scope": self._SERVICE_SCOPE,
                # access_type and prompt are needed to get a refresh token
                "access_type": "offline",
                "prompt": "consent",
                "login_hint": self.email or "",
                "state": state,
            })
        if self.auth_type == "outlook":
            client_id = Config.get_param("microsoft_outlook_client_id")
            if not client_id or not Config.get_param(
                    "microsoft_outlook_client_secret"):
                raise UserError(_(
                    "Microsoft 365 OAuth is not configured. Ask your "
                    "administrator to set the Outlook credentials in the "
                    "general settings."))
            return url_join(
                self._get_microsoft_endpoint(), "authorize?%s" % url_encode({
                    "client_id": client_id,
                    "response_type": "code",
                    "redirect_uri": self._ow_oauth_redirect_uri(),
                    "response_mode": "query",
                    "scope": "offline_access %s" % self._OUTLOOK_SCOPE,
                    "login_hint": self.email or "",
                    "state": state,
                }))
        raise UserError(_("Select a Gmail or Microsoft 365 authentication "
                          "type first."))

    def action_connect_oauth(self):
        """Form button: send the browser to the provider's consent screen.

        Owner-only (or admin): the callback later re-verifies ownership and
        the signed state, so this check is UX, not the security boundary.
        """
        self.ensure_one()
        if (self.user_id.id != self.env.user.id
                and not self.env.user.has_group("base.group_system")):
            raise AccessError(_(
                "Only the account owner can connect this mailbox."))
        return {
            "type": "ir.actions.act_url",
            "url": self._ow_oauth_authorize_uri(),
            "target": "self",
        }

    def _ow_oauth_exchange_code(self, code):
        """Exchange the authorization code for tokens and store them.

        Runs sudo'ed from the callback controller (after ownership + CSRF
        checks). Not delegated to the mixins because their token request
        hardcodes the admin-only redirect URI, which must match the one
        used in the authorization request.
        """
        self.ensure_one()
        Config = self.env["ir.config_parameter"].sudo()
        if self.auth_type == "gmail":
            response = requests.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": Config.get_param("google_gmail_client_id"),
                    "client_secret": Config.get_param(
                        "google_gmail_client_secret"),
                    "grant_type": "authorization_code",
                    "redirect_uri": self._ow_oauth_redirect_uri(),
                    "code": code,
                },
                timeout=_TOKEN_TIMEOUT,
            )
            payload = self._ow_oauth_check_token_response(response)
            vals = {
                "google_gmail_refresh_token": payload["refresh_token"],
                "google_gmail_access_token": payload["access_token"],
                "google_gmail_access_token_expiration":
                    int(time.time()) + int(payload["expires_in"]),
            }
            # Odoo 18's mixin also tracks the raw authorization code; the
            # field was dropped in Odoo 19 — write it only when it exists.
            if "google_gmail_authorization_code" in self._fields:
                vals["google_gmail_authorization_code"] = code
            self.write(vals)
        elif self.auth_type == "outlook":
            response = requests.post(
                url_join(self._get_microsoft_endpoint(), "token"),
                data={
                    "client_id": Config.get_param(
                        "microsoft_outlook_client_id"),
                    "client_secret": Config.get_param(
                        "microsoft_outlook_client_secret"),
                    "scope": "offline_access %s" % self._OUTLOOK_SCOPE,
                    "redirect_uri": self._ow_oauth_redirect_uri(),
                    "grant_type": "authorization_code",
                    "code": code,
                },
                timeout=_TOKEN_TIMEOUT,
            )
            payload = self._ow_oauth_check_token_response(response)
            self.write({
                "microsoft_outlook_refresh_token": payload["refresh_token"],
                "microsoft_outlook_access_token": payload["access_token"],
                "microsoft_outlook_access_token_expiration":
                    int(time.time()) + int(payload["expires_in"]),
            })
        else:
            raise UserError(_("This account does not use OAuth."))
        return True

    @api.model
    def _ow_oauth_check_token_response(self, response):
        if not response.ok:
            try:
                detail = response.json().get("error_description") \
                    or response.json().get("error") or ""
            except Exception:
                detail = ""
            _logger.warning("ow_mail_oauth: token request failed (%s): %s",
                            response.status_code, detail or response.text[:200])
            raise UserError(_(
                "An error occurred when fetching the OAuth token. %s", detail))
        return response.json()
