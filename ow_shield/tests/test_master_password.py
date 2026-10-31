"""Database master-password brute-force protection (round 14).

The master password guards the whole INSTANCE, so the tests pin the
documented production shape: a strict single-database dbfilter. That
makes the scope resolution (core/masterbp.scope_db) deterministic --
exactly one candidate -- and every counter/lockout row provably lives in
the served database. Counter rows are written on OW Shield's own pooled
connection (committed immediately, survives any request-transaction
rollback), so fixtures and reads use the same raw-connection pattern as
test_firewall_http.

Destructive db operations are never executed: every probe uses a WRONG
master password (odoo itself refuses before the operation), and the one
success-path test patches odoo.service.db.exp_change_admin_password --
the OPERATION -- while the real check_super verification still runs.

Each test maps to one acceptance point of the round-14 brief:
  01 failed checks count exactly once each       11 rollback keeps the record
  02 threshold starts the lockout, refuses early 12 expiry/reset = fresh window
  03 correct guess during lockout refused first  13 learning never locks
  04 manager-page GETs count nothing             14 pre-verification rejects free
  05 success clears only the master window       15 header spoofing/IPv6
  06 ordinary login success clears nothing       16 allow-list vs policy exempts
  07 HTTP/XML-RPC/JSON-RPC share one budget      17 no credentials, bounded mail
  08 database switching cannot reset the budget  18 idempotent + non-HTTP ok
  09 pre-database window is covered              19 degraded store = visible fail-open
  10 parallel workers, one atomic counter

Round-14 review follow-up (19.0.2.9.1, findings F01 + F02):
  21 reset RPC denied: plain internal user    26 learning lifts the ENFORCEMENT
  22 reset RPC denied: portal-like user          of an existing lockout (web)
  23 rejected calls leave counters intact     27 enforce: verifier untouched,
  24 authorized manager RPC reset works         even for a correct guess (RPC)
  25 reset touches ONLY the master ledger    28 learning: correct password runs
                                                   the real verifier (db RPC)
  F01 also pins the reset against the instance-wide scope store (the strict
  dbfilter rig proves scope == served db); 29 pins learning with an ACTIVE
  lockout: odoo's own denial kept, logged, never a new counter or lockout.
"""
import io
import json
import threading
import urllib.parse
from datetime import timedelta
from unittest.mock import patch

import odoo
import odoo.service.db as service_db
from odoo.exceptions import AccessDenied, AccessError
from odoo.service import security as session_security
from odoo.sql_db import db_connect
from odoo.tests import HttpCase, tagged
from odoo.tests.common import HOST as TEST_HOST
from odoo.tools.config import crypt_context

from ..core import masterbp, middleware
from ..core import recovery

SENTINEL = "Sup3r-S3cret-MASTER-9e1f"  # must never reach logs, hits or mail

# committed probe identities for the reset-authorization tests
PROBE_LOGINS = ("waf_probe_plain", "waf_probe_portal", "waf_probe_mgr")

# every source this class probes from. The sweeps below must remove ALL
# waf.hit rows these probes generate: the built-in dbmanager endpoint
# policy (and any rule) logs them in learning mode too -- kind=master
# filtering alone used to leak endpoint rows into later tests.
PROBE_IPS = tuple(
    ["203.0.113.%d" % i for i in range(161, 196)] + ["2001:db8:110::77"])


@tagged("post_install", "-at_install", "ow_shield")
class TestMasterPasswordBruteForce(HttpCase):

    @classmethod
    def _dbwrite(cls, fn):
        with db_connect(cls.env.cr.dbname).cursor() as cr:
            cr.rollback()
            env = api_environment(cr)
            result = fn(env)
            cr.commit()
        middleware.reset_caches()
        return result

    @classmethod
    def _master_sql(cls, sql, params=None):
        """Raw read/write against the SHARED master counter store.

        params stays None when absent: psycopg2 then performs NO
        percent-interpolation at all, so SQL may contain literal
        LIKE patterns such as '203.0.113.%'.
        """
        with db_connect(cls.scope).cursor() as cr:
            cr.rollback()
            cr.execute(sql, params)
            rows = cr.fetchall() if cr.description else []
            cr.commit()
            return rows

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # document-and-test the production shape: strict single-db
        # dbfilter -> one scope candidate for every worker
        cls._saved_dbfilter = odoo.tools.config["dbfilter"]
        odoo.tools.config["dbfilter"] = "^%s$" % cls.env.cr.dbname
        # the master-password-protected routes contain an "insecure
        # default" branch (master password literally "admin" -> the
        # submitted password is first re-verified through the RPC
        # change flow). Test a HARDENED instance: a set, non-default
        # master password, restored on teardown.
        cls._saved_admin_passwd = odoo.tools.config.options.get("admin_passwd")
        odoo.tools.config["admin_passwd"] = crypt_context.hash(
            "ow-shield-rig-instance-master-NOT-a-sentinel")
        middleware.reset_caches()

        def _setup(env):
            cfg = env["waf.config"].get_singleton()
            cfg.write({"mode": "enforce",
                       "trusted_proxies": "127.0.0.0/8, ::1",
                       "alert_email": "waf-alerts@example.com",
                       "master_bp_enabled": True,
                       "master_bp_max": 5,
                       "master_bp_window": 10,
                       "master_bp_lockout_minutes": 30})
            env["waf.rule"].search(
                [("code", "=", "waf-test-masterbp-probe")]
            ).unlink()
            env["waf.rule"].create({
                "code": "waf-test-masterbp-probe",
                "name": "Test block rule (master bp class)",
                "category": "test", "scope": "url",
                "pattern": "/waf-test-masterbp-probe",
                "action": "block", "severity": 5, "no_session": False})
            env["waf.master.hit"].search([]).unlink()
            # ALL hit kinds these probes ever produce (master, plus the
            # endpoint/rule rows the built-in dbmanager policy logs for
            # them in learning mode), incl. rows a crashed earlier run
            # may have left behind:
            env["waf.hit"].search([("kind", "=", "master")]).unlink()
            env["waf.hit"].search([("ip", "in", list(PROBE_IPS))]).unlink()
            env["waf.hit"].search([
                ("ip", "=", "127.0.0.1"),
                ("kind", "in", ("master", "endpoint")),
                ("path", "like", "/web/database%")]).unlink()
            env["waf.ip.entry"].search(
                [("network", "like", "203.0.113.%")]).unlink()
            env["mail.mail"].sudo().search(
                [("subject", "ilike", "OW Shield: master-password%")]
            ).unlink()
        cls._dbwrite(_setup)
        cls.scope = masterbp.scope_db()
        # the instance-wide scope rule must resolve to the served db
        # here; every master row below is asserted in cls.scope
        assert cls.scope == cls.env.cr.dbname, (
            "master counter scope must resolve to the served test db, "
            "got %r" % cls.scope)

    @classmethod
    def tearDownClass(cls):
        middleware.flush_hits()

        def _teardown(env):
            env["waf.master.hit"].search([]).unlink()
            env["waf.login.hit"].search([]).unlink()
            # every hit row this class's probes created, whatever kind
            # the engine filed it under (see PROBE_IPS note):
            env["waf.hit"].search([("kind", "=", "master")]).unlink()
            env["waf.hit"].search([("ip", "in", list(PROBE_IPS))]).unlink()
            env["waf.hit"].search([
                ("ip", "=", "127.0.0.1"),
                ("kind", "in", ("master", "endpoint")),
                ("path", "like", "/web/database%")]).unlink()
            env["waf.rule"].search(
                [("code", "=", "waf-test-masterbp-probe")]
            ).unlink()
            env["waf.ip.entry"].search(
                [("network", "like", "203.0.113.%")]).unlink()
            env["mail.mail"].sudo().search(
                [("subject", "ilike", "OW Shield: master-password%")]
            ).unlink()
            env["res.users"].search(
                [("login", "in", list(PROBE_LOGINS))]).unlink()
            env["waf.config"].get_singleton().write({"mode": "learning"})
        cls._dbwrite(_teardown)
        odoo.tools.config["dbfilter"] = cls._saved_dbfilter
        if cls._saved_admin_passwd:
            odoo.tools.config["admin_passwd"] = cls._saved_admin_passwd
        else:
            odoo.tools.config.options.pop("admin_passwd", None)
        middleware.reset_caches()
        super().tearDownClass()

    # ------------------------------------------------------------- probes

    def _headers(self, ip, **extra):
        return dict({"X-Real-IP": ip}, **extra)

    def _attempt(self, ip, pwd="WRONG-guess", **extra):
        """POST /web/database/change_password: dispatch_rpc verifies the
        master password BEFORE the (mocked-in-spirit) operation; wrong
        pwd never changes anything. csrf=False route, urlencoded body."""
        body = urllib.parse.urlencode(
            {"master_pwd": pwd, "master_pwd_new": "Never-used-N7"}).encode()
        return self.url_open("/web/database/change_password", data=body,
                             headers=self._headers(ip, **dict(
                                 extra, **{"Content-Type":
                                           "application/x-www-form-urlencoded"})))

    def _rows(self, net):
        return self._master_sql(
            "SELECT hits, blocked_until FROM waf_master_hit "
            "WHERE network = %s", (net,))

    def _events(self, ip):
        middleware.flush_hits()
        return self._dbread(lambda env: [
            (h.rule_name or "", h.blocked, h.category or "")
            for h in env["waf.hit"].search(
                [("kind", "=", "master"), ("ip", "=", ip)])
        ])

    def _dbread(cls_fn, fn):
        with db_connect(cls_fn.scope).cursor() as cr:
            cr.rollback()
            env = api_environment(cr)
            return fn(env)

    def _clear_probe(self, *ips):
        nets = [ip + ("/128" if ":" in ip else "/32") for ip in ips]
        self._master_sql("DELETE FROM waf_master_hit WHERE network = ANY(%s)",
                         (nets,))
        self._dbwrite(lambda env: (
            env["waf.hit"].search([("ip", "in", list(ips))]).unlink(),
            env["waf.ip.entry"].search(
                [("network", "in", nets)]).unlink(),
            env["mail.mail"].sudo().search(
                [("subject", "ilike", "OW Shield: master-password%")]
            ).unlink(),
        ))

    # -------------------------------------------------- probe identities
    #
    # The reset must be unreachable over REAL RPC, so the probe users
    # must be COMMITTED rows (request cursors never see the test
    # cursor's uncommitted writes) -- and the test cursor provably does
    # NOT see rows committed mid-class (REPEATABLE READ snapshot,
    # verified on this rig), so odoo's own HttpCase.authenticate, which
    # authenticates through self.env, cannot use them. _as_session
    # therefore builds the session the same way HttpCase.authenticate
    # does, except the uid/session_token come from a fresh raw
    # connection that CAN see the committed user. Request cursors then
    # verify the session server-side: route auth, session machinery and
    # service.model.call_kw dispatch (which performs NO model-ACL check
    # for custom methods -- the exact hole review F01 exploited) all
    # run for real. No shortcut around the access path.

    def _mk_users(self):
        def _w(env):
            grp_user = env.ref("base.group_user").id
            grp_mgr = env.ref("ow_shield.group_waf_manager").id
            plain = env["res.users"].create({
                "name": "WAF Probe Plain", "login": PROBE_LOGINS[0],
                "password": "Waf-Probe-2026!",
                "group_ids": [(6, 0, [grp_user])]})
            # the portal module is not installed in this rig; a user
            # with NO internal group is strictly weaker than a portal
            # user (portal grants no WAF access at all), so this probes
            # that acceptance point with an even lower-privilege caller.
            portal = env["res.users"].create({
                "name": "WAF Probe Portal-like", "login": PROBE_LOGINS[1],
                "password": "Waf-Probe-2026!",
                "group_ids": [(6, 0, [])]})
            mgr = env["res.users"].create({
                "name": "WAF Probe Manager", "login": PROBE_LOGINS[2],
                "password": "Waf-Probe-2026!",
                "group_ids": [(6, 0, [grp_user, grp_mgr])]})
            return {"plain": plain.id, "portal": portal.id,
                    "mgr": mgr.id,
                    "cfg": env["waf.config"].get_singleton().id}
        return self._dbwrite(_w)

    def _drop_users(self):
        self._dbwrite(lambda env: env["res.users"].search(
            [("login", "in", list(PROBE_LOGINS))]).unlink())

    def _as_session(self, uid, login):
        session = odoo.http.root.session_store.new()
        session.update(odoo.http.get_default_session(),
                       db=self.env.cr.dbname, _trace_disable=True)
        session.context["lang"] = odoo.http.DEFAULT_LANG
        session.uid = uid
        session.login = login
        with db_connect(self.env.cr.dbname).cursor() as cr:
            cr.rollback()
            env = api_environment(cr)
            session.session_token = session_security.compute_session_token(
                session, env)
            session.context = dict(
                env["res.users"].browse(uid).context_get())
        odoo.http.root.session_store.save(session)
        self.opener.cookies.set("session_id", session.sid, domain=TEST_HOST)
        return session

    def _reset_rpc(self, cfg_id):
        """POST the config button's EXACT RPC as the CURRENT session."""
        return self.url_open("/web/dataset/call_kw", data=json.dumps({
            "jsonrpc": "2.0", "method": "call", "id": 1,
            "params": {"model": "waf.config",
                       "method": "action_reset_master_counters",
                       "args": [[cfg_id]], "kwargs": {}}}).encode(),
            headers={"Content-Type": "application/json"})

    def _admin_reset(self):
        """Authorized reset over the admin session's real HTTP RPC (the
        same production path test_24 drives for a manager). Deliberately
        NOT a _dbwrite call: odoo's pool hands out THREAD-AFFINE
        connections, so the action's own store transaction would share
        -- and clobber -- a raw env cursor the test still holds. The
        action's fresh-transaction pattern requires owning the call."""
        aid = self._dbread(lambda env: env.ref("base.user_admin").id)
        cfg = self._dbread(lambda env: env["waf.config"].get_singleton().id)
        session = self._as_session(aid, "admin")
        try:
            r = self._reset_rpc(cfg)
        finally:
            odoo.http.root.session_store.delete(session)
        body = r.json()
        self.assertNotIn("error", body)
        return body["result"]

    def _assert_denied(self, r):
        body = r.json()
        self.assertIn("error", body, "unauthorized reset must not pass")
        err = json.dumps(body["error"])
        self.assertIn("odoo.exceptions.AccessError", err)
        self.assertIn("Only OW Shield managers", err)

    def _lockout_fixture(self, ip):
        """Active lockout via 5 real failed checks (enforce mode)."""
        for _ in range(5):
            self._attempt(ip)
        self.assertEqual(self._attempt(ip).status_code, 403)
        rows = self._rows(ip + "/32")
        self.assertEqual(len(rows), 1)
        self.assertGreater(rows[0][1], odoo.fields.Datetime.now())
        return rows

    # ----------------------------------------------------- acceptance pts

    def test_01_below_threshold_each_failure_counts_once(self):
        ip = "203.0.113.161"
        try:
            for i in range(4):
                r = self._attempt(ip, pwd="WRONG-%d" % i)
                # odoo's OWN denial behaviour preserved below the limit
                self.assertEqual(r.status_code, 200)
                self.assertIn(b"Master password update error", r.content)
                self.assertNotIn(b"OW Shield", r.content)
                rows = self._rows(ip + "/32")
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0][0], i + 1)   # exactly one bump each
                self.assertIsNone(rows[0][1])        # no lockout yet
            self.assertEqual(len(self._events(ip)), 4)
        finally:
            self._clear_probe(ip)

    def test_02_fifth_failure_locks_out_and_refuses_before_the_checker(self):
        ip = "203.0.113.162"
        try:
            for _ in range(5):
                self._attempt(ip)
            rows = self._rows(ip + "/32")
            self.assertEqual(len(rows), 1)
            hits, until = rows[0]
            self.assertEqual(hits, 0)     # window ENDED at the crossing
            self.assertIsNotNone(until)
            self.assertGreater(until, odoo.fields.Datetime.now())
            # the sixth request is refused BEFORE routing: the WAF page,
            # not odoo's "update error" page -- and nothing new counted
            r = self._attempt(ip, pwd="WRONG-after")
            self.assertEqual(r.status_code, 403)
            self.assertIn(b"OW Shield", r.content)
            self.assertTrue(r.headers.get("X-WAF-Ref"))
            self.assertEqual(self._rows(ip + "/32"), [(0, until)])
            names = [n for (n, _b, _c) in self._events(ip)]
            self.assertIn("master password lockout active", names)
            # failures 1..4 announce themselves as "failure N of 5"; the
            # FIFTH (crossing) failure is the distinct lockout-START
            # event, and the sixth request logged the refusal above:
            self.assertEqual(
                sum(1 for n in names
                    if n.startswith("master password failure")), 4)
            self.assertEqual(
                sum(1 for n in names
                    if n.startswith("master password lockout:")), 1)
        finally:
            self._clear_probe(ip)

    def test_03_correct_guess_during_lockout_never_reaches_verification(self):
        ip = "203.0.113.163"
        saved = odoo.tools.config.options.get("admin_passwd")
        try:
            ours = crypt_context.hash(SENTINEL)
            odoo.tools.config["admin_passwd"] = ours
            for _ in range(5):
                self._attempt(ip)
            r = self._attempt(ip, pwd=SENTINEL)   # CORRECT during lockout
            self.assertEqual(r.status_code, 403)
            self.assertIn(b"OW Shield", r.content)
            # the controller never ran: the stored hash is still exactly
            # the one THIS test installed (no re-hash, no change op)
            self.assertEqual(odoo.tools.config.options.get("admin_passwd"),
                             ours)
        finally:
            if saved:
                odoo.tools.config["admin_passwd"] = saved
            self._clear_probe(ip)

    def test_04_manager_page_gets_count_nothing(self):
        ip = "203.0.113.164"
        for _ in range(3):
            r = self.url_open("/web/database/manager",
                              headers=self._headers(ip))
            self.assertNotEqual(r.status_code, 403)
            self.url_open("/web/database/selector", headers=self._headers(ip))
        self.assertEqual(self._rows(ip + "/32"), [])
        self.assertEqual(self._events(ip), [])

    def test_05_correct_verification_clears_only_the_master_window(self):
        ip = "203.0.113.165"
        net = ip + "/32"
        saved = odoo.tools.config["admin_passwd"]
        try:
            odoo.tools.config["admin_passwd"] = crypt_context.hash(SENTINEL)
            self._attempt(ip)
            self._attempt(ip)
            self.assertEqual(self._rows(net)[0][0], 2)
            # other layers' counters for the SAME network stay untouched
            now = odoo.fields.Datetime.now()
            for table in ("waf_login_hit", "waf_rate_hit"):
                self._master_sql(
                    "INSERT INTO %s (network, window_start, hits) "
                    "VALUES (%%s, %%s, 3) ON CONFLICT (network) "
                    "DO UPDATE SET hits = 3" % table, (net, now))
            calls = []
            # mock the OPERATION only; the real check_super verification
            # still decides pass/fail (brief: never run real admin work)
            with patch.object(service_db, "exp_change_admin_password",
                              side_effect=lambda *a: calls.append(a) or True):
                r = self._attempt(ip, pwd=SENTINEL)
            self.assertEqual(calls, [("Never-used-N7",)])  # operation ran
            self.assertEqual(r.status_code, 200)
            self.assertNotIn(b"update error", r.content)
            self.assertEqual(self._rows(net), [])           # window cleared
            for table in ("waf_login_hit", "waf_rate_hit"):
                rows = self._master_sql(
                    "SELECT hits FROM %s WHERE network = %%s" % table, (net,))
                self.assertEqual(rows, [(3,)], table)
                self._master_sql("DELETE FROM %s WHERE network = %%s"
                                 % table, (net,))
        finally:
            odoo.tools.config["admin_passwd"] = saved
            self._clear_probe(ip)

    def test_06_ordinary_login_success_does_not_clear_master_window(self):
        ip = "203.0.113.166"
        try:
            self._attempt(ip)
            self._attempt(ip)
            self.assertEqual(self._rows(ip + "/32")[0][0], 2)
            # created on the TEST cursor: rolled back with the test, no
            # residue for the next suite run
            user = self.env["res.users"].create({
                "name": "WAF Master Test User",
                "login": "waf_master_test_user",
                "password": "Waf-Test-2026!",
            })
            auth = self.env["res.users"].with_context(ip=ip).authenticate(
                {"login": "waf_master_test_user",
                 "password": "Waf-Test-2026!", "type": "password"}, {})
            self.assertTrue(auth["uid"])
            # the master window is untouched by login-layer machinery
            self.assertEqual(self._rows(ip + "/32")[0][0], 2)
        finally:
            self._clear_probe(ip)

    def test_07_http_xmlrpc_jsonrpc_share_one_budget(self):
        ip = "203.0.113.167"
        try:
            self._attempt(ip)                                   # web route
            xml = (b"<?xml version=\"1.0\"?><methodCall>"
                   b"<methodName>change_admin_password</methodName><params>"
                   b"<param><value><string>WRONG-XML</string></value></param>"
                   b"<param><value><string>N7</string></value></param>"
                   b"</params></methodCall>")
            for path in ("/xmlrpc/db", "/xmlrpc/2/db"):
                self.url_open(path, data=xml, headers=self._headers(
                    ip, **{"Content-Type": "text/xml"}))
            self.url_open("/jsonrpc", data=json.dumps({
                "jsonrpc": "2.0", "method": "call", "id": 1,
                "params": {"service": "db",
                           "method": "change_admin_password",
                           "args": ["WRONG-JSON", "N7"]}}).encode(),
                headers=self._headers(
                    ip, **{"Content-Type": "application/json"}))
            rows = self._rows(ip + "/32")
            self.assertEqual(len(rows), 1, "one shared source budget")
            self.assertEqual(rows[0][0], 4)
            # ordinary object RPC must NOT count as db administration
            self.url_open("/jsonrpc", data=json.dumps({
                "jsonrpc": "2.0", "method": "call", "id": 2,
                "params": {"service": "object", "method": "authenticate",
                           "args": ["db", 1, "pw"]}}).encode(),
                headers=self._headers(
                    ip, **{"Content-Type": "application/json"}))
            self.assertEqual(self._rows(ip + "/32")[0][0], 4)
        finally:
            self._clear_probe(ip)

    def test_08_database_switching_cannot_reset_the_budget(self):
        ip = "203.0.113.168"
        try:
            self._attempt(ip, **{"X-Odoo-Database": self.env.cr.dbname})
            self._attempt(ip, **{"X-Odoo-Database": "totally-other-db"})
            r = self.url_open(
                "/web/database/change_password?db=yetanother",
                data=urllib.parse.urlencode(
                    {"master_pwd": "WRONG-q", "master_pwd_new": "N7"}
                ).encode(),
                headers=self._headers(
                    ip, **{"Content-Type": "application/x-www-form-urlencoded"}))
            self.assertNotEqual(r.status_code, 403)   # below threshold
            rows = self._rows(ip + "/32")
            self.assertEqual(len(rows), 1,
                             "one instance-wide counter, one budget")
            self.assertEqual(rows[0][0], 3)
        finally:
            self._clear_probe(ip)

    def test_09_protection_covers_the_pre_database_window(self):
        ip = "203.0.113.169"
        try:
            for _ in range(5):
                self._attempt(ip)
            # a request that resolves to NO database at all (a host that
            # matches no dbfilter): build the environ by hand and drive
            # the real guard(); the original app must never be reached
            called = []

            def _original(*a):
                called.append(True)
                return []
            body = b"master_pwd=WRONG-pre&master_pwd_new=N7"
            environ = {
                "REQUEST_METHOD": "POST",
                "PATH_INFO": "/web/database/drop",
                "QUERY_STRING": "",
                "REMOTE_ADDR": ip,
                "HTTP_HOST": "nowhere-outside-dbfilter.example",
                "CONTENT_TYPE": "application/x-www-form-urlencoded",
                "CONTENT_LENGTH": str(len(body)),
                "wsgi.input": __import__("io").BytesIO(body),
            }
            status = []
            handled = middleware.guard(None, environ,
                                       lambda s, h: status.append(s), _original)
            self.assertIsNotNone(handled)
            handled(environ, lambda s, h: status.append(s))
            self.assertEqual(status, ["403 Forbidden"])
            self.assertEqual(called, [], "refused before routing")
        finally:
            self._clear_probe(ip)

    def test_10_parallel_workers_share_one_atomic_counter(self):
        net = "203.0.113.170/32"
        snap = {"master": {"enabled": True, "max": 1000, "window": 600,
                           "minutes": 30}}
        ctx = {"db": self.scope, "snap": snap, "ip": "203.0.113.170",
               "network": net, "exempt": False, "counted": False}
        errors = []

        def _hammer():
            try:
                for _ in range(5):
                    res = masterbp._bump(ctx)
                    if res is None:
                        errors.append("bump failed")
            except Exception as exc:          # pragma: no cover
                errors.append(exc)
        threads = [threading.Thread(target=_hammer) for _ in range(8)]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            # 8 concurrent "workers" x 5 failures each: not one bump lost
            self.assertEqual(self._rows(net), [(40, None)])
        finally:
            self._master_sql("DELETE FROM waf_master_hit "
                             "WHERE network = %s", (net,))

    def test_11_request_rollback_does_not_discard_the_failure(self):
        ip = "203.0.113.171"
        try:
            # the jsonrpc dispatcher rolls back the REQUEST transaction
            # on the raised AccessDenied -- the counter lives on OW
            # Shield's own committed connection and must survive
            r = self.url_open("/jsonrpc", data=json.dumps({
                "jsonrpc": "2.0", "method": "call", "id": 1,
                "params": {"service": "db",
                           "method": "change_admin_password",
                           "args": ["WRONG-rollback", "N7"]}}).encode(),
                headers=self._headers(
                    ip, **{"Content-Type": "application/json"}))
            self.assertIn(b"AccessDenied", r.content)
            self.assertEqual(self._rows(ip + "/32"), [(1, None)])
        finally:
            self._clear_probe(ip)

    def test_12_expiry_and_manual_reset_give_a_fresh_window(self):
        ip = "203.0.113.172"
        try:
            for _ in range(5):
                self._attempt(ip)
            # simulate the lockout expiring
            self._master_sql(
                "UPDATE waf_master_hit SET blocked_until = %s "
                "WHERE network = %s",
                (odoo.fields.Datetime.now() - timedelta(minutes=1),
                 ip + "/32"))
            r = self._attempt(ip, pwd="WRONG-after-expiry")
            self.assertNotEqual(r.status_code, 403)      # allowed again...
            rows = self._rows(ip + "/32")
            self.assertLess(rows[0][1], odoo.fields.Datetime.now())
            self.assertEqual(rows[0][0], 1)              # ...fresh window
            # (a row parked AT the threshold would immediately reblock:
            # the crossing failure zeroes hits, so the post-expiry
            # window starts at 1, verified right here)
            for _ in range(4):                           # relock
                self._attempt(ip)
            self.assertIsNotNone(self._rows(ip + "/32")[0][1])
            self.assertEqual(self._attempt(ip).status_code, 403)
            # authorized manual reset (admin button): lifts the lockout
            # AND ends the window in one committed step
            self._admin_reset()
            self.assertEqual(self._rows(ip + "/32"), [])
            r = self._attempt(ip, pwd="WRONG-post-reset")
            self.assertNotEqual(r.status_code, 403)      # no stale reblock
            self.assertEqual(self._rows(ip + "/32")[0][0], 1)
        finally:
            self._clear_probe(ip)

    def test_13_recovery_link_lifts_only_that_network(self):
        ip = "203.0.113.173"
        try:
            for _ in range(5):
                self._attempt(ip)
            self.assertEqual(self._attempt(ip).status_code, 403)
            secret = self._dbread(lambda env: env["waf.config"]
                                  .get_singleton().recover_secret)
            token = recovery.make_token(secret, self.scope, ip + "/32")
            r = self.url_open("/waf/recover/" + token)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(self._rows(ip + "/32"), [])
            r = self._attempt(ip, pwd="WRONG-post-recovery")
            self.assertNotEqual(r.status_code, 403)     # fresh window
            self.assertEqual(self._rows(ip + "/32")[0][0], 1)
            # a token for a network with nothing to lift is inert, not fatal
            r = self.url_open("/waf/recover/" + token)
            self.assertEqual(r.status_code, 200)
        finally:
            self._clear_probe(ip)

    def test_14_learning_mode_records_but_never_locks(self):
        ip = "203.0.113.174"
        try:
            self._dbwrite(lambda env: env["waf.config"].get_singleton()
                          .write({"mode": "learning"}))
            for _ in range(8):
                r = self._attempt(ip)
                self.assertNotEqual(r.status_code, 403)
            self.assertEqual(self._rows(ip + "/32"), [])  # no counters at all
            events = self._events(ip)
            self.assertEqual(len(events), 8)
            self.assertTrue(all(not blocked for (_n, blocked, _c) in events))
            self.assertTrue(all(cat == "master" for (_n, _b, cat) in events))
        finally:
            self._dbwrite(lambda env: env["waf.config"].get_singleton()
                          .write({"mode": "enforce"}))
            self._clear_probe(ip)

    def test_15_requests_rejected_before_verification_cost_nothing(self):
        ip = "203.0.113.175"
        try:
            # missing required parameters: the controller never reaches
            # the password check at all
            r = self.url_open("/web/database/drop", data=b"",
                              headers=self._headers(
                                  ip, **{"Content-Type":
                                        "application/x-www-form-urlencoded"}))
            self.assertNotIn(b"OW Shield", r.content)
            # invalid database name: rejected BEFORE dispatch_rpc/check_super
            r = self.url_open("/web/database/create",
                              data=urllib.parse.urlencode({
                                  "master_pwd": "WRONG-before-check",
                                  "name": "not a valid db name!!",
                                  "lang": "en_US", "password": "x",
                                  "login": "admin"}).encode(),
                              headers=self._headers(
                                  ip, **{"Content-Type":
                                        "application/x-www-form-urlencoded"}))
            self.assertIn(b"naming issue", r.content)
            # db service method that verifies NO password (whitelisted)
            self.url_open("/jsonrpc", data=json.dumps({
                "jsonrpc": "2.0", "method": "call", "id": 1,
                "params": {"service": "db", "method": "db_exist",
                           "args": [self.env.cr.dbname]}}).encode(),
                headers=self._headers(
                    ip, **{"Content-Type": "application/json"}))
            self.assertEqual(self._rows(ip + "/32"), [])
            self.assertEqual(self._events(ip), [])
        finally:
            self._clear_probe(ip)

    def test_16_forwarded_header_spoofing_cannot_move_the_counter(self):
        untrusted = "203.0.113.176"
        v6 = "2001:db8:110::77"
        try:
            # with NO trusted proxies configured, forwarded headers are
            # noise: the socket peer owns the counter
            self._dbwrite(lambda env: env["waf.config"].get_singleton()
                          .write({"trusted_proxies": "10.0.0.0/8"}))
            for spoof in ("198.51.100.7", "198.51.100.8"):
                self._attempt(untrusted, **{"X-Real-IP": spoof})
            self.assertEqual(self._rows("198.51.100.7/32"), [])
            self.assertEqual(self._rows("198.51.100.8/32"), [])
            rows = self._rows("127.0.0.1/32")     # the socket peer pays
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0], 2)
            self._master_sql("DELETE FROM waf_master_hit "
                             "WHERE network = '127.0.0.1/32'")
            # with nginx-on-localhost (the documented default) IPv6 flows
            # attribute consistently (/128 host mask, iputil semantics)
            self._dbwrite(lambda env: env["waf.config"].get_singleton()
                          .write({"trusted_proxies": "127.0.0.0/8, ::1"}))
            self._attempt(v6)
            self.assertEqual([r[0] for r in self._rows(v6 + "/128")], [1])
        finally:
            self._clear_probe(untrusted, v6)
            self._master_sql("DELETE FROM waf_master_hit "
                             "WHERE network = '127.0.0.1/32'")

    def test_17_allow_entries_exempt_policy_exempts_do_not(self):
        allow_ip = "203.0.113.177"
        pol_ip = "203.0.113.178"
        probe_ip = "203.0.113.179"
        try:
            self._dbwrite(lambda env: env["waf.ip.entry"].create({
                "network": allow_ip + "/32", "kind": "allow",
                "source": "manual", "reason": "test allow"}))
            for _ in range(6):
                r = self._attempt(allow_ip)
                self.assertNotEqual(r.status_code, 403)
            self.assertEqual(self._rows(allow_ip + "/32"), [])  # exempt

            # the dbmanager POLICY blocks its route group...
            before = self._policy("waf-ep-dbmanager", action="block")
            try:
                r = self.url_open("/web/database/manager",
                                  headers=self._headers(probe_ip))
                self.assertEqual(r.status_code, 403)
                # ... unless the network is exempted FOR THAT POLICY.
                self._policy("waf-ep-dbmanager",
                             exempt_nets=pol_ip + "/32")
                r = self.url_open("/web/database/manager",
                                  headers=self._headers(pol_ip))
                self.assertNotEqual(r.status_code, 403)
                # but the policy exemption does NOT exempt the network
                # from master-password counting, and the lockout then
                # still applies (only to the db-management surface)
                for _ in range(5):
                    self._attempt(pol_ip)
                r = self._attempt(pol_ip)
                self.assertEqual(r.status_code, 403)
                # the endpoint-policy block and this gate hand out the
                # IDENTICAL OW Shield page: only the distinct master-
                # gate EVENTS prove which layer refused -- and that the
                # policy exemption never exempted the counting (4
                # "failure" events, 1 lockout started, 1 pre-routing
                # refusal):
                events = self._events(pol_ip)
                self.assertIn(("master password lockout active",
                               True, "master"), events)
                self.assertEqual(
                    sum(1 for n, _b, _c in events
                        if n.startswith("master password lockout:")), 1)
                self.assertEqual(
                    sum(1 for n, _b, _c in events
                        if n.startswith("master password failure")), 4)
                # unrelated protections keep working for exempt networks
                self.assertEqual(self.url_open(
                    "/waf-test-masterbp-probe",
                    headers=self._headers(pol_ip)).status_code, 403)
            finally:
                self._policy("waf-ep-dbmanager", **before)
        finally:
            self._clear_probe(allow_ip, pol_ip, probe_ip)

    def _policy(self, code, **vals):
        fields = ["action", "severity", "rate_max", "rate_window", "rate_key",
                  "rate_param", "rate_block_minutes", "ids_max",
                  "exempt_nets", "on_unverified"]

        def _w(env):
            pol = env["waf.endpoint.policy"].search([("code", "=", code)])
            self.assertTrue(pol, "built-in policy %s missing" % code)
            before = pol.read(fields)[0]
            pol.write(vals)
            return before
        return self._dbwrite(_w)

    def test_18_no_credentials_anywhere_and_mail_volume_bounded(self):
        ip = "203.0.113.180"
        try:
            for _ in range(5):
                self._attempt(ip, pwd=SENTINEL)
            self.assertEqual(self._attempt(ip, pwd=SENTINEL).status_code, 403)
            middleware.flush_hits()
            # materialise INSIDE the _dbread connection: recordsets
            # returned from it are dead the moment that connection
            # closes (lazy field fetch would hit a closed cursor)
            hit_rows = self._dbread(lambda env: [
                (h.path, h.query, h.ua, h.rule_name, h.category)
                for h in env["waf.hit"].search(
                    [("kind", "=", "master"), ("ip", "=", ip)])])
            self.assertTrue(hit_rows)
            for row in hit_rows:
                for value in row:
                    self.assertNotIn(SENTINEL, value or "")
            mails = self._dbread(lambda env: [
                (m.subject or "", m.body_html or "")
                for m in env["mail.mail"].sudo().search(
                    [("subject", "ilike",
                      "OW Shield: master-password%")])])
            self.assertEqual(len(mails), 1, "one alert per lockout")
            joined = " ".join(s + " " + b for s, b in mails)
            self.assertNotIn(SENTINEL, joined)
            self.assertNotIn("master_pwd", joined)
            self.assertIn(ip, joined)
            # re-lockout of the SAME network inside the window must not
            # spam a second mail (suppression + budget bound the volume)
            self._admin_reset()
            for _ in range(5):
                self._attempt(ip, pwd=SENTINEL)
            middleware.flush_hits()
            mails2 = self._dbread(lambda env: env["mail.mail"].sudo(
                ).search_count(
                [("subject", "ilike",
                  "OW Shield: master-password%")]))
            self.assertEqual(mails2, 1)
        finally:
            self._clear_probe(ip)

    def test_19_install_is_idempotent_and_non_http_calls_keep_working(self):
        # installing twice must not double-wrap
        masterbp.install()
        masterbp.install()
        wrapper = service_db.check_super
        self.assertTrue(getattr(wrapper, "_owshield_wrapped", False))
        saved = odoo.tools.config["admin_passwd"]
        try:
            odoo.tools.config["admin_passwd"] = crypt_context.hash(SENTINEL)
            # outside any HTTP request there is no attributable client:
            # odoo's own semantics preserved, no counters invented
            with self.assertRaises(AccessDenied):
                service_db.check_super("definitely-wrong")
            self.assertTrue(service_db.check_super(SENTINEL))
            self.assertTrue(service_db.exp_db_exist(self.env.cr.dbname))
            self.assertEqual(self._master_sql(
                "SELECT network FROM waf_master_hit "
                "WHERE network NOT LIKE '203.0.113.%' "
                "AND network NOT LIKE '2001:db8%' "
                "AND network != '127.0.0.1/32'"), [])
        finally:
            odoo.tools.config["admin_passwd"] = saved

    def test_20_degraded_store_is_visible_fail_open(self):
        ip = "203.0.113.181"
        try:
            # (a) no database hosts the counter store yet (fresh/racing
            # upgrade): documented fail-open, protection NOT claimed
            with patch.object(masterbp, "_store_ready", return_value=False):
                masterbp.reset_cache()
                r = self._attempt(ip, pwd="WRONG-degraded-scope")
                self.assertNotEqual(r.status_code, 403)   # odoo handles it
                self.assertEqual(self._rows(ip + "/32"), [])
            masterbp.reset_cache()

            # (b) the store itself is unreachable mid-attack: the request
            # still faces odoo's own denial (never our 500), and the gap
            # is an explicit coverage event, not silence. _store_ready is
            # pinned True on purpose: the scope must resolve NORMALLY so
            # the failing store is the thing under test (otherwise the
            # request never reaches the counting layer at all and no
            # coverage event would fire).
            class _Boom:
                def cursor(self, *a, **kw):
                    raise RuntimeError("counter store down")

            with patch.object(masterbp, "_store_ready", return_value=True), \
                    patch.object(masterbp, "db_connect",
                                 lambda *a, **kw: _Boom()):
                masterbp.reset_cache()
                r = self._attempt(ip, pwd="WRONG-degraded-store")
                self.assertEqual(r.status_code, 200)
                self.assertIn(b"Master password update error", r.content)
            events = self._events(ip)
            self.assertTrue(any("counter store unavailable" in n
                                for (n, _b, _c) in events),
                            "degraded coverage must be visible")
            self.assertTrue(any(cat == "coverage" for (_n, _b, cat) in events))
        finally:
            middleware.reset_caches()
            self._clear_probe(ip)

    # ------------------------------------- review F01: reset authorization

    def test_21_reset_rpc_denied_to_plain_internal_user(self):
        """Acceptance 1+3 (F01): a real authenticated internal user
        without the WAF-manager group drives the button's exact RPC;
        the method's own authorization check refuses it, and BOTH the
        counter rows and the live lockout are asserted unchanged (a
        response-shape check alone would not be enough)."""
        ip = "203.0.113.183"
        session = None
        try:
            before = self._lockout_fixture(ip)
            ids = self._mk_users()
            session = self._as_session(ids["plain"], PROBE_LOGINS[0])
            r = self._reset_rpc(ids["cfg"])
            self._assert_denied(r)
            self.assertEqual(self._rows(ip + "/32"), before)
            # the lockout is still ENFORCED at the live layer, not just
            # still present in the table:
            self.assertEqual(
                self._attempt(ip, pwd="WRONG-still-locked").status_code,
                403)
        finally:
            if session:
                odoo.http.root.session_store.delete(session)
            self._drop_users()
            self._clear_probe(ip)

    def test_22_reset_rpc_denied_to_portal_like_user(self):
        """Acceptance 2+3 (F01): same through a no-group (portal-like,
        strictly weaker than a portal user here) account."""
        ip = "203.0.113.184"
        session = None
        try:
            before = self._lockout_fixture(ip)
            ids = self._mk_users()
            session = self._as_session(ids["portal"], PROBE_LOGINS[1])
            r = self._reset_rpc(ids["cfg"])
            self._assert_denied(r)
            self.assertEqual(self._rows(ip + "/32"), before)
            self.assertEqual(
                self._attempt(ip, pwd="WRONG-still-locked").status_code,
                403)
        finally:
            if session:
                odoo.http.root.session_store.delete(session)
            self._drop_users()
            self._clear_probe(ip)

    def test_23_direct_method_calls_are_rejected_too(self):
        """Acceptance 3+6 (F01): bypassing RPC entirely (direct model-
        method invocation under each unauthorized user, on a fresh
        connection that sees the committed probe users) must fail the
        same way, leaving counters and lockout untouched."""
        ip = "203.0.113.185"
        try:
            before = self._lockout_fixture(ip)
            ids = self._mk_users()
            for key in ("plain", "portal"):
                with db_connect(self.env.cr.dbname).cursor() as cr:
                    cr.rollback()
                    # odoo 19 Environment has no with_user(): build the
                    # env directly under the probe user's uid.
                    env = odoo.api.Environment(cr, ids[key], {})
                    try:
                        env["waf.config"].browse(
                            ids["cfg"]).action_reset_master_counters()
                        self.fail("unauthorized %s reached the reset" % key)
                    except AccessError:
                        pass
                    cr.rollback()
            self.assertEqual(self._rows(ip + "/32"), before)
        finally:
            self._drop_users()
            self._clear_probe(ip)

    def test_24_authorized_waf_manager_reset_works_over_rpc(self):
        """Acceptance 4 (F01): a genuine WAF-manager account drives the
        same RPC and lifts the intended (instance-wide) counters: the
        table is empty afterwards AND the next attempt gets odoo's own
        refusal instead of our 403 -- a fresh window, proving the LIVE
        layer read the same store the button cleared."""
        ip = "203.0.113.186"
        session = None
        try:
            self._lockout_fixture(ip)
            ids = self._mk_users()
            session = self._as_session(ids["mgr"], PROBE_LOGINS[2])
            r = self._reset_rpc(ids["cfg"])
            body = r.json()
            self.assertNotIn("error", body)
            self.assertEqual(body["result"]["tag"], "display_notification")
            self.assertEqual(body["result"]["params"]["type"], "success")
            self.assertIn("instance-wide counter store",
                          body["result"]["params"]["message"])
            self.assertEqual(self._rows(ip + "/32"), [])
            r = self._attempt(ip, pwd="WRONG-after-mgr-reset")
            self.assertNotEqual(r.status_code, 403)
            self.assertEqual(self._rows(ip + "/32")[0][0], 1)
        finally:
            if session:
                odoo.http.root.session_store.delete(session)
            self._drop_users()
            self._clear_probe(ip)

    def test_25_authorized_reset_touches_only_the_master_ledger(self):
        """Acceptance 5 (F01): with a master lockout AND deliberately
        planted other-layer state for/around the same sources (login +
        rate counters, allow and deny entries, traffic-log rows), the
        reset clears exactly the master counter store and nothing else."""
        ip = "203.0.113.187"
        net = ip + "/32"
        allow_net, deny_net = "203.0.113.190/32", "203.0.113.191/32"
        try:
            self._lockout_fixture(ip)
            now = odoo.fields.Datetime.now()
            for table in ("waf_login_hit", "waf_rate_hit"):
                self._master_sql(
                    "INSERT INTO %s (network, window_start, hits) "
                    "VALUES (%%s, %%s, 3) ON CONFLICT (network) "
                    "DO UPDATE SET hits = 3" % table, (net, now))
            self._dbwrite(lambda env: (
                env["waf.ip.entry"].create({
                    "network": allow_net, "kind": "allow",
                    "source": "manual", "reason": "F01 fixture"}),
                env["waf.ip.entry"].create({
                    "network": deny_net, "kind": "deny",
                    "source": "manual", "reason": "F01 fixture"}),
            ))
            middleware.flush_hits()
            events_before = self._dbread(
                lambda env: env["waf.hit"].search_count(
                    [("kind", "=", "master"), ("ip", "=", ip)]))
            self.assertGreaterEqual(events_before, 5)  # failures+lockout+refusal
            # authorized path (admin: group_system implies the WAF
            # manager group; the plain-manager RPC path is test_24):
            self._admin_reset()
            self.assertEqual(self._rows(net), [])     # intended store cleared
            for table in ("waf_login_hit", "waf_rate_hit"):
                self.assertEqual(self._master_sql(
                    "SELECT hits FROM %s WHERE network = %%s" % table,
                    (net,)), [(3,)], table)
            self.assertEqual(self._dbread(lambda env: (
                env["waf.ip.entry"].search_count(
                    [("network", "in", [allow_net, deny_net])]),)), (2,))
            # the traffic log keeps every master-layer EVENT row (the
            # reset clears counters, never the audit trail; the admin's
            # own reset request may add new rows, so compare the
            # probe's own master events, not a global total):
            self.assertEqual(self._dbread(
                lambda env: env["waf.hit"].search_count(
                    [("kind", "=", "master"), ("ip", "=", ip)])),
                events_before)
        finally:
            for table in ("waf_login_hit", "waf_rate_hit"):
                self._master_sql("DELETE FROM %s WHERE network = %%s"
                                 % table, (net,))
            self._dbwrite(lambda env: env["waf.ip.entry"].search(
                [("network", "in", [allow_net, deny_net])]).unlink())
            self._clear_probe(ip)

    # ------------------------- review F02: learning vs existing lockouts

    def test_26_learning_lifts_enforcement_of_existing_lockout(self):
        """F02 acceptance 1, 2, 3, 5 (web route) and 7: an active
        enforce-mode lockout must stop refusing the moment the instance
        switches to learning -- the correct password reaches odoo's OWN
        verifier and passes (the OPERATION is stubbed, no db is built
        or changed). The lockout row itself is RETAINED (documented
        semantics), and switching back to enforce restores refusal
        until the original expiry."""
        ip = "203.0.113.192"
        saved = odoo.tools.config.options.get("admin_passwd")
        try:
            before = self._lockout_fixture(ip)      # enforce, active lockout
            odoo.tools.config["admin_passwd"] = crypt_context.hash(SENTINEL)
            self._dbwrite(lambda env: env["waf.config"].get_singleton()
                          .write({"mode": "learning"}))
            calls = []
            with patch.object(service_db, "exp_change_admin_password",
                              side_effect=lambda *a: calls.append(a) or True):
                r = self._attempt(ip, pwd=SENTINEL)  # CORRECT password
            self.assertEqual(
                calls, [("Never-used-N7",)],
                "odoo's verifier ran and passed; the operation itself was "
                "stubbed, so no real db-management change happened")
            self.assertEqual(r.status_code, 200)
            self.assertNotIn(b"OW Shield", r.content)
            self.assertNotIn(b"update error", r.content)
            # retention across the mode switch: not enforced, not cleared
            self.assertEqual(self._rows(ip + "/32"), before)
            # switching BACK to enforce while blocked_until is still in
            # the future restores refusal (documented, asserted here):
            self._dbwrite(lambda env: env["waf.config"].get_singleton()
                          .write({"mode": "enforce"}))
            r = self._attempt(ip, pwd="WRONG-back-to-enforce")
            self.assertEqual(r.status_code, 403)
            self.assertIn(b"OW Shield", r.content)
            self.assertEqual(self._rows(ip + "/32"), before)
        finally:
            odoo.tools.config["admin_passwd"] = saved
            self._dbwrite(lambda env: env["waf.config"].get_singleton()
                          .write({"mode": "enforce"}))
            self._clear_probe(ip)

    def test_27_enforce_lockout_wrapper_never_calls_the_verifier(self):
        """F02 acceptance 6, wrapper level: over the JSON-RPC db service
        (the path with NO WSGI gate -- refusal here is the wrapper's
        alone), an active lockout with the CORRECT password is refused
        before odoo's verifier: the stubbed operation (which a passing
        verification would reach) runs zero times, and the counters do
        not move."""
        ip = "203.0.113.193"
        saved = odoo.tools.config.options.get("admin_passwd")
        try:
            before = self._lockout_fixture(ip)
            odoo.tools.config["admin_passwd"] = crypt_context.hash(SENTINEL)
            calls = []
            with patch.object(service_db, "exp_change_admin_password",
                              side_effect=lambda *a: calls.append(a) or True):
                r = self.url_open("/jsonrpc", data=json.dumps({
                    "jsonrpc": "2.0", "method": "call", "id": 1,
                    "params": {"service": "db",
                               "method": "change_admin_password",
                               "args": [SENTINEL, "Never-used-N9"]}}).encode(),
                    headers=self._headers(
                        ip, **{"Content-Type": "application/json"}))
            self.assertIn(b"AccessDenied", r.content)
            self.assertEqual(
                calls, [], "the original verifier must never run while a "
                           "lockout is enforced -- even a correct guess")
            self.assertEqual(self._rows(ip + "/32"), before)
        finally:
            odoo.tools.config["admin_passwd"] = saved
            self._clear_probe(ip)

    def test_28_learning_correct_password_passes_real_verifier_rpc(self):
        """F02 acceptance 3 + 5 (JSON-RPC db-service path): with the
        SAME active lockout but the instance in learning mode, the
        correct master password is no longer refused by OW Shield's
        lockout -- it reaches and passes odoo's verifier, the stubbed
        db-service operation runs, and the JSON result is a plain
        success (no error envelope). The retained row is untouched."""
        ip = "203.0.113.194"
        saved = odoo.tools.config.options.get("admin_passwd")
        try:
            before = self._lockout_fixture(ip)
            odoo.tools.config["admin_passwd"] = crypt_context.hash(SENTINEL)
            self._dbwrite(lambda env: env["waf.config"].get_singleton()
                          .write({"mode": "learning"}))
            calls = []
            with patch.object(service_db, "exp_change_admin_password",
                              side_effect=lambda *a: calls.append(a) or True):
                r = self.url_open("/jsonrpc", data=json.dumps({
                    "jsonrpc": "2.0", "method": "call", "id": 1,
                    "params": {"service": "db",
                               "method": "change_admin_password",
                               "args": [SENTINEL, "Never-used-N8"]}}).encode(),
                    headers=self._headers(
                        ip, **{"Content-Type": "application/json"}))
            body = r.json()
            self.assertNotIn("error", body)
            self.assertEqual(calls, [("Never-used-N8",)],
                             "operation ran under odoo's own verdict only")
            self.assertEqual(self._rows(ip + "/32"), before)
        finally:
            odoo.tools.config["admin_passwd"] = saved
            self._dbwrite(lambda env: env["waf.config"].get_singleton()
                          .write({"mode": "enforce"}))
            self._clear_probe(ip)

    def test_29_learning_wrong_password_keeps_odoo_denial_no_lockout(self):
        """F02 acceptance 4: with an active (retained) lockout and the
        instance in learning mode, a WRONG password still runs through
        odoo's verifier and keeps odoo's own denial page -- it is
        logged per the learning policy but never counted, never extends
        or re-arms anything, and never becomes an OW Shield 403."""
        ip = "203.0.113.195"
        try:
            before = self._lockout_fixture(ip)      # enforce phase
            enforce_events = self._events(ip)
            self._dbwrite(lambda env: env["waf.config"].get_singleton()
                          .write({"mode": "learning"}))
            for pwd in ("WRONG-learn-active-1", "WRONG-learn-active-2"):
                r = self._attempt(ip, pwd=pwd)
                self.assertNotEqual(r.status_code, 403)
                self.assertIn(b"Master password update error", r.content)
            self.assertEqual(self._rows(ip + "/32"), before)  # no bump/extend
            events = self._events(ip)
            self.assertEqual(
                sum(1 for n, _b, _c in events
                    if n.endswith("(learning mode)")), 2)
            # the ONLY blocked event is the enforce-phase gate refusal;
            # learning traffic never enforces, so nothing was added:
            self.assertEqual(
                sum(1 for _n, b, _c in events if b),
                sum(1 for _n, b, _c in enforce_events if b))
        finally:
            self._dbwrite(lambda env: env["waf.config"].get_singleton()
                          .write({"mode": "enforce"}))
            self._clear_probe(ip)

    def test_30_master_query_string_never_stores_credentials(self):
        """Round 16 F03: the traffic log stored QUERY_STRING verbatim.

        masterbp never logs a BODY (the bodies carry credentials) but a
        credential smuggled into the query string of a db-manager call
        survived into waf.hit.query. Master hits run through the same
        storage-time redaction as every other waf.hit writer."""
        ip = "203.0.113.196"
        secret = "LeakCheck-Master-Q9"
        try:
            for _ in range(2):
                body = urllib.parse.urlencode(
                    {"master_pwd": "WRONG-guess",
                     "master_pwd_new": "Never-used-N7"}).encode()
                self.url_open(
                    "/web/database/change_password?master_pwd=" + secret,
                    data=body,
                    headers=self._headers(
                        ip, **{"Content-Type":
                               "application/x-www-form-urlencoded"}))
            self.assertTrue(self._events(ip),
                            "the failed master attempts are logged")
            queries = self._dbread(lambda env: [
                h.query or "" for h in env["waf.hit"].search(
                    [("kind", "=", "master"), ("ip", "=", ip)])])
            self.assertTrue(queries)
            for q in queries:
                self.assertNotIn(secret, q,
                                 "the query-string credential is stored "
                                 "as [redacted], never verbatim")
            self.assertIn("master_pwd=[redacted]", " ".join(queries),
                          "the parameter NAME stays visible")
        finally:
            self._clear_probe(ip)


def api_environment(cr):
    return odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
