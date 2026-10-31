import logging
import time
from collections import deque
from datetime import timedelta

from odoo import SUPERUSER_ID, api, fields, models
from odoo.exceptions import AccessDenied
from odoo.sql_db import db_connect

from ..core import iputil, recovery

_logger = logging.getLogger(__name__)

# caches populated by request data need a hard bound: distributed
# brute-force from rotating sources must not grow process memory.
# Fallback-only map, so a coarse bound with a wholesale clear is fine.
LOGIN_FAIL_MAX_KEYS = 4096


class ResUsers(models.Model):
    _inherit = "res.users"

    def _waf_login_ip(self):
        """Trust-aware client IP for the authentication attempt."""
        cfg = self.env["waf.config"].sudo().get_singleton()
        ip = self.env.context.get("ip")
        try:
            from odoo.http import request
            environ = request.httprequest.environ
            return iputil.resolve_client_ip(
                environ.get("REMOTE_ADDR"), environ.get("HTTP_X_REAL_IP"),
                environ.get("HTTP_X_FORWARDED_FOR"),
                iputil.parse_cidrs(cfg.trusted_proxies))
        except Exception:
            return ip

    def _waf_login_failures(self):
        reg = self.env.registry
        if not hasattr(reg, "_waf_login_failures"):
            reg._waf_login_failures = {}
        return reg._waf_login_failures

    def authenticate(self, credential, user_agent_env):
        """Odoo 19 signature: authenticate(credential, user_agent_env).

        Logins run as the public user, which has no waf access rights:
        every config/entry touch below must be sudo() or a fresh SUPERUSER
        cursor, otherwise failed/successful logins would 403 on our ACLs.
        """
        cfg = self.env["waf.config"].sudo().get_singleton()
        # Off: no counting. Learning: count and log only -- a lockout is an
        # IP deny entry, and those block in every mode. Enforce: full lockout.
        track = cfg.login_fail_enabled and cfg.mode != "off"
        ip = self._waf_login_ip() if cfg.login_fail_enabled else None
        try:
            auth_info = super().authenticate(credential, user_agent_env)
        except AccessDenied:
            # count actual authentication failures only: core raises
            # AccessDenied for a wrong login/password; a DB outage or an
            # unrelated bug inside authenticate must not burn a visitor's
            # login budget nor auto-block them for the server's problems.
            if track and ip:
                self._waf_record_login_failure(cfg, ip)
            raise
        if cfg.login_fail_enabled and ip:
            # a success ends the window in every mode: learning-phase
            # worker-local counts and the note throttle must not survive
            # a good login either
            self._waf_login_failures().pop(ip, None)
            self._waf_login_noted().pop(ip, None)
            self._waf_clear_login_hits(ip)
        return auth_info

    def _waf_record_login_failure(self, cfg, ip):
        """Credit one failed login to *ip*; lock out at the threshold.

        Credit lives in waf_login_hit: one shared row per network, so
        every worker credits the same total (bump mechanics:
        core/middleware._bump_lockout). The deque is the DB-failure
        fallback only. Learning never touches the shared table; it
        credits the deque and logs one note per IP per window.
        """
        from ..core import middleware  # deferred: models <-> core cycle
        # per-registry marker: the mode the login deque credit was earned
        # in (same pattern as middleware._credit_mode). Deque credit from
        # another mode must not leak in: after a Learning -> Enforce flip,
        # a DB hiccup falling back to the deque would lock out on failures
        # that were only ever meant to be noted. Wipe the deque and the
        # note throttle on a mode flip; mode flips are rare admin actions.
        reg = self.env.registry
        if getattr(reg, "_waf_login_credit_mode", None) != cfg.mode:
            reg._waf_login_credit_mode = cfg.mode
            self._waf_login_failures().clear()
            self._waf_login_noted().clear()
        window = (cfg.login_fail_window or 30) * 60
        limit = cfg.login_fail_max or 10
        if cfg.mode == "learning":
            self._waf_note_login_learning(cfg, ip, window, limit)
            return
        network = ip + ("/128" if ":" in ip else "/32")
        shared = middleware._bump_lockout(self.env.cr.dbname, network,
                                          window, table="waf_login_hit")
        if shared is not None:
            # shared truth wins: drop this worker's stale deque so it can
            # never contradict the shared count on a later DB hiccup
            self._waf_login_failures().pop(ip, None)
            if shared < limit:
                return
            self._waf_apply_login_lockout(cfg, ip, network, shared)
            return
        hits = self._waf_credit_login_deque(ip, window)
        if hits < limit:
            return
        self._waf_apply_login_lockout(cfg, ip, network, hits)

    def _waf_credit_login_deque(self, ip, window):
        """One failed login into this worker's deque; returns the count.

        Pruned to the window and hard-bounded: rotating brute-force
        sources must not grow process memory.
        """
        failures = self._waf_login_failures()
        now = time.time()
        dq = failures.setdefault(ip, deque())
        dq.append(now)
        while dq and now - dq[0] > window:
            dq.popleft()
        if len(failures) > LOGIN_FAIL_MAX_KEYS:
            cutoff = now - window
            for key in [k for k, v in failures.items()
                        if not v or v[-1] < cutoff]:
                del failures[key]
            if len(failures) > LOGIN_FAIL_MAX_KEYS:
                failures.clear()
        return len(dq)

    def _waf_login_noted(self):
        """ip -> monotonic ts of the last learning-mode note (per
        registry; bounded below in _waf_note_login_learning)."""
        reg = self.env.registry
        if not hasattr(reg, "_waf_login_noted"):
            reg._waf_login_noted = {}
        return reg._waf_login_noted

    def _waf_note_login_learning(self, cfg, ip, window, limit):
        """Learning-phase login credit: deque count + ONE log-only note.

        The shared table is enforce machinery and must stay empty in
        Learning, so the count stays worker-local. When the local count
        crosses the configured maximum, ONE waf.hit per IP per window
        records the would-be lockout: a brute-force run must not flood
        the traffic log.
        """
        from ..core import middleware  # deferred: models <-> core cycle
        count = self._waf_credit_login_deque(ip, window)
        if count < limit:
            return
        noted = self._waf_login_noted()
        now = time.time()
        if now - noted.get(ip, 0.0) < window:
            return  # already noted this window: log-flood protection
        noted[ip] = now
        if len(noted) > LOGIN_FAIL_MAX_KEYS:
            noted.clear()  # coarse bound, same shape as the deque
        path = "/web/session/authenticate"
        try:
            from odoo.http import request
            path = (request.httprequest.path or path)[:512]
        except Exception:
            pass  # no live web request (xmlrpc, shell): keep the label
        snap = middleware._get_snapshot(self.env.cr.dbname)
        if not isinstance(snap, dict):
            snap = {"mode": "learning"}  # note must not fail on a snap hiccup
        middleware._buffer_hit(self.env.cr.dbname, {
            "ip": ip, "method": "POST", "path": path, "query": "",
            "ua": "",
        }, {
            "kind": "ip", "action": "log", "severity": 3,
            "rule_id": None,
            "rule_name": "login brute-force: would lock out "
                         "(%d failures in %d min)"
                         % (count, cfg.login_fail_window or 30),
            "category": "login", "blocked": False,
        }, snap, None)

    def _waf_apply_login_lockout(self, cfg, ip, network, hits):
        # Write on a *separate pooled connection*: failed logins roll back the
        # request transaction (JSON-RPC/xmlrpc dispatch re-raise), which would
        # also undo a lockout row created on it. In test mode this also
        # bypasses the registry's shared TestCursor, so the row really commits.
        with db_connect(self.env.cr.dbname).cursor() as cr:
            cr.rollback()  # fresh txn: pool may hold a stale RR transaction
            env = api.Environment(cr, SUPERUSER_ID, {})
            # the window that produced this block is over: a counter row
            # parked at >= max would re-block the network the moment its
            # entry is lifted or expires
            cr.execute("DELETE FROM waf_login_hit WHERE network = %s",
                       (network,))
            # do not stack a second entry if one is already in force, and
            # "in force" is expiry-aware: an expired entry the hourly
            # sweeper has not deactivated yet must not mask a fresh lockout
            if env["waf.ip.entry"].search_count([
                    ("network", "=", network), ("kind", "=", "deny"),
                    ("active", "=", True),
                    "|", ("expires", "=", False),
                    ("expires", ">", fields.Datetime.now())]):
                cr.commit()
                return
            env["waf.ip.entry"].create({
                "network": network,
                "kind": "deny",
                "source": "auto",
                "reason": "login brute-force: %d failures in %d min" % (
                    hits, cfg.login_fail_window),
                "expires": fields.Datetime.now() + timedelta(
                    minutes=cfg.login_fail_minutes or 60),
            })
            # distributed brute-forcers would otherwise mail the admin one
            # alert per source IP: the WAF layer's per-database budget
            # bounds the total (the block itself is always applied)
            from ..core import middleware
            if not middleware._alert_budget_ok(cr.dbname):
                cr.commit()
                return
            base_url = env["ir.config_parameter"].sudo().get_param(
                "web.base.url", "")
            token = recovery.make_token(cfg.recover_secret, cr.dbname, network)
            env["waf.config"].get_singleton()._notify_admins(
                subject="OW Shield: login brute-force lockout (%s)" % ip,
                body=(
                    "<p>The IP <b>%s</b> was blocked after %d failed login "
                    "attempts against %s.</p>"
                    "<p>If this was you, use this recovery link (valid 15 "
                    "minutes, so act promptly): "
                    "<a href=\"%s/waf/recover/%s\">%s/waf/recover/%s</a></p>"
                    % (ip, hits, base_url or "this instance",
                       base_url, token, base_url, token)
                ),
            )
            cr.commit()
        _logger.warning("WAF: login brute-force lockout applied to %s", network)

    def _waf_clear_login_hits(self, ip):
        """A successful login ends the shared counter window for this IP."""
        network = ip + ("/128" if ":" in ip else "/32")
        try:
            with db_connect(self.env.cr.dbname).cursor() as cr:
                cr.rollback()
                cr.execute("DELETE FROM waf_login_hit WHERE network = %s",
                           (network,))
                cr.commit()
        except Exception:
            pass  # rows age out on their own; nothing here may break a
                  # successful login
