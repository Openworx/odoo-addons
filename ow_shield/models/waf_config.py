import html
import logging
import secrets
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.sql_db import db_connect
from odoo.tools import config as server_config

from ..core import iputil, masterbp

_logger = logging.getLogger(__name__)

# Core's server-global session parameter, in seconds. This module never writes
# it; the constant only seeds _default_session_idle from an admin's own value.
SESSION_INACTIVITY_PARAM = "sessions.max_inactivity_seconds"

# Minutes added to the eviction cutoff: core refreshes
# res.device.log.last_activity at most once per hour (Session.update_trace
# rewrites only when the stored value is >= 3600s old), so a session's
# recorded activity can lag its real activity by up to an hour.
SESSION_ACTIVITY_SLACK = 60

# Any idle timeout below SESSION_IDLE_FLOOR would fight that hourly refresh:
# it would evict users who are demonstrably still working.
SESSION_IDLE_FLOOR = 60

# Bounds for body_scan_max (4096 .. 1MiB). At or below
# middleware._JSON_CHUNK_OVERLAP the decoded-JSON chunker emits one scan
# candidate per character, so the minimum keeps its step sane; above the
# maximum every request buffers that much memory. engine.build_snapshot
# clamps stale values to this same window; mirror both pairs together.
BODY_SCAN_MIN = 4096
BODY_SCAN_MAX = 1024 * 1024


class WafConfig(models.Model):
    _name = "waf.config"
    _description = "OW Shield Configuration"

    def _default_secret(self):
        return secrets.token_hex(32)

    def _default_trusted_proxies(self):
        return "127.0.0.0/8, ::1"

    def _default_session_idle(self):
        # reflect an externally pre-set core parameter (minutes, 0 = off);
        # a value below the floor is ignored (stays disabled), since passing
        # it through would fail the config create against that floor
        raw = self.env["ir.config_parameter"].sudo().get_param(
            SESSION_INACTIVITY_PARAM)
        try:
            seconds = int(raw)
        except (TypeError, ValueError):
            return 0
        return seconds // 60 if seconds >= SESSION_IDLE_FLOOR * 60 else 0

    mode = fields.Selection(
        [("off", "Off"), ("learning", "Learning (log only)"), ("enforce", "Enforce")],
        default="learning", required=True,
        help="Learning: all rules run and are logged but only manually blocked IPs "
             "are denied. Enforce: matching requests are blocked. Start in learning "
             "mode for a few days, review the traffic log, then switch to enforce.",
    )
    trusted_proxies = fields.Text(
        default=_default_trusted_proxies,
        help="CIDRs of proxies allowed to speak for a client (X-Real-IP / "
             "X-Forwarded-For are only honored when the socket peer matches). "
             "Keep this as narrow as possible. With nginx on the same host the "
             "default (localhost) is correct.",
    )
    country_mode = fields.Selection(
        [("off", "Disabled"), ("allow", "Allow listed countries only"),
         ("deny", "Block listed countries")], default="off",
        help="Requires a MaxMind GeoLite2 Country database configured via the "
             "odoo server option geoip_country_db.")
    country_ids = fields.Many2many(
        "res.country", "waf_config_country_ids", "waf_config_id", "res_country_id",
        string="Countries")
    body_scan_max = fields.Integer(
        default=65536,
        help="Only the first N bytes of a request body are inspected inline. "
             "Larger uploads are covered afterwards by the scanner's attachment "
             "check instead. Allowed range: 4096 to 1048576 bytes: below the "
             "minimum, scanning decoded JSON degrades into one signature pass "
             "per character; above the maximum, every request buffers that "
             "many bytes in memory.")

    auto_lockout_enabled = fields.Boolean(
        default=True, string="Automatic IP Lockout",
        help="Automatically block an IP after too many matching (severity-weighted)"
             " firewall hits.")
    auto_lockout_max = fields.Integer(
        default=10, string="Lockout after (hits)")
    auto_lockout_window = fields.Integer(
        default=10, string="Hit window (minutes)")
    auto_lockout_minutes = fields.Integer(
        default=60, string="Lockout duration (minutes)")

    login_fail_enabled = fields.Boolean(
        default=True, string="Lock Out After Login Failures")
    login_fail_max = fields.Integer(default=10, string="Max login failures per IP")
    login_fail_window = fields.Integer(default=30, string="Window (minutes)")
    login_fail_minutes = fields.Integer(default=60, string="Lockout (minutes)")

    # ------------------------------------------------ database master password
    # A third layer, separate from the two above and from the endpoint-policy
    # layer (the settings form explains the difference to admins): it counts
    # confirmed failed master-password checks only, in one counter shared by
    # every worker and protocol of the instance. See core/masterbp.py.
    master_bp_enabled = fields.Boolean(
        default=True, string="Master-Password Brute Force",
        help="Count confirmed failed database master-password checks and "
             "temporarily refuse the database-management surface to the "
             "source past the threshold. Requires the module installed "
             "(with this version) in at least one database of the "
             "instance; the counters then live in that database for the "
             "WHOLE instance -- switching database or session never "
             "resets the budget.")
    master_bp_max = fields.Integer(
        default=5, string="Failures before lockout",
        help="The Nth failure in the window starts the lockout; later "
             "db-management requests from that source are refused "
             "BEFORE any password check runs -- a correct guess during "
             "the lockout is refused too.")
    master_bp_window = fields.Integer(
        default=10, string="Failure window (minutes)")
    master_bp_lockout_minutes = fields.Integer(
        default=30, string="DB-management lockout (minutes)",
        help="Scoped to database management (create/duplicate/drop/"
             "backup/restore/change-password routes and the db XML-RPC/"
             "JSON-RPC service). Ordinary website and backend access is "
             "never denied by this feature.")

    retention_days = fields.Integer(
        default=30, help="Traffic log entries and expired auto blocks older than "
                         "this are removed by a scheduled action.")

    session_idle_minutes = fields.Integer(
        string="Idle Session Timeout (minutes)", default=_default_session_idle,
        help="0 = disabled, otherwise at least 60. When set, sessions whose "
             "most recent recorded activity is older than this are force-"
             "logged-out by a scheduled action (every 15 minutes), this "
             "database only. NB: Odoo records a session's activity at most "
             "once per hour, so the actual logout lands between T and T+60 "
             "idle minutes, and values below an hour are refused: they would "
             "log out actively working users. This setting does NOT touch "
             "Odoo's server-global 'sessions.max_inactivity_seconds' "
             "(session cookie lifetime) -- that parameter is shared by every "
             "database on the server and stays under your own control.")
    alert_email = fields.Char(
        help="Address notified on automatic lockouts (falls back to all internal "
             "administrators when empty).")

    scan_files = fields.Boolean(
        default=True, string="Scan Add-ons Files",
        help="The security scan walks non-core add-on directories for "
             "webshell/obfuscation signatures.")
    scan_attachments = fields.Boolean(
        default=True, string="Scan Attachments",
        help="The security scan inspects uploaded attachments for suspicious "
             "content and dangerous extensions.")

    recover_secret = fields.Char(
        default=_default_secret, copy=False,
        help="Signs self-unblock ('recovery') links. Rotate to invalidate all "
             "previously issued links.")

    rule_count = fields.Integer(compute="_compute_counts")
    block_count = fields.Integer(compute="_compute_counts")
    hits_24h = fields.Integer(compute="_compute_counts")
    blocked_24h = fields.Integer(compute="_compute_counts")
    live_sessions = fields.Integer(compute="_compute_counts")

    # ------------------------------------------------------------- singleton

    @api.model
    def get_singleton(self):
        rec = self.search([], limit=1, order="id")
        if not rec:
            rec = self.create({})
        return rec

    @api.model_create_multi
    def create(self, vals_list):
        # hard singleton guard: the form action must never sneak in a second
        # config row (the engine only reads the lowest id)
        if self.search_count([]):
            raise UserError(_(
                "Only one firewall configuration record can exist; it was "
                "already created at install."))
        return super().create(vals_list)

    @api.constrains("session_idle_minutes")
    def _check_session_idle_minutes(self):
        for rec in self:
            minutes = rec.session_idle_minutes or 0
            if 0 < minutes < SESSION_IDLE_FLOOR:
                raise ValidationError(_(
                    "The idle session timeout must be 0 (disabled) or at "
                    "least %(floor)d minutes: Odoo only records a session's "
                    "activity once per hour, so anything shorter would log "
                    "out users who are still working.",
                    floor=SESSION_IDLE_FLOOR))

    @api.constrains("master_bp_max", "master_bp_window",
                    "master_bp_lockout_minutes")
    def _check_master_bp_values(self):
        for rec in self:
            for fname in ("master_bp_max", "master_bp_window",
                          "master_bp_lockout_minutes"):
                if (rec[fname] or 0) < 1:
                    raise ValidationError(_(
                        "Master-password protection settings must be "
                        "positive integers (%s)." % fname))

    @api.constrains("body_scan_max")
    def _check_body_scan_max(self):
        # a window at or below the JSON chunk overlap makes _json_chunks emit
        # one candidate per character; 0 or a negative value is no window at
        # all, and an absurd one makes every request buffer that many bytes
        for rec in self:
            if not BODY_SCAN_MIN <= (rec.body_scan_max or 0) <= BODY_SCAN_MAX:
                raise ValidationError(_(
                    "The body scan window must be between %(min)d and "
                    "%(max)d bytes: below the minimum, scanning decoded "
                    "JSON degrades into one signature pass per character; "
                    "above the maximum, every request buffers that many "
                    "bytes in memory.",
                    min=BODY_SCAN_MIN, max=BODY_SCAN_MAX))

    def init(self):
        """Safety net for the single row (installed via post-init hook too).

        Re-asserts one row, then puts a unique index on a constant expression:
        a second config row can never be committed again.
        """
        super().init()
        extra = self.search([], order="id")[1:]
        if extra:
            _logger.warning("waf.config: removing %d duplicate config row(s)",
                            len(extra))
            extra.unlink()
        if not self.search_count([]):
            self.create({})
        self.env.cr.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS waf_config_singleton "
            "ON waf_config ((true))")

    # ------------------------------------------------------------- statistics

    def _compute_display_name(self):
        """Singleton settings record: show a friendly label everywhere
        (breadcrumbs, chatter) instead of 'waf.config(1)'."""
        for rec in self:
            rec.display_name = _("Firewall Configuration")

    @api.depends()
    def _compute_counts(self):
        now = fields.Datetime.now()
        cutoff = now - timedelta(hours=24)
        active_net = ["&", ("active", "=", True),
                      "|", ("expires", "=", False), ("expires", ">", now)]
        for rec in self:
            rec.rule_count = self.env["waf.rule"].search_count([("active", "=", True)])
            rec.block_count = self.env["waf.ip.entry"].search_count(
                active_net + [("kind", "=", "deny")])
            rec.hits_24h = self.env["waf.hit"].search_count([("create_date", ">=", cutoff)])
            rec.blocked_24h = self.env["waf.hit"].search_count(
                [("create_date", ">=", cutoff), ("blocked", "=", True)])
            rec.live_sessions = self.env["res.device"].search_count([])

    # ------------------------------------------------------------- dashboard

    @api.model
    def get_dashboard_data(self, days=1):
        """Aggregated figures for the OWL dashboard (manager rights)."""
        try:
            days = int(days)
        except (TypeError, ValueError):
            days = 1
        if days not in (1, 7, 30):
            days = 1
        cfg = self.get_singleton()
        now = fields.Datetime.now()
        day = [("create_date", ">=", now - timedelta(hours=24))]
        since = now - timedelta(days=days)
        active_net = ["&", ("active", "=", True),
                      "|", ("expires", "=", False), ("expires", ">", now)]
        Hit, Rule = self.env["waf.hit"], self.env["waf.rule"]
        Entry = self.env["waf.ip.entry"]

        def top(expr, params_limit=8):
            self.env.cr.execute(
                "SELECT %s AS label, COUNT(*) FROM waf_hit "
                "WHERE create_date >= %%s GROUP BY label "
                "ORDER BY COUNT(*) DESC LIMIT %%s" % expr,
                (since, params_limit))
            return [{"label": label or "—", "count": count}
                    for label, count in self.env.cr.fetchall()]

        # top IPs additionally carry the GeoIP country of their hits so the
        # Top-attackers card can flag origins. MAX() over the hits: one IP maps
        # to one country in practice; NULLIF keeps empty values from winning
        self.env.cr.execute(
            "SELECT COALESCE(NULLIF(ip, ''), 'unknown') AS label, "
            "MAX(NULLIF(country, '')) AS country, COUNT(*) FROM waf_hit "
            "WHERE create_date >= %s GROUP BY label "
            "ORDER BY COUNT(*) DESC LIMIT 8", (since,))
        top_ips = [{"label": label or "—", "count": count,
                    "country": country or ""}
                   for label, country, count in self.env.cr.fetchall()]
        top_rules = top("COALESCE(NULLIF(rule_name, ''), NULLIF(kind, ''), 'unknown')")

        # timeline: hourly buckets for 1d, daily for 7/30d, zero-filled
        trunc = "hour" if days == 1 else "day"
        self.env.cr.execute(
            "SELECT date_trunc(%s, create_date) AS bucket, COUNT(*), "
            "COUNT(*) FILTER (WHERE blocked) FROM waf_hit "
            "WHERE create_date >= %s GROUP BY bucket ORDER BY bucket",
            (trunc, since))
        counts = {fields.Datetime.to_string(b): (t, bl)
                  for b, t, bl in self.env.cr.fetchall()}
        step = timedelta(hours=1) if trunc == "hour" else timedelta(days=1)
        bucket = (since.replace(minute=0, second=0, microsecond=0)
                  if trunc == "hour"
                  else since.replace(hour=0, minute=0, second=0, microsecond=0))
        timeline = []
        while bucket <= now:
            key = fields.Datetime.to_string(bucket)
            total, blocked = counts.get(key, (0, 0))
            timeline.append({"date": key, "total": total, "blocked": blocked})
            bucket += step

        self.env.cr.execute(
            "SELECT COALESCE(NULLIF(category, ''), 'other'), COUNT(*) "
            "FROM waf_hit WHERE create_date >= %s "
            "GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 7", (since,))
        by_category = [{"label": cat, "count": cnt}
                       for cat, cnt in self.env.cr.fetchall()]

        # attack origins for the dashboard world map; non-empty only when
        # a MaxMind GeoLite2-Country database powers IP geolocation. No LIMIT:
        # every distinct origin must be in the payload, and hits without
        # attribution aggregate under '?'
        self.env.cr.execute(
            "SELECT COALESCE(NULLIF(country, ''), '?') AS code, COUNT(*), "
            "COUNT(*) FILTER (WHERE blocked) "
            "FROM waf_hit WHERE create_date >= %s "
            "GROUP BY code ORDER BY COUNT(*) DESC", (since,))
        by_country = [{"code": code, "total": total, "blocked": blocked}
                      for code, total, blocked in self.env.cr.fetchall()]
        try:
            from odoo.http import root
            country_ready = root.geoip_country_db is not None
        except Exception:
            country_ready = False

        recent = [{
            "date": fields.Datetime.to_string(h["create_date"]),
            "method": h["method"] or "",
            "path": (h["path"] or "")[:90],
            "ip": h["ip"] or "",
            "rule": h["rule_name"] or h["kind"] or "",
            "blocked": h["blocked"],
            "severity": h["severity"],
        } for h in Hit.search_read(
            [], ["create_date", "method", "path", "ip", "rule_name", "blocked",
                 "severity", "kind"], order="id desc", limit=15)]

        scan = self.env["waf.scan"].search([], order="id desc", limit=1)
        return {
            "config_id": cfg.id,
            "mode": cfg.mode,
            "country_mode": cfg.country_mode,
            "hits_24h": Hit.search_count(day),
            "blocked_24h": Hit.search_count(day + [("blocked", "=", True)]),
            "hits_total": Hit.search_count([]),
            "rules_active": Rule.search_count([("active", "=", True)]),
            "rules_custom": Rule.search_count(
                [("active", "=", True), ("source", "=", "custom")]),
            "rules_off": Rule.search_count([("active", "=", False)]),
            "ip_deny": Entry.search_count(active_net + [("kind", "=", "deny")]),
            "ip_allow": Entry.search_count(active_net + [("kind", "=", "allow")]),
            "auto_lockouts": Entry.search_count(
                active_net + [("kind", "=", "deny"), ("source", "=", "auto")]),
            "top_ips": top_ips,
            "top_rules": top_rules,
            "recent_hits": recent,
            "days": days,
            "period": {1: "24h", 7: "7d", 30: "30d"}[days],
            "timeline": timeline,
            "by_category": by_category,
            "by_country": by_country,
            "country_ready": country_ready,
            "last_scan": {
                "id": scan.id,
                "grade": scan.grade or "?",
                "score": scan.score,
                "state": scan.state,
                "date": fields.Datetime.to_string(scan.date_start),
                "critical": scan.n_critical,
                "high": scan.n_high,
                "medium": scan.n_medium,
                "low": scan.n_low,
                "info": scan.n_info,
            } if scan else None,
        }

    # ------------------------------------------------------------- helpers

    def _now_plus_minutes(self, minutes):
        return fields.Datetime.now() + timedelta(minutes=minutes or 60)

    def _client_ip(self):
        """Best-effort, trust-aware client IP for the current web request."""
        ip = self.env.context.get("ip")
        try:
            from odoo.http import request
            environ = request.httprequest.environ
        except Exception:
            return ip
        return iputil.resolve_client_ip(
            environ.get("REMOTE_ADDR"), environ.get("HTTP_X_REAL_IP"),
            environ.get("HTTP_X_FORWARDED_FOR"),
            iputil.parse_cidrs(self.get_singleton().trusted_proxies))

    def _notify_admins(self, subject, body):
        self.ensure_one()
        emails = []
        if self.alert_email:
            emails.append(self.alert_email)
        else:
            admins = self.env["res.users"].search(
                [("share", "=", False), ("active", "=", True),
                 ("all_group_ids", "in", self.env.ref("base.group_erp_manager").ids)])
            emails = [e for e in admins.mapped("email") if e]
        emails = list(dict.fromkeys(emails))
        if not emails:
            _logger.warning("WAF alert without recipients: %s", subject)
            return
        try:
            # create *only*, no mail.send(): an attacker-triggered lockout
            # must never block a worker on SMTP I/O. Delivery: the core mail
            # queue or the _cron_alerts_flush cron (data/crons.xml).
            self.env["mail.mail"].sudo().create({
                "subject": subject,
                "body_html": body,
                "email_to": ",".join(emails),
            })
        except Exception:
            _logger.warning("WAF: could not queue alert mail (no mail server?)",
                            exc_info=True)

    @api.model
    def _cron_alerts_flush(self, batch=20):
        """Send OW Shield mails queued by _notify_admins (bounded delay
        instead of SMTP inside an attacker-triggered request). Scoped to
        our own subject prefix: it never touches other mail."""
        queued = self.env["mail.mail"].sudo().search(
            [("state", "=", "outgoing"),
             ("subject", "=like", "OW Shield:%")],
            limit=batch, order="id")
        if queued:
            queued.process_email_queue(email_ids=queued.ids, batch_size=batch)
        return len(queued)

    # ------------------------------------------------------------- actions

    def action_allow_current_ip(self):
        self.ensure_one()
        ip = self._client_ip()
        if not ip:
            return False
        host_mask = "/128" if ":" in ip else "/32"
        self.env["waf.ip.entry"].create({
            "network": ip + host_mask,
            "kind": "allow",
            "source": "manual",
            "reason": "Allowed from Firewall settings (your current IP)",
            "expires": self._now_plus_minutes(72 * 60),
        })
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("IP allowed"),
                "message": _("%s may now bypass the firewall for 72 hours.", ip + host_mask),
                "type": "success",
            },
        }

    def _is_waf_manager(self):
        # single source of truth for who may drive OW Shield's admin actions,
        # independent of view/button plumbing. base.group_system implies
        # group_waf_manager (security.xml), so install admins keep working.
        return self.env.user.has_group("ow_shield.group_waf_manager")

    def action_reset_master_counters(self):
        """Admin reset of the instance-wide master-password counters: ends
        every source's failure window and lifts active lockouts (both live in
        the same rows). The recovery link in the alert mail is the self-service
        path and lifts one network only; no other protection layer is touched.

        Authorization is checked here, before any elevated read or write: the
        RPC layer performs no model-level access check for custom methods, so
        an unchecked ``sudo()`` here would let any authenticated caller wipe
        the whole counter store; an unauthorized caller gets ``AccessError``.

        The target is the store ``core.masterbp.scope_db`` resolves, reached
        over its own connection when this database is not the host. No
        resolvable store means the layer was not running, and that is reported
        as such.
        """
        if not self._is_waf_manager():
            raise AccessError(_(
                "Only OW Shield managers may reset the master-password "
                "failure counters."))
        self.ensure_one()
        scope = masterbp.scope_db()
        if not scope:
            # nothing hosts the counter store yet (masterbp's fail-open
            # window): warn instead of claiming a reset
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Nothing to reset"),
                    "message": _("No database currently hosts the "
                                 "instance-wide master-password counters, "
                                 "so no lockout was active to lift. "
                                 "Nothing was modified."),
                    "type": "warning",
                },
            }
        try:
            # own connection to the store's database, with the same
            # rollback-first transaction pattern as every counter write
            # (see middleware._fetch_snapshot)
            with db_connect(scope).cursor() as cr:
                cr.rollback()  # fresh txn (see _fetch_snapshot)
                cr.execute("DELETE FROM waf_master_hit")
                count = cr.rowcount
                cr.commit()
        except Exception:
            _logger.warning("WAF: master-password counter store "
                            "unreachable while resetting in %s", scope,
                            exc_info=True)
            raise UserError(_("Could not reach the master-password "
                              "counter store in database %s. Nothing "
                              "was reset.", scope))
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Master-password counters reset"),
                "message": _("%(count)d failure window(s)/lockout(s) "
                             "ended in the instance-wide counter store "
                             "(database %(db)s). Other protections were "
                             "not modified.", count=count, db=scope),
                "type": "success",
            },
        }

    def action_reload_rules(self):
        created, updated = self.env["waf.rule"].load_builtin_rules()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Rule packs reloaded"),
                "message": _("%(created)d rules created, %(updated)d updated.",
                             created=created, updated=updated),
                "type": "success",
                "next": {"type": "ir.actions.act_window",
                         "res_model": "waf.rule",
                         "views": [(False, "list"), (False, "form")],
                         "target": "current"},
            },
        }

    def action_run_scan(self):
        self.ensure_one()
        scan = self.env["waf.scan"].create({
            "scan_files": self.scan_files,
            "scan_attachments": self.scan_attachments,
        })
        scan.action_run()
        return {
            "type": "ir.actions.act_window",
            "res_model": "waf.scan",
            "res_id": scan.id,
            "views": [(False, "form"), (False, "list")],
            "target": "current",
        }

    # ------------------------------------------------------------- cron

    @api.model
    def _cron_cleanup(self):
        cfg = self.get_singleton()
        cutoff = fields.Datetime.now() - timedelta(days=cfg.retention_days or 30)
        removed = self.env["waf.hit"].sudo().search(
            [("create_date", "<", cutoff)]).unlink()
        expired = self.env["waf.ip.entry"].sudo().search(
            [("expires", "!=", False), ("expires", "<", fields.Datetime.now())])
        count = len(expired)
        if expired:
            expired.write({"active": False})
        return {"hits": removed, "expired_entries": count}

    @api.model
    def _cron_session_timeout(self):
        """Force-logout sessions idle past ``session_idle_minutes``: deletes
        *this database's* idle session files through core's
        ``delete_from_identifiers`` and marks their ``res.device.log`` rows
        revoked. No-op while the timeout is disabled (the default).

        Idle is decided per session, not per device-log row: a session keeps
        one log per IP or browser it used, and only its most recent activity
        counts, or a departed network's old log would evict a session that is
        live on the current one. The cutoff adds SESSION_ACTIVITY_SLACK for
        core's hourly activity refresh.

        Not core's mtime-based ``session_store.vacuum()``: the session-file
        directory is shared by every database on the server, so this
        database's setting must not evict other databases' sessions.
        """
        cfg = self.get_singleton()
        minutes = cfg.session_idle_minutes or 0
        if minutes <= 0:
            return {"disabled": True}
        if minutes < SESSION_IDLE_FLOOR:
            # a stale sub-floor value would evict continuously active users
            # (last_activity only refreshes hourly): clamp, and log it
            _logger.warning(
                "OW Shield: idle session timeout of %d min is below the "
                "%d-minute floor; enforcing %d minutes instead.",
                minutes, SESSION_IDLE_FLOOR, SESSION_IDLE_FLOOR)
            minutes = SESSION_IDLE_FLOOR
        from odoo.http import root
        cutoff = fields.Datetime.now() - timedelta(
            minutes=minutes + SESSION_ACTIVITY_SLACK)
        stale_logs = self.env["res.device.log"].sudo().search([
            ("revoked", "=", False),
            ("last_activity", "<", cutoff),
        ])
        candidates = sorted({
            ident for ident in stale_logs.mapped("session_identifier")
            if ident})
        if not candidates:
            return {"revoked_sessions": 0}
        # any recent log of the session keeps it alive, revoked or not: a
        # session re-used after an old revocation mark is still live
        alive = set(self.env["res.device.log"].sudo().search([
            ("session_identifier", "in", candidates),
            ("last_activity", ">=", cutoff),
        ]).mapped("session_identifier"))
        dead = [ident for ident in candidates if ident not in alive]
        killed = 0
        if dead:
            root.session_store.delete_from_identifiers(dead)
            self.env["res.device.log"].sudo().search([
                ("session_identifier", "in", dead),
                ("revoked", "=", False),
            ]).write({"revoked": True})
            killed = len(dead)
        return {"revoked_sessions": killed}
