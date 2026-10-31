from odoo import _, fields, models

SEVERITIES = [
    ("critical", "Critical"),
    ("high", "High"),
    ("medium", "Medium"),
    ("low", "Low"),
    ("info", "Info"),
]

# presentation for the PDF report: (badge label, accent colour).
# plain strings: module-level _() has no language context (it logs "no
# translation language detected" on every boot and breaks string
# handling). Translate at render time instead (report_ui).
SEVERITY_UI = {
    "critical": ("Critical · Fail", "#a71d2a"),
    "high": ("High · Fail", "#c0392b"),
    "medium": ("Medium · Warning", "#e8940c"),
    "low": ("Low · Notice", "#6c757d"),
    "info": ("Info", "#868e96"),
}


class WafFinding(models.Model):
    _name = "waf.finding"
    _description = "Security Finding"
    _order = "severity desc, id desc"

    scan_id = fields.Many2one("waf.scan", required=True, ondelete="cascade")
    severity = fields.Selection(SEVERITIES, required=True, default="medium",
                                index=True)
    name = fields.Char(required=True)
    message = fields.Text()
    recommendation = fields.Text()
    model_name = fields.Char(help="Technical model of the affected record, if any.")
    res_id = fields.Integer()
    path = fields.Char(help="File path for file-scan findings.")

    def report_ui(self):
        """(badge label, accent colour) for the PDF layout."""
        label, color = SEVERITY_UI.get(
            self.severity, (self.severity or "", "#6c757d"))
        return {"label": _(label), "color": color}
