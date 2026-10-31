"""Targeted exceptions: rule/policy scoped, with reason and expiry.

An exception never disables protection wholesale: it is scoped to a rule or
an endpoint policy (optionally narrowed by route, HTTP method and a parsed
RPC field), carries a mandatory reason, and can expire. While live, the
matching decision stays LOGGED but the block/lockout is not applied.
"""
import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class WafException(models.Model):
    _name = "waf.exception"
    _description = "Firewall Exception"
    _order = "expires asc nulls first, id desc"

    name = fields.Char(required=True,
                       help="Short label, e.g. 'Nightly sync exports res.partner'.")
    rule_id = fields.Many2one("waf.rule", string="Rule",
                              help="Exception applies to this signature rule.")
    policy_id = fields.Many2one("waf.endpoint.policy", string="Endpoint policy",
                                help="Exception applies to this endpoint "
                                     "policy (route group / RPC service / "
                                     "rate limit).")
    rule_code = fields.Char(related="rule_id.code", store=True, index=True,
                            string="Rule code",
                            help="The rule's stable code; the engine matches "
                                 "the exception against the code, so a rule "
                                 "recreated by a pack reload stays covered.")
    policy_code = fields.Char(related="policy_id.code", store=True,
                              index=True, string="Policy code")
    route_re = fields.Char(
        help="Optional: restrict the exception to paths matching this "
             "regular expression (e.g. ^/web/export/). Empty = any route.")
    methods = fields.Char(
        help="Optional comma-separated HTTP methods (e.g. POST). Empty = any.")
    param = fields.Char(
        string="RPC parameter match",
        help="Optional 'field=value' (exact, case-insensitive) against the "
             "STRUCTURED request context: service, method, model, or a "
             "form/query parameter name -- e.g. 'model=res.partner'. This "
             "never matches incidental text inside business data.")
    reason = fields.Char(
        required=True,
        help="Why this exception exists (who/what integration needs it). "
             "Shown in the traffic log for every excepted hit.")
    expires = fields.Datetime(
        help="Optional expiry; after this instant the exception is inert. "
             "Timeboxed exceptions are strongly recommended for one-off jobs.")
    active = fields.Boolean(default=True)

    @api.constrains("rule_id", "policy_id")
    def _check_target(self):
        for exc in self:
            if not exc.rule_id and not exc.policy_id:
                raise ValidationError(_(
                    "An exception must target a rule or an endpoint policy."))

    @api.constrains("route_re")
    def _check_route_re(self):
        for exc in self:
            try:
                re.compile(exc.route_re or "")
            except re.error as e:
                raise ValidationError(
                    _("Invalid regular expression: %s", e)) from e

    @api.constrains("param")
    def _check_param(self):
        for exc in self:
            if exc.param and "=" not in exc.param:
                raise ValidationError(_(
                    "The parameter match must be written field=value "
                    "(e.g. model=res.partner)."))
