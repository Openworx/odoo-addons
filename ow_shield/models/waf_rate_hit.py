from odoo import fields, models


class WafRateHit(models.Model):
    _name = "waf.rate.hit"
    _description = "Endpoint Rate Counter"
    _rec_name = "network"
    _order = "window_start desc"

    # Shared rate-limit counter for endpoint policies with a rate_max:
    # one row per counter key. The "network" column holds that key, not
    # necessarily a CIDR: an IP, a user id, an ip+user pair, or a
    # rate-limit target parameter value. Bump mechanics: see
    # core/middleware._bump_lockout. Nothing here is user-facing.
    _network_uniq = models.Constraint(
        "unique(network)",
        "One rate counter row per counter key.")

    network = fields.Char(required=True, index=True, copy=False)
    window_start = fields.Datetime(required=True, index=True, readonly=True)
    hits = fields.Integer(default=0, readonly=True)
