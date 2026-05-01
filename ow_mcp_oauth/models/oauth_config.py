"""ow.mcp.oauth.config — OAuth 2.1 settings + RSA key material.

Singleton, like ow.mcp.config. Stores the RSA private key used to sign
RS256 JWT access tokens and exposes endpoints metadata + TTLs.
"""
import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

from ..tools import jwt_helper


_logger = logging.getLogger(__name__)


class OauthConfig(models.Model):
    _name = 'ow.mcp.oauth.config'
    _description = 'MCP OAuth 2.1 Configuration'

    name = fields.Char(default='MCP OAuth 2.1', readonly=True)
    oauth_enabled = fields.Boolean(
        default=True,
        help='When off, the OAuth endpoints all return 404/403 and the MCP '
             'server stops advertising the OAuth challenge — clients fall '
             'back to API-key auth only.',
    )
    oauth_dcr_enabled = fields.Boolean(
        default=True,
        help='When off, /ow_mcp/oauth/register returns 403. Existing clients '
             'keep working; new ones must be created manually by an admin.',
    )

    # ---- Endpoints / canonical resource ----
    canonical_resource_uri = fields.Char(
        help='Canonical URI of the protected resource (the MCP server). '
             'Tokens issued here MUST carry this value as their `aud` claim '
             'and clients MUST send it as the `resource` parameter '
             '(RFC 8707). Defaults to <web.base.url>/mcp at first read.',
    )

    # ---- TTLs ----
    access_token_ttl_seconds = fields.Integer(default=900)        # 15 min
    refresh_token_ttl_seconds = fields.Integer(default=2592000)   # 30 d
    auth_code_ttl_seconds = fields.Integer(default=60)            # 1 min
    consent_remember_days = fields.Integer(default=30)
    unused_client_purge_days = fields.Integer(default=7)

    # ---- DCR throttle ----
    dcr_rate_limit_per_ip_per_hour = fields.Integer(default=20)

    # ---- Key material ----
    rsa_private_key_pem = fields.Text(
        help='Active RS256 private key in PKCS8 PEM. Auto-generated on '
             'first access. Treat as secret.',
        groups='ow_mcp_server.group_mcp_admin',
    )
    rsa_private_key_kid = fields.Char(
        help='Identifier ("kid") of the active key. Published on JWKS.',
    )
    rsa_previous_public_key_pem = fields.Text(
        help='Previous public key, kept after rotation so already-issued '
             'tokens stay valid until they expire naturally.',
    )
    rsa_previous_public_key_kid = fields.Char()

    @api.model
    def get_singleton(self):
        rec = self.search([], limit=1)
        if rec:
            self._ensure_keypair(rec)
            return rec
        # Advisory lock — converge concurrent first-access requests.
        self.env.cr.execute(
            'SELECT pg_advisory_xact_lock(%s)', (self._SINGLETON_LOCK_KEY,),
        )
        rec = self.search([], limit=1)
        if not rec:
            rec = self.create({})
        self._ensure_keypair(rec)
        return rec

    _SINGLETON_LOCK_KEY = 0x4F57_4F41  # 'OWOA'

    def _ensure_keypair(self, rec):
        if rec.rsa_private_key_pem and rec.rsa_private_key_kid:
            return
        priv, _pub, kid = jwt_helper.generate_rsa_keypair()
        rec.sudo().write({
            'rsa_private_key_pem': priv,
            'rsa_private_key_kid': kid,
        })
        _logger.info('ow_mcp_oauth: generated RSA keypair, kid=%s', kid)

    def public_keys_by_kid(self):
        """Return {kid: public_pem} for the current and (optional) previous keys."""
        self.ensure_one()
        out = {}
        if self.rsa_private_key_pem and self.rsa_private_key_kid:
            try:
                pub = jwt_helper.public_pem_from_private_pem(self.rsa_private_key_pem)
                out[self.rsa_private_key_kid] = pub
            except Exception:
                _logger.exception('ow_mcp_oauth: failed to read current public key')
        if self.rsa_previous_public_key_pem and self.rsa_previous_public_key_kid:
            out[self.rsa_previous_public_key_kid] = self.rsa_previous_public_key_pem
        return out

    def jwks_document(self):
        self.ensure_one()
        keys = []
        for kid, pub in self.public_keys_by_kid().items():
            try:
                keys.append(jwt_helper.jwk_from_public_pem(pub, kid))
            except Exception:
                _logger.exception('ow_mcp_oauth: skip bad public key kid=%s', kid)
        return {'keys': keys}

    def rotate_keypair(self):
        """Generate a new keypair; demote the current public key to 'previous'.

        Existing access tokens keep verifying until exp; new tokens are signed
        with the new key. Refresh tokens carry no signature so they're
        unaffected.
        """
        self.ensure_one()
        old_pub = None
        old_kid = self.rsa_private_key_kid
        if self.rsa_private_key_pem:
            try:
                old_pub = jwt_helper.public_pem_from_private_pem(self.rsa_private_key_pem)
            except Exception:
                _logger.exception('ow_mcp_oauth: cannot derive previous public key')
        priv, _pub, kid = jwt_helper.generate_rsa_keypair()
        self.sudo().write({
            'rsa_private_key_pem': priv,
            'rsa_private_key_kid': kid,
            'rsa_previous_public_key_pem': old_pub or False,
            'rsa_previous_public_key_kid': old_kid or False,
        })
        _logger.info(
            'ow_mcp_oauth: rotated key. new kid=%s, prev kid=%s', kid, old_kid,
        )
        return True

    def base_url(self):
        """Return the canonical issuer base (no trailing slash)."""
        return (
            self.env['ir.config_parameter']
            .sudo().get_param('web.base.url', '').rstrip('/')
        )

    def issuer_url(self):
        return self.base_url()

    def effective_canonical_resource_uri(self):
        self.ensure_one()
        if self.canonical_resource_uri:
            return self.canonical_resource_uri.rstrip('/')
        return f'{self.base_url()}/mcp'

    def _cron_purge_expired(self):
        """Hourly: delete expired auth codes, expired refresh tokens,
        expired access-token JTIs, expired consent rows, and unused DCR
        clients past `unused_client_purge_days`.
        """
        self.ensure_one()
        now = fields.Datetime.now()
        env = self.env

        # Auth codes (very short-lived).
        env['ow.mcp.oauth.authorization_code'].sudo().search(
            ['|', ('expires_at', '<', now), ('consumed', '=', True)]
        ).unlink()

        # Refresh tokens past expiry or revoked + > 30d old.
        env['ow.mcp.oauth.refresh_token'].sudo().search(
            [('expires_at', '<', now)]
        ).unlink()

        # Access tokens past expiry — keep recently revoked rows for audit
        # for 7 days then drop.
        env.cr.execute(
            "DELETE FROM ow_mcp_oauth_access_token "
            "WHERE expires_at < (NOW() - INTERVAL '7 days')"
        )

        # Consent rows past expiry.
        env['ow.mcp.oauth.consent'].sudo().search(
            [('expires_at', '<', now)]
        ).unlink()

        # Unused dynamic clients (no last_used_at) registered > N days ago.
        if self.unused_client_purge_days > 0:
            env.cr.execute(
                "DELETE FROM ow_mcp_oauth_client "
                "WHERE is_dynamic = TRUE "
                "  AND last_used_at IS NULL "
                "  AND create_date < (NOW() - INTERVAL %s)",
                (f'{self.unused_client_purge_days} days',),
            )

        # DCR rate-limit ledger: drop rows older than the sliding window.
        from ..controllers import _utils as u
        u.dcr_prune(env)
        return True

    @api.ondelete(at_uninstall=False)
    def _forbid_unlink(self):
        raise UserError(
            'ow.mcp.oauth.config is a singleton and cannot be deleted. '
            'Edit the existing record or uninstall the module to remove it.'
        )
