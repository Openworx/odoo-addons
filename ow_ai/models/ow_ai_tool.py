# -*- coding: utf-8 -*-
import json
import re

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..engine.tools_registry import get_builtin
from ..utils.schema import SchemaError, schema_summary, validate_schema

_TOOL_NAME_RE = re.compile(r'[a-zA-Z0-9_]{1,64}')
_LOCKED_FIELDS = {
    'name', 'tool_name', 'schema', 'kind', 'builtin_key', 'server_action_id', 'is_native',
    'is_write', 'model_name',
}


class OwAiTool(models.Model):
    _name = 'ow.ai.tool'
    _description = 'AI Tool'
    _order = 'name, id'

    _tool_name_unique = models.UniqueIndex(
        '(tool_name) WHERE active',
        "This tool name is already used by another active tool.",
    )

    name = fields.Char(required=True, translate=True)
    tool_name = fields.Char(required=True, help="Function name sent to the model")
    description = fields.Text(required=True, translate=True)
    schema = fields.Text(
        default='{"type": "object", "properties": {}}',
        help="JSON schema of the parameters object")
    kind = fields.Selection(
        [('builtin', "Built-in"), ('server_action', "Server Action")],
        required=True, default='builtin')
    builtin_key = fields.Char()
    server_action_id = fields.Many2one('ir.actions.server', ondelete='cascade')
    is_write = fields.Boolean(default=False, help="Changes data")
    requires_confirmation = fields.Boolean(compute='_compute_requires_confirmation', store=True, readonly=False)
    thinking_text = fields.Char(translate=True, help="Shown while the tool runs, e.g. 'Searching the database'")
    model_name = fields.Char(help="Restrict this tool to sessions about this model")
    active = fields.Boolean(default=True)
    is_native = fields.Boolean(readonly=True, default=False)
    schema_summary = fields.Char(compute='_compute_schema_summary')

    @api.depends('is_write')
    def _compute_requires_confirmation(self):
        for tool in self:
            tool.requires_confirmation = tool.is_write

    @api.depends('schema')
    def _compute_schema_summary(self):
        for tool in self:
            try:
                tool.schema_summary = schema_summary(json.loads(tool.schema or '{}'))
            except (ValueError, TypeError):
                tool.schema_summary = ''

    @api.constrains('tool_name')
    def _check_tool_name(self):
        for tool in self:
            if not tool.tool_name or not _TOOL_NAME_RE.fullmatch(tool.tool_name):
                raise ValidationError(self.env._(
                    "Tool name '%(name)s' must contain only letters, digits and "
                    "underscores (max 64 characters).", name=tool.tool_name))

    @api.constrains('schema')
    def _check_schema(self):
        for tool in self:
            try:
                parsed = json.loads(tool.schema or '')
            except ValueError as exc:
                raise ValidationError(self.env._(
                    "Invalid JSON schema: %(error)s", error=str(exc))) from exc
            try:
                validate_schema(parsed)
            except SchemaError as exc:
                raise ValidationError(self.env._(
                    "Invalid tool schema: %(error)s", error=str(exc))) from exc

    @api.constrains('kind', 'builtin_key', 'server_action_id')
    def _check_kind(self):
        for tool in self:
            if tool.kind == 'builtin':
                if not tool.builtin_key or get_builtin(tool.builtin_key) is None:
                    raise ValidationError(self.env._(
                        "Unknown built-in tool key '%(key)s'.", key=tool.builtin_key))
            elif tool.kind == 'server_action':
                # The selection value is kept for a later version; running a
                # server action as a tool is not implemented yet (`run_tool`
                # refuses it), so such a tool cannot be created meanwhile.
                raise ValidationError(self.env._(
                    "Server-action tools are not available in this version."))

    def _check_native_lock(self, vals):
        if self.env.context.get('install_mode') or self.env.context.get('module'):
            return
        if any(tool.is_native for tool in self) and (set(vals) & _LOCKED_FIELDS):
            raise UserError(self.env._("Native tools cannot have this field changed."))

    def write(self, vals):
        self._check_native_lock(vals)
        return super().write(vals)

    def unlink(self):
        if not (self.env.context.get('install_mode') or self.env.context.get('module')):
            for tool in self:
                if tool.is_native:
                    raise UserError(self.env._("Native tools cannot be deleted."))
        return super().unlink()

    def _get_schema(self):
        self.ensure_one()
        return json.loads(self.schema or '{}')

    def _to_neutral_tool(self):
        self.ensure_one()
        return {
            'name': self.tool_name,
            'instructions': self.description,
            'schema': self._get_schema(),
        }
