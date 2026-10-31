{
    "name": "OW Shield – Web Application Firewall & Security",
    "summary": "Web application firewall & security suite: IP blocks, country blocks, attack "
               "rules, endpoint & rate policies, brute-force lockouts, active session control, "
               "live traffic log, scanner and grade.",
    "description": """
OW Shield - a web application firewall that runs
inside Odoo itself (no nginx rules required), plus a security scanner and
healthcheck with an A-F grade and PDF report.

Firewall
    * Enforces at the WSGI entry point, before routing/ORM: every request is
      inspected, including probes against routes with no database.
    * IP allow/deny lists (CIDR), country allow/deny (maxmind GeoLite2,
      optional), rate-based auto-lockouts whose attack and login-failure
      counters are shared by every worker process, so the threshold means
      the same on 1 or 10 workers.
    * Curated, versioned rule packs tuned for Odoo (SQLi, XSS, SSTI, path
      traversal, shellshock/log4j, scanner/bad-bot user agents, probes
      against /web/database/*, .env, .git, xmlrpc, ...).
    * Structured endpoint & rate policies, separate from attack
      signatures: whole route groups and RPC services (database manager,
      XML-RPC, JSON-RPC, /json) matched from the parsed request, plus
      rate limits per IP / user / IP+user / request target for signup,
      password-reset, report and list-export bursts. Counters are shared
      by every worker process.
    * Learning mode (log only) by default, per-rule toggles, live traffic
      log, block page with a signed "recovery link" so you can never lock
      yourself out.
    * Brute-force protection on top of the core login cooldown: per-IP
      failure counters shared across workers, with automatic IP lockout
      and recovery email; a successful login clears the counter.
    * Database master-password brute-force protection: every confirmed
      failed master-password check (web manager, XML-RPC, JSON-RPC)
      counts against one instance-wide per-source counter; past the
      threshold, database management is refused to that source for a
      cooling-off period - never website/backend access - with a signed
      recovery link and an admin reset. The Nth failure refuses even a
      correct guess before verifying it.

Session control
    * Active sessions view: every logged-in user, device, browser and
      (proxy-corrected) IP, with a one-click force-logout for hijacked or
      stale sessions.
    * Optional idle-session timeout: sessions whose recorded activity is N
      minutes old or more (>= 60; Odoo records activity hourly, so the real
      logout lands between N and N+60 idle minutes) are force-logged-out by
      a 15-minute scheduled action (default: off). This affects only the
      database you enable it in; Odoo's server-global session-lifetime
      parameter (shared by every database) is never written, and a value
      you set there yourself is never removed either.

Scanner / healthcheck
    * Read-only audit of server & app configuration (list_db, master
      password, dbfilter, demo data, admin hygiene, 2FA coverage, API keys,
      server actions / automations / scheduled actions with Python code,
      mail transports, base URL, ...).
    * Optional file scan of custom addons for webshell/obfuscation
      signatures, and attachment scan for suspicious uploads.
    * Findings with severity + recommendations, 0-100 score, A-F grade,
      shareable PDF report, history.

Design notes
    * In-process blocking only: no nginx/snippet export, no shell calls.
    * Fail-open: a WAF error never breaks your site (logged at error level).
    * Every worker serves protected from its very first request when the
      databases are listed in the server option `db_name = <dbname>` (Odoo
      then preloads each worker's registry at boot; without it, lazy
      registry loading leaves a brief unguarded window after each restart).
    * No data ever leaves your server (rule updates are bundled, opt-in).
    * The traffic log is sanitised at write time: credential values in
      stored query strings (password, access token, ...) are replaced by
      [redacted] on their way into the row, before anything is stored.
    * Client IP is resolved from X-Forwarded-For only when the socket peer
      is a configured trusted proxy (default: localhost), so spoofed XFF
      headers from the internet are ignored.
""",
    "version": "19.0.1.0.0",
    "category": "Security",
    "author": "Openworx <info@openworx.nl>",
    "website": "https://www.openworx.nl",
    "license": "LGPL-3",
    "images": [
        "static/description/screenshot_dashboard.png",
        "static/description/screenshot_trafficlog.png",
        "static/description/screenshot_sessions.png",
        "static/description/screenshot_scan.png",
        "static/description/screenshot_pdf.png",
        "static/description/screenshot_blockpage.png",
    ],
    "image": "static/description/screenshot_dashboard.png",
    "depends": ["base", "mail"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/crons.xml",
        "data/ruleset_load.xml",
        "views/waf_hit_views.xml",
        "views/waf_rule_views.xml",
        "views/waf_endpoint_policy_views.xml",
        "views/waf_exception_views.xml",
        "views/waf_ip_entry_views.xml",
        "views/waf_scan_views.xml",
        "views/waf_finding_views.xml",
        "views/res_device_views.xml",
        "views/waf_config_views.xml",
        "views/waf_dashboard_views.xml",
        "views/menus.xml",
        "report/waf_report.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "ow_shield/static/src/scss/dashboard.scss",
            "ow_shield/static/src/scss/config.scss",
            "ow_shield/static/src/js/geo_paths.js",
            "ow_shield/static/src/js/dashboard.js",
            "ow_shield/static/src/js/dashboard.xml",
        ],
    },
    "post_init_hook": "post_init_hook",
    "application": True,
    "installable": True,
}
