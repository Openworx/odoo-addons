import json

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class MCPModelAccess(models.Model):
    _name = 'ow.mcp.model.access'
    _description = 'MCP per-model access configuration'
    _rec_name = 'model_name'

    model_id = fields.Many2one(
        'ir.model', required=True, ondelete='cascade', string='Model',
    )
    model_name = fields.Char(
        related='model_id.model', store=True, index=True, string='Technical name',
    )
    allow_read = fields.Boolean(default=True)
    allow_write = fields.Boolean(default=False)
    allow_create = fields.Boolean(default=False)
    allow_delete = fields.Boolean(default=False)
    allowed_fields_json = fields.Text(
        help='JSON array of field names. Empty = all fields allowed.',
    )
    group_ids = fields.Many2many('res.groups', string='Required groups')
    max_records = fields.Integer(
        default=0, help='0 = use global max_limit from ow.mcp.config.',
    )
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ('model_uniq', 'unique(model_id)',
         'Each model may have only one MCP access row.'),
    ]

    @api.constrains('allowed_fields_json')
    def _check_allowed_fields_json(self):
        for rec in self:
            if not rec.allowed_fields_json:
                continue
            try:
                data = json.loads(rec.allowed_fields_json)
            except Exception as e:
                raise ValidationError(
                    f'allowed_fields_json must be valid JSON: {e}'
                )
            if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
                raise ValidationError(
                    'allowed_fields_json must be a JSON array of strings.'
                )

    def allowed_fields(self):
        """Return list of allowed field names, or None if no allow-list is set."""
        self.ensure_one()
        if not self.allowed_fields_json:
            return None
        return json.loads(self.allowed_fields_json)
