import os

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import config as odoo_config


class MCPConfig(models.Model):
    _name = 'ow.mcp.config'
    _description = 'MCP Server Configuration'

    name = fields.Char(default='MCP Server', readonly=True)
    yolo_mode = fields.Selection(
        [('off', 'Off (production)'),
         ('read', 'YOLO read (dev only)'),
         ('full', 'YOLO full (dev only — DANGEROUS)')],
        default='off', required=True,
    )
    default_limit = fields.Integer(default=80, required=True)
    max_limit = fields.Integer(default=500, required=True)
    max_text_len = fields.Integer(default=2000, required=True)
    max_relational_preview = fields.Integer(default=20, required=True)
    enable_http_transport = fields.Boolean(
        default=True,
        help='Master switch for POST /mcp. When off, the endpoint returns '
             '503 Service Unavailable.',
    )
    enable_sse = fields.Boolean(
        default=False,
        help='Enable the SSE probe on GET /mcp. When off, GET returns '
             '405 Method Not Allowed.',
    )
    enable_cors = fields.Boolean(
        default=False,
        help='When on, /mcp emits CORS headers and handles OPTIONS '
             'preflight. Origin is echoed only if listed in '
             'cors_origins (or if cors_origins is empty, which falls back '
             'to "*" for backward compatibility — not recommended).',
    )
    cors_origins = fields.Text(
        help='One origin per line, e.g. https://chat.example.com. '
             'Empty = wildcard (*), which is the old permissive behavior. '
             'Exact match against the request Origin header; no wildcards '
             'inside entries.',
    )
    max_body_bytes = fields.Integer(
        default=1048576,
        help='Hard limit on incoming POST body size. Requests above this '
             'return 413 before any parsing or auth work. 0 disables the '
             'limit (not recommended in production).',
    )
    require_https = fields.Boolean(
        default=False,
        help='Reject requests whose scheme is not https. Trusts '
             'X-Forwarded-Proto only when proxy_mode is set in odoo.conf. '
             'Enable in production; leave off for local http:// dev.',
    )
    rate_limit_per_minute = fields.Integer(
        default=120,
        help='Per-API-key rolling limit. 0 disables the limiter. '
             'In-memory per worker: with N workers, effective limit is '
             'N * this value.',
    )
    audit_log = fields.Boolean(
        default=False,
        help='Record every MCP call in ow.mcp.audit.log (admin-only).',
    )
    audit_retention_days = fields.Integer(
        default=30,
        help='Daily cron deletes ow.mcp.audit.log rows older than this. '
             '0 disables retention (rows kept forever).',
    )
    cache_enabled = fields.Boolean(
        default=True,
        help='Cache results of list_models / search_records / get_record '
             'in memory per worker. Invalidated per model on MCP writes.',
    )
    cache_ttl_seconds = fields.Integer(
        default=60,
        help='How long a cached response stays valid (seconds). '
             'In-memory per worker; 60–300s is practical for most setups.',
    )
    ip_whitelist = fields.Text(
        help='One CIDR or IP per line (# and blank lines ignored). '
             'Empty = no IP restriction. IPv4 and IPv6 may be mixed:\n'
             '10.0.0.0/8\n192.168.1.42\n::1\n2001:db8::/32\n'
             '# office network\n172.16.0.0/12\n\n'
             'IPv4-mapped IPv6 clients (::ffff:a.b.c.d) are matched against '
             'their IPv4 form, so a v4 entry covers dual-stack clients. '
             'Behind a reverse proxy, set proxy_mode = True in odoo.conf '
             'so X-Forwarded-For is trusted; otherwise this checks the '
             'proxy IP instead of the real client.',
    )
    warning_banner = fields.Char(compute='_compute_warning_banner')
    proxy_mode_warning = fields.Char(compute='_compute_proxy_mode_warning')

    @api.depends('yolo_mode')
    def _compute_warning_banner(self):
        dev_on = bool(odoo_config.get('dev_mode')) or bool(
            odoo_config.get('test_enable'),
        )
        env_on = os.environ.get('OW_MCP_ALLOW_YOLO') == '1'
        active = dev_on and env_on
        for rec in self:
            if rec.yolo_mode == 'off':
                rec.warning_banner = False
            elif active and rec.yolo_mode == 'full':
                rec.warning_banner = (
                    'YOLO FULL mode is ACTIVE — NEVER use in production.'
                )
            elif active and rec.yolo_mode == 'read':
                rec.warning_banner = 'YOLO READ mode is ACTIVE — dev only.'
            else:
                missing = []
                if not dev_on:
                    missing.append('--dev or --test-enable')
                if not env_on:
                    missing.append('OW_MCP_ALLOW_YOLO=1 (env)')
                rec.warning_banner = (
                    f'YOLO {rec.yolo_mode.upper()} is configured but '
                    f'INACTIVE — missing: {", ".join(missing)}. Access '
                    f'falls back to the normal MCP rules.'
                )

    @api.depends('ip_whitelist')
    def _compute_proxy_mode_warning(self):
        proxy_on = bool(odoo_config.get('proxy_mode'))
        for rec in self:
            has_list = bool(rec.ip_whitelist and rec.ip_whitelist.strip())
            if has_list and not proxy_on:
                rec.proxy_mode_warning = (
                    "proxy_mode is off in odoo.conf. If a reverse proxy "
                    "sits in front of Odoo, the IP whitelist will match "
                    "the proxy's IP instead of the real client — set "
                    "proxy_mode = True so X-Forwarded-For is trusted."
                )
            else:
                rec.proxy_mode_warning = False

    @api.model
    def get_singleton(self):
        rec = self.search([], limit=1)
        if rec:
            return rec
        # No row yet: take the advisory lock before creating so parallel
        # first-access requests converge on the same row.
        self.env.cr.execute(
            'SELECT pg_advisory_xact_lock(%s)', (self._SINGLETON_LOCK_KEY,),
        )
        rec = self.search([], limit=1)
        if rec:
            return rec
        return self.create({})

    # Arbitrary, module-local advisory-lock key. Serializes singleton creation
    # so two concurrent callers cannot both pass the count check.
    _SINGLETON_LOCK_KEY = 0x4F57_4D43  # 'OWMC'

    @api.ondelete(at_uninstall=False)
    def _forbid_unlink(self):
        """Block deletion of the singleton row at ORM level.

        The form view already hides the Delete button, but this guard covers
        XML-RPC, `odoo shell`, and any code path that reaches `unlink()`
        directly. `at_uninstall=False` lets the module uninstall cleanly —
        only interactive / programmatic deletes are refused.
        """
        raise UserError(
            'ow.mcp.config is a singleton and cannot be deleted. '
            'Edit the existing record instead, or uninstall the module to '
            'remove it.'
        )

    @api.model_create_multi
    def create(self, vals_list):
        # PG advisory lock held for the txn — prevents a TOCTOU between the
        # search_count() check and the INSERT when two workers race.
        self.env.cr.execute(
            'SELECT pg_advisory_xact_lock(%s)', (self._SINGLETON_LOCK_KEY,),
        )
        if self.search_count([]) + len(vals_list) > 1:
            from odoo.exceptions import ValidationError
            raise ValidationError(
                'ow.mcp.config is a singleton — only one row is allowed. '
                'Edit the existing record instead of creating a new one.'
            )
        return super().create(vals_list)
