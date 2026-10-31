"""Database master-password brute-force protection.

Wraps ``odoo.service.db.check_super`` -- the single chokepoint every
master-password-protected db-admin path funnels through -- and credits
*confirmed failed master-password checks* only: it raises
``AccessDenied`` if and only if the submitted password failed
verification, so malformed requests, AccessDenied from other layers and
server errors never reach this counter.

Counters live in one shared table in one scope database per instance,
so a client cannot regain a failure budget by switching served
database, session database or route flavour. When no such database
exists, requests pass the layer with a throttled warning: fail-open by
module design, never a false claim of protection.

The Nth recorded failure sets ``blocked_until`` (now + lockout minutes)
and zeroes the window; while it is in the future, protected requests
are refused BEFORE any master-password check runs -- even one that
would have guessed correctly. Learning mode logs and never bumps or
enforces; learning traffic never clears, extends or shortens an
existing lockout row -- it just ages, and switching back to enforce
resumes refusal until the original expiry. Submitted passwords are
never stored or logged.
"""
import html
import logging
import re
import threading
import time
from datetime import timedelta

import odoo
from odoo.sql_db import db_connect

from . import iputil, recovery
from .normalize import normalize

_logger = logging.getLogger(__name__)

# routes that actually run a master-password verification or db-management
# operation: the manager page, selector and db list verify no password, and
# locking out page views would deny ordinary users for an admin's mistake.
_MASTER_OP_RE = re.compile(
    r"^/web/database/(?:create|duplicate|drop|backup|restore|change_password)"
    r"(?:/|$)", re.IGNORECASE)
# the db service over XML-RPC (both route families) always carries the
# master password as its first RPC arg for the protected methods; refusing
# the whole service while locked out is the honest boundary.
_XMLRPC_DB_RE = re.compile(r"^/xmlrpc/(?:2/)?db$", re.IGNORECASE)

# scope-database resolution cache (per worker process)
SCOPE_TTL = 10.0            # seconds a resolved scope db is reused
SCOPE_MISS_TTL = 2.0        # retries while nothing qualifies
LOCKOUT_RETENTION = 2 * 3600   # keep aged-out rows briefly, then sweep
PRUNE_EVERY = 10 * 60          # per process

_scope = {"dbname": None, "expiry": 0.0}
_prune_ts = 0.0
_last_warn_ts = 0.0


# ------------------------------------------------------------- scope

def scope_db():
    """The one database holding this instance's master-password counters.

    db_list(force=True) reads PostgreSQL's own database list through the
    host's dbfilter, so every worker resolves the same candidate set --
    the worker-local registry LRU would let two workers pick two scope
    databases and hand out two failure budgets. Each candidate must have
    both a usable OW Shield snapshot and the waf_master_hit store, so a
    half-upgraded database (older schema) is skipped.
    """
    now = time.monotonic()
    if _scope["expiry"] > now:
        return _scope["dbname"]
    from . import middleware           # deferred: middleware owns us
    try:
        from odoo.http import db_list
        cands = db_list(force=True)
    except Exception:
        cands = None
    if not cands:
        # degraded resolution: fall back to the worker's loaded
        # registries -- may differ per worker, which is why the pg-level
        # list above is the primary source.
        cands = middleware._loaded_dbs()
    dbname = None
    for name in sorted({c for c in (cands or []) if c}):
        if not middleware._get_snapshot(name):
            continue
        if _store_ready(name):
            dbname = name
            break
    _scope["dbname"] = dbname
    _scope["expiry"] = now + (SCOPE_TTL if dbname else SCOPE_MISS_TTL)
    return dbname


def _store_ready(dbname):
    """Does this database actually host the waf_master_hit counter table?"""
    try:
        with db_connect(dbname).cursor() as cr:
            cr.rollback()  # fresh txn (see _fetch_snapshot)
            cr.execute("SELECT to_regclass('public.waf_master_hit')")
            return cr.fetchone()[0] is not None
    except Exception:
        return False


def reset_cache():
    _scope["dbname"] = None
    _scope["expiry"] = 0.0


def _degraded(message, *args):
    """The feature is NOT running (for this request): say so visibly.

    Never a silent claim of protection: a throttled warning per process,
    plus (where a failure happened) one coverage traffic-log entry, so
    the degraded window is auditable.
    """
    global _last_warn_ts
    now = time.monotonic()
    if now - _last_warn_ts > 30:
        _last_warn_ts = now
        _logger.warning("OW Shield master-password guard: %s",
                        message % args if args else message)


# ------------------------------------------------------- per-request ctx

def _ctx(environ):
    """Protection context for this request, built at most once per request.

    Carried in the WSGI environ so the WSGI gate and the check_super
    wrapper share one dedup flag: overlapping verification calls in one
    request credit ONE failure. ``{}`` caches a negative ("this request
    sees no protection") answer.
    """
    cached = environ.get("_owshield_master_ctx")
    if cached is not None:
        return cached or None
    dbname = scope_db()
    if not dbname:
        environ["_owshield_master_ctx"] = {}
        _degraded("no database with an OW Shield master-password counter "
                  "store is available -- master-password protection is NOT "
                  "active for this request")
        return None
    from . import middleware           # deferred (cycle)
    snap = middleware._get_snapshot(dbname)
    master = (snap or {}).get("master") or {}
    if not snap or not master.get("enabled"):
        environ["_owshield_master_ctx"] = {}
        return None
    ip = iputil.resolve_client_ip(
        environ.get("REMOTE_ADDR"), environ.get("HTTP_X_REAL_IP"),
        environ.get("HTTP_X_FORWARDED_FOR"), snap["trusted_networks"])
    if not ip:
        environ["_owshield_master_ctx"] = {}
        return None
    network = ip + ("/128" if ":" in ip else "/32")
    ip_obj = iputil.parse_ip(ip)
    # global allow entries exempt this layer too: "allow" means trusted
    # in every layer of this module.
    exempt = ip_obj is not None and iputil.in_any(
        ip_obj, [net for net, _id, _reason in snap["allow_entries"]])
    ctx = {"db": dbname, "snap": snap, "ip": ip, "network": network,
           "exempt": exempt, "counted": False}
    environ["_owshield_master_ctx"] = ctx
    return ctx


def _mini_req(environ):
    """Traffic-log view of the request: route + source only.

    Never the body: these bodies carry credentials; the traffic log has
    no body column, and nothing here may invent one.
    """
    return {
        "ip": environ.get("_owshield_master_ctx", {}).get("ip") or "",
        "method": environ.get("REQUEST_METHOD") or "",
        "path": (environ.get("PATH_INFO") or "")[:512],
        # raw here on purpose: _buffer_hit sanitises credential values
        # at storage time. Do not pre-cut this copy.
        "query": environ.get("QUERY_STRING") or "",
        "ua": environ.get("HTTP_USER_AGENT") or "",
    }


# ------------------------------------------------------------ counters

def _read_state(ctx):
    """Current shared row state: (hits, blocked_until) or None.

    None means "no row" as well as "store unreadable" (the caller then
    behaves as unlocked -- fail-open by module design -- with the
    degraded coverage note from _degraded).
    """
    try:
        with db_connect(ctx["db"]).cursor() as cr:
            cr.rollback()  # fresh txn (see _fetch_snapshot)
            cr.execute(
                "SELECT hits, blocked_until FROM waf_master_hit "
                "WHERE network = %s", (ctx["network"],))
            return cr.fetchone()
    except Exception:
        _degraded("master-password counter store unreadable in %s", ctx["db"])
        return None


def _locked(ctx):
    """True while an active lockout covers this source."""
    row = _read_state(ctx)
    if not row or row[1] is None:
        return False
    return row[1] > odoo.fields.Datetime.now()


# Counter bump: UPDATE-first, one shared row per network. A plain UPDATE
# re-locks and re-applies its SET expressions against the latest
# committed row under READ COMMITTED, so concurrent bumps from several
# workers never collide. ON CONFLICT DO UPDATE reads through the
# transaction snapshot instead, and concurrent bumps of one row abort
# with 40001, which is the normal case at this choke point. The INSERT
# branch only runs for a network's first hit; if two first-failures
# race, the loser's DO NOTHING returns nothing and the UPDATE is simply
# re-applied. The repeated "aged window" / "new hits" CASE expressions
# cannot reference the just-updated values, hence the duplication;
# RETURNING reports the post-update row state.
_UPDATE_SQL = """
                    UPDATE waf_master_hit SET
                        window_start = CASE
                            WHEN waf_master_hit.window_start
                                 <= %(now)s - make_interval(secs => %(window)s)
                            THEN %(now)s
                            ELSE waf_master_hit.window_start
                        END,
                        hits = CASE
                            -- the crossing failure also ENDS the window:
                            -- hits=0 + a future blocked_until (see the
                            -- locked flag) means "this bump armed the
                            -- lockout"
                            WHEN (CASE
                                      WHEN waf_master_hit.window_start
                                           <= %(now)s
                                              - make_interval(secs => %(window)s)
                                      THEN 1
                                      ELSE waf_master_hit.hits + 1
                                  END) >= %(max)s
                            THEN 0
                            WHEN waf_master_hit.window_start
                                 <= %(now)s - make_interval(secs => %(window)s)
                            THEN 1
                            ELSE waf_master_hit.hits + 1
                        END,
                        blocked_until = CASE
                            WHEN (CASE
                                      WHEN waf_master_hit.window_start
                                           <= %(now)s
                                              - make_interval(secs => %(window)s)
                                      THEN 1
                                      ELSE waf_master_hit.hits + 1
                                  END) >= %(max)s
                            THEN %(unlock)s
                            ELSE waf_master_hit.blocked_until
                        END
                    WHERE waf_master_hit.network = %(net)s
                    RETURNING hits, blocked_until,
                              blocked_until > %(now)s AS locked
"""

_INSERT_SQL = """
                    INSERT INTO waf_master_hit
                            (network, window_start, hits, blocked_until)
                    VALUES (%(net)s, %(now)s,
                            CASE WHEN %(max)s <= 1 THEN 0 ELSE 1 END,
                            CASE WHEN %(max)s <= 1 THEN %(unlock)s
                                 ELSE NULL END)
                    ON CONFLICT (network) DO NOTHING
                    RETURNING hits, blocked_until,
                              blocked_until > %(now)s AS locked
"""


def _bump_state(cr, params):
    """The UPDATE-first bump on an open cursor; returns RETURNING row."""
    cr.execute(_UPDATE_SQL, params)
    state = cr.fetchone()
    if state is None:                    # this network's first failure
        cr.execute(_INSERT_SQL, params)
        state = cr.fetchone()
        if state is None:                # lost a first-insert race
            cr.execute(_UPDATE_SQL, params)
            state = cr.fetchone()
    return state


def _bump(ctx):
    """Atomically credit one confirmed failure; may START a lockout.

    One UPDATE...RETURNING per failure (INSERT only while the network
    has no row yet), in the same shared-table shape as the attack/login
    /rate counters. When the Nth failure is crossed the statement sets
    ``blocked_until`` AND zeroes the window: a row parked at the
    threshold would re-block the source the very minute the lockout
    expires.

    Returns (hits_after, blocked_until, locked_now) or None on a store
    failure -- the caller turns None into a visible degraded-coverage
    note. There is no worker-local fallback counter here: it would
    silently multiply the threshold.
    """
    global _prune_ts
    master = ctx["snap"]["master"]
    window = max(1, master["window"])
    limit = max(1, master["max"])
    try:
        now = odoo.fields.Datetime.now()  # naive UTC, matches columns
        unlock = now + timedelta(minutes=max(1, master["minutes"]))
        prune = time.monotonic() - _prune_ts > PRUNE_EVERY
        with db_connect(ctx["db"]).cursor() as cr:
            cr.rollback()  # fresh txn (see _fetch_snapshot)
            # UPDATE-first (see _UPDATE_SQL); the bounded retry is
            # insurance for the remaining serialization cases (deadlock,
            # prune races); a genuinely down store still falls through
            # to the documented degraded path instead of stalling
            # requests.
            params = {"net": ctx["network"], "now": now,
                      "unlock": unlock, "window": window, "max": limit}
            state = None
            for attempt in range(4):
                try:
                    # the odoo pool opens at REPEATABLE READ, where any
                    # concurrent write to the row aborts with 40001:
                    # this ONE transaction runs at READ COMMITTED. SET
                    # LOCAL lapses at COMMIT and at the retry rollback,
                    # so the pooled connection is never reconfigured for
                    # its next user.
                    cr.execute("SET LOCAL TRANSACTION ISOLATION LEVEL "
                               "READ COMMITTED")
                    state = _bump_state(cr, params)
                    break
                except Exception as exc:
                    cr.rollback()  # txn aborted; fresh one for retry
                    if (attempt >= 3
                            or getattr(exc, "pgcode", None)
                            not in ("40001", "40P01")):
                        raise
                    time.sleep(0.02 * (attempt + 1))
            hits, until, locked = state
            if prune:
                # housekeeping (bounded table): rows matter only while
                # their window runs or their lockout is active; the
                # blocked_until guard stops a live lockout being pruned.
                _prune_ts = time.monotonic()
                cr.execute(
                    "DELETE FROM waf_master_hit "
                    "WHERE window_start <= %(now)s "
                    "       - make_interval(secs => %(keep)s) "
                    "  AND (blocked_until IS NULL "
                    "       OR blocked_until <= %(now)s)",
                    {"now": now, "keep": LOCKOUT_RETENTION})
            cr.commit()
            return hits, until, bool(locked)
    except Exception:
        _degraded("master-password counter store unavailable in %s -- "
                  "failure NOT counted (request still denied by odoo)",
                  ctx["db"])
        return None


def _clear(ctx):
    """A confirmed CORRECT verification ends the source's failure window.

    The guard stops a racing lockout from being cleared by a success
    that started before it armed (the wrapper rejects locked sources
    before verifying anyway). It also implements the documented
    mode-change retention: a success in LEARNING mode passes the real
    verifier, yet never lifts an ACTIVE lockout row -- only expired rows
    and plain windows are deleted, in any mode. A successful ordinary
    user login goes through res.users, never check_super, so it can
    never clear this window.
    """
    try:
        with db_connect(ctx["db"]).cursor() as cr:
            cr.rollback()
            cr.execute(
                "DELETE FROM waf_master_hit "
                "WHERE network = %s "
                "  AND (blocked_until IS NULL OR blocked_until <= %s)",
                (ctx["network"], odoo.fields.Datetime.now()))
            cr.commit()
    except Exception:
        pass  # a parked row ages out on its own; never break a SUCCESS


# ------------------------------------------------------------- events

def _log_event(ctx, environ, decision):
    from . import middleware           # deferred (cycle)
    middleware._buffer_hit(ctx["db"], _mini_req(environ), decision,
                           ctx["snap"], None)


def _notify_lockout(ctx, environ):
    """Admin mail for a master-password lockout (bounded, credential-free).

    Reuses the existing alert budget and per-network suppression, and
    the signed recovery-token machinery: the link names (scope db,
    network) and lifts only that network's master-password counter -- it
    can never grant an allowlist.
    """
    from . import middleware           # deferred (cycle)
    dbname = ctx["db"]
    if not (middleware._alert_budget_ok(dbname)
            and not middleware._recent_network_alert(dbname,
                                                     ctx["network"])):
        return
    try:
        snap = ctx["snap"]
        master = snap["master"]
        with db_connect(dbname).cursor() as cr:
            cr.rollback()
            env = odoo.api.Environment(cr, odoo.SUPERUSER_ID,
                                       {"lang": "en_US"})
            base = middleware._email_base_url(env, environ)
            token = recovery.make_token(
                snap.get("recover_secret") or "", dbname, ctx["network"])
            url = f"{base}/waf/recover/{token}"
            env["waf.config"].get_singleton()._notify_admins(
                subject="OW Shield: master-password brute-force lockout "
                        "(%s)" % ctx["ip"],
                body=(
                    f"<p><b>{html.escape(ctx['ip'])}</b> failed the Odoo "
                    f"database master password "
                    f"{master['max']} times in "
                    f"{max(1, master['window'] // 60)} minutes. Database "
                    f"management (create/drop/backup/restore/"
                    f"change-password and the db RPC service) is refused "
                    f"for that address for {master['minutes']} minutes. "
                    f"Ordinary website and backend access is unaffected, "
                    f"and no other firewall rule was weakened.</p>"
                    f"<p>If this was you, this recovery link (valid 15 "
                    f"minutes) ends the counters for that address: "
                    f"<a href=\"{html.escape(url)}\">"
                    f"{html.escape(url)}</a></p>"
                    f"<p>The submitted password is never logged.</p>"
                ),
            )
            cr.commit()
    except Exception:
        _degraded("could not queue the master-password lockout alert "
                  "for %s in %s", ctx["network"], dbname)


def _record_failure(ctx, environ):
    """Credit ONE confirmed failed master-password check + its event.

    Learning mode: event only, never a counter and never a lockout, and
    no bump, so failures learned while observing cannot silently arm an
    enforce-mode lockout the moment the mode flips.
    """
    master = ctx["snap"]["master"]
    window_min = max(1, master["window"] // 60)
    if ctx["snap"]["mode"] != "enforce":
        _log_event(ctx, environ, {
            "kind": "master", "action": "log", "severity": 3,
            "rule_id": None, "blocked": False,
            "rule_name": "master password failure (learning mode)",
            "category": "master"})
        return
    res = _bump(ctx)
    if res is None:
        # store failure: the request still faces odoo's own denial, but
        # the coverage gap must be in the traffic log, not just the log
        _log_event(ctx, environ, {
            "kind": "master", "action": "log", "severity": 4,
            "rule_id": None, "blocked": False,
            "rule_name": "master password failure: counter store "
                         "unavailable (not counted)",
            "category": "coverage"})
        return
    hits, _until, locked = res
    if locked:
        _log_event(ctx, environ, {
            "kind": "master", "action": "block", "severity": 5,
            "rule_id": None, "blocked": False,
            "rule_name": "master password lockout: %d failures in %d "
                         "min; db management refused for %d min"
                         % (master["max"], window_min, master["minutes"]),
            "category": "master"})
        _logger.warning(
            "WAF: master-password brute-force lockout for %s on %s "
            "(%d failures in %d min, db management locked %d min)",
            ctx["network"], ctx["db"], master["max"], window_min,
            master["minutes"])
        _notify_lockout(ctx, environ)
    else:
        _log_event(ctx, environ, {
            "kind": "master", "action": "log", "severity": 3,
            "rule_id": None, "blocked": False,
            "rule_name": "master password failure %d of %d (in %d min)"
                         % (hits, master["max"], window_min),
            "category": "master"})


# --------------------------------------------------------------- gate

def handle_request(environ, start_response):
    """WSGI pre-refusal for master-protected operations (guard hook).

    Called by the middleware for every request, but does nothing until
    the path is one that actually runs a master-password verification or
    db-management operation -- the manager page, the db selector and the
    login flow stay completely untouched, in every mode.

    While a lockout covers the source, the request is refused BEFORE
    routing: no controller, no db operation, and no second chance at the
    password -- a correct guess during the lockout is refused like a
    wrong one. Returns a blocked-response callable, or None to let odoo
    route.
    """
    path = environ.get("PATH_INFO") or ""
    norm = normalize(path)
    if not (_MASTER_OP_RE.match(norm) or _XMLRPC_DB_RE.match(norm)):
        return None
    ctx = _ctx(environ)
    if ctx is None or ctx["exempt"]:
        return None
    if ctx["snap"]["mode"] != "enforce":
        return None          # learning mode records, never enforces
    if not _locked(ctx):
        return None
    decision = {
        "kind": "master", "action": "block", "severity": 5,
        "rule_id": None, "blocked": True,
        "rule_name": "master password lockout active",
        "category": "master",
    }
    _log_event(ctx, environ, decision)
    _logger.warning(
        "WAF blocked %s %s from %s: master-password lockout active "
        "(ref %s)", environ.get("REQUEST_METHOD", ""), path, ctx["ip"],
        decision["ref"])
    from . import middleware           # deferred (cycle)
    return lambda e, sr: middleware._blocked_response(decision["ref"], e, sr)


# ------------------------------------------------------------ wrapper

# Attribution: a published request slot, not odoo.http.request. odoo's
# dispatch_rpc() wraps every db-service call in borrow_request(), which
# pops the request off the local stack for the whole db dispatch, so
# inside check_super the request proxy is unbound and cannot be our
# identity source. The WSGI layer runs on the same thread that later
# dispatches the request, so it publishes the environ in a per-thread
# slot at request start and clears it at request end (workers reuse
# threads: a stale slot must never misattribute the next request).
# Non-HTTP callers (shell, upgrades, direct service calls) leave the
# slot empty: the layer then neither counts nor blocks -- we never
# invent a client IP.

_local = threading.local()


def begin_request(environ):
    """WSGI entry hook: bind this thread's attribution slot."""
    _local.environ = environ
    _local.ctx = None


def end_request():
    """WSGI exit hook (always, finally): release the slot."""
    _local.environ = None
    _local.ctx = None


def _request_ctx():
    """Attribution for the check_super wrapper (see slot note above).

    Reuses the per-request context the WSGI gate already built (shared
    dedup flag), or builds one from the published environ when the
    operation arrived over a route the path-level gate does not
    pre-screen (``/jsonrpc`` db service). Outside an in-flight HTTP
    request the slot is empty -> None.
    """
    environ = getattr(_local, "environ", None)
    if environ is None:
        return None
    ctx = getattr(_local, "ctx", None)
    if ctx is None:
        ctx = _ctx(environ)
        _local.ctx = ctx
    return ctx


def _wrapped_check_super(original):
    def check_super(passwd):
        ctx = _request_ctx()
        # enforce active lockouts only in enforce mode -- the same mode
        # rule the WSGI gate applies above: switching an instance to
        # learning must never itself be a punishment that outlives the
        # switch. A lockout row is kept untouched by learning traffic
        # and enforced again on the flip back, until the original
        # blocked_until expiry.
        if (ctx is not None and not ctx["exempt"]
                and ctx["snap"]["mode"] == "enforce"
                and _locked(ctx)):
            # an ACTIVE lockout rejects before verification: even a
            # correct guess does not get the password compared.
            environ = getattr(_local, "environ", None)
            if environ is not None:
                _log_event(ctx, environ, {
                    "kind": "master", "action": "block", "severity": 5,
                    "rule_id": None, "blocked": True,
                    "rule_name": "master password lockout active "
                                 "(refused before verification)",
                    "category": "master"})
            raise odoo.exceptions.AccessDenied()
        try:
            result = original(passwd)
        except odoo.exceptions.AccessDenied:
            # check_super raises AccessDenied only for a failed master
            # password: a confirmed failure. Other exceptions propagate
            # uncounted (server problems must not burn a budget).
            if ctx is not None and not ctx["exempt"] and not ctx["counted"]:
                ctx["counted"] = True   # once per request, layer overlap
                environ = getattr(_local, "environ", None)
                if environ is not None:
                    _record_failure(ctx, environ)
                else:  # pragma: no cover - slot cleared mid-request
                    _degraded("could not attribute a master-password "
                              "failure to a request context")
            raise
        if ctx is not None and not ctx["exempt"]:
            _clear(ctx)      # a correct verification ends the window
        return result
    return check_super


def install():
    """Wrap odoo.service.db.check_super exactly once per process.

    Every module-level call site (controllers, dispatch, service db.py)
    resolves the function through the module attribute at call time, so
    patching the attribute covers all entry points -- including the
    ``g[exp_name]`` globals lookup inside dispatch -- without touching
    any controller logic.
    """
    from odoo.service import db as db_service
    current = db_service.check_super
    if getattr(current, "_owshield_wrapped", False):
        return
    wrapped = _wrapped_check_super(current)
    wrapped._owshield_wrapped = True
    db_service.check_super = wrapped
