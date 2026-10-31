from odoo import fields, models


class WafLockoutHit(models.Model):
    _name = "waf.lockout.hit"
    _description = "Auto-Lockout Attack Counter"
    _rec_name = "network"
    _order = "window_start desc"

    # Shared auto-lockout counter for the WAF middleware: one row per
    # attacked network, written only for rule-matched attack hits and
    # pruned as their window ages out. Bump mechanics: see
    # core/middleware._bump_lockout. Nothing here is user-facing.
    _network_uniq = models.Constraint(
        "unique(network)",
        "One auto-lockout counter row per network.")

    network = fields.Char(required=True, index=True, copy=False)
    window_start = fields.Datetime(required=True, index=True, readonly=True)
    hits = fields.Integer(default=0, readonly=True)
