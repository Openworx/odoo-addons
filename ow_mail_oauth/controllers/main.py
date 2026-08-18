"""Per-user OAuth callback routes for OW Mail accounts.

The standard ``/google_gmail/confirm`` and ``/microsoft_outlook/confirm``
routes are hard-restricted to ``base.group_system`` (mail servers are admin
infrastructure). An OW Mail account belongs to one regular user, so these
routes accept any authenticated user and enforce, in order:

1. the account exists and is owned by the requesting user;
2. the ``state`` carries a valid HMAC (database secret) binding the
   account id AND the owner id (see ``_ow_oauth_csrf_token``);
3. only then is the code exchanged and written — via ``sudo()``, because
   the token fields remain ``base.group_system`` at the ORM level.
"""
import json
import logging

from werkzeug.exceptions import Forbidden

from odoo import http
from odoo.exceptions import UserError
from odoo.http import request
from odoo.tools import consteq

_logger = logging.getLogger(__name__)


class OwMailOauthController(http.Controller):

    @http.route("/ow_mail_oauth/gmail/confirm", type="http", auth="user")
    def gmail_confirm(self, code=None, state=None, error=None, **kwargs):
        return self._confirm("gmail", code, state, error)

    @http.route("/ow_mail_oauth/outlook/confirm", type="http", auth="user")
    def outlook_confirm(self, code=None, state=None,
                        error_description=None, **kwargs):
        return self._confirm("outlook", code, state, error_description)

    def _confirm(self, provider, code, state, error):
        if error:
            _logger.info("ow_mail_oauth: provider returned an error: %s",
                         str(error)[:200])
            return request.redirect(
                "/odoo/action-ow_mail.action_ow_mail_account")

        try:
            state = json.loads(state)
            account_id = int(state["account_id"])
            csrf_token = state["csrf_token"]
        except Exception:
            _logger.warning("ow_mail_oauth: malformed state %r", state)
            raise Forbidden()

        # ir.rule already scopes browse+read to the owner; the explicit
        # user check is defense-in-depth, mirroring ow_mail's controllers.
        account = request.env["ow.mail.account"].browse(account_id).exists()
        if not account or account.user_id.id != request.env.user.id:
            raise Forbidden()
        if account.auth_type != provider:
            raise Forbidden()
        if not csrf_token or not consteq(
                csrf_token, account._ow_oauth_csrf_token()):
            _logger.warning(
                "ow_mail_oauth: wrong CSRF token for account %s", account_id)
            raise Forbidden()

        account_sudo = account.sudo()
        try:
            account_sudo._ow_oauth_exchange_code(code)
        except UserError as e:
            # Surface the failure on the account form (error_message field).
            account_sudo.write({"state": "error", "error_message": str(e)})
            return request.redirect(f"/odoo/ow.mail.account/{account.id}")

        # Best effort: validate the fresh token and load the folder tree so
        # the user lands in a working client. Failures leave the account in
        # 'error' state with the message visible on the form.
        try:
            account_sudo.action_test_connection()
            account_sudo.action_sync()
        except Exception as e:
            _logger.warning(
                "ow_mail_oauth: post-connect check failed for %s: %s",
                account_sudo.email, e)
            return request.redirect(f"/odoo/ow.mail.account/{account.id}")

        return request.redirect(
            "/odoo/action-ow_mail.action_ow_mail_mailclient")
