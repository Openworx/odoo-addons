import ipaddress

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

FULL_NETS = {ipaddress.ip_network("0.0.0.0/0"), ipaddress.ip_network("::/0")}


class WafIpEntry(models.Model):
    _name = "waf.ip.entry"
    _description = "Firewall IP Entry"
    _rec_name = "network"
    _order = "id desc"

    network = fields.Char(
        required=True, index=True,
        help="IP or CIDR, e.g. 203.0.113.7 or 198.51.100.0/24. "
             "Stored normalized (bare IPs become /32 or /128).")
    kind = fields.Selection(
        [("allow", "Allow (bypass all rules)"), ("deny", "Block")],
        required=True, default="deny")
    source = fields.Selection(
        [("manual", "Manual"), ("auto", "Automatic (lockout)"),
         ("recovery", "Recovery link")],
        required=True, default="manual", readonly=True)
    reason = fields.Char()
    expires = fields.Datetime(
        help="Entry automatically deactivates after this date "
             "(handled by the firewall cleanup scheduled action).")
    active = fields.Boolean(default=True)

    @api.constrains("network")
    def _check_network(self):
        for rec in self:
            try:
                net = ipaddress.ip_network(rec.network.strip(), strict=False)
            except ValueError as e:
                raise ValidationError(
                    _("Not a valid IP or CIDR: %s", rec.network)) from e
            if net in FULL_NETS:
                raise ValidationError(_(
                    "Blocking or allowing the whole internet (0.0.0.0/0, ::/0) "
                    "is not permitted here — use the firewall mode instead."))
            normalized = str(net)
            if normalized != rec.network:
                rec.network = normalized

    def action_deactivate(self):
        self.write({"active": False})
        return True
