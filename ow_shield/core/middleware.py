"""WSGI-level firewall: inspects every request before Odoo routing/ORM.

Runs inside every worker process (patched onto odoo.http.Application.__call__
at module import). Designed fail-open: any internal error lets the request
through, logged at most once per 30 seconds to keep logs readable.
"""
import atexit
import html
import io
import json
import logging
import re
import threading
import time
import uuid
from collections import deque

import odoo
from odoo.sql_db import db_connect
from urllib.parse import parse_qs, unquote_plus

from . import engine, iputil, masterbp, recovery
from .normalize import normalize, normalize_headers

_logger = logging.getLogger(__name__)

SNAP_TTL = 5.0          # seconds; per-worker rule/IP/config refresh delay
STALE_GRACE_MAX = 300.0  # seconds a fetch failure may keep the last good
                         # policy alive before failing open
DB_TTL = 10.0           # seconds; host -> dbname resolution cache
UID_TTL = 30.0          # seconds; uid -> internal-user cache
FLUSH_MAX = 50          # hit log batch size
FLUSH_AGE = 5.0         # flush hits older than this (seconds)
ALERT_WINDOW = 15 * 60  # lockout-alert rate limiting window (seconds)
ALERT_MAX_PER_WINDOW = 25   # per database: bounds inbox flooding + mail volume
BODY_METHODS = ("POST", "PUT", "PATCH")
STATIC_RE = re.compile(r"^/[^/]+/static/")

# Body content types scanned inline. application/json-rpc is in core's
# JsonRPCDispatcher.mimetypes (odoo/http.py): Odoo processes such bodies
# as JSON-RPC, so they must be scanned like plain JSON.
# multipart/form-data is handled separately: only non-file parts are scanned.
SCAN_TYPES = (
    "application/json", "application/json-rpc",
    "application/x-www-form-urlencoded", "text/plain",
    "text/xml", "application/xml", "application/javascript",
    "multipart/form-data", "",
)

# JSON-RPC dispatcher family (odoo 19: the /jsonrpc root AND every
# /web/dataset/call_* route): the body IS the dispatched operation, so the
# structured policy layer parses these under its own, wider capture cap.
# Signature-rule scanning still stops at body_scan_max (regex CPU stays
# bounded); only the envelope parse goes further.
_RPC_JSON_PATH_RE = re.compile(r"^(?:/jsonrpc$|/web/dataset/)",
                               re.IGNORECASE)
RPC_PARSE_MAX = 512 * 1024

# per-process state
_snaps = {}             # dbname -> (expiry_ts, snap or None)
_FETCH_FAILED = object()  # _fetch_snapshot: read error, NOT "no policy"
_snap_last_ok = {}      # dbname -> monotonic ts of last successful fetch
_snap_fail_ts = {}      # dbname -> monotonic ts of last logged fetch failure
_db_by_host = {}        # host -> (expiry_ts, dbname or None)
_uid_cache = {}         # dbname -> {uid: (expiry_ts, is_internal)}
_counters = {}          # dbname -> {ip: deque}
_hit_buffer = []
_hit_buffer_t0 = None
_hit_lock = threading.Lock()   # guards _hit_buffer swap/append (threaded mode)
_next_flush_ts = 0.0
_recent_alerts = {}     # (dbname, network) -> last alert monotonic ts
_alert_history = {}     # dbname -> deque(monotonic ts of alerts sent)
_last_error_ts = 0.0
_credit_mode = {}       # dbname -> mode the local counters were earned in


class _ReplayStream:
    """wsgi.input replacement: replays a captured prefix, then delegates.

    ``remaining`` budgets how many more bytes may be pulled from the
    underlying stream; -1 means unknown/unbounded (chunked bodies, which
    carry no Content-Length).
    """

    def __init__(self, prefix, underlying, remaining):
        self._buf = io.BytesIO(prefix)
        self._stream = underlying
        self._remaining = remaining

    def _take(self, size):
        # pull up to *size* bytes from the underlying stream, honoring the
        # remaining budget (-1 = unbounded)
        if self._remaining == 0:
            return b""
        if self._remaining < 0:
            chunk = self._stream.read(size) or b""
        else:
            chunk = self._stream.read(min(size, self._remaining)) or b""
            self._remaining -= len(chunk)
        return chunk

    def read(self, size=-1):
        if size is None or size < 0:
            # read-to-EOF: whatever is buffered plus the rest of the body
            data = self._buf.read()
            if self._remaining != 0:
                rest = b""
                if self._remaining < 0:
                    rest = self._stream.read() or b""
                else:
                    rest = self._stream.read(self._remaining) or b""
                    self._remaining -= len(rest)
                data += rest
            return data
        data = self._buf.read(size)
        need = size - len(data)
        if need > 0:
            # only stream reads consume the budget; replaying the captured
            # prefix must not (would double-count a truncated body)
            data += self._take(need)
        return data

    def readline(self, size=-1):
        limit = size if size and size > 0 else -1
        line = self._buf.readline(limit if limit > 0 else -1)
        if line.endswith(b"\n") or (limit > 0 and len(line) >= limit):
            return line
        # captured prefix ends mid-line: keep reading the underlying stream
        # until the newline instead of splitting the line at the boundary
        while self._remaining != 0 and (limit < 0 or len(line) < limit):
            want = -1 if self._remaining < 0 else self._remaining
            if limit > 0:
                want = min(want, limit - len(line))
            chunk = self._stream.readline(want) or b""
            if not chunk:
                break
            if self._remaining >= 0:
                self._remaining -= len(chunk)
            line += chunk
            if chunk.endswith(b"\n"):
                break
        return line

    def readlines(self, hint=-1):
        lines, total = [], 0
        while True:
            line = self.readline()
            if not line:
                break
            lines.append(line)
            total += len(line)
            if hint >= 0 and total >= hint:
                break
        return lines

    def __iter__(self):
        while True:
            line = self.readline()
            if not line:
                return
            yield line


# ---------------------------------------------------------------- helpers

def _loaded_dbs():
    try:
        # Odoo 19 keeps registries in an LRU keyed by database name, so
        # list() yields exactly the db names callers need.
        from odoo.modules.registry import Registry
        return list(Registry.registries)
    except Exception:
        return []


def _cookie_value(environ, name):
    for chunk in (environ.get("HTTP_COOKIE") or "").split(";"):
        key, _, val = chunk.strip().partition("=")
        if key == name and val:
            return val
    return None


def _load_session(environ):
    """Resolve the real session behind the session_id cookie.

    Odoo 19 keeps sessions in server-side files (the cookie is only a
    lookup key), so a forged cookie resolves to nothing while a genuine
    one yields the user's uid/db claims. Read-only: store.get() never
    writes to disk. Returns None when unusable.
    """
    sid = _cookie_value(environ, "session_id")
    if not sid:
        return None
    try:
        from odoo.http import root
        store = root.session_store
        if not store.is_valid_key(sid):
            return None
        return store.get(sid)
    except Exception:
        return None


def _resolve_db(environ, session):
    """Pick the policy database exactly the way odoo 19's
    Request._get_session_and_dbname picks the served one:
    session.db (if the dbfilter accepts it) -> X-Odoo-Database header (if
    accepted) -> single-database fallback.

    The ?db= query parameter is ignored: odoo does not route on it either,
    and honouring it lets one request steer the WAF policy to a weaker (or
    WAF-less) database than the one it actually serves.
    """
    host = environ.get("HTTP_HOST", "")
    try:
        from odoo.http import db_filter
    except Exception:
        db_filter = None

    def accepted(name):
        if not name:
            return False
        if db_filter is None:
            return True
        try:
            return bool(db_filter([name], host=host))
        except Exception:
            return True

    sdb = (session or {}).get("db") or ""
    if accepted(sdb):
        return sdb
    hdb = environ.get("HTTP_X_ODOO_DATABASE", "")
    if accepted(hdb):
        return hdb

    # single-database (monodb) fallback: db_list(force=True) works with
    # list_db=False (the hardened production setting), where a non-forced
    # list raises AccessDenied. This step has a real cost, so it is
    # cached -- read and write use the SAME key (host).
    now = time.monotonic()
    cached = _db_by_host.get(host)
    if cached and cached[0] > now:
        return cached[1]
    dbname = None
    available = []
    try:
        from odoo.http import db_list
        available = db_list(force=True, host=host)
    except Exception:
        available = []
    if not available:
        # db listing unavailable (e.g. denied or pg hiccup): fall back to
        # loaded registries, but only those the host's db filter accepts --
        # an unrelated database could apply a weaker/no-WAF policy.
        try:
            from odoo.http import db_filter
            available = db_filter(_loaded_dbs(), host=host)
        except Exception:
            available = []
    if len(available) == 1:
        dbname = available[0]
    elif available:
        # multi-db host without session/header: odoo itself serves its db
        # selector here; apply some policy anyway (ip denials, url rules)
        # for visibility -- first dbfilter-matched db.
        dbname = sorted(available)[0]
    # dbfilter excludes every candidate -> None: odoo itself cannot route
    # this host either; never fall back to an unrelated database.
    # The cache is keyed by request data (the Host header), so it needs a
    # bound like the counters: a flood of unique Host headers must not be
    # a memory leak. Expired entries go first, then wholesale clear.
    if len(_db_by_host) > 4096:
        for h in [h for h, (exp, _v) in _db_by_host.items() if exp <= now]:
            del _db_by_host[h]
        if len(_db_by_host) > 4096:
            _db_by_host.clear()
    _db_by_host[host] = (now + (DB_TTL if dbname else 2.0), dbname)
    return dbname


def _fetch_snapshot(dbname):
    """Load & compile the per-DB policy with plain SQL (no ORM on hot path).

    Return contract: None means "this DB deliberately has no active WAF
    policy" (module not installed, no config row, or mode off);
    _FETCH_FAILED means "the policy exists but could not be read right
    now" (pool outage, half-applied upgrade). _get_snapshot treats those
    very differently.
    """
    try:
        with db_connect(dbname).cursor() as cr:
            # the pool may hand back a connection left idle-in-transaction
            # by other code, and every odoo 19 connection is REPEATABLE
            # READ: roll back first to force a fresh snapshot read.
            cr.rollback()
            try:
                cr.execute(
                    "SELECT id, mode, trusted_proxies, country_mode, body_scan_max, "
                    "auto_lockout_enabled, auto_lockout_max, auto_lockout_window, "
                    "auto_lockout_minutes, alert_email, recover_secret "
                    "FROM waf_config ORDER BY id LIMIT 1"
                )
            except Exception as exc:
                # 42P01 undefined_table: module not (yet) installed in this
                # database -- a legitimate "no policy". Anything else is a
                # failure and must not masquerade as one (pgcode/sqlstate:
                # psycopg2 / psycopg3 spellings).
                if getattr(exc, "pgcode", None) == "42P01" or \
                        getattr(exc, "sqlstate", None) == "42P01":
                    return None
                raise
            row = cr.fetchone()
            if row is None or row[1] == "off":
                return None
            config = {
                "mode": row[1], "trusted_proxies": row[2] or "",
                "country_mode": row[3] or "off", "body_scan_max": row[4] or 65536,
                "auto_lockout_enabled": row[5], "auto_lockout_max": row[6],
                "auto_lockout_window": row[7], "auto_lockout_minutes": row[8],
                "alert_email": row[9], "recover_secret": row[10] or "",
                "country_codes": [],
            }
            if config["country_mode"] != "off":
                cr.execute(
                    "SELECT c.code FROM waf_config_country_ids l "
                    "JOIN res_country c ON c.id = l.res_country_id "
                    "WHERE l.waf_config_id = %s", (row[0],))
                config["country_codes"] = [r[0] for r in cr.fetchall()]
            cr.execute(
                "SELECT id, code, name, category, scope, pattern, action, severity, "
                "no_session, case_sensitive FROM waf_rule WHERE active = TRUE")
            cols = [d[0] for d in cr.description]
            rules = [dict(zip(cols, r)) for r in cr.fetchall()]
            cr.execute(
                "SELECT id, network, kind, reason, "
                "EXTRACT(EPOCH FROM expires) FROM waf_ip_entry WHERE active = TRUE")
            ips = []
            for r in cr.fetchall():
                ips.append({"id": r[0], "network": r[1], "kind": r[2],
                            "reason": r[3], "expires": r[4]})
            cr.execute(
                "SELECT id, code, name, category, route_re, methods, "
                "rpc_service, rpc_model, rpc_method, ids_max, action, "
                "severity, exempt_nets, rate_max, rate_window, rate_key, "
                "rate_param, rate_block_minutes, apply_pre_db, "
                "on_unverified "
                "FROM waf_endpoint_policy WHERE active = TRUE")
            cols = [d[0] for d in cr.description]
            policies = [dict(zip(cols, r)) for r in cr.fetchall()]
            # Master-password brute-force settings: read as a SEPARATE,
            # separately-failed query. On a server hosting several
            # databases a not-yet-upgraded one legitimately has no
            # master_bp_* columns, and its existing policy must keep
            # being served -- with this feature's defaults -- instead of
            # the whole snapshot collapsing.
            master = {}
            try:
                cr.execute(
                    "SELECT master_bp_enabled, master_bp_max, "
                    "master_bp_window, master_bp_lockout_minutes "
                    "FROM waf_config WHERE id = %s", (row[0],))
                m = cr.fetchone()
                if m:
                    # NULL column -> default True: the feature is ON
                    # unless explicitly turned off, and 0/NULLs fall
                    # through to the engine's clamped defaults.
                    master = {"enabled": m[0] if m[0] is not None else True,
                              "max": m[1], "window": (m[2] or 10) * 60,
                              "minutes": m[3]}
            except Exception:
                cr.rollback()  # 42703 undefined_column on an old schema
            cr.execute(
                "SELECT x.id, x.rule_id, r.code, x.policy_id, e.code, "
                "x.route_re, x.methods, x.param, x.reason, "
                "EXTRACT(EPOCH FROM x.expires) "
                "FROM waf_exception x "
                "LEFT JOIN waf_rule r ON r.id = x.rule_id "
                "LEFT JOIN waf_endpoint_policy e ON e.id = x.policy_id "
                "WHERE x.active = TRUE")
            exceptions = []
            for r in cr.fetchall():
                exceptions.append({
                    "id": r[0], "rule_id": r[1], "rule_code": r[2],
                    "policy_id": r[3], "policy_code": r[4], "route_re": r[5],
                    "methods": r[6], "param": r[7], "reason": r[8],
                    "expires": r[9]})
            config["master"] = master
            return engine.build_snapshot(config, rules, ips,
                                         policies=policies,
                                         exceptions=exceptions)
    except Exception:
        # everything reaching THIS handler (pool outage, connection died
        # mid-cursor, half-applied upgrade) is a read FAILURE -- it must
        # not masquerade as "this DB has no policy", which would silently
        # blank a working cached snapshot. Only the explicit paths above
        # (42P01 / no config row / mode off) return the legitimate None.
        return _FETCH_FAILED


def _get_snapshot(dbname):
    now = time.monotonic()
    cached = _snaps.get(dbname)
    if cached and cached[0] > now:
        return cached[1]
    snap = _fetch_snapshot(dbname)
    if snap is _FETCH_FAILED:
        # a refresh hiccup (pool outage, half-applied upgrade, schema
        # race) must not swap a WORKING policy for "no protection": keep
        # serving the last good snapshot for up to STALE_GRACE_MAX,
        # retrying every couple of seconds; past that window fall open
        # (fail-open remains the design). The transition is logged
        # (throttled) either way.
        prev = cached[1] if cached else None
        if prev is not None and now - _snap_last_ok.get(dbname, 0.0) \
                <= STALE_GRACE_MAX:
            snap = prev
        else:
            snap = None
        _snaps[dbname] = (now + 2.0, snap)
        if now - _snap_fail_ts.get(dbname, 0.0) > 30:
            _snap_fail_ts[dbname] = now
            _logger.warning(
                "WAF: policy refresh failed for database %s -- %s",
                dbname, "serving last good policy" if snap is not None
                else "no policy available, requests pass unprotected")
        return snap
    _snaps[dbname] = (now + (SNAP_TTL if snap else 2.0), snap)
    _snap_last_ok[dbname] = now
    return snap


# MaxMind reader resolution happens once per process: root.geoip_country_db
# is a cached_property that raises while no database is installed, and
# repeating the failed open() on every request is wasteful. Installing the
# mmdb takes effect at the next restart.
_geo = {"resolved": False, "reader": None}


def _geo_country(ip):
    # Attribution runs whenever a GeoIP database is present, independent
    # of country_mode: enforcing the country lists stays gated in
    # engine.evaluate, but the dashboard world map needs the data even
    # when country blocking is off.
    if not _geo["resolved"]:
        try:
            from odoo.http import root
            _geo["reader"] = root.geoip_country_db
        except Exception:
            _geo["reader"] = None
        _geo["resolved"] = True
    if _geo["reader"] is None or not ip:
        return None
    try:
        return getattr(_geo["reader"].country(ip).country, "iso_code", None)
    except Exception:
        return None


# A multipart body must be scanned exactly as the APPLICATION parses it, or
# a crafted body reaches Odoo through a scanner that saw a different body.
# Werkzeug 3.x (Odoo 19's stack) parses multipart/form-data with a byte
# state machine (werkzeug.sansio.multipart) whose precise rules (boundary
# must follow a line break, duplicate Content-Disposition resolves to the
# first, the first boundary may omit its line break) repeatedly defeat
# hand-rolled splitters. So run werkzeug's OWN decoder over the captured
# bytes: the scanner's view and the app's view cannot diverge. Field
# values are scanned; file parts are skipped as the app skips them;
# preamble/epilogue bytes are scanned (junk to the app -- scanning junk
# can cost a false positive, never a blind spot). A decode error falls
# back to scanning the whole raw prefix: fail toward scanning.
try:
    from werkzeug.http import parse_options_header as _mp_parse_options_header
    from werkzeug.sansio.multipart import (
        Data as _MPData,
        Epilogue as _MPEpilogue,
        Field as _MPField,
        File as _MPFile,
        MultipartDecoder as _MPDecoder,
        NeedData as _MPNeedData,
        Preamble as _MPPreamble,
    )
except ImportError:  # pragma: no cover - werkzeug < 3.0 (Odoo <= 17)
    _mp_parse_options_header = None
    _MPDecoder = None


def _multipart_text(prefix, ctype_full):
    """Extract the scanned text of a multipart/form-data body prefix.

    Uses werkzeug's own multipart state machine (see above). A truncated
    prefix (body over ``body_scan_max``) still yields every part boundary
    and value decoded so far; an undecodable body is returned raw so the
    rules get to look at it -- the app rejects such bodies anyway.
    """
    if _MPDecoder is None:  # pragma: no cover
        return prefix
    try:
        if _mp_parse_options_header is not None:
            boundary = _mp_parse_options_header(ctype_full)[1].get("boundary", "")
        else:  # pragma: no cover - matches formparser's own encoding
            boundary = _content_boundary(ctype_full)
        if not boundary:
            return prefix       # formparser raises "Missing boundary" too
        decoder = _MPDecoder(boundary.encode("ascii"))
        decoder.receive_data(prefix)
        out = []
        scan_part = True        # preamble is scanned until a part says otherwise
        while True:
            event = decoder.next_event()
            if isinstance(event, _MPFile):
                scan_part = False
            elif isinstance(event, _MPField):
                scan_part = True
            elif isinstance(event, (_MPData, _MPPreamble, _MPEpilogue)):
                if scan_part and event.data:
                    out.append(event.data)
            elif isinstance(event, _MPNeedData):
                break           # capture ended (e.g. body hit the cap)
        return b"\n".join(out)
    except Exception:
        # undecodable body: the app will reject the request, but inspect it
        # anyway rather than hand it a scan-free pass
        return prefix


def _content_boundary(ctype_full):
    for param in ctype_full.split(";")[1:]:
        key, _, val = param.strip().partition("=")
        if key.lower() == "boundary":
            return val.strip().strip('"')
    return ""


def _read_body(environ, snap, req):
    """Best-effort inline body scan capture; replay-safe for the app.

    Capture runs when body-scope rules exist OR the endpoint-policy layer
    needs the structured view (RPC envelope / form params / id counts):
    the policies must see exactly the bytes the application parses, so the
    SAME captured prefix feeds both the scan and the structured parse.
    """
    if not ("body" in snap["rules"] or snap.get("wants_rpc")
            or snap.get("wants_ids")) \
            or req["method"] not in BODY_METHODS:
        return
    ctype_full = environ.get("CONTENT_TYPE") or ""
    ctype = ctype_full.split(";")[0].lower()
    if ctype not in SCAN_TYPES:
        return
    try:
        cl = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        cl = 0
    if cl < 0:
        return
    chunked = environ.get("HTTP_TRANSFER_ENCODING", "").strip().lower() \
        == "chunked"
    if cl == 0 and not chunked:
        # no body to scan. An absent Content-Length *with*
        # Transfer-Encoding: chunked is a real body (werkzeug hands the
        # app a de-chunked stream) and must be captured too.
        return
    stream = environ.get("wsgi.input")
    if stream is None:
        return
    cap = snap["body_scan_max"]
    # JSON-RPC-family bodies get the wider structured-parse capture (see
    # RPC_PARSE_MAX): the same bytes feed the replay AND the envelope
    # parse, so what the policy layer sees is exactly what the app parses.
    rpc_cap = cap
    if snap.get("wants_rpc") and _RPC_JSON_PATH_RE.match(req.get("path") or ""):
        rpc_cap = max(cap, RPC_PARSE_MAX)
    read_cap = max(cap, rpc_cap)
    if cl:
        prefix = stream.read(min(cl, read_cap))
        remaining = cl - len(prefix)
    else:
        # chunked: total length unknown; capture up to the cap, the rest
        # streams through untouched
        prefix = stream.read(read_cap)
        remaining = -1
    environ["wsgi.input"] = _ReplayStream(prefix, stream, remaining)
    if not prefix:
        req["body_prefix"] = b""
        req["body_ctype"] = ctype
        return
    truncated = (cl > cap) if cl else (chunked and len(prefix) >= cap)
    # when the cap truncates a body the app parses inline (JSON/form/text/
    # xml), neither the raw nor the parity scan sees the remainder while the
    # controller receives it whole: a real inspection blind spot, so guard()
    # turns the flag into one throttled partial-inspection waf.hit. Blocking
    # on overflow stays a roadmap decision (legitimate bulk writes exceed the
    # cap); JSON-RPC service restrictions do not depend on the scan cap --
    # they use the wider parse below.
    # multipart is NOT flagged: file parts are never inline-scanned by
    # design, and large uploads are covered by the scanner's attachment check.
    req["body_overflow"] = truncated and ctype != "multipart/form-data"
    # the wider RPC capture can truncate too -- then a JSON-RPC operation
    # may exist that no parse of the capture can ever see. _rpc_extract
    # turns that into an "operation unverified" marker for the policy layer
    # instead of silently empty fields.
    req["rpc_truncated"] = bool(
        (cl > rpc_cap) if cl else (chunked and len(prefix) >= rpc_cap))
    texts = []
    if ctype == "multipart/form-data":
        # decoder-based: sees exactly the parts the app will deliver
        raw = _multipart_text(prefix, ctype_full)
        if raw:
            texts.append(normalize(raw[:cap], max_len=cap))
        if snap.get("wants_rpc") or snap.get("wants_ids"):
            # odoo merges multipart form fields into request.params just
            # like urlencoded ones -- the policy layer must see the same
            # fields (a reset POST split over multipart must not hide its
            # login target from the counters).
            req["multipart_fields"] = _multipart_fields(prefix, ctype_full)
    elif ctype in ("application/json", "application/json-rpc"):
        # parser parity: odoo hands the controller json.loads(body), so a
        # payload whose characters are written as \uXXXX escapes is inert to
        # the raw scan but plain text to the app. Scan the raw bytes AND
        # every decoded string value.
        texts.append(normalize(prefix[:cap], max_len=cap))
        # the decoded walk follows the CAPTURE (RPC_PARSE_MAX on JSON-RPC
        # routes), not the signature scan cap, and reaches the rules as
        # chunked candidates: coverage may never depend on traversal order
        # or on where one joined candidate is truncated. Chunking (with
        # overlap, so no pattern can straddle a seam) keeps the walk
        # complete while the scanned total stays bounded by the captured
        # bytes the app json.loads()es anyway.
        for cand in _json_chunks(prefix, cap):
            texts.append(normalize(cand, max_len=cap))
    elif ctype == "application/x-www-form-urlencoded":
        # same parity rule for forms: werkzeug decodes '+' as a space
        # (query-string convention), so "x=1+UNION+SELECT..." reaches the
        # app as "1 UNION SELECT..." and must be scanned that way too.
        # '%2B' still survives as a literal plus.
        texts.append(normalize(prefix[:cap], decode_plus=True, max_len=cap))
    else:
        # text/plain, xml, javascript, missing type: raw scan only. XML
        # parser parity is on the roadmap (needs an entity-safe parser).
        texts.append(normalize(prefix[:cap], max_len=cap))
    req["norm_body"] = texts
    # keep the captured bytes for the structured policy layer (rpc context)
    req["body_prefix"] = prefix
    req["body_ctype"] = ctype


# decoded parity candidates are cut this far apart; the overlap keeps the
# seam safe for any signature (every pattern here is far shorter than 512)
_JSON_CHUNK_OVERLAP = 512


def _json_chunks(prefix, chunk):
    """json.loads the body the way the app will, then collect every string
    key and value for rule matching (parser parity).

    Returns a LIST of scan candidates; empty means "nothing decodable":
    a body json.loads cannot parse degrades to raw-only inspection,
    never to a scan-free pass.

    The walk is COMPLETE over the capture: every string of the parsed
    tree appears in some candidate, and rules scan candidates one after
    another, so coverage never depends on placement or traversal order.

    Bounded, by something the request path pays anyway:

    * an explicit stack walks the whole tree (json.loads' parser limits
      are core's);
    * total collected characters <= 2x the captured bytes: a decoded
      string is never longer than its JSON source;
    * each candidate is at most ``chunk`` characters, overlapping by
      ``_JSON_CHUNK_OVERLAP`` down to a half-window floor -- the window
      is clamped to >= 4096 upstream, so no signature is split by a
      seam.

    Remaining incompleteness is the capture cap truncating the body --
    _read_body flags it (partial-inspection hit), never silently.
    """
    try:
        data = json.loads(prefix)
    except Exception:
        return []
    vals = []
    total = 0
    stack = [data]
    while stack:
        obj = stack.pop()
        if isinstance(obj, str):
            if obj:
                vals.append(obj)
                total += len(obj)
        elif isinstance(obj, dict):
            for key, val in obj.items():
                if isinstance(key, str) and key:
                    vals.append(key)
                    total += len(key)
                stack.append(val)
        elif isinstance(obj, (list, tuple)):
            stack.extend(obj)
        if total > 2 * len(prefix):  # unreachable for parseable input
            break
    if not vals:
        return []
    joined = "\n".join(vals)
    if len(joined) <= chunk:
        return [joined]
    out = []
    # the step is floored at half a window so the candidate count stays a
    # small multiple of capture/chunk; the window itself is clamped to
    # >= 4096 upstream, so the overlap here is always the full
    # _JSON_CHUNK_OVERLAP and every signature is far shorter than a
    # half-window.
    step = max(chunk // 2, chunk - _JSON_CHUNK_OVERLAP)
    pos = 0
    while pos < len(joined):
        out.append(joined[pos:pos + chunk])
        pos += step
    return out


# ---------------------------------------------------------------------
# Structured request context for endpoint policies: policies match the
# OPERATION, never an incidental string inside the serialized body. We
# parse exactly what Odoo's own dispatchers parse (the JSON-RPC params
# envelope, the path-encoded dataset routes, the urlencoded form fields,
# the XML-RPC methodName element, the /report doc-ids path segment) into
# a small bounded field dict. Malformed or absent context means the
# structured policies cannot fire; the app rejects that same input
# itself, so there is no scan-free pass to hide in.
_XMLRPC_PATH_RE = re.compile(r"^/xmlrpc/(?:2/)?([a-z][a-z0-9_]{0,31})$",
                            re.IGNORECASE)
_XMLRPC_METHOD_RE = re.compile(
    rb"<methodName>\s*([A-Za-z0-9_.]{1,64})\s*</methodName>")
# cosmetic <model>.<method> suffix on the JSON-RPC dataset routes (odoo
# web/controllers/dataset.py). Informational fallback ONLY -- see
# _rpc_extract: the BODY carries the operation Odoo dispatches.
_DATASET_PATH_RE = re.compile(
    r"^/web/dataset/(?:call_kw|call_button|call|query)/([\w.]{1,80})$")
# /json/2/<model>/<method> (odoo rpc/controllers/json2.py, Json2Dispatcher):
# there the PATH args override the body (request.params = body | args),
# so the path segments ARE the authoritative operation.
_JSON2_PATH_RE = re.compile(
    r"^/json/2/([\w.]{1,80})/([\w.]{1,80})$", re.IGNORECASE)
_REPORT_IDS_RE = re.compile(r"^/report/[^/]+/[^/]+/([\d,]{1,8192})$")
_JSON_CTYPES = ("application/json", "application/json-rpc")
FIELD_CAP = 32

# credentials/tokens never enter the structured context: they must not be
# matchable, counter-derivable, or ever reachable from the traffic log.
_CREDENTIAL_PARAMS = frozenset({
    "password", "new_password", "confirm_password", "passwd", "pwd",
    "master_pwd", "master_password", "access_token", "api_key", "key",
    "session_id", "sid", "csrf_token", "token", "authorization",
    "secret", "recover_secret",
})


def _redact_query(qs):
    """Sanitise a query string for STORAGE in waf.hit.

    Rewrites ONLY the value of a credential-named pair to "[redacted]";
    the raw parameter name stays (the log must still show WHICH
    parameter was tried) and every other pair is kept byte-for-byte, so
    a stored hit still shows the attack payload as the rule engine saw
    it. Rule matching keeps the raw query: this runs at storage time
    only (see _buffer_hit).

    Names are matched url-decoded, stripped and case-folded; a pair
    without "=" is kept as is. Never raises: a query the parser chokes
    on is replaced wholesale rather than stored unsanitised.
    """
    try:
        out = []
        for pair in (qs or "").split("&"):
            name, sep, _value = pair.partition("=")
            if sep and (unquote_plus(name).strip().lower()
                        in _CREDENTIAL_PARAMS):
                pair = name + "=[redacted]"
            out.append(pair)
        return "&".join(out)
    except Exception:
        return "[unparseable query redacted]"


def _kv_pairs(text):
    """name -> value pairs of a urlencoded query string or form body.

    Parameter PRESENCE follows the application: werkzeug builds a
    MultiDict and ``dict(...)`` -- what Request.get_http_params() merges
    -- keeps the FIRST occurrence of a repeated name, empty value or
    not. So the first occurrence is kept and the rest are dropped:
    counting a later duplicate would move the counted target away from
    the parameter the controller actually reads (the multipart twin
    below follows the same rule).

    An empty first value stays "": it still shadows the query string in
    the _rpc_extract merge, because an empty parameter is what the app
    sees there. Counting the query value instead would blame someone
    else for an empty-field request.
    """
    out = {}
    if not text:
        return out
    try:
        for name, vals in parse_qs(text, keep_blank_values=True).items():
            lname = name[:40].lower()
            if lname in _CREDENTIAL_PARAMS or lname in out:
                continue          # first occurrence wins, like the app
            value = (vals[0] if vals else "").strip()
            out[lname] = value[:80].lower()
            if len(out) >= FIELD_CAP:
                break
    except Exception:
        return out
    return out


def _ids_from_list(value):
    if isinstance(value, list):
        return len(value)
    if isinstance(value, str) and value.strip():
        return min(value.count(",") + 1, 10000)
    return None


def _mask_secret(value):
    """Display form for account identifiers in logs: first two characters
    only (enough to tell two targets apart, never a full login)."""
    value = value or ""
    return (value[:2] + "***") if len(value) > 2 else (value[:1] + "***")


def _multipart_fields(prefix, ctype_full):
    """Form fields of a multipart body as name -> value pairs, for the
    structured policy layer. odoo's Request.get_http_params() merges
    multipart form fields into request.params exactly like urlencoded
    ones, so a signup/reset POST split over multipart must not hide its
    login target from the counters. File parts carry no string parameter
    the controller could read as a target; skipped, as in
    _multipart_text.

    A repeated field name resolves the way werkzeug reads it: the FIRST
    part's complete value (its continuation chunks included) and no
    later part. Concatenating duplicates instead would change the WAF's
    counter key on every request while the controller keeps targeting
    the same account.
    """
    if _MPDecoder is None:  # pragma: no cover
        return {}
    try:
        if _mp_parse_options_header is not None:
            boundary = _mp_parse_options_header(ctype_full)[1].get("boundary", "")
        else:  # pragma: no cover - matches formparser's own encoding
            boundary = _content_boundary(ctype_full)
        if not boundary:
            return {}
        decoder = _MPDecoder(boundary.encode("ascii"))
        decoder.receive_data(prefix)
        chunks = {}
        current = None              # field name collecting value bytes
        while True:
            event = decoder.next_event()
            if isinstance(event, _MPFile):
                current = None
            elif isinstance(event, _MPField):
                name = (event.name or "").strip()[:40].lower()
                if name and name not in _CREDENTIAL_PARAMS \
                        and name not in chunks \
                        and len(chunks) < FIELD_CAP:
                    # first occurrence only: a second part with this name
                    # contributes nothing, like dict(MultiDict)
                    current = name
                    chunks[current] = []
                else:
                    current = None
            elif isinstance(event, _MPData):
                # continuation chunks of the FIRST occurrence keep being
                # collected (a long value arrives in several events)
                if current is not None and event.data \
                        and sum(map(len, chunks[current])) < 240:
                    chunks[current].append(event.data)
            elif isinstance(event, _MPNeedData):
                break               # capture ended (body hit the cap)
        out = {}
        for name, parts in chunks.items():
            # "" kept: the field WAS in the body, so it shadows the query
            # string in _rpc_extract even when empty
            out[name] = b"".join(parts).decode("utf-8", "replace") \
                          .strip()[:80].lower()
        return out
    except Exception:
        return {}                   # undecodable: no field evidence at all


def _rpc_extract(req):
    """Populate req["rpc"] = {"fields": {...}, "unverified": bool} and
    req["ids_count"] from the parsed request. Every extraction is
    bounded (prefix already capped; field/values capped; ids counted
    only up to a cap).

    Authority rules -- the WAF matches the SAME parameters Odoo's own
    dispatchers use (validated against odoo 19 http.py,
    web/controllers/dataset.py and the rpc add-on):
    * /jsonrpc and /web/dataset/call_* run on JsonRPCDispatcher:
      request.params = the parsed BODY's params (plus path args). The
      body's model/method decide what executes; the cosmetic
      /web/dataset/call_kw/<model>.<method> suffix only decorates the
      route match. So the body is parsed FIRST and the suffix fills in
      only when the body said nothing -- a res.partner.read suffix
      wrapping a res.users/write body IS a res.users/write call.
    * /json/2/<model>/<method> runs on Json2Dispatcher where params =
      body | path args -- there the path segments are authoritative.
    * form/query merge mirrors get_http_params(): {**args, **form,
      **files} -- the POST body wins over the query string, so a
      "?login=decoy" cannot move the counted reset target behind a
      form body's login.
    """
    path = req.get("path") or ""
    fields = {}

    def add(name, value):
        if isinstance(value, str):
            value = value.strip()
            if value:
                fields.setdefault(name, value[:80].lower())

    def force(name, value):
        if isinstance(value, str) and value.strip():
            fields[name] = value.strip()[:80].lower()

    ids_count = None
    prefix = req.get("body_prefix") or b""
    ctype = req.get("body_ctype") or ""

    # XML-RPC: the service IS the path (both route families); method name
    # from the XML element, found by a bounded search over the capture.
    m = _XMLRPC_PATH_RE.match(path)
    if m:
        add("service", m.group(1))
        if prefix:
            mm = _XMLRPC_METHOD_RE.search(prefix[:16384])
            if mm:
                add("method", mm.group(1).decode("ascii", "replace"))

    # JSON-2: path args override the body (jsonrequest | args), so they
    # are authoritative here -- force, and the body arm below cannot
    # overwrite them (add() only fills gaps).
    m = _JSON2_PATH_RE.match(path)
    if m:
        force("model", m.group(1))
        force("kw_method", m.group(2))

    # JSON envelopes: JsonRPCDispatcher hands the controller the parsed
    # body; same bytes, same parser.
    unverified = False
    if prefix and ctype in _JSON_CTYPES:
        try:
            envelope = json.loads(prefix)
        except Exception:
            envelope = None
            if _RPC_JSON_PATH_RE.match(path) and req.get("rpc_truncated"):
                # the dispatcher reads the full stream, so an operation
                # exists that this capped capture can never parse: mark the
                # structured view "operation unverified" rather than
                # silently empty. Policies that opted into fail-closed
                # (on_unverified="match") can act on it; every other policy
                # still cannot fire on missing evidence. A body NOT
                # truncated (garbage JSON the app itself rejects with a
                # 400) is not a blind spot and is never marked.
                unverified = True
        if isinstance(envelope, dict):
            params = envelope.get("params")
            params = params if isinstance(params, dict) else envelope
            if path == "/jsonrpc":
                add("service", params.get("service"))
                add("method", params.get("method"))
                args = params.get("args")
                if isinstance(args, list):
                    if (len(args) >= 5 and isinstance(args[3], str)
                            and isinstance(args[4], str)):
                        # execute_kw(db, uid, password, model, method, ids)
                        add("model", args[3])
                        add("kw_method", args[4])
                        if len(args) >= 6:
                            ids_count = _ids_from_list(args[5])
            else:
                # route-style JSON bodies (call_kw params, /web/database/list)
                add("service", params.get("service"))
                add("model", params.get("model"))
                add("kw_method", params.get("method"))
                args = params.get("args")
                if isinstance(args, list) and args:
                    ids_count = _ids_from_list(args[0])

    # cosmetic dataset suffix: informational fallback only (authority
    # rules above) -- body values are already in place via add() and win.
    # Skipped when the operation is unverified: the suffix must never
    # pose as evidence about a body this scanner could not parse.
    if not fields.get("model") and not unverified:
        m = _DATASET_PATH_RE.match(path)
        if m:
            model, _, method = m.group(1).rpartition(".")
            if model and model != path:
                add("model", model)
                add("kw_method", method)

    # form/query fields: signup+reset targets, export id lists. The body
    # wins over the query string (get_http_params parity, above). A name
    # the body sent is never taken from the query string -- not even when
    # its value is empty, because that is what the controller receives,
    # and an empty login targets nobody.
    merged = {}
    if ctype == "application/x-www-form-urlencoded" and prefix:
        merged.update(_kv_pairs(prefix.decode("utf-8", "replace")))
    merged.update(req.get("multipart_fields") or {})
    for key, value in _kv_pairs(req.get("query") or "").items():
        merged.setdefault(key, value)
    for key, value in merged.items():
        if value:
            fields.setdefault(key, value)
    if ids_count is None:
        for name in ("ids", "id", "docids"):
            if merged.get(name):
                ids_count = _ids_from_list(merged[name])
                break

    # /report/<converter>/<reportname>/<docids> -- the id list IS a path
    # segment, no body parsing involved.
    if ids_count is None:
        m = _REPORT_IDS_RE.match(path)
        if m:
            ids_count = min(m.group(1).count(",") + 1, 10000)

    if fields or unverified:
        rpc = {"fields": fields}
        if unverified:
            rpc["unverified"] = True
        req["rpc"] = rpc
    if ids_count is not None:
        req["ids_count"] = min(ids_count, 10000)


# no_session rule exemptions ONLY for requests whose cookie resolves to a
# real, authenticated server-side session of an *internal* user AND that
# target these RPC endpoints, where logged-in users legitimately post
# rich/payload-like content (ORM calls, report designers, HTML editors).
# Cookie presence alone proves nothing: forged strings resolve to no
# session at all, and anonymous sessions carry no uid. The uid must also
# be an INTERNAL user (core's stored res_users.share flag): portal and
# community users (free sign-up sites!) otherwise gain the exemption and
# skip every no_session rule on the RPC surface.
_SESSION_TRUST_PATHS = (
    "/web/dataset", "/web/action", "/web/report", "/json", "/websocket",
)


def _in_rpc_paths(path):
    # segment-boundary matching: "/json" must not match "/jsonrpc"
    return any(path == p or path.startswith(p + "/")
               for p in _SESSION_TRUST_PATHS)


def _internal_uid(dbname, uid):
    """Is *uid* an internal user (member of base.group_user)?

    Small per-worker TTL cache so authenticated RPCs pay at most one cheap
    SQL per user per interval. Fails closed (no exemption) on any error.
    """
    now = time.monotonic()
    cache = _uid_cache.setdefault(dbname, {})
    hit = cache.get(uid)
    if hit and hit[0] > now:
        return hit[1]
    internal = False
    try:
        with db_connect(dbname).cursor() as cr:
            cr.rollback()  # fresh transaction (see _fetch_snapshot)
            # res_users.share is core's stored "not internal" flag;
            # AND active mirrors core: an archived user's leftover
            # session file must not keep the no_session exemption.
            cr.execute("SELECT share FROM res_users "
                       "WHERE id = %s AND active LIMIT 1",
                       (uid,))
            row = cr.fetchone()
            internal = row is not None and not row[0]
    except Exception:
        internal = False
    if len(cache) >= 4096:
        cache.clear()
    cache[uid] = (now + UID_TTL, internal)
    return internal


def _session_uid(session, dbname):
    """The session's uid, but ONLY when the session verifiably belongs
    to *dbname*; None means "anonymous here".

    _resolve_db() may lawfully land on a database other than the
    session's own (dbfilter rejects session.db and the X-Odoo-Database
    header or the monodb fallback wins). A uid is just an id in the
    session's OWN res_users table: handed out unfiltered, a session from
    database A could pose as a user of database B -- win the no_session
    exemption there (uid 2 is admin everywhere) and steer that
    database's uid/ip_uid rate buckets.
    Must never raise: session payloads are external data.
    """
    try:
        if not session:
            return None
        uid = session.get("uid")
        if not (isinstance(uid, int) and uid > 0):
            return None
        if session.get("db") != dbname:
            return None
        return uid
    except Exception:
        return None


def _trusted_session(session, path, dbname):
    """True only for a *verified* internal user (real session file,
    group claim present) whose session BELONGS TO the policy database,
    on an endpoint with legitimate rich payloads.

    Accepted risk (documented in the README): the payload's
    ``session_token`` is NOT re-verified here. A password change or TOTP
    reset makes core reject the old session, but that only bites when
    the app itself inspects the token -- until then the WAF still sees
    the session as trusted. Recomputing ``_compute_session_token``
    (extra query + password hash) on every request's hot path was
    considered and rejected; the blast radius is bounded: it only
    relaxes no_session-tagged rules, only for genuine internal sessions,
    only on the RPC paths where such payloads are legitimate anyway.
    """
    if not session:
        return False
    uid = _session_uid(session, dbname)
    if uid is None or not _in_rpc_paths(path):
        return False
    return _internal_uid(dbname, uid)


def _extract(environ, snap, session, dbname):
    path = environ.get("PATH_INFO", "/")
    req = {
        "method": environ.get("REQUEST_METHOD", "GET"),
        "path": path,
        "query": environ.get("QUERY_STRING", ""),
        "ua": environ.get("HTTP_USER_AGENT", ""),
        "has_session": _trusted_session(session, path, dbname),
        "ip": iputil.resolve_client_ip(
            environ.get("REMOTE_ADDR"), environ.get("HTTP_X_REAL_IP"),
            environ.get("HTTP_X_FORWARDED_FOR"), snap["trusted_networks"]),
        "norm_body": None,
    }
    # verified authenticated identity for rate keys: the uid comes from the
    # SERVER-SIDE session store (odoo's session files), not from cookie or
    # Authorization header contents -- those prove nothing at this layer.
    # The session must also BELONG to the selected database (_resolve_db can
    # land on another one); a foreign uid would misattribute this database's
    # rate buckets. On a mismatch uid simply stays unset: the rate key falls
    # back to the IP, as for an anonymous request.
    uid = _session_uid(session, dbname)
    if uid is not None:
        req["uid"] = uid
    scoped_scopes = set(snap["rules"])
    if STATIC_RE.match(req["path"]):
        req["static"] = True   # engine: only url/ua rules apply to static files
        scoped_scopes &= {"url", "ua"}
    else:
        _read_body(environ, snap, req)
    if "url" in scoped_scopes:
        req["norm_path"] = normalize(req["path"])
    if "query" in scoped_scopes:
        req["norm_query"] = normalize(req["query"], decode_plus=True)
    if "ua" in scoped_scopes:
        req["norm_ua"] = normalize(req["ua"])
    if "header" in scoped_scopes:
        req["headers_blob"] = normalize_headers(
            environ, ["x-forwarded-for", "x-real-ip", "x-forwarded-host",
                      "referer"], trusted=snap["trusted_networks"])
    if (snap.get("wants_rpc") or snap.get("wants_ids")) \
            and not req.get("static"):
        _rpc_extract(req)
    return req


def _buffer_hit(dbname, req, decision, snap, country):
    global _hit_buffer_t0
    now = time.time()
    ref = uuid.uuid4().hex[:10]
    with _hit_lock:
        if _hit_buffer_t0 is None:
            _hit_buffer_t0 = now
        _hit_buffer.append((dbname, {
            "ref": ref,
            "ip": req.get("ip") or "",
            "country": country or "",
            "method": req.get("method") or "",
            "path": (req.get("path") or "")[:512],
            # storage-time sanitising only: rule matching already saw the
            # raw query; every waf.hit writer funnels through here (guard,
            # _pre_db_guard, recovery visits, masterbp's _log_event), so
            # this single cut keeps credential values out of the log
            "query": _redact_query(req.get("query") or "")[:1024],
            "ua": (req.get("ua") or "")[:512],
            "rule_id": decision.get("rule_id"),
            "rule_name": (decision.get("rule_name") or "")[:256],
            "category": (decision.get("category") or decision["kind"])[:64],
            "severity": decision.get("severity") or 0,
            "kind": decision["kind"],
            "action": decision["action"],
            "mode": snap["mode"],
            "blocked": decision.get("blocked", False),
        }))
        full = len(_hit_buffer) >= FLUSH_MAX or \
            now - _hit_buffer_t0 > FLUSH_AGE
    decision["ref"] = ref
    if full:
        flush_hits()


def flush_if_stale(now=None):
    """Time-based flush, independent of new hits arriving.

    Without this a quiet worker keeps its buffered hits until the *next*
    hit (or never), and they are lost on restart. Called from the request
    path at most once per FLUSH_AGE seconds (one cheap compare otherwise);
    the batched write itself stays batched, not per-request.
    """
    global _next_flush_ts
    now = time.monotonic() if now is None else now
    if now < _next_flush_ts:
        return
    _next_flush_ts = now + FLUSH_AGE
    flush_hits()


def flush_hits():
    """Batch-write buffered hits; never raise, never lose the request."""
    global _hit_buffer, _hit_buffer_t0, _last_error_ts
    with _hit_lock:
        if not _hit_buffer:
            return
        # atomic swap: concurrent appends land in the fresh list, never in
        # the batch being written (threaded workers share this process)
        batch, _hit_buffer, _hit_buffer_t0 = _hit_buffer, [], None
    by_db = {}
    for dbname, values in batch:
        by_db.setdefault(dbname, []).append(values)
    for dbname, rows in by_db.items():
        try:
            with db_connect(dbname).cursor() as cr:
                cr.rollback()  # fresh transaction (see _fetch_snapshot)
                env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
                env["waf.hit"].create(rows)
                cr.commit()
        except Exception:
            # One bad row (unknown Selection value, field typo) must not
            # discard the whole batch: retry row by row, each in its own
            # savepoint so a failure only undoes itself. The survivors are
            # committed once after the loop, and `stored` counts only rows
            # whose savepoint exited cleanly -- so the warning below states
            # what really survives.
            stored = 0
            try:
                with db_connect(dbname).cursor() as cr:
                    cr.rollback()
                    env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
                    for values in rows:
                        try:
                            with cr.savepoint():
                                env["waf.hit"].create(values)
                        except Exception:
                            continue
                        stored += 1
                    cr.commit()
            except Exception:
                stored = 0
            # same 30s throttle as guard(): without recording the
            # timestamp here, every failed flush while the DB is down
            # logged its own full traceback
            now = time.monotonic()
            if now - _last_error_ts > 30:
                _last_error_ts = now
                _logger.warning("WAF: could not flush hits for %s "
                                "(%d of %d stored)", dbname, stored,
                                len(rows), exc_info=True)


def _email_base_url(env, environ):
    """Absolute base for emailed links -- NEVER derived from client headers.

    Building the link from Host / X-Forwarded-Host lets an attacker trigger
    a lockout with their own Host header and phish the *valid* recovery
    token out of the admin's click (the mail client follows the link on the
    attacker's host, which captures it and replays it for real).
    web.base.url is the admin-owned setting every core outbound link uses;
    the fallback only reads socket-level values the client cannot influence.
    """
    try:
        base = env["ir.config_parameter"].sudo().get_param("web.base.url", "")
    except Exception:
        base = ""
    base = (base or "").rstrip("/")
    if base:
        return base
    scheme = environ.get("wsgi.url_scheme") or "https"
    server = environ.get("SERVER_NAME") or "localhost"
    port = environ.get("SERVER_PORT") or ""
    if port and port not in ("80", "443"):
        server = "%s:%s" % (server, port)
    return "%s://%s" % (scheme, server)


def _alert_budget_ok(dbname):
    """Rate-limit lockout alert e-mails per database (bounded inbox
    flooding and mail volume under distributed attack). Callers still
    apply the block itself -- only the mail is dropped."""
    dq = _alert_history.setdefault(dbname, deque())
    now = time.monotonic()
    while dq and now - dq[0] > ALERT_WINDOW:
        dq.popleft()
    if len(dq) >= ALERT_MAX_PER_WINDOW:
        _logger.warning(
            "WAF: lockout alert e-mail budget exhausted for %s", dbname)
        return False
    dq.append(now)
    return True


def _recent_network_alert(dbname, network):
    """True (suppress) if we already mailed for this exact network inside
    the window; the emailed link would unblock the same entry anyway."""
    key = (dbname, network)
    now = time.monotonic()
    last = _recent_alerts.get(key, 0.0)
    if now - last < ALERT_WINDOW:
        return True
    _recent_alerts[key] = now
    if len(_recent_alerts) > 10000:
        _recent_alerts.clear()
    return False


_overflow_seen = {}       # (dbname, ip) -> last partial-inspection note ts


def _overflow_note_recent(dbname, ip):
    """True (suppress) if the partial-inspection coverage note already
    fired for this db+IP inside the window -- staff uploading legitimate
    >cap JSON-RPC attachments must not flood the traffic log, and the
    map itself stays bounded."""
    key = (dbname, ip)
    now = time.monotonic()
    last = _overflow_seen.get(key, 0.0)
    if now - last < ALERT_WINDOW:
        return True
    _overflow_seen[key] = now
    if len(_overflow_seen) > 10000:
        _overflow_seen.clear()
    return False


LOCKOUT_RETENTION = 2 * 3600  # seconds; keep aged-out counter rows briefly
LOCKOUT_PRUNE_EVERY = 10 * 60  # housekeeping sweep at most this often
_PRUNE_TABLES = ("waf_lockout_hit", "waf_login_hit", "waf_rate_hit")
    # fixed SQL table names: callers may only pick from this whitelist
    # (the name is interpolated into SQL)
_prune_ts = {}            # table -> monotonic ts of last sweep (per process)

# UPDATE-first bump, same shape as masterbp._UPDATE_SQL (see the race
# explained there); a lost first-insert race simply re-applies the UPDATE.
_COUNTER_UPDATE = """
                UPDATE {t} SET
                    window_start = CASE
                        WHEN {t}.window_start
                             <= %(now)s - make_interval(secs => %(window)s)
                        THEN %(now)s
                        ELSE {t}.window_start
                    END,
                    hits = CASE
                        WHEN {t}.window_start
                             <= %(now)s - make_interval(secs => %(window)s)
                        THEN 1
                        ELSE {t}.hits + 1
                    END
                WHERE {t}.network = %(net)s
                RETURNING hits
"""
_COUNTER_INSERT = """
                INSERT INTO {t} (network, window_start, hits)
                VALUES (%(net)s, %(now)s, 1)
                ON CONFLICT (network) DO NOTHING
                RETURNING hits
"""


def _bump_counter(cr, table, params):
    """The UPDATE-first bump on an open cursor; returns the new hit
    count, or None if no branch touched a row (the row aged out and was
    pruned between the losing INSERT and the re-applied UPDATE)."""
    cr.execute(_COUNTER_UPDATE.replace("{t}", table), params)
    row = cr.fetchone()
    if row is None:                    # this network's first hit
        cr.execute(_COUNTER_INSERT.replace("{t}", table), params)
        row = cr.fetchone()
        if row is None:                # lost a first-insert race
            cr.execute(_COUNTER_UPDATE.replace("{t}", table), params)
            row = cr.fetchone()
    return row[0] if row else None


def _bump_lockout(dbname, network, window, table="waf_lockout_hit"):
    """Atomically add one hit to a per-network counter IN THE DB.

    Two counters share this machinery, never each other's rows:
    waf_lockout_hit credits rule-matched attack signatures, waf_login_hit
    credits failed logins.

    The deque in engine.evaluate is one worker process's local view, so
    with N workers (SO_REUSEPORT spreads nginx upstream connections
    round-robin) the configured threshold effectively becomes N x max;
    one shared row per network makes it mean what it says. The bump runs
    only for rule-matched block/auto hits in enforce mode, never for
    clean traffic (guard() skips the call while learning).

    Tumbling window: when the row's previous window has fully aged out,
    the count restarts at 1. A block or a recovery link deletes the row
    outright: a row parked at >= max would re-block the victim on its
    very next flagged hit.

    The bump is the masterbp._bump shape (see masterbp._UPDATE_SQL):
    UPDATE-first inside a SET LOCAL READ COMMITTED transaction, bounded
    retries on 40001/40P01; the prune DELETE rides the same transaction.

    Returns the shared hit count, or None if the round-trip failed --
    the caller then falls back to the worker-local deque (degraded, but
    a DB hiccup must never disable lockout entirely).
    """
    if table not in _PRUNE_TABLES:
        table = _PRUNE_TABLES[0]  # never interpolate anything but our own
    try:
        now = odoo.fields.Datetime.now()  # naive UTC, matches columns
        prune = time.monotonic() - _prune_ts.get(table, 0.0) > LOCKOUT_PRUNE_EVERY
        with db_connect(dbname).cursor() as cr:
            cr.rollback()  # fresh transaction (see _fetch_snapshot)
            params = {"net": network, "now": now, "window": window}
            hits = None
            for attempt in range(4):
                try:
                    cr.execute("SET LOCAL TRANSACTION ISOLATION LEVEL "
                               "READ COMMITTED")
                    hits = _bump_counter(cr, table, params)
                    break
                except Exception as exc:
                    cr.rollback()  # txn aborted; fresh one for retry
                    if (attempt >= 3
                            or getattr(exc, "pgcode", None)
                            not in ("40001", "40P01")):
                        raise
                    time.sleep(0.02 * (attempt + 1))
            if hits is None:
                return None  # row vanished mid-bump (prune race): same
                             # degraded contract as a failed round-trip
            if prune:
                # housekeeping: rows stop mattering once their window
                # aged out; sweep at most once per LOCKOUT_PRUNE_EVERY
                # per process (and per table) so an attack flood never
                # turns every request into an extra table scan
                _prune_ts[table] = time.monotonic()
                cr.execute(
                    "DELETE FROM {t} "
                    "WHERE window_start <= %s - make_interval(secs => %s)"
                    .replace("{t}", table),
                    (now, LOCKOUT_RETENTION))
            cr.commit()
            return hits
    except Exception:
        return None


def _apply_lockout(dbname, req, decision, snap, environ,
                   counter_table="waf_lockout_hit", counter_key=None,
                   minutes=None):
    """Create the automatic deny entry, end the counter window and
    queue the recovery-link mail.

    ``counter_table`` names the shared counter whose window ends with the
    block (attack credit, login credit, or an endpoint rate counter --
    validated against the SQL-name whitelist); ``counter_key`` is that
    row's key when it is not the blocked network itself (rate counters
    key on bare IPs, user ids, target hashes ...). ``minutes`` overrides
    the lockout duration (rate policies configure their own; the default
    is the signature auto-lockout duration)."""
    if counter_table not in _PRUNE_TABLES:
        counter_table = _PRUNE_TABLES[0]
    ip = req.get("ip")
    if not ip:
        return
    try:
        with db_connect(dbname).cursor() as cr:
            cr.rollback()  # fresh transaction (see _fetch_snapshot)
            env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {"lang": "en_US"})
            cfg = env["waf.config"].get_singleton()
            host_mask = "/128" if ":" in ip else "/32"
            network = ip + host_mask
            # the counter's only job was to reach this block: a row
            # parked at >= max would re-block the network the moment its
            # entry is lifted (recovery link or expiry). End the window
            # here, in the same transaction as the entry, BEFORE the
            # early exits below.
            cr.execute(f"DELETE FROM {counter_table} WHERE network = %s",
                       (counter_key or network,))
            # do not stack a second entry/mail if one is already in
            # force -- "in force" means what the engine enforces: active
            # AND not yet expired. The hourly cleanup lags, so an
            # expired-but-not-yet-deactivated entry must not mask a
            # fresh lockout.
            if env["waf.ip.entry"].search_count([
                    ("network", "=", network), ("kind", "=", "deny"),
                    ("source", "=", "auto"), ("active", "=", True),
                    "|", ("expires", "=", False),
                    ("expires", ">", odoo.fields.Datetime.now())]):
                cr.commit()
                return
            entry = env["waf.ip.entry"].create({
                "network": network,
                "kind": "deny",
                "source": "auto",
                "reason": "%s (%d hits)" % (
                    decision.get("rule_name"),
                    decision.get("auto_hits",
                                  decision.get("rate_hits", 0))),
                "expires": cfg._now_plus_minutes(
                    cfg.auto_lockout_minutes if minutes is None else minutes),
            })
            if not (_alert_budget_ok(dbname)
                    and not _recent_network_alert(dbname, network)):
                cr.commit()
                return
            token = recovery.make_token(cfg.recover_secret, dbname, network)
            url = f"{_email_base_url(env, environ)}/waf/recover/{token}"
            cfg._notify_admins(
                subject="OW Shield: automatic IP block applied",
                body=(
                    f"<p>The OW Shield automatically blocked <b>{ip}</b> "
                    f"(rule: {html.escape(str(decision.get('rule_name')))}).</p>"
                    f"<p>If this was you, use this recovery link (valid 15 "
                    f"minutes): "
                    f"<a href=\"{html.escape(url)}\">{html.escape(url)}</a></p>"
                    f"<p>The link only lifts this <i>automatic</i> block -- "
                    f"normal firewall rules apply to the address again "
                    f"afterwards.</p>"
                    f"<p>Entry: {entry.network} expires automatically.</p>"
                ),
            )
            cr.commit()
    except Exception:
        global _last_error_ts
        now = time.monotonic()
        if now - _last_error_ts > 30:
            _last_error_ts = now
            _logger.warning("WAF: could not apply auto lockout", exc_info=True)
    finally:
        # make *this* worker re-read the policy immediately: its cached
        # snapshot does not know about the entry it just created, and
        # until SNAP_TTL expires it would keep rule-deciding (and thus
        # DB-crediting) the very network it blocked. Other workers pick
        # the entry up within SNAP_TTL on their own.
        _snaps.pop(dbname, None)


_JSON_PATHS = ("/jsonrpc", "/web/dataset", "/xmlrpc/", "/web/service",
               "/json/")
# exact-match additions: blocked responses that must speak JSON because the
# route itself is a JSON-RPC/JSON endpoint even though no prefix matches
_JSON_EXACT = {"/json", "/web/database/list"}


def _blocked_response(ref, environ, start_response):
    path = environ.get("PATH_INFO", "")
    if path in _JSON_EXACT or any(path.startswith(p) for p in _JSON_PATHS):
        body = json.dumps({
            "jsonrpc": "2.0", "id": None,
            "error": {"code": 403, "message": "Blocked by OW Shield",
                      "data": {"name": "WAF", "message": f"Blocked by OW Shield (ref {ref})"}},
        }).encode()
        ctype = "application/json"
    else:
        safe_ref = html.escape(ref)
        body = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<title>Blocked by the OW Shield</title>"
            "<style>body{font-family:sans-serif;max-width:640px;margin:60px auto;"
            "color:#333}h1{color:#c0392b}code{background:#eee;padding:2px 6px}"
            "</style></head><body>"
            "<h1>Access blocked</h1>"
            "<p>Your request was blocked by <b>OW Shield</b>. "
            "If you believe this is an error, contact the administrator and quote "
            f"this reference: <code>{safe_ref}</code></p>"
            "<p>If the block was automatic and you are an administrator, check your "
            "e-mail for a recovery link.</p>"
            "</body></html>"
        ).encode()
        ctype = "text/html; charset=utf-8"
    start_response("403 Forbidden", [
        ("Content-Type", ctype),
        ("Content-Length", str(len(body))),
        ("X-Content-Type-Options", "nosniff"),
        ("X-WAF-Ref", ref),
    ])
    return [body]


# ---------------------------------------------------------------- entry point

_PRE_DB_PATH_RE = re.compile(r"^/web/database(?:/|$)")


def _pre_db_guard(environ, start_response):
    """Database-manager protection across the pre-database window.

    Odoo serves the /web/database route group WITHOUT any database: hosts
    that match no dbfilter are still served the manager, so the normal
    per-database policy binding cannot apply to them. For that route
    group, and only for policies flagged apply_pre_db, borrow the
    endpoint policies and IP entries of the first loaded database with a
    snapshot. A borrowed refusal cannot break any workflow on a host
    that serves no database from that db; the alternative is a scan-free
    hole on the most dangerous route group. Every other route keeps
    strict hostname/database boundaries.
    """
    norm_path = normalize(environ.get("PATH_INFO", "/"))
    if not _PRE_DB_PATH_RE.search(norm_path):
        return None
    method = environ.get("REQUEST_METHOD", "GET")
    for dbname in sorted(_loaded_dbs()):
        snap = _get_snapshot(dbname)
        if not snap:
            continue  # db without OW Shield policy (uninstalled/off)
        ip_str = iputil.resolve_client_ip(
            environ.get("REMOTE_ADDR"), environ.get("HTTP_X_REAL_IP"),
            environ.get("HTTP_X_FORWARDED_FOR"), snap["trusted_networks"])
        ip_obj = iputil.parse_ip(ip_str) if ip_str else None
        req = {
            "method": method,
            "path": environ.get("PATH_INFO", "/"),
            "query": environ.get("QUERY_STRING", ""),
            "ua": environ.get("HTTP_USER_AGENT", ""),
            "ip": ip_str or "",
        }
        if ip_obj is not None:
            for net, _e, _r in snap["allow_entries"]:
                if ip_obj in net:
                    return None  # allowlisted on the borrowed db
            for net, _e, reason in snap["deny_entries"]:
                if ip_obj in net:
                    decision = {
                        "kind": "ip", "action": "block", "severity": 5,
                        "rule_name": (f"blocked ip ({reason})" if reason
                                      else "blocked ip"),
                        "rule_id": None,
                    }
                    _buffer_hit(dbname, req, decision, snap, None)
                    return lambda e, sr: _blocked_response(
                        decision["ref"], e, sr)
        pol = None
        for p in snap["policies"]:
            if not p["apply_pre_db"] or not p["regex"].search(norm_path):
                continue
            if p["methods"] and method.upper() not in p["methods"]:
                continue
            if ip_obj is not None and p["exempt"] \
                    and iputil.in_any(ip_obj, p["exempt"]):
                continue  # trusted management network exception
            pol = p
            break
        if pol is None:
            # a later database may still cover this route; deny/allow
            # entry handling above already decided for this db, only the
            # policy match moves on
            continue
        decision = {
            "kind": "endpoint",
            "action": pol["action"],
            "severity": pol["severity"],
            "rule_id": None,
            "rule_name": f"{pol['name']} (pre-database)",
            "category": pol["category"],
        }
        decision["blocked"] = bool(pol["action"] == "block"
                                   and snap["mode"] == "enforce")
        _buffer_hit(dbname, req, decision, snap, None)
        if decision["blocked"]:
            _logger.warning(
                "WAF blocked %s %s from %s before database selection "
                "(%s, ref %s)", method, req["path"], req["ip"],
                decision["rule_name"], decision.get("ref"))
            return lambda e, sr: _blocked_response(decision["ref"], e, sr)
        return None
    return None  # no loaded registry has a policy yet (documented fail-open)


def guard(self, environ, start_response, original_call):
    """Wrap of Application.__call__; returns a WSGI app or None to continue."""
    try:
        flush_if_stale()  # time-based, throttled: keeps quiet workers from
                          # sitting on buffered hits until restart
        handled = masterbp.handle_request(environ, start_response)
        if handled is not None:
            # master-password lockout active for this source: refused
            # BEFORE routing, before any password check or db-management
            # operation
            return handled
        session = _load_session(environ)
        dbname = _resolve_db(environ, session)
        if not dbname:
            return _pre_db_guard(environ, start_response)
        snap = _get_snapshot(dbname)
        if not snap or snap["mode"] == "off":
            return None
        req = _extract(environ, snap, session, dbname)
        counters = _counters.setdefault(dbname, {})
        if _credit_mode.get(dbname) != snap["mode"]:
            # worker-local deque credit learned under another mode (or
            # another life of this process) must not leak into this one.
            # Mode flips are rare admin actions; wiping the free
            # in-memory fallback on them costs nothing and removes a
            # class of phantom lockouts.
            _credit_mode[dbname] = snap["mode"]
            counters.clear()
        country = _geo_country(req["ip"])
        if req["method"] == "GET" and req["path"].startswith("/waf/recover/"):
            # recovery links must reach the token check even from a blocked
            # IP; the controller validates the HMAC signature + expiry and
            # then lifts only the automatic deny entry the token names --
            # it can never grant an allowlist, so the link is not a
            # rule bypass for whoever (or whatever) triggered the block.
            decision = {
                "kind": "recovery", "action": "log", "severity": 1,
                "rule_name": "recovery link visit", "category": "recovery",
                "blocked": False,
            }
            # the token itself never enters the traffic log: the URL that
            # carries it is the secret (15-min window aside, log readers
            # must not be able to replay it)
            req["path"] = "/waf/recover/[redacted]"
            _buffer_hit(dbname, req, decision, snap, country)
            return None
        decision = engine.evaluate(snap, req, counters=counters, country=country)
        if req.get("body_overflow") and not _overflow_note_recent(
                dbname, req["ip"] or "?"):
            # the capture cap truncated a body type we DO inspect
            # (JSON/form/text/xml): everything past the cap went unseen
            # by the raw scan while the app parses it all. Record it
            # explicitly, independent of the rule decision: one log-only
            # waf.hit per db+IP per window, in both modes. Blocking on
            # overflow would break legitimate JSON-RPC attachments / bulk
            # writes; visibility does not wait for that roadmap decision.
            # (On JSON-RPC routes a body between body_scan_max and
            # RPC_PARSE_MAX IS fully parity-scanned decoded; the gap this
            # note reports there is the raw window only.)
            _buffer_hit(dbname, req, {
                "kind": "body", "action": "log", "severity": 1,
                "rule_name": "body exceeds scan cap: partial inspection",
                "category": "coverage", "blocked": False,
            }, snap, country)
        if not decision:
            return None
        if (decision.get("lockout_credit") and req["ip"]
                and snap["auto_lockout"]["enabled"]
                and snap["mode"] == "enforce"):
            # Cross-worker truth: credit the hit in the DB (see
            # _bump_lockout) and let the shared count drive the lockout.
            # Learning mode never applies lockouts, so the round-trip
            # there would be pure overhead during the phase with the most
            # false positives; the free in-memory deque still fills. When
            # the DB round-trip fails we fall back to engine.evaluate's
            # worker-local trigger, degrading to per-worker behaviour
            # instead of disabling lockout outright.
            shared = _bump_lockout(
                dbname,
                req["ip"] + ("/128" if ":" in req["ip"] else "/32"),
                snap["auto_lockout"]["window"])
            if shared is not None:
                # shared truth WINS. The engine's local trigger is
                # overwritten unconditionally -- below the threshold
                # means no lockout however much stale credit this
                # worker's deque held -- and the deque is dropped, so it
                # can never contradict the shared counter later either.
                # The local trigger remains in play only when this
                # round-trip failed (shared is None): degraded
                # per-worker operation.
                decision["auto_hits"] = shared
                decision["trigger_lockout"] = (
                    shared >= snap["auto_lockout"]["max"])
                counters.pop(req["ip"], None)
        rates = decision.get("rates")
        block_rate = None
        if rates:
            # Endpoint rate limits: one shared counter row per key
            # (waf_rate_hit, same machinery as the attack and login
            # counters) so the limit means the same whatever the worker
            # count. Counting runs in BOTH modes (burst visibility is the
            # point of the log-only policies); blocking and auto-deny
            # entries stay enforce-only. There is no per-worker fallback:
            # a worker-local view would silently multiply every limit. A
            # failed DB round-trip fails open. Several rate policies can
            # apply at once: every counter ticks, the strongest crossing
            # decides.
            crossed = []          # (rate, label) for each crossing
            block_rate = None
            for rate in rates:
                shared = _bump_lockout(dbname, rate["key"], rate["window"],
                                       table="waf_rate_hit")
                if shared is None:
                    continue
                decision["rate_hits"] = shared
                # rate_max is the number of matching requests ALLOWED per
                # window: the request that EXCEEDS it crosses (max=3 -> the
                # 4th is refused), matching the pack wording "up to N POSTs
                # per window".
                if shared <= rate["max"]:
                    continue
                label = f" ({shared} in {max(1, rate['window'] // 60)} min"
                if rate["key_by"] == "target":
                    fields = (req.get("rpc") or {}).get("fields") or {}
                    label += (", targeting "
                              f"{_mask_secret(fields.get(rate['param'], ''))}")
                label += ")"
                crossed.append((rate, label))
                if block_rate is None and rate["action"] == "block":
                    block_rate = rate
            if crossed:
                decision["rate_crossed"] = True
                for _rate, label in crossed:
                    decision["rule_name"] = \
                        (decision["rule_name"] + label)[:250]
        if rates and decision["kind"] == "endpoint":
            # the policies' own verdicts (engine.evaluate explains the
            # gating): an unthresholded block policy fires on every
            # matching request ("block_unconditional"); a rate-gated one
            # only once its own counter crossed; a merely counted request
            # below its limit stays unblocked AND unlogged (otherwise the
            # protected endpoints themselves would be a traffic-log
            # flood). force_log marks the exceeded-document-ids case.
            decision["blocked"] = bool(
                snap["mode"] == "enforce"
                and decision["action"] == "block"
                and (decision.get("block_unconditional")
                     or block_rate is not None
                     or decision.get("force_log")))
            if (crossed or decision["blocked"] or decision.get("force_log")
                    or decision.get("unthrottled")):
                _buffer_hit(dbname, req, decision, snap, country)
        else:
            decision["blocked"] = bool(
                decision["action"] == "block"
                and (snap["mode"] == "enforce" or decision["kind"] == "ip")
            )
            _buffer_hit(dbname, req, decision, snap, country)
        if decision["blocked"]:
            if decision.get("trigger_lockout"):
                _apply_lockout(dbname, req, decision, snap, environ)
            elif block_rate is not None and block_rate["minutes"] > 0:
                # crossed rate limit with an IP-cooling-off configured:
                # same expiring auto-deny + recovery-mail machinery as
                # the login brute-force lockout. The counter row's
                # window ends WITH the entry (a parked row would
                # re-block the instant the entry lifts); the entry
                # expires by itself, so nothing here is ever permanent.
                _apply_lockout(dbname, req, decision, snap, environ,
                               counter_table="waf_rate_hit",
                               counter_key=block_rate["key"],
                               minutes=block_rate["minutes"])
            _logger.warning(
                "WAF blocked %s %s from %s (%s, ref %s)", req["method"], req["path"],
                req["ip"], decision.get("rule_name"), decision.get("ref"))
            return lambda e, sr: _blocked_response(decision["ref"], e, sr)
        if decision.get("trigger_lockout") and snap["mode"] == "enforce":
            _apply_lockout(dbname, req, decision, snap, environ)
    except Exception:
        global _last_error_ts
        now = time.monotonic()
        if now - _last_error_ts > 30:
            _last_error_ts = now
            _logger.exception("WAF: error while inspecting request (fail-open)")
    return None


def install():
    """Patch odoo.http.Application.__call__ exactly once per process."""
    from odoo.http import Application
    if getattr(Application, "_waf_patched", False):
        return
    original = Application.__call__

    def wsgi_call(app_self, environ, start_response):
        # master-password attribution slot (see core/masterbp: odoo's
        # dispatch_rpc unbinds odoo.http.request for db-service calls,
        # so the wrapper needs this slot, always on the same thread
        # that later dispatches the request); end_request ALWAYS runs.
        masterbp.begin_request(environ)
        try:
            handled = guard(app_self, environ, start_response, original)
            if handled is not None:
                return handled(environ, start_response)
            return original(app_self, environ, start_response)
        finally:
            masterbp.end_request()

    Application.__call__ = wsgi_call
    Application._waf_patched = True
    # The master-password guard wraps the authoritative odoo check
    # (odoo.service.db.check_super) exactly once per process, so every
    # db-admin entry point -- web manager, XML-RPC, JSON-RPC -- is
    # counted at the single place odoo itself decides pass/fail.
    try:
        masterbp.install()
    except Exception:  # pragma: no cover - never break the server
        _logger.exception("OW Shield: master-password guard install failed")
    # graceful restarts/SIGTERM must not drop buffered hits
    atexit.register(flush_hits)


def reset_caches():
    _snaps.clear()
    _db_by_host.clear()
    _uid_cache.clear()
    _counters.clear()
    _credit_mode.clear()
    _snap_last_ok.clear()
    _snap_fail_ts.clear()
    masterbp.reset_cache()
    _geo["resolved"] = False
    _geo["reader"] = None
