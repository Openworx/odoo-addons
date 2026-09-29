from odoo import api, fields, models
from odoo.exceptions import AccessError

from ..provider.client import ProviderClient
from ..provider.errors import AIProviderError
from ..utils import params, websearch

# Settings stored as `ir.config_parameter` values. They are read and written
# here through utils/params.py rather than with the fields' `config_parameter`
# attribute: Odoo 19's own handling unsets a False boolean or a zero number,
# after which the default wins again.
_OW_AI_PARAMS = {
    'ow_ai_base_url': 'ow_ai.base_url',
    'ow_ai_default_model': 'ow_ai.default_model',
    'ow_ai_max_rounds': 'ow_ai.max_rounds',
    'ow_ai_max_tool_calls_per_round': 'ow_ai.max_tool_calls_per_round',
    'ow_ai_timeout': 'ow_ai.timeout',
    'ow_ai_job_stale_minutes': 'ow_ai.job_stale_minutes',
    'ow_ai_max_tool_result_chars': 'ow_ai.max_tool_result_chars',
    'ow_ai_max_history_chars': 'ow_ai.max_history_chars',
    'ow_ai_web_search_url': 'ow_ai.web_search_url',
    'ow_ai_web_search_results': 'ow_ai.web_search_results',
    'ow_ai_web_timeout': 'ow_ai.web_timeout',
    'ow_ai_web_search_language': 'ow_ai.web_search_language',
    'ow_ai_web_search_default': 'ow_ai.web_search_default',
}
_PARAM_ACCESSORS = {
    'char': (params.get_str, params.set_str),
    'integer': (params.get_int, params.set_int),
    'float': (params.get_float, params.set_float),
    'boolean': (params.get_bool, params.set_bool),
}


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
        string="Base URL",
        default='https://openrouter.ai/api/v1',
        help="OpenAI-compatible endpoint, e.g. https://openrouter.ai/api/v1, "
             "https://hostyourai.com/api/v1, https://api.openai.com/v1, http://ollama:11434/v1",
    )
    ow_ai_default_model = fields.Char(
        string="Default Model",
        default='openai/gpt-6-luna',
        help="Model id at that endpoint, e.g. openai/gpt-6-luna (OpenRouter), "
             "hyai/loes-large (HostYourAI), qwen2.5:3b (Ollama)",
    )
    ow_ai_max_rounds = fields.Integer(
        string="Max Rounds", default=30,
    )
    ow_ai_max_tool_calls_per_round = fields.Integer(
        string="Max Tool Calls per Round",
        default=20,
    )
    ow_ai_timeout = fields.Integer(
        string="Timeout (s)", default=120,
    )
    ow_ai_job_stale_minutes = fields.Integer(
        string="Job Stale After (minutes)",
        default=10,
    )
    ow_ai_max_tool_result_chars = fields.Integer(
        string="Max Tool Result Characters",
        default=60000,
    )
    ow_ai_max_history_chars = fields.Integer(
        string="Max History Characters",
        default=200000,
    )

    ow_ai_web_search_url = fields.Char(
        string="SearXNG URL",
        default='',
        help="SearXNG base URL, e.g. http://searxng:8080. Empty disables web search.",
    )
    ow_ai_web_search_results = fields.Integer(
        string="Results",
        default=5,
        help="Number of search results returned to the model (max 10).",
    )
    ow_ai_web_timeout = fields.Integer(
        string="Web Timeout (s)",
        default=15,
    )
    ow_ai_web_search_language = fields.Char(
        string="Language",
        default='',
        help="Language code passed to SearXNG, e.g. nl. Empty uses the user's language.",
    )
    ow_ai_web_search_default = fields.Boolean(
        string="On by Default",
        default=False,
        help="Whether new chats start with web search switched on.",
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
        for name, key in _OW_AI_PARAMS.items():
            field = self._fields[name]
            get, _set = _PARAM_ACCESSORS[field.type]
            values[name] = get(self.env, key, field.default(self))
        return values

    def set_values(self):
        super().set_values()
        for record in self:
            for name, key in _OW_AI_PARAMS.items():
                field = record._fields[name]
                _get, set_ = _PARAM_ACCESSORS[field.type]
                value = record[name]
                if field.type == 'char':
                    # like Odoo's own handling: no stray spaces in model ids/URLs
                    value = (value or '').strip()
                    if name == 'ow_ai_web_search_url':
                        value = value.rstrip('/')
                if name == 'ow_ai_web_search_results':
                    value = max(1, min(value or 0, 10))
                elif name == 'ow_ai_web_timeout':
                    value = max(1, value or 0)
                set_(record.env, key, value)
            if record.ow_ai_api_key:
                params.set_str(record.env, 'ow_ai.api_key', record.ow_ai_api_key.strip())
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
        params.set_str(self.env, 'ow_ai.api_key', '')
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

    def action_ow_ai_test_web_search(self):
        self.ensure_one()
        self._check_ow_ai_admin()
        try:
            result = websearch.search(self.env, 'Odoo', count=3)
        except websearch.WebSearchError as exc:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': "Web search",
                    'message': exc.message,
                    'type': 'danger',
                    'sticky': False,
                },
            }
        count = len(result['results'])
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': "Web search",
                'message': f"Search works: {count} result{'' if count == 1 else 's'}.",
                'type': 'success',
                'sticky': False,
            },
        }
