from odoo import fields, models


class WafMasterHit(models.Model):
    _name = "waf.master.hit"
    _description = "Master Password Brute-Force Counter"
    _rec_name = "network"
    _order = "window_start desc"

    # Shared master-password counter (core/masterbp): one row per source
    # network, in ONE scope database per instance (the master password is
    # a server-wide secret) and carrying the temporary lockout in
    # ``blocked_until``. Bump mechanics: see masterbp._UPDATE_SQL.
    _network_uniq = models.Constraint(
        "unique(network)",
        "One master-password counter row per network.")

    network = fields.Char(required=True, index=True, copy=False)
    window_start = fields.Datetime(required=True, index=True, readonly=True)
    hits = fields.Integer(default=0, readonly=True)
    blocked_until = fields.Datetime(index=True, readonly=True)
