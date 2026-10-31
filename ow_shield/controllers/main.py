import logging

from odoo import SUPERUSER_ID, api, http
from odoo.http import Response, db_filter

from ..core import recovery

_logger = logging.getLogger(__name__)


class WafController(http.Controller):

    _GENERIC_ERROR = "Invalid or expired recovery link."

    def _reject(self, reason, *args):
        """Single opaque 400 for every failure mode: the route is reachable
        even from blocked IPs (that is its purpose), so distinct 400/404/500
        answers would let anyone enumerate databases on the PostgreSQL
        server. The real reason only goes to the log."""
        if args:
            reason = reason % args
        _logger.warning("WAF: recovery link rejected (%s)", reason)
        return Response(self._GENERIC_ERROR, status=400)

    @http.route("/waf/recover/<path:token>", auth="none", csrf=False,
                type="http")
    def recover(self, token, **kwargs):
        """Self-unblock URL emailed on automatic lockouts (signed, 15 min TTL).

        * the token payload names a database: it is validated against the
          host's dbfilter *before* any connect attempt, and every failure
          answers the same 400, so the route cannot probe databases;
        * a valid token deactivates only the *automatic* deny entry it
          names (never a manual block, and it never creates an allow
          entry): the unblocked address returns to normal rule evaluation
          immediately. A replayed token can therefore never become a
          standing WAF bypass: at worst an attacker gets their own
          transient auto-block lifted once, after which the next attack
          re-blocks them and re-alerts the admin.
        """
        parts = (token or "").split("~")
        dbname = parts[0] if parts else ""
        if not dbname or "/" in dbname or "~" in dbname:
            return self._reject("malformed token")

        host = http.request.httprequest.headers.get("Host")
        try:
            allowed = db_filter([dbname], host=host)
        except Exception:
            return self._reject("dbfilter check failed")
        if dbname not in (allowed or []):
            return self._reject("db %s outside dbfilter", dbname)

        try:
            from odoo.sql_db import db_connect
            with db_connect(dbname).cursor() as cr:
                cr.rollback()  # fresh txn: pool may hold a stale RR transaction
                cr.execute(
                    "SELECT recover_secret FROM waf_config ORDER BY id LIMIT 1")
                row = cr.fetchone()
                if not row:
                    return self._reject("no firewall config in db %s", dbname)
                result = recovery.check_token(row[0], token)
                if not result:
                    return self._reject("bad signature or expired (db %s)",
                                        dbname)
                _db, network = result
                env = api.Environment(cr, SUPERUSER_ID, {})
                blocked = env["waf.ip.entry"].search([
                    ("network", "=", network), ("kind", "=", "deny"),
                    ("source", "=", "auto"), ("active", "=", True),
                ])
                if blocked:
                    blocked.write({"active": False})
                # the counter windows that could have produced the block
                # are over too: parked at >= max they would re-block the
                # lifted network on its next flagged hit or failed login,
                # with no second mail (alert suppression): the
                # false-positive trap
                cr.execute("DELETE FROM waf_lockout_hit WHERE network = %s",
                           (network,))
                cr.execute("DELETE FROM waf_login_hit WHERE network = %s",
                           (network,))
                # master-password lockouts are NOT ip entries: the whole
                # block is this row's blocked_until (see core/masterbp).
                # Same recovery contract: the link names (db, network) and
                # lifts only that network's counter. to_regclass guard: a
                # db whose schema predates the feature has no such table.
                cr.execute("SELECT to_regclass('public.waf_master_hit')")
                if cr.fetchone()[0]:
                    cr.execute("DELETE FROM waf_master_hit WHERE network = %s",
                               (network,))
                cr.commit()
        except Exception:
            _logger.exception("WAF: recovery link failure")
            return self._reject("internal error (see server log)")
        return Response(
            "The automatic block has been lifted. Normal firewall rules "
            "apply again. Redirecting to the login page...", status=200,
            headers={"Refresh": "3;url=/web/login?db=%s" % dbname})
