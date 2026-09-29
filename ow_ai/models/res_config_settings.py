from odoo import api, fields, models
from odoo.exceptions import AccessError

from ..provider.client import ProviderClient
from ..provider.errors import AIProviderError


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # Not stored, no config_parameter: get_values() must never expose the
    # API key back to the browser. Handled manually in get_values/set_values.
    ow_ai_api_key = fields.Char(
        string="API Key",
        help="Optional for endpoints without authentication. Stored server-side "
             "only; never sent back to the browser.",
    )
    ow_ai_api_key_state = fields.Selection(
        [
            ('missing', "Not set"),
            ('param', "Set in settings"),
            ('env', "Set via environment variable"),
            ('conf', "Set via odoo.conf"),
        ],
        string="API Key Status",
        compute='_compute_ow_ai_api_key_state',
    )
    ow_ai_api_key_hint = fields.Char(
        string="API Key Hint",
        compute='_compute_ow_ai_api_key_state',
    )

    ow_ai_base_url = fields.Char(
        string="Base URL", config_parameter='ow_ai.base_url',
        default='https://openrouter.ai/api/v1',
        help="OpenAI-compatible endpoint, e.g. https://openrouter.ai/api/v1, "
             "https://hostyourai.com/api/v1, https://api.openai.com/v1, http://ollama:11434/v1",
    )
    ow_ai_default_model = fields.Char(
        string="Default Model", config_parameter='ow_ai.default_model',
        default='openai/gpt-6-luna',
        help="Model id at that endpoint, e.g. openai/gpt-6-luna (OpenRouter), "
             "hyai/loes-large (HostYourAI), qwen2.5:3b (Ollama)",
    )
    ow_ai_max_rounds = fields.Integer(
        string="Max Rounds", config_parameter='ow_ai.max_rounds', default=30,
    )
    ow_ai_max_tool_calls_per_round = fields.Integer(
        string="Max Tool Calls per Round",
        config_parameter='ow_ai.max_tool_calls_per_round', default=20,
    )
    ow_ai_timeout = fields.Integer(
        string="Timeout (s)", config_parameter='ow_ai.timeout', default=120,
    )
    ow_ai_job_stale_minutes = fields.Integer(
        string="Job Stale After (minutes)",
        config_parameter='ow_ai.job_stale_minutes', default=10,
    )
    ow_ai_max_tool_result_chars = fields.Integer(
        string="Max Tool Result Characters",
        config_parameter='ow_ai.max_tool_result_chars', default=60000,
    )
    ow_ai_max_history_chars = fields.Integer(
        string="Max History Characters",
        config_parameter='ow_ai.max_history_chars', default=200000,
    )

    def _get_ow_ai_effective_api_key(self):
        """Return (key, source) where source is 'param', 'env', 'conf' or None:
        the key the provider client itself uses."""
        return ProviderClient.resolve_api_key(self.env)

    @api.depends('ow_ai_api_key')
    def _compute_ow_ai_api_key_state(self):
        for record in self:
            key, source = record._get_ow_ai_effective_api_key()
            record.ow_ai_api_key_state = source or 'missing'
            record.ow_ai_api_key_hint = f"…{key[-4:]}" if key else ''

    @api.model
    def get_values(self):
        values = super().get_values()
        values.pop('ow_ai_api_key', None)
        return values

    def set_values(self):
        super().set_values()
        for record in self:
            if record.ow_ai_api_key:
                self.env['ir.config_parameter'].sudo().set_str(
                    'ow_ai.api_key', record.ow_ai_api_key.strip())
                # The key now lives in ir.config_parameter only: do not leave
                # a plaintext copy behind on this (transient) settings row.
                record.ow_ai_api_key = False

    def _check_ow_ai_admin(self):
        """These buttons are public methods: allow them to administrators only,
        like ``res.config.settings.execute`` itself."""
        if not self.env.is_admin():
            raise AccessError(self.env._("Only administrators can manage the AI settings."))

    def action_ow_ai_clear_api_key(self):
        self._check_ow_ai_admin()
        self.env['ir.config_parameter'].sudo().set_str('ow_ai.api_key', '')
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def action_ow_ai_test_connection(self):
        self.ensure_one()
        self._check_ow_ai_admin()
        try:
            models_list = ProviderClient.from_env(self.env).list_models()
        except AIProviderError as exc:
            message = exc.user_message
            detail = str(exc)
            if detail:
                message = f"{message} {detail}"
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': "AI Provider",
                    'message': message,
                    'type': 'danger',
                    'sticky': False,
                },
            }
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': "AI Provider",
                'message': f"Connected. {len(models_list)} models available.",
                'type': 'success',
                'sticky': False,
            },
        }
