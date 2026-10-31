import os
import re

import yaml

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

RULES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "ruleset.yaml")

SCOPES = [
    ("url", "URL path"),
    ("query", "Query string / URL parameters"),
    ("ua", "User-Agent"),
    ("header", "Forwarding headers (XFF, X-Real-IP, X-Fwd-Host, Referer)"),
    ("body", "Request body"),
]


class WafRule(models.Model):
    _name = "waf.rule"
    _description = "Firewall Rule"
    _order = "severity desc, category, code"

    code = fields.Char(index=True,
                       help="Stable identifier of a built-in rule (e.g. waf-xss-1).")
    name = fields.Char(required=True)
    category = fields.Char(required=True, index=True,
                           help="Ruleset group, used for filtering and reporting.")
    scope = fields.Selection(SCOPES, required=True, default="url",
                             help="Which part of the request is matched.")
    pattern = fields.Char(
        required=True,
        help="Python regular expression, matched against the normalized "
             "(percent-decoded, whitespace-collapsed, case-folded) request part.")
    action = fields.Selection(
        [("log", "Log only"), ("block", "Block"),
         ("auto", "Block + counts toward lockout")],
        default="block", required=True)
    severity = fields.Integer(default=3,
                              help="1 = informational, 5 = critical. Higher "
                                   "severity rules are evaluated first.")
    no_session = fields.Boolean(
        default=True,
        help="Apply only to visitors without an Odoo session cookie. Prevents "
             "false positives on editors/admins posting legitimate HTML. "
             "Uncheck for probe/scanner rules that should apply to everyone.")
    case_sensitive = fields.Boolean(default=False)
    active = fields.Boolean(default=True)
    source = fields.Selection([("builtin", "Built-in pack"), ("custom", "Custom")],
                              default="custom", required=True, readonly=True)
    note = fields.Text()

    test_text = fields.Char(
        string="Test sample",
        help="Paste a payload here to check this rule in the form view.")
    test_result = fields.Char(compute="_compute_test_result")

    _code_uniq = models.Constraint(
        "unique(code)", "A rule with this code already exists.")

    # ------------------------------------------------------------- validation

    @api.constrains("pattern")
    def _check_pattern(self):
        for rule in self:
            try:
                re.compile(rule.pattern or "")
            except re.error as e:
                raise ValidationError(
                    _("Invalid regular expression: %s", e)) from e

    @api.depends("pattern", "test_text", "case_sensitive")
    def _compute_test_result(self):
        for rule in self:
            if not rule.test_text or not rule.pattern:
                rule.test_result = False
                continue
            try:
                flags = 0 if rule.case_sensitive else re.IGNORECASE
                hit = re.search(rule.pattern, rule.test_text, flags)
                rule.test_result = (
                    _("Matches (%s)", hit.group(0)[:120]) if hit else _("No match"))
            except re.error as e:
                rule.test_result = _("Invalid regex: %s", e)

    # ------------------------------------------------------------- built-in packs

    @api.model
    def _read_builtin_rules(self):
        try:
            with open(RULES_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            return []
        return data.get("rules") or []

    @api.model
    def load_builtin_rules(self):
        """Create/update built-in rules from the bundled rule pack.

        Existing rules keep their ``active`` flag and their local tuning
        of ``action`` / ``severity`` / ``no_session``: what the admin
        tuned survives a reload or upgrade. Everything the pack ships
        (pattern, name, category, case_sensitive, note) is updated.
        The lookup runs with ``active_test=False``: a deactivated rule
        is still found here, otherwise the loader would ``create()`` the
        same code again and trip the unique(code) constraint.
        """
        vals_by_code = {}
        for row in self._read_builtin_rules():
            code = row.get("id")
            if not code:
                continue
            scope = row.get("scope", "url")
            if scope not in dict(SCOPES):
                scope = "url"
            action = row.get("action", "block")
            if action not in ("log", "block", "auto"):
                action = "block"
            vals_by_code[code] = {
                "code": code,
                "name": row.get("name") or code,
                "category": row.get("category") or "custom",
                "scope": scope,
                "pattern": row.get("pattern") or "(?!never-match-anything)",
                "action": action,
                "severity": int(row.get("severity") or 3),
                "no_session": bool(row.get("no_session", True)),
                "case_sensitive": bool(row.get("case_sensitive", False)),
                "source": "builtin",
                "note": row.get("note") or "",
            }
        existing = {r.code: r for r in self.with_context(
            active_test=False).search([("code", "!=", False)])}
        created = updated = 0
        pack_tuning = ("action", "severity", "no_session")
        for code, vals in vals_by_code.items():
            rule = existing.get(code)
            if rule:
                rule.write({k: v for k, v in vals.items()
                            if k not in pack_tuning})
                updated += 1
            else:
                self.create(vals)
                created += 1
        # retire built-in rules that vanished from the pack
        stale = [r for c, r in existing.items()
                 if c not in vals_by_code and r.source == "builtin" and r.active]
        for rule in stale:
            rule.write({"active": False})
        return created, updated
