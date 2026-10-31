"""Structured endpoint policies: route groups, RPC services, rate limits.

Distinct from waf.rule signature matching: a policy describes an *operation*
(a route family, an RPC service/method, an id-count limit), never an attack
signature, and is matched against a structured view of the request (parsed
RPC envelope, path, method, verified session; see core/engine.py). So a
business string that merely contains "db" cannot fire the database-service
policy.

Bundled policies ship in data/policies.yaml and are (re)loaded by
load_builtin_policies() on install AND upgrade.
"""
import os
import re

import yaml

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

POLICIES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "policies.yaml")

RATE_KEYS = [
    ("ip", "Per source IP"),
    ("user", "Per authenticated user (falls back to IP)"),
    ("ip_user", "Per IP + authenticated user"),
    ("target", "Per request target parameter (e.g. the login being reset)"),
]


class WafEndpointPolicy(models.Model):
    _name = "waf.endpoint.policy"
    _description = "Endpoint Policy"
    _order = "severity desc, category, code"

    code = fields.Char(index=True,
                       help="Stable identifier of a built-in policy "
                            "(e.g. waf-ep-dbmanager).")
    name = fields.Char(required=True)
    category = fields.Char(required=True, index=True, default="endpoint",
                           help="Policy group, used for filtering and reporting.")
    route_re = fields.Char(
        required=True,
        help="Python regular expression matched against the normalized URL "
             "path (whole-route-group policies anchor it: ^...$).")
    methods = fields.Char(
        help="Comma-separated HTTP methods this policy applies to "
             "(e.g. POST). Empty = any method.")
    rpc_service = fields.Char(
        help="Comma-separated RPC service names (exact match against the "
             "PARSED /jsonrpc envelope, e.g. db). A path-only policy leaves "
             "this empty. Never matched against the raw serialized body.")
    rpc_model = fields.Char(
        help="Comma-separated model names, matched against the parsed RPC "
             "call (e.g. res.users).")
    rpc_method = fields.Char(
        help="Comma-separated RPC/call_kw method names (e.g. export, "
             "list, drop).")
    on_unverified = fields.Selection(
        [("off", "Cannot match (missing evidence, fail open)"),
         ("match", "Treat as matching (hidden operation, fail closed)")],
        default="off", required=True,
        help="What this policy does when a request sits on its route "
             "group AND carries an RPC operation that could NOT be parsed "
             "(a JSON-RPC body padded past even the RPC parse capture). "
             "'off' (default): the policy cannot fire without evidence. "
             "'match': the hidden operation counts as matching this policy "
             "-- the padding itself is the evasion attempt, so high-risk "
             "surface policies (the bundled database-service one) refuse "
             "such requests when their action is Block. Cost: legitimate "
             "requests HUGE enough to hide their operation are refused on "
             "this policy too. Part of the built-in pack's operational "
             "shape: pack upgrades restore it.")
    ids_max = fields.Integer(
        default=0,
        help="Policy also flags requests whose parseable document-id count "
             "exceeds this (report doc-ids in the path, export id lists). "
             "With a rate limit this is only a visible mark; without one "
             "it is also the fire threshold: the policy acts only on "
             "requests ABOVE the limit. 0 = feature off.")
    action = fields.Selection(
        [("log", "Log only"), ("block", "Block")],
        default="log", required=True,
        help="Log: matching requests stay visible in the traffic log but are "
             "never refused. Block: refused with the generic block page while "
             "the firewall runs in Enforce mode.")
    severity = fields.Integer(default=4,
                              help="1 = informational, 5 = critical.")
    exempt_nets = fields.Text(
        help="CIDRs explicitly exempt from THIS policy (trusted management "
             "networks, e.g. the office VPN that may reach the database "
             "manager). One per line or comma-separated.")
    rate_max = fields.Integer(
        default=0,
        help="Requests matching this policy are counted in a counter shared "
             "by every worker process; this is the limit per window. "
             "0 = no rate limit (plain allow/log/block policy).")
    rate_window = fields.Integer(
        default=10, string="Rate window (minutes)")
    rate_key = fields.Selection(RATE_KEYS, default="ip", required=True,
                                help="What the rate counter keys on. 'user' "
                                     "uses the VERIFIED server-side session "
                                     "uid; a session cookie or Authorization "
                                     "header alone never counts as an "
                                     "authenticated user at this layer.")
    rate_param = fields.Char(
        default="login",
        help="With key 'target': the request parameter that identifies the "
             "target (e.g. the login a password reset is requested for). "
             "Never logged in full.")
    rate_block_minutes = fields.Integer(
        default=0,
        help="When the rate limit crosses, also add an automatic deny entry "
             "for this many minutes (0 = block matching requests in place "
             "only, never the whole IP). The entry expires by itself -- a "
             "crossed limit can never permanently lock anybody out.")
    apply_pre_db = fields.Boolean(
        default=False,
        help="Also enforce this policy for hosts that resolve to NO database "
             "(Odoo still serves those routes without any database, e.g. the "
             "database manager). Only meant for database-manager-class route "
             "groups; leave off for anything database-bound.")
    active = fields.Boolean(default=True)
    source = fields.Selection([("builtin", "Built-in pack"), ("custom", "Custom")],
                              default="custom", required=True, readonly=True)
    note = fields.Text()

    _code_uniq = models.Constraint(
        "unique(code)", "An endpoint policy with this code already exists.")

    # ------------------------------------------------------------- validation

    @api.constrains("route_re")
    def _check_route_re(self):
        for pol in self:
            try:
                re.compile(pol.route_re or "")
            except re.error as e:
                raise ValidationError(
                    _("Invalid regular expression: %s", e)) from e

    @api.constrains("rate_max", "rate_window")
    def _check_rate(self):
        for pol in self:
            if pol.rate_max and pol.rate_window <= 0:
                raise ValidationError(_(
                    "The rate window must be at least 1 minute."))

    # ------------------------------------------------------------- pack load

    @api.model
    def _read_builtin_policies(self):
        try:
            with open(POLICIES_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            return []
        return data.get("policies") or []

    @api.model
    def load_builtin_policies(self):
        """Create/update built-in policies from the bundled pack.

        Same contract as waf.rule.load_builtin_rules: admins keep their
        tuning (action, active, exemptions, severity, rate settings); the
        pack owns the operational shape (route, methods, RPC matchers,
        apply_pre_db, name, category, note). Lookup runs with
        active_test=False so a deactivated built-in is found and not
        re-created against the unique(code) constraint.
        """
        vals_by_code = {}
        for row in self._read_builtin_policies():
            code = row.get("id")
            if not code:
                continue
            action = row.get("action", "log")
            if action not in ("log", "block"):
                action = "log"
            rate_key = row.get("rate_key") or "ip"
            if rate_key not in ("ip", "user", "ip_user", "target"):
                rate_key = "ip"
            on_unverified = row.get("on_unverified") or "off"
            if on_unverified not in ("off", "match"):
                on_unverified = "off"
            vals_by_code[code] = {
                "code": code,
                "name": row.get("name") or code,
                "category": row.get("category") or "endpoint",
                "route_re": row.get("route") or "(?!never-match-anything)",
                "methods": row.get("methods") or "",
                "rpc_service": row.get("rpc_service") or "",
                "rpc_model": row.get("rpc_model") or "",
                "rpc_method": row.get("rpc_method") or "",
                "on_unverified": on_unverified,
                "action": action,
                "severity": int(row.get("severity") or 4),
                # pack defaults for the admin-tunable fields: updates
                # never overwrite these on existing rows (the admin's
                # tuning wins), but on create they carry the pack's flood
                # limits and target-keyed counter shapes.
                "exempt_nets": row.get("exempt_nets") or "",
                "rate_max": int(row.get("rate_max") or 0),
                "rate_window": int(row.get("rate_window") or 10),
                "rate_key": rate_key,
                "rate_param": row.get("rate_param") or "login",
                "rate_block_minutes": int(row.get("rate_block_minutes") or 0),
                "ids_max": int(row.get("ids_max") or 0),
                "apply_pre_db": bool(row.get("apply_pre_db", False)),
                "source": "builtin",
                "note": row.get("note") or "",
            }
        existing = {p.code: p for p in self.with_context(
            active_test=False).search([("code", "!=", False)])}
        created = updated = 0
        admin_tuning = ("action", "severity", "exempt_nets", "rate_max",
                        "rate_window", "rate_key", "rate_param",
                        "rate_block_minutes", "ids_max")
        for code, vals in vals_by_code.items():
            pol = existing.get(code)
            if pol:
                pol.write({k: v for k, v in vals.items()
                           if k not in admin_tuning})
                updated += 1
            else:
                self.create(vals)
                created += 1
        stale = [p for c, p in existing.items()
                 if c not in vals_by_code and p.source == "builtin" and p.active]
        for pol in stale:
            pol.write({"active": False})
        return created, updated
