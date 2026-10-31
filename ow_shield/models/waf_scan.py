import json
import logging
import os
import re
from datetime import timedelta

from odoo import _, api, fields, models
from odoo import release as odoo_release
from odoo.tools import config as server_config, str2bool

_logger = logging.getLogger(__name__)

SEVERITY_WEIGHT = {"critical": 25, "high": 12, "medium": 6, "low": 2, "info": 0}
GRADE_BORDERS = [(90, "A"), (80, "B"), (65, "C"), (50, "D"), (35, "E")]

ADDON_SIGNATURES = [
    # (regex, severity, description)
    (rb"(?<![\w.])subprocess\.(Popen|call|run|check_output|check_call)\s*\(",
     "low", "Spawns operating-system commands (subprocess)."),
    (rb"(?<![\w.])os\.system\s*\(", "medium", "Spawns shell commands (os.system)."),
    (rb"(?<![\w.])os\.popen\s*\(", "medium", "Spawns shell commands (os.popen)."),
    (rb"eval\s*\([^)]*(?:b64decode|decode\(|bytes\.fromhex|compile\s*\()",
     "critical", "eval() on decoded/compiled data (webshell pattern)."),
    (rb"exec\s*\(\s*compile\s*\(", "critical", "exec(compile(...)) dynamic code."),
    (rb"(?:base64\.)?b64decode\s*\([^)]{0,80}\)\s*\)?\s*\.?(?:decode\(\))?[^#\n]*\beval\b",
     "critical", "Eval of base64-decoded payload."),
    (rb"(?<![\w.])pty\.spawn\s*\(", "critical", "Reverse-shell helper (pty.spawn)."),
    (rb"socket\.socket\s*\([^)]*\)[^#\n]{0,120}\.connect\s*\(",
     "high", "Outbound raw socket connection (reverse shell pattern)."),
    (rb"(?<![\w.])__import__\s*\(\s*(?:os|subprocess|socket|pty|builtins)",
     "high", "Indirect import of dangerous module."),
    (rb"(?:GET|POST)\s*\[\s*['\"](?:cmd|cmd0?|shell|exec|command)['\"]",
     "critical", "Webshell superglobal command parameter."),
]
DANGEROUS_FILENAMES = re.compile(
    r"(?:^|/)(?:c99|r57|b37k|webshell|backdoor|wso|adminer)[\w.-]*\.(?:php|jsp|html|py|pl|sh)$|"
    r"\.(?:php\d?|phtml\d?|jsp|jspx|asp|aspx|war|elf|xll|vba)$", re.IGNORECASE)

DANGEROUS_EXT_RE = re.compile(
    r"\.(php\d?|phtml\d?|jsp|jspx?|asp|aspx|exe|com|scr|bat|cmd|msi|sh|jar|elf)$",
    re.IGNORECASE)
DANGEROUS_MIMES = {
    "application/x-php", "text/x-php", "application/x-executable",
    "application/x-sh", "application/x-msdownload", "application/java-archive",
    "text/x-python", "application/x-shellscript",
}


class WafScan(models.Model):
    _name = "waf.scan"
    _description = "Security Scan"
    _order = "id desc"

    name = fields.Char(compute="_compute_name", store=True)
    state = fields.Selection(
        [("draft", "Not run"), ("done", "Done"), ("error", "Error")],
        default="draft", required=True)
    date_start = fields.Datetime(readonly=True)
    date_end = fields.Datetime(readonly=True)
    duration = fields.Integer(readonly=True, help="seconds")
    scan_files = fields.Boolean(string="Scan add-on files")
    scan_attachments = fields.Boolean(string="Scan attachments")

    finding_ids = fields.One2many("waf.finding", "scan_id", readonly=True)
    score = fields.Integer(readonly=True)
    grade = fields.Char(readonly=True)
    n_critical = fields.Integer(readonly=True)
    n_high = fields.Integer(readonly=True)
    n_medium = fields.Integer(readonly=True)
    n_low = fields.Integer(readonly=True)
    n_info = fields.Integer(readonly=True)
    posture_json = fields.Text(
        readonly=True,
        help="WAF configuration + traffic snapshot captured when the scan "
             "finished; the PDF appendix renders this, so an old report keeps "
             "describing the firewall as it was at scan time.")

    @api.depends("date_start")
    def _compute_name(self):
        for scan in self:
            scan.name = "Scan %s" % (
                scan.date_start.strftime("%Y-%m-%d %H:%M")
                if scan.date_start else "(pending)")

    # ------------------------------------------------------------------ run

    def action_run(self):
        for scan in self:
            # start from a clean slate: if this run fails halfway, the stored
            # results must not keep a previous healthy grade/score/posture
            scan.write({
                "state": "draft",
                "date_start": fields.Datetime.now(),
                "date_end": False,
                "duration": 0,
                "score": 0,
                "grade": False,
                "n_critical": 0,
                "n_high": 0,
                "n_medium": 0,
                "n_low": 0,
                "n_info": 0,
                "posture_json": False,
            })
            scan.finding_ids.unlink()  # re-running a scan must not duplicate
            # each step in its own savepoint: a check that trips a DB
            # error would abort the whole transaction, and the
            # state="error" write below must survive to record it.
            try:
                with self.env.cr.savepoint():
                    scan._run_checks()
                if scan.scan_files:
                    with self.env.cr.savepoint():
                        scan._run_file_scan()
                if scan.scan_attachments:
                    with self.env.cr.savepoint():
                        scan._run_attachment_scan()
                findings = scan.finding_ids
                counts = {sev: len(findings.filtered(lambda f: f.severity == sev))
                          for sev in SEVERITY_WEIGHT}
                penalty = sum(SEVERITY_WEIGHT[s] * n for s, n in counts.items())
                score = max(0, 100 - penalty)
                grade = "F"
                for border, letter in GRADE_BORDERS:
                    if score >= border:
                        grade = letter
                        break
                end = fields.Datetime.now()
                scan.write({
                    "state": "done",
                    "date_end": end,
                    "duration": int((end - scan.date_start).total_seconds()),
                    "score": score,
                    "grade": grade,
                    "n_critical": counts["critical"],
                    "n_high": counts["high"],
                    "n_medium": counts["medium"],
                    "n_low": counts["low"],
                    "n_info": counts["info"],
                    "posture_json": json.dumps(scan._posture_snapshot()),
                })
                if counts["critical"]:
                    cfg = self.env["waf.config"].get_singleton()
                    cfg._notify_admins(
                        subject=_("Security scan: %d critical finding(s), grade %s",
                                  counts["critical"], grade),
                        body="<p>%s</p>" % "; ".join(
                            f.name for f in findings.filtered(
                                lambda f: f.severity == "critical")),
                    )
            except Exception:
                _logger.exception("Security scan failed")
                scan.write({"state": "error", "date_end": fields.Datetime.now()})
        return True

    # ------------------------------------------------------------- report

    GRADE_COLORS = {"A": "#1a7f37", "B": "#4c9a2a", "C": "#e8940c",
                    "D": "#c0392b", "E": "#8b1f1f", "F": "#7a1010"}
    SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")

    def report_grade_color(self):
        self.ensure_one()
        return self.GRADE_COLORS.get(self.grade or "", "#6c757d")

    def report_meta(self):
        self.ensure_one()
        icp = self.env["ir.config_parameter"].sudo()
        return {
            "organization": self.env.company.name,
            "url": icp.get_param("web.base.url", ""),
            "database": self.env.cr.dbname,
            "version": odoo_release.version,
            "run_on": fields.Datetime.to_string(
                self.date_start or fields.Datetime.now()),
            "danger": "%d / %d" % (self.n_critical, self.n_high),
            "total": (self.n_critical + self.n_high + self.n_medium
                      + self.n_low + self.n_info),
        }

    def report_summary_text(self):
        self.ensure_one()
        if self.n_critical:
            return _("This Odoo instance has significant weaknesses. Address "
                     "the priority actions below as soon as possible.")
        if self.n_high or self.n_medium:
            return _("No critical issues were found, but some weaknesses "
                     "remain. Review the priority actions below.")
        return _("No significant weaknesses found. Keep this configuration "
                 "hardened and re-run the scan regularly.")

    # -- presentation-order helpers (PDF report) -----------------------------
    # One canonical ordering (worst first, then creation order) drives the
    # F-nn refs in the TOC, the findings section, the priority actions and
    # the remediation checklist so every number in the report agrees.

    def _report_findings(self):
        self.ensure_one()
        rank = {s: i for i, s in enumerate(self.SEVERITY_ORDER)}
        return self.finding_ids.sorted(key=lambda f: (rank[f.severity], f.id))

    def report_numbered_findings(self):
        self.ensure_one()
        return [{"ref": "F-%02d" % i,
                 "severity": f.severity,
                 "name": f.name,
                 "message": f.message or "",
                 "path": f.path or "",
                 "recommendation": f.recommendation or "",
                 "ui": f.report_ui()}
                for i, f in enumerate(self._report_findings(), 1)]

    def report_severity_counts(self):
        """Segments of the stacked severity bar, worst first. Tiny counts
        get a 4% floor so a single critical finding still shows a colour;
        widths are re-normalised to sum to 100."""
        self.ensure_one()
        from .waf_finding import SEVERITIES, SEVERITY_UI
        counts = {"critical": self.n_critical, "high": self.n_high,
                  "medium": self.n_medium, "low": self.n_low,
                  "info": self.n_info}
        present = [(sev, counts[sev]) for sev in self.SEVERITY_ORDER
                   if counts[sev]]
        total = sum(n for _sev, n in present) or 1
        raw = [max(4.0, 100.0 * n / total) for _sev, n in present]
        scale = 100.0 / sum(raw) if raw else 0
        short = dict(SEVERITIES)
        return [{"severity": sev, "count": n,
                 "label": _(short[sev]),
                 "color": SEVERITY_UI[sev][1],
                 "pct": round(p * scale, 2)}
                for (sev, n), p in zip(present, raw)]

    def report_priority_actions(self):
        self.ensure_one()
        rank = {"critical": 0, "high": 1, "medium": 2}
        refs = {f.id: "F-%02d" % i
                for i, f in enumerate(self._report_findings(), 1)}
        acts = [f for f in self.finding_ids if f.severity in rank]
        acts.sort(key=lambda f: (rank[f.severity], f.id))
        return [{"ref": refs[f.id],
                 "name": f.name,
                 "recommendation": (f.recommendation or "").split(". ")[0]}
                for f in acts[:5]]

    def report_remediation(self):
        """Checklist rows: the critical/high findings worth scheduling."""
        self.ensure_one()
        return [fd for fd in self.report_numbered_findings()
                if fd["severity"] in ("critical", "high")]

    # -- posture snapshot (appendix renders the firewall as it was at run) --

    def _posture_snapshot(self):
        cfg = self.env["waf.config"].get_singleton()
        Entry = self.env["waf.ip.entry"]
        Rule = self.env["waf.rule"]
        Hit = self.env["waf.hit"]
        cfg_fields = self.env["waf.config"]._fields
        # same validity rule the engine and dashboard use: active AND not
        # past its expiry (cleanup deactivates expired entries only later)
        active_net = ["&", ("active", "=", True),
                      "|", ("expires", "=", False),
                      ("expires", ">", fields.Datetime.now())]
        day = [("create_date", ">=", fields.Datetime.now()
                - timedelta(days=1))]
        lockout = _("disabled")
        if cfg.auto_lockout_enabled:
            lockout = _("after %d weighted hits in %d min, lock %d min") % (
                cfg.auto_lockout_max, cfg.auto_lockout_window,
                cfg.auto_lockout_minutes)
        login = _("disabled")
        if cfg.login_fail_enabled:
            login = _("%d failures per IP in %d min, lock %d min") % (
                cfg.login_fail_max, cfg.login_fail_window,
                cfg.login_fail_minutes)
        country = dict(cfg_fields["country_mode"].selection)[cfg.country_mode]
        if cfg.country_ids:
            country += " (%d countries)" % len(cfg.country_ids)
        return {
            "captured": fields.Datetime.to_string(fields.Datetime.now()),
            "mode": dict(cfg_fields["mode"].selection)[cfg.mode],
            "country": country,
            "rules_active": Rule.search_count([("active", "=", True)]),
            "rules_custom": Rule.search_count(
                [("active", "=", True), ("source", "=", "custom")]),
            "rules_off": Rule.search_count([("active", "=", False)]),
            "ip_allow": Entry.search_count(active_net + [("kind", "=", "allow")]),
            "ip_deny": Entry.search_count(active_net + [("kind", "=", "deny")]),
            "auto_lockouts": Entry.search_count(
                active_net + [("kind", "=", "deny"), ("source", "=", "auto")]),
            "lockout": lockout,
            "login_lockout": login,
            "hits_24h": Hit.search_count(day),
            "blocked_24h": Hit.search_count(day + [("blocked", "=", True)]),
        }

    def report_posture(self):
        self.ensure_one()
        if not self.posture_json:
            return None
        try:
            return json.loads(self.posture_json)
        except ValueError:
            return None

    def report_scope(self):
        self.ensure_one()
        dur = self.duration or 0
        return {
            "items": [
                (_("Server configuration (odoo.conf)"), True),
                (_("Users, administrators, 2FA, API keys"), True),
                (_("Server actions / code execution surface"), True),
                (_("Add-on file signatures"), bool(self.scan_files)),
                (_("Attachment content signatures"), bool(self.scan_attachments)),
            ],
            "duration": _("%d min %02d s") % divmod(dur, 60) if dur
            else _("less than a second"),
        }

    def _finding(self, severity, name, message, recommendation=None,
                 model_name=None, res_id=None, path=None):
        self.env["waf.finding"].create({
            "scan_id": self.id,
            "severity": severity,
            "name": name,
            "message": message,
            "recommendation": recommendation,
            "model_name": model_name,
            "res_id": res_id,
            "path": path,
        })

    # ------------------------------------------------------- config/account checks

    def _run_checks(self):
        env = self.env
        cfg = server_config

        # -- server configuration ------------------------------------------
        if cfg["list_db"]:
            self._finding(
                "high", "Database manager listing is enabled (list_db)",
                "The database list/manager is reachable from the web server "
                "process.", "Set list_db = False in the odoo config file, or "
                "keep nginx returning 404 for /web/database/* (you already do).")
        admin_pw = cfg["admin_passwd"]
        if admin_pw in (None, False, "", "admin"):
            self._finding(
                "critical", "Master (master password) is default or missing",
                "The database master password is unset or the literal 'admin'.",
                "Set a strong admin_passwd and store it HASHED "
                "($pbkdf2-sha512$...) in odoo.conf.")
        elif isinstance(admin_pw, str) and not admin_pw.startswith("$"):
            self._finding(
                "medium", "Master password stored in clear text",
                "admin_passwd is a plaintext string in the config file.",
                "Hash it: python3 -c \"import passlib...\" or use "
                "$pbkdf2-sha512$ hashes.")
        if not cfg["dbfilter"] and len(cfg["db_name"] or []) != 1:
            self._finding(
                "medium", "No dbfilter configured",
                "All databases of the server are exposed to every hostname.",
                "Set dbfilter = ^yourdb$ (or per-host regex).")
        if not cfg["proxy_mode"]:
            self._finding(
                "low", "proxy_mode is disabled",
                "Odoo does not trust X-Forwarded-* headers; client IPs in logs "
                "and by this firewall will be the proxy IP unless nginx talks "
                "proxy protocol directly.",
                "Run odoo with --proxy-mode when behind nginx.")
        if cfg["log_level"] in ("debug_sql", "debug_rpc", "debug"):
            self._finding("low", "Verbose debug logging enabled",
                          "log_level=%s may leak sensitive data into logs."
                          % cfg["log_level"], "Use log_level = info in production.")

        # -- accounts -------------------------------------------------------
        demo_users = env["res.users"].search_count(
            [("login", "=ilike", "demo%"), ("share", "=", False)])
        if demo_users:
            self._finding(
                "high", "%d 'demo' user accounts present" % demo_users,
                "Demo logins typically ship with well-known passwords.",
                "Delete demo users or rename them and set unique passwords.")
        default_admin = env["res.users"].search_count([("login", "=", "admin")])
        if default_admin:
            self._finding(
                "medium", "The login 'admin' is in use",
                "Attackers always try the 'admin' login first.",
                "Rename the login to something unique.")
        erp_group = env.ref("base.group_erp_manager")
        admins = env["res.users"].search(
            [("all_group_ids", "in", erp_group.ids), ("share", "=", False)])
        if not admins:
            self._finding("critical", "No administrator users exist",
                          "No user is member of the administrators group.",
                          "Create at least one admin account.")
        if len(admins) > 4:
            self._finding("low", "%d administrator accounts" % len(admins),
                          "Administrator rights spread over many accounts.",
                          "Reduce admins to the people who truly need them.")
        inactive_admins = env["res.users"].with_context(active_test=False).search_count(
            [("all_group_ids", "in", erp_group.ids), ("active", "=", False)])
        if inactive_admins:
            self._finding("medium", "%d inactive administrator account(s)"
                          % inactive_admins,
                          "Inactive accounts can often be reactivated by anyone "
                          "with Settings rights.", "Delete them instead.")

        # -- 2FA -------------------------------------------------------------
        totp_installed = env["ir.module.module"].search_count(
            [("name", "in", ["auth_totp", "auth_totp_portal"]),
             ("state", "=", "installed")])
        if not totp_installed:
            self._finding("medium", "Two-factor authentication is not installed",
                          "No auth_totp module; passwords are the only factor.",
                          "Install auth_totp (and enforce it for admins).")
        elif "totp_secret" in env["res.users"]._fields:
            # 19 guards totp_secret with a field-level ACL that denies the
            # read to everyone (administrators included) -> inspect via SQL.
            self.env.cr.execute(
                "SELECT login FROM res_users "
                "WHERE id IN %s AND (totp_secret IS NULL OR totp_secret = '')",
                (tuple(admins.ids) or (-1,),))
            unprotected = [row[0] for row in self.env.cr.fetchall()]
            if unprotected:
                self._finding("high", "Administrator(s) without 2FA: %s"
                              % ", ".join(unprotected[:10]),
                              "Admin logins protected only by a password.",
                              "Enable 2FA for every administrator.")

        # -- API keys ---------------------------------------------------------
        if "res.users.apikeys" in env:
            keys = env["res.users.apikeys"].search([])
            no_expiry = [k.name for k in keys if not k.expiration_date]
            if no_expiry:
                self._finding("medium", "%d API key(s) without expiration"
                              % len(no_expiry),
                              "API keys '%s' never expire." % ", ".join(no_expiry[:8]),
                              "Recreate keys with an expiration date.")

        # -- code execution surface -----------------------------------------
        code_actions = env["ir.actions.server"].search([])
        code_actions = code_actions.filtered(lambda a: a.code)
        if len(code_actions) > 5:
            self._finding("info", "%d server actions with Python code"
                          % len(code_actions),
                          "Server actions can execute arbitrary Python as superuser.",
                          "Review their necessity; names: %s"
                          % ", ".join(a.name for a in code_actions[:10]))
        if "base.automation" in env:
            # Odoo 18/19: automations carry no own code field; the Python
            # payload lives on the linked server actions. active_test=False
            # so archived automations (the point of the check) are seen.
            autos = env["base.automation"].with_context(
                active_test=False).search([]).filtered(
                lambda a: not a.active and any(
                    act.state == "code" and (act.code or "").strip()
                    for act in a.action_server_ids))
            if autos:
                self._finding("low", "%d inactive automations containing code"
                              % len(autos),
                              "Inactive automations keep their Python payload.",
                              "Delete unused automations.")
        recent_crons = env["ir.cron"].search(
            [("create_date", ">=", fields.Datetime.now() - timedelta(days=14))])
        if recent_crons:
            self._finding("info", "%d scheduled actions created in the last 14 days"
                          % len(recent_crons),
                          "New cron entries: %s" % ", ".join(
                              c.name for c in recent_crons[:10]),
                          "Verify these were created intentionally.")

        # -- mail / web urls ---------------------------------------------------
        base_url = env["ir.config_parameter"].sudo().get_param("web.base.url", "")
        if base_url.startswith("http://"):
            self._finding("medium", "web.base.url uses plain HTTP",
                          "%s is not encrypted." % base_url,
                          "Point web.base.url at the https:// URL.")
        mail_servers = env["ir.mail_server"].search([])
        # 19 renamed the field to smtp_encryption; fall back for older runtimes
        enc_field = ("smtp_encryption"
                     if "smtp_encryption" in env["ir.mail_server"]._fields
                     else "encryption")
        plain = [s.name for s in mail_servers
                 if (getattr(s, enc_field, None) or "none") == "none"]
        if plain:
            self._finding("medium", "%d mail server(s) without TLS" % len(plain),
                          "SMTP connections for: %s are unencrypted."
                          % ", ".join(plain[:5]),
                          "Enable SSL/TLS on outbound mail servers.")

        # -- modules -----------------------------------------------------------
        thirdparty = env["ir.module.module"].search([
            ("state", "=", "installed"),
            ("name", "not in", ["web", "base", "mail", "auth_totp", "auth_signup"]),
        ]).filtered(lambda m: m.author not in (
            "Odoo S.A.", "Odoo Community Association (OCA)", False))
        if thirdparty:
            self._finding("info", "%d third-party module(s) installed"
                          % len(thirdparty),
                          "Modules not from Odoo S.A./OCA: %s"
                          % ", ".join(thirdparty.mapped("name")[:12]),
                          "Third-party code runs with full server rights; "
                          "audit its source (see also your sls healthcheck).")

        # -- sign-up -----------------------------------------------------------
        signup = env["ir.config_parameter"].sudo().get_param(
            "auth_signup.allow_uninvited")
        if str2bool(signup):
            self._finding("high", "Free sign-up is enabled",
                          "Anyone can create a portal account on this instance.",
                          "Disable public sign-up unless you really need it.")
        return True

    # ------------------------------------------------------------- file scan

    def _addon_roots(self):
        roots = []
        addons = server_config["addons_path"] or ""
        # 19 may hand back a list instead of the legacy comma string
        paths = addons.split(",") if isinstance(addons, str) else list(addons)
        for path in paths:
            path = path.strip()
            if not path or not os.path.isdir(path):
                continue
            roots.append(path)
        # exclude the core odoo/addons directory itself: it is signed code
        import odoo
        core_pkgs = [p for p in getattr(odoo, "__path__", ()) if p]
        core_addons = {os.path.abspath(os.path.join(p, "addons"))
                       for p in core_pkgs}
        roots = [r for r in roots
                 if os.path.abspath(r) not in core_addons]
        return roots

    def _run_file_scan(self):
        max_size = 2 * 1024 * 1024
        compiled = [(re.compile(rx), sev, desc) for rx, sev, desc in ADDON_SIGNATURES]
        # never scan ourselves: the signature literals in waf_scan.py would
        # match our own ADDON_SIGNATURES regexes (self false-positive)
        own = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
        own += os.sep
        seen = set()
        for root in self._addon_roots():
            for dirpath, _dirnames, filenames in os.walk(root):
                if "/." in dirpath or dirpath.endswith("/.git"):
                    continue
                for fname in filenames:
                    full = os.path.join(dirpath, fname)
                    if full.startswith(own):
                        continue
                    if DANGEROUS_FILENAMES.search("/" + fname):
                        if full not in seen:
                            seen.add(full)
                            self._finding(
                                "high", "Suspicious file name: %s" % fname,
                                "File name matches known webshell/secondary "
                                "extension patterns.", "Remove or verify the file.",
                                path=full)
                        continue
                    if not fname.endswith((".py", ".js", ".xml", ".html", ".qweb")):
                        continue
                    try:
                        if os.path.getsize(full) > max_size:
                            continue
                        with open(full, "rb") as f:
                            data = f.read()
                    except OSError:
                        continue
                    for rx, sev, desc in compiled:
                        m = rx.search(data)
                        if m:
                            self._finding(
                                sev, "Code signature in %s" % fname,
                                "%s (match: %.120r)" % (desc, m.group(0)),
                                "Inspect this file: it may be malicious or "
                                "legit but risky.", path=full)
                            seen.add(full)
                            break
        return True

    # ------------------------------------------------------- attachment scan

    def _run_attachment_scan(self):
        batch = 200
        offset = 0
        limit = 5000
        suspicious = 0
        Attachment = self.env["ir.attachment"]
        while offset < limit:
            # newest first: when the cap is reached, the *recent* uploads
            # (the interesting attack surface) are the ones covered
            attachments = Attachment.sudo().search(
                [("res_model", "!=", False)], limit=batch, offset=offset,
                order="id desc")
            if not attachments:
                break
            for att in attachments:
                name = att.name or ""
                risky_name = bool(DANGEROUS_EXT_RE.search(name))
                risky_mime = (att.mimetype or "") in DANGEROUS_MIMES
                # sniff only the first bytes: att.datas would load and
                # base64-decode the whole file per attachment, letting a
                # few large PDFs/videos push a worker over its memory cap
                sniff = b""
                try:
                    if att.store_fname:
                        sniff = att._file_read(att.store_fname, 512) or b""
                    elif att.db_datas:
                        sniff = bytes(att.db_datas)[:512]
                except Exception:
                    sniff = b""
                magic_hit = (
                    sniff.startswith(b"#!/") or sniff.startswith(b"<?php")
                    or sniff.startswith(b"<%") or b"cmd.exe" in sniff[:256])
                if risky_name and att.public:
                    suspicious += 1
                    self._finding(
                        "high", "Public attachment with dangerous extension: %s" % name,
                        "Attachment %s (id %s, model %s) is publicly served with "
                        "a dangerous extension/mime." % (name, att.id, att.res_model),
                        "Delete it or make it private.",
                        model_name="ir.attachment", res_id=att.id)
                elif magic_hit and att.public:
                    suspicious += 1
                    self._finding(
                        "critical", "Suspicious content in public attachment: %s" % name,
                        "Attachment %s (id %s) starts with a shebang/PHP tag or "
                        "contains cmd.exe." % (name, att.id),
                        "Delete it or make it private.",
                        model_name="ir.attachment", res_id=att.id)
                elif (risky_name or risky_mime or magic_hit) and not att.public:
                    suspicious += 1
                    self._finding(
                        "medium", "Dangerous attachment (private): %s" % name,
                        "Attachment %s (id %s) looks dangerous but is not "
                        "publicly served." % (name, att.id),
                        "Verify it is expected.",
                        model_name="ir.attachment", res_id=att.id)
            offset += batch
        if offset >= limit:
            self._finding("info", "Attachment scan capped at %d records" % limit,
                          "Only the first %d attachments were scanned." % limit,
                          "Lower limit is intentional to bound scan time.")
        return True
