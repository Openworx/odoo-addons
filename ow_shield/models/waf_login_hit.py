from odoo import fields, models


class WafLoginHit(models.Model):
    _name = "waf.login.hit"
    _description = "Login Brute-Force Counter"
    _rec_name = "network"
    _order = "window_start desc"

    # Shared login-failure counter for the res.users authenticate
    # override: one row per source network. A separate table from
    # waf.lockout.hit: login credit and attack-signature credit must
    # never add up to one block decision. Bump mechanics: see
    # core/middleware._bump_lockout. Nothing here is user-facing.
    _network_uniq = models.Constraint(
        "unique(network)",
        "One login counter row per network.")

    network = fields.Char(required=True, index=True, copy=False)
    window_start = fields.Datetime(required=True, index=True, readonly=True)
    hits = fields.Integer(default=0, readonly=True)
