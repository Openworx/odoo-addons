"""Pure evaluation engine (no ORM imports: it runs on the hot request path).

A *snapshot* is a plain dict compiled from DB rows, rebuilt at most every
few seconds per worker process. Evaluate takes a normalized request dict
and returns a decision dict or None.

Three protection layers feed one decision stream:
* signature rules (waf.rule): regex over normalized request parts;
* endpoint policies (waf.endpoint.policy): structured route-group / RPC
  service / rate-limit matching -- matched against a PARSED view of the
  request, never a regex over the serialized body, so incidental business
  text cannot fire an operation policy;
* targeted exceptions (waf.exception): downgrade a rule or policy decision
  to log-only, scoped by rule/policy, route, method and parsed RPC field,
  with reason and expiry.
"""
import hashlib
import ipaddress
import re
import time
from collections import deque

from . import iputil
from .normalize import normalize

SCOPE_PATH = "path"
SCOPE_QUERY = "query"
SCOPE_UA = "ua"
SCOPE_HEADER = "header"
SCOPE_BODY = "body"
BODY_SCOPES = (SCOPE_QUERY, SCOPE_BODY)  # obey the no_session exemption

# hard ceiling on the worker-local fallback counter map (per database):
# unbounded distinct-source tracking is itself a resource attack surface.
COUNTER_MAX_KEYS = 4096

# body_scan_max clamp window. Duplicated from waf_config.BODY_SCAN_MIN/
# BODY_SCAN_MAX on purpose: core must not import models (the model
# imports core). build_snapshot clamps so a stale or raw-SQL-written row
# can never hand the JSON chunker a window at or below its overlap --
# which would make it emit one candidate per character (the chunker
# itself is hardened independently). Mirror both pairs together.
BODY_SCAN_MIN = 4096
BODY_SCAN_MAX = 1024 * 1024


class RuleCompileError(ValueError):
    pass


def compile_rule(rule_row):
    """rule_row: dict with id,name,category,scope,pattern,action,severity,no_session,case_sensitive"""
    flags = 0 if rule_row.get("case_sensitive") else re.IGNORECASE
    try:
        regex = re.compile(rule_row["pattern"], flags)
    except re.error as e:
        raise RuleCompileError(f"{rule_row.get('code', rule_row.get('id'))}: {e}") from e
    return {
        "id": rule_row["id"],
        "code": rule_row.get("code"),
        "name": rule_row.get("name") or "rule",
        "category": rule_row.get("category") or "custom",
        "scope": rule_row["scope"],
        "regex": regex,
        "pattern": rule_row["pattern"],
        "action": rule_row.get("action") or "block",
        "severity": int(rule_row.get("severity") or 3),
        "no_session": bool(rule_row.get("no_session")),
    }


def _csv_set(value):
    if not value:
        return None
    out = {v.strip().lower() for v in str(value).split(",") if v.strip()}
    return out or None


def _methods_set(value):
    """HTTP methods are compared against the uppercased request method."""
    if not value:
        return None
    out = {v.strip().upper() for v in str(value).split(",") if v.strip()}
    return out or None


def compile_policy(row):
    """row: dict with id,code,name,category,route_re,methods,rpc_service,
    rpc_model,rpc_method,ids_max,action,severity,exempt_nets,rate_max,
    rate_window,rate_key,rate_param,rate_block_minutes,match_unverified,
    apply_pre_db"""
    try:
        regex = re.compile(row["route_re"], re.IGNORECASE)
    except re.error as e:
        raise RuleCompileError(f"policy {row.get('code', row.get('id'))}: {e}") from e
    svc = _csv_set(row.get("rpc_service"))
    mdl = _csv_set(row.get("rpc_model"))
    meth = _csv_set(row.get("rpc_method"))
    rpc = {"service": svc, "model": mdl, "method": meth} \
        if (svc or mdl or meth) else None
    rate = None
    try:
        rate_max = int(row.get("rate_max") or 0)
    except (TypeError, ValueError):
        rate_max = 0
    if rate_max > 0:
        try:
            window = int(row.get("rate_window") or 10)
        except (TypeError, ValueError):
            window = 10
        try:
            minutes = int(row.get("rate_block_minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0
        rate = {
            "max": rate_max,
            "window": max(1, window) * 60,
            # NB "key_by" (the keying MODE), not "key": the decision's
            # rate dict carries the computed counter key under "key", so
            # a name collision here would clobber it.
            "key_by": row.get("rate_key") or "ip",
            "param": (row.get("rate_param") or "login")[:64],
            "minutes": max(0, minutes),
        }
    return {
        "id": row["id"],
        "code": row.get("code"),
        "name": row.get("name") or "policy",
        "category": row.get("category") or "endpoint",
        "regex": regex,
        "methods": _methods_set(row.get("methods")),
        "rpc": rpc,
        # what to do when the request is on this policy's route group but
        # its operation could NOT be parsed (body over the RPC capture
        # cap): "off" = the policy cannot fire (classic fail-open),
        # "match" = treat the hidden operation as matching (fail closed).
        "on_unverified": row.get("on_unverified") or "off",
        "ids_max": int(row.get("ids_max") or 0),
        "action": row.get("action") or "log",
        "severity": int(row.get("severity") or 4),
        "exempt": iputil.parse_cidrs(row.get("exempt_nets") or ""),
        "rate": rate,
        "apply_pre_db": bool(row.get("apply_pre_db")),
    }


def compile_exception(row):
    """row: dict with id,rule_id,rule_code,policy_id,policy_code,route_re,
    methods,param,reason,expires (epoch float or None)"""
    regex = None
    if row.get("route_re"):
        try:
            regex = re.compile(row["route_re"], re.IGNORECASE)
        except re.error as e:
            raise RuleCompileError(f"exception {row.get('id')}: {e}") from e
    param = None
    raw = (row.get("param") or "").strip()
    if raw and "=" in raw:
        field, _, value = raw.partition("=")
        param = (field.strip().lower(), value.strip().lower())
    return {
        "id": row["id"],
        "rule_id": row.get("rule_id"),
        "rule_code": row.get("rule_code"),
        "policy_id": row.get("policy_id"),
        "policy_code": row.get("policy_code"),
        "route_re": regex,
        "methods": _methods_set(row.get("methods")),
        "param": param,
        "reason": (row.get("reason") or "")[:200],
        "expires": row.get("expires"),
    }


def build_snapshot(config, rules, ip_entries, policies=(), exceptions=()):
    """Assemble the fast-path snapshot. Failures upstream must fail-open."""
    trusted = iputil.parse_cidrs(config.get("trusted_proxies"))
    allow, deny = [], []
    for row in ip_entries:
        try:
            net = ipaddress.ip_network(row["network"], strict=False)
        except ValueError:
            continue
        expires = row.get("expires")
        if expires and time.time() > expires:
            continue
        item = (net, row.get("id"), row.get("reason") or "")
        (allow if row["kind"] == "allow" else deny).append(item)
    # specific prefixes first
    allow.sort(key=lambda t: t[0].prefixlen, reverse=True)
    deny.sort(key=lambda t: t[0].prefixlen, reverse=True)

    compiled = {}
    for row in rules:
        try:
            rule = compile_rule(row)
        except RuleCompileError:
            continue  # rules are validated on write; skip broken at runtime
        compiled.setdefault(rule["scope"], []).append(rule)
    for group in compiled.values():
        group.sort(key=lambda r: (-r["severity"], r["id"]))

    compiled_policies = []
    for row in policies:
        try:
            pol = compile_policy(row)
        except RuleCompileError:
            continue  # validated on write; skip broken at runtime
        compiled_policies.append(pol)
    compiled_policies.sort(key=lambda p: (-p["severity"], p["id"]))

    compiled_exceptions = []
    now = time.time()
    for row in exceptions:
        try:
            exc = compile_exception(row)
        except RuleCompileError:
            continue
        if exc["expires"] is not None and exc["expires"] < now:
            continue  # expired here, and re-checked at match time anyway
        compiled_exceptions.append(exc)

    countries = {c.upper() for c in (config.get("country_codes") or []) if c}
    # Precomputed view for exempt (authenticated internal) requests: the
    # no_session rules are dropped BEFORE matching, so the exemption also
    # skips the regex CPU cost, not just the verdict.
    rules_ns = {scope: [r for r in group if not r["no_session"]]
                for scope, group in compiled.items()}
    return {
        "mode": config.get("mode") or "learning",
        "trusted_networks": trusted,
        "allow_entries": allow,
        "deny_entries": deny,
        "country_mode": config.get("country_mode") or "off",
        "country_codes": countries,
        "rules": compiled,
        "rules_no_exempt": rules_ns,
        "policies": compiled_policies,
        "exceptions": compiled_exceptions,
        # middleware parses the structured RPC/form context ONLY when some
        # policy actually needs it: clean installs and simple rule setups
        # pay nothing on the hot path.
        "wants_rpc": any(p["rpc"] for p in compiled_policies)
                     or any(p["rate"] and p["rate"]["key_by"] == "target"
                            for p in compiled_policies),
        "wants_ids": any(p["ids_max"] for p in compiled_policies),
        # clamped here as defense in depth: the ORM bounds the field,
        # but a row written before the constraint (or by raw SQL) still
        # reaches this dict, and both failure directions (chunker step
        # -> 1, absurd per-request buffering) bite in the WSGI hot path.
        # 0/None keep the default.
        "body_scan_max": min(max(
            int(config.get("body_scan_max") or 65536), BODY_SCAN_MIN),
            BODY_SCAN_MAX),
        "auto_lockout": {
            "enabled": bool(config.get("auto_lockout_enabled")),
            "max": int(config.get("auto_lockout_max") or 10),
            "window": int(config.get("auto_lockout_window") or 10) * 60,
            "minutes": int(config.get("auto_lockout_minutes") or 60),
        },
        # Master-password brute-force protection defaults live here, not
        # in the ORM only: a database whose schema predates the feature
        # yields an empty config["master"] from the raw snapshot read,
        # and its traffic must then use the documented defaults (enabled,
        # 5 failures / 10 min / 30 min), never "feature off". masterbp.py
        # is the only consumer.
        "master": {
            "enabled": bool(config.get("master", {}).get("enabled", True)),
            "max": max(1, int(config.get("master", {}).get("max") or 5)),
            "window": max(60, int(config.get("master", {}).get("window")
                                 or 600)),
            "minutes": max(1, int(config.get("master", {}).get("minutes")
                                  or 30)),
        },
        "recover_secret": config.get("recover_secret") or "",
    }


def _matching_rules(rules, candidates):
    """Every rule in a scope that matches at least one candidate text.

    All scopes/rules are evaluated and the strongest match wins (see
    evaluate), so a low-severity log rule can never shadow a block rule.
    """
    out = []
    for rule in rules:
        for text in candidates:
            if text and rule["regex"].search(text):
                out.append(rule)
                break
    return out


def _matching_policies(snap, req, ip_obj, norm_path):
    """ALL endpoint policies covering this request.

    A route can belong to several policy groups at once (the reset page
    is flooded per-IP *and* per-account; /xmlrpc/db belongs to the db
    service policy and to the whole-XML-RPC policy). Returning only the
    first match starves the other group's counters and can pick a
    weaker verdict.
    """
    if req.get("static"):
        return []
    out = []
    for pol in snap["policies"]:
        if not pol["regex"].search(norm_path):
            continue
        if pol["methods"] and req["method"].upper() not in pol["methods"]:
            continue
        if ip_obj is not None and pol["exempt"] \
                and iputil.in_any(ip_obj, pol["exempt"]):
            continue  # explicit trusted-management-network exception
        if pol["ids_max"] and not pol["rate"]:
            # a document-id limit without a rate limit is the FIRE
            # threshold for this policy: at/below the limit the surface is
            # ordinary traffic (no decision at all); above it the policy
            # acts. A policy with NEITHER limit is unconditional by design
            # (that is what the db-manager surface policy is). Missing
            # evidence (ids not extractable) never fires: fail open.
            n = req.get("ids_count")
            if n is None or n <= pol["ids_max"]:
                continue
        if pol["rpc"]:
            # structured matchers need the PARSED request context; without
            # it the policy cannot fire (never block on missing evidence).
            # One exception: when the middleware flagged the operation as
            # UNVERIFIABLE -- a JSON-RPC-family body too large for even
            # the RPC parse cap, so the app WILL see an operation we
            # cannot -- a policy with on_unverified="match" treats the
            # hidden fields as matching.
            rpc_ctx = req.get("rpc") or {}
            fields = rpc_ctx.get("fields") or {}
            hidden = (bool(rpc_ctx.get("unverified"))
                      and pol["on_unverified"] == "match")

            if pol["rpc"]["service"]:
                have = fields.get("service", "")
                if have:
                    if have not in pol["rpc"]["service"]:
                        continue
                elif not hidden:
                    continue
            if pol["rpc"]["model"]:
                have = ({fields.get("model", ""),
                         fields.get("kw_model", "")}
                        & pol["rpc"]["model"])
                if not have and not hidden:
                    continue
            if pol["rpc"]["method"]:
                have = ({fields.get("method", ""),
                         fields.get("kw_method", "")}
                        & pol["rpc"]["method"])
                if not have and not hidden:
                    continue
        out.append(pol)
    return out


def _rate_key(pol, req, ip_obj):
    """SUBJECT part of the counter key for a rate-limited policy (the
    caller namespaces it per policy -- see evaluate). The 'user' component
    is the uid of a VERIFIED server-side session (req["uid"], set by the
    middleware from odoo's session store) -- a session cookie or an
    Authorization header alone never authenticates at this layer."""
    rate = pol["rate"]
    uid = req.get("uid")
    if rate["key_by"] == "user":
        return f"u{uid}" if uid else (str(ip_obj) if ip_obj else None)
    if rate["key_by"] == "ip_user":
        if ip_obj and uid:
            return f"{ip_obj}|u{uid}"
        if ip_obj:
            return str(ip_obj)
        return f"u{uid}" if uid else None
    if rate["key_by"] == "target":
        target = ((req.get("rpc") or {}).get("fields") or
                  {}).get(rate["param"])
        if not target:
            return None
        # hashed so a sensitive identifier (the login being reset) never
        # lands verbatim in the counter table either; display masks it
        digest = hashlib.sha256(
            f"{rate['param']}:{target}".encode("utf-8", "replace"))
        return f"t:{rate['param']}:{digest.hexdigest()[:24]}"
    return str(ip_obj) if ip_obj else None


def _policy_probe(p):
    """Minimal decision view of a policy, for per-policy exception scoping."""
    return {"kind": "endpoint", "action": p["action"],
            "severity": p["severity"], "rule_id": None,
            "rule_code": p["code"], "rule_name": p["name"],
            "policy_id": p["id"]}


def _matching_exception(snap, decision, norm_path, req):
    """Live targeted exception covering this decision, or None."""
    if decision["kind"] not in ("rule", "endpoint"):
        return None
    now = time.time()
    for exc in snap["exceptions"]:
        if exc["expires"] is not None and exc["expires"] < now:
            continue
        target_rule = decision["kind"] == "rule"
        target_pol = decision["kind"] == "endpoint"
        if not ((target_rule and (
                    (exc["rule_id"] is not None
                     and exc["rule_id"] == decision.get("rule_id"))
                    or (exc["rule_code"]
                        and exc["rule_code"] == decision.get("rule_code"))))
                or (target_pol and (
                    (exc["policy_id"] is not None
                     and exc["policy_id"] == decision.get("policy_id"))
                    or (exc["policy_code"]
                        and exc["policy_code"] == decision.get("rule_code"))))):
            continue
        if exc["route_re"] and not exc["route_re"].search(norm_path):
            continue
        if exc["methods"] and req["method"].upper() not in exc["methods"]:
            continue
        if exc["param"]:
            field, value = exc["param"]
            fields = (req.get("rpc") or {}).get("fields") or {}
            if fields.get(field, "") != value:
                continue
        return exc
    return None


def evaluate(snap, req, counters=None, country=None):
    """Evaluate one request.

    req keys: ip, path, query, ua, headers_blob, body, method, has_session
    plus structured context (optional, middleware-extracted): rpc
    {"fields": {...}}, uid (verified session user id), target (rate-limit
    target param, sanitized), ids_count (parsed document-id count).
    Returns decision dict or None.
    """
    ip_obj = iputil.parse_ip(req["ip"]) if req.get("ip") else None
    now = time.time()

    if ip_obj is not None:
        for net, _entry_id, _reason in snap["allow_entries"]:
            if ip_obj in net:
                return None  # allowlisted: skip evaluation entirely (not logged)
        for net, entry_id, reason in snap["deny_entries"]:
            if ip_obj in net:
                return {
                    "kind": "ip", "action": "block", "severity": 5,
                    "rule_name": f"blocked ip ({reason})" if reason else "blocked ip",
                    "rule_id": None,
                }

        # country gate (only meaningful when a geo database is configured)
        mode = snap["country_mode"]
        if mode != "off" and country:
            code = country.upper()
            if mode == "deny" and code in snap["country_codes"]:
                return {
                    "kind": "country", "action": "block", "severity": 4,
                    "rule_name": f"country {code} denied", "rule_id": None,
                }
            if mode == "allow" and code not in snap["country_codes"]:
                return {
                    "kind": "country", "action": "block", "severity": 4,
                    "rule_name": f"country {code} not allowed", "rule_id": None,
                }

    has_session = req.get("has_session")
    is_static = req.get("static")
    norm_path = req.get("norm_path") or normalize(req.get("path") or "")
    norm_query = req.get("norm_query") or normalize(req.get("query") or "", decode_plus=True)
    norm_ua = req.get("norm_ua") or normalize(req.get("ua") or "")
    norm_body = req.get("norm_body")
    # middleware may hand several body texts (raw + parser-parity
    # decoded forms, see _read_body); rules match any of them
    body_cands = (norm_body if isinstance(norm_body, list)
                  else [norm_body] if norm_body else [])

    matches = []
    for scope, cands in (
            ("ua", [norm_ua]),
            ("url", [norm_path]),
            ("query", [norm_query]),
            ("header", [req.get("headers_blob") or ""]),
            ("body", body_cands)):
        if is_static and scope in ("query", "header", "body"):
            continue
        view = snap["rules"]
        if has_session and scope in BODY_SCOPES:
            # exempt: verified authenticated internal user (middleware
            # checked uid in odoo's server-side session store) -- the
            # precomputed view drops no_session rules BEFORE matching,
            # so the exemption skips the regex cost too, not just the
            # verdict
            view = snap["rules_no_exempt"]
        for rule in _matching_rules(view.get(scope, ()), cands):
            matches.append(rule)

    rule_decision = None
    if matches:
        # Targeted exceptions apply to EACH matching rule BEFORE the
        # combine: an exception scoped to one rule may only downgrade
        # THAT rule -- every other matching rule keeps its own verdict.
        # Applying the exception to the merged (strongest-wins) decision
        # would let an exception on one rule silence an unrelated block
        # from another rule on the same request.
        cands = []
        for r in matches:
            d = {
                "kind": "rule",
                "action": "block" if r["action"] in ("block", "auto")
                          else "log",
                "severity": r["severity"],
                "rule_id": r["id"],
                "rule_code": r["code"],
                "rule_name": r["name"],
                "category": r["category"],
            }
            exc = (_matching_exception(snap, d, norm_path, req)
                   if snap["exceptions"] else None)
            if exc is not None:
                # downgraded, never hidden: the log says why
                d["action"] = "log"
                d["rule_name"] = (
                    f"{r['name']} [excepted: {exc['reason']}]")[:250]
                d["excepted_by"] = exc["id"]
            cands.append(d)
        # strongest action wins: a real (not excepted) block outranks log,
        # then higher severity
        rule_decision = max(cands,
                            key=lambda d: (d["action"] == "block",
                                           d["severity"]))

    # ------------------------------------------------ endpoint policy layer
    pols = _matching_policies(snap, req, ip_obj, norm_path) if not is_static \
        else []
    pol_decision = None
    if pols:
        # same per-subject exception rule as the rules layer: an
        # exception scoped to one policy downgrades only that policy and
        # stops only ITS counters; other covering policies keep their
        # verdict and their counting.
        pairs = [(p,
                  _matching_exception(snap, _policy_probe(p), norm_path, req)
                  if snap["exceptions"] else None)
                 for p in pols]
        # the strongest verdict across ALL covering policies names the
        # decision (block -- but only if not excepted -- then severity,
        # then lower id for pack determinism)
        strongest, exc = max(
            pairs, key=lambda t: (t[1] is None
                                  and t[0]["action"] == "block",
                                  t[0]["severity"], -t[0]["id"]))
        pol_decision = {
            "kind": "endpoint",
            "action": ("log" if exc is not None
                       or strongest["action"] == "log" else "block"),
            "severity": strongest["severity"],
            "rule_id": None,
            "rule_code": strongest["code"],
            "rule_name": strongest["name"],
            "category": strongest["category"],
            "policy_id": strongest["id"],
        }
        if exc is not None:
            pol_decision["rule_name"] = (
                f"{strongest['name']} [excepted: {exc['reason']}]")[:250]
            pol_decision["excepted_by"] = exc["id"]
            # a downgraded match stays visible even when every covering
            # policy is rate-gated (downgrade, never hide)
            pol_decision["force_log"] = True
        if strongest["ids_max"]:
            n = req.get("ids_count")
            if n is not None and n > strongest["ids_max"]:
                # the document-id limit is exceeded: say so in the traffic
                # log (the policy's own action still decides log vs block)
                pol_decision["severity"] = max(pol_decision["severity"], 4)
                pol_decision["rule_name"] = (
                    f"{pol_decision['rule_name']} ({n} document ids > "
                    f"limit {strongest['ids_max']})")[:250]
                pol_decision["force_log"] = True
        # the block verdict of an UNTHRESHOLDED policy (no rate of its
        # own; an ids-only policy has already passed its gate in
        # _matching_policies) fires on every matching request. An
        # overlapping monitoring policy's counters must never turn it
        # into a threshold-dependent decision. "unthrottled" marks the
        # same shape for the logging side: a policy that acts on the
        # plain MATCH (not a counter threshold) is itself the signal,
        # so its matches stay visible; only purely rate-counted traffic
        # below its limit stays unlogged (the traffic-flood contract).
        pol_decision["unthrottled"] = any(
            e is None and not p["rate"] for p, e in pairs)
        pol_decision["block_unconditional"] = any(
            e is None and p["action"] == "block" and not p["rate"]
            for p, e in pairs)
        rates = []
        for p, exc in pairs:
            if not p["rate"] or exc is not None:
                continue
            key = _rate_key(p, req, ip_obj)
            if not key:
                continue
            # counters are namespaced PER POLICY: nine signup hits and
            # two reset hits from one IP no longer add up to "11" in a
            # single row. Cross-policy counter sharing would need
            # explicit configuration; no key format offers it today.
            # "key" LAST: spreading the policy's rate config first would
            # clobber the computed counter key with the keying MODE
            # ("ip"/"user"/...).
            rates.append({**p["rate"], "action": p["action"],
                          "policy_id": p["id"], "code": p["code"],
                          "key": f"p{p['id']}:{key}"})
        if rates:
            pol_decision["rates"] = rates

    # ------------------------------------------------ combine the strongest
    if rule_decision and pol_decision:
        # severity may only compare VERDICTS: a rate-GATED endpoint
        # block is not a refusal yet -- the guard acts only once that
        # policy's own counter crosses -- so it must never outrank an
        # immediate signature block on nominal severity (an over-ranked
        # gated policy would let the middleware allow the request and
        # silently cancel the independent block on the same body).
        # Rank: refusals first, refusals already in effect before
        # refusals still waiting for a counter, then severity -- and the
        # rule wins ties, for pack determinism.
        def _rank(d):
            in_effect = d["action"] in ("block", "auto") and (
                d["kind"] != "endpoint"
                or bool(d.get("block_unconditional") or d.get("force_log")))
            return (d["action"] in ("block", "auto"), in_effect, d["severity"])
        decision = max((rule_decision, pol_decision), key=_rank)
        # rate counters must tick even when a signature rule outvotes the
        # policy (e.g. a scanner hitting a flooded signup endpoint): the
        # attack lockout and the flood counters are separate ledgers, and
        # a block by one never suspends the other
        if "rates" in pol_decision:
            decision["rates"] = pol_decision["rates"]
    else:
        decision = rule_decision or pol_decision
    if decision is None:
        return None

    # Auto-lockout credit: explicit "auto" rules plus real block rules of
    # medium+ severity (the bundled rule set uses action=block, so both
    # credit). These counters are worker-local; the threshold applies
    # per worker process. Endpoint blocks of the same severity credit too
    # (a probe flood against a blocked route group is an attack), but NOT
    # rate-limited policies: those own their shared counters and must not
    # double-credit into the attack lockout.
    if decision["kind"] == "rule" and counters is not None and ip_obj is not None:
        credits = (decision["action"] == "auto"
                   or (decision["action"] == "block"
                       and decision["severity"] >= 3))
    elif decision["kind"] == "endpoint" and counters is not None \
            and ip_obj is not None:
        # rate-limited policies own their shared counters and must not
        # double-credit into the attack lockout -- but an UNCONDITIONAL
        # block policy does credit even when a monitoring policy attached
        # counters alongside it (its block is not the rate's doing).
        credits = (decision["action"] == "block"
                   and decision["severity"] >= 3
                   and ("rates" not in pol_decision
                        or decision.get("block_unconditional")))
    else:
        credits = False
    if credits:
        window = snap["auto_lockout"]["window"]
        limit = snap["auto_lockout"]["max"]
        # source-address rotation (especially IPv6) must not grow this
        # per-worker dict without bound. Keys whose window has fully
        # aged out are worthless, so they go first; if the attack volume
        # itself keeps >MAX keys inside their windows, clear the map --
        # the shared DB counter is authoritative, the deque is only a
        # DB-failure fallback, so trading a degraded fallback for
        # bounded memory is the right direction.
        if len(counters) > COUNTER_MAX_KEYS:
            cutoff = now - window
            for key in [k for k, v in counters.items()
                        if not v or v[-1] < cutoff]:
                del counters[key]
            if len(counters) > COUNTER_MAX_KEYS:
                counters.clear()
        key = str(ip_obj)
        dq = counters.setdefault(key, deque())
        dq.append(now)
        while dq and now - dq[0] > window:
            dq.popleft()
        decision["auto_hits"] = len(dq)
        decision["lockout_credit"] = True
        if snap["auto_lockout"]["enabled"] and len(dq) >= limit:
            decision["trigger_lockout"] = True
            counters.pop(key, None)
    return decision
