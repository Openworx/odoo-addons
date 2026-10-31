from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class WafHit(models.Model):
    _name = "waf.hit"
    _description = "Firewall Traffic Log"
    _order = "id desc"

    ref = fields.Char(index=True, readonly=True)
    ip = fields.Char(index=True, readonly=True)
    country = fields.Char(readonly=True)
    method = fields.Char(readonly=True)
    path = fields.Char(readonly=True)
    query = fields.Char(readonly=True)
    ua = fields.Char(readonly=True)
    rule_id = fields.Many2one("waf.rule", string="Rule", readonly=True,
                              ondelete="set null")
    rule_name = fields.Char(readonly=True)
    category = fields.Char(readonly=True)
    severity = fields.Integer(readonly=True)
    kind = fields.Selection(
        [("ip", "IP entry"), ("country", "Country"), ("rule", "Rule"),
         ("endpoint", "Endpoint policy"),
         ("master", "Master password"),
         ("recovery", "Recovery"),
         ("body", "Body coverage")],
        readonly=True)
    action = fields.Selection(
        [("log", "Logged"), ("block", "Block")], readonly=True)
    mode = fields.Selection([("learning", "Learning"), ("enforce", "Enforce")],
                            readonly=True)
    blocked = fields.Boolean(readonly=True, index=True)

    def _request_ip(self):
        self.ensure_one()
        ip = (self.ip or "").strip()
        if not ip:
            # hits are stored with whatever the IP resolver produced: a
            # missing IP must not TypeError the Block/Allow IP buttons
            raise UserError(_(
                "This traffic-log entry has no source IP address, so it "
                "cannot be used to block or allow an address."))
        return ip + ("/128" if ":" in ip else "/32")

    def action_block_ip(self):
        for hit in self:
            self.env["waf.ip.entry"].create({
                "network": hit._request_ip(),
                "kind": "deny",
                "source": "manual",
                "reason": _("Blocked from traffic log (rule: %s)", hit.rule_name or "-"),
            })
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": _("IP blocked"), "type": "success",
                       "message": _("The firewall will now deny this IP.")},
        }

    def action_allow_ip(self):
        for hit in self:
            self.env["waf.ip.entry"].create({
                "network": hit._request_ip(),
                "kind": "allow",
                "source": "manual",
                "reason": _("Allowed from traffic log"),
                "expires": fields.Datetime.now() + timedelta(hours=72),
            })
        return True
