# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
import logging

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class PaymentProvider(models.Model):
    _inherit = "payment.provider"

    def _sepa_get_or_create_token(self, partner, mandate):
        self.ensure_one()
        mandate_id = mandate.get("id")
        if not mandate_id or not mandate_id.startswith("mdt_"):
            return self.env["payment.token"]
        token = self.env["payment.token"].sudo().with_context(active_test=False).search([
            ("provider_id", "=", self.id),
            ("provider_ref", "=", mandate_id),
        ], limit=1)
        if token:
            token._sepa_update_from_mandate(mandate)
            # Odoo archives all tokens when the provider state changes; restore
            # the ones whose mandate is still usable at Mollie.
            if (
                not token.active
                and not token.sepa_superseded
                and mandate.get("status") in ("valid", "pending")
                and token._sepa_is_current_mode()
                and self.state != "disabled"
                and token.payment_method_id in self.with_context(active_test=False).payment_method_ids
            ):
                # Disabling the provider also deactivated its payment methods.
                token.payment_method_id.filtered(lambda m: not m.active).sudo().active = True
                token.write({"active": True})
            return token
        details = mandate.get("details", {})
        iban = details.get("consumerAccount") or ""
        payment_method = self._sepa_get_payment_method()
        token = self.env["payment.token"].sudo().create({
            "provider_id": self.id,
            "payment_method_id": payment_method.id,
            "partner_id": partner.commercial_partner_id.id,
            "provider_ref": mandate_id,
            "payment_details": f"****{iban[-4:]}" if iban else mandate_id,
            "mollie_customer_id": mandate.get("customerId"),
            "sepa_status": mandate.get("status"),
            "sepa_mandate_reference": mandate.get("mandateReference"),
            "sepa_consumer_name": details.get("consumerName") or partner.commercial_partner_id.name,
            "sepa_consumer_account": iban,
            "sepa_signature_date": mandate.get("signatureDate") or False,
        })
        if mandate.get("customerId"):
            self._sepa_store_customer(token.partner_id, mandate["customerId"])
        return token


class PaymentToken(models.Model):
    _inherit = "payment.token"

    sepa_status = fields.Selection(
        selection=[
            ("valid", "Valid"),
            ("pending", "Pending"),
            ("invalid", "Invalid"),
        ],
        string="Mandate Status",
        help="The status of the SEPA mandate at Mollie, as last synced.",
    )
    sepa_mode = fields.Selection(
        selection=[("test", "Test"), ("live", "Live")],
        string="Mollie Mode",
        readonly=True,
        help="Mode (API key) of the provider when this token was created. A "
        "test-mode mandate cannot be charged in live mode and vice versa.",
    )
    sepa_superseded = fields.Boolean(
        string="Replaced by a Newer Mandate", readonly=True, copy=False,
        help="Archived because the customer granted a newer mandate online. "
        "Syncing from Mollie does not restore it.",
    )
    sepa_mandate_reference = fields.Char(
        string="Mandate Reference",
        help="The unique mandate reference registered at Mollie and the bank.",
    )
    sepa_signature_date = fields.Date(string="Signature Date")
    sepa_consumer_name = fields.Char(string="Consumer Name")
    sepa_consumer_account = fields.Char(
        string="Consumer Account (IBAN)", groups="account.group_account_invoice",
    )
    is_sepa_mandate = fields.Boolean(
        string="SEPA Mandate", compute="_compute_is_sepa_mandate"
    )

    @api.depends("provider_code", "provider_ref")
    def _compute_is_sepa_mandate(self):
        for token in self:
            token.is_sepa_mandate = (
                token.provider_code == "mollie"
                and (token.provider_ref or "").startswith("mdt_")
            )

    @api.model_create_multi
    def create(self, vals_list):
        Provider = self.env["payment.provider"]
        for vals in vals_list:
            provider = Provider.browse(vals.get("provider_id")).sudo()
            if provider.code == "mollie" and not vals.get("sepa_mode"):
                vals["sepa_mode"] = provider._sepa_mode()
        tokens = super().create(vals_list)
        # Registering a mandate is the customer's consent to be collected.
        tokens.filtered(
            lambda t: t.is_sepa_mandate and t.provider_id.sepa_enabled
        ).partner_id.commercial_partner_id.sudo().write({"sepa_collect_enabled": True})
        return tokens

    def _sepa_is_current_mode(self):
        self.ensure_one()
        return not self.sepa_mode or self.sepa_mode == self.provider_id._sepa_mode()

    def _sepa_is_chargeable(self):
        self.ensure_one()
        return (
            self.active
            and self.is_sepa_mandate
            and self.provider_id.state in ("enabled", "test")
            and not self.provider_id.capture_manually
            and self.sepa_status != "invalid"
            and self._sepa_is_current_mode()
        )

    @api.model
    def _get_sepa_token_for_partner(self, partner, provider):
        """Return the partner company's best mandate: valid first, then newest."""
        tokens = self.sudo().search([
            ("partner_id", "child_of", partner.commercial_partner_id.id),
            ("provider_id", "=", provider.id),
            ("provider_ref", "=like", "mdt_%"),
            ("active", "=", True),
            ("sepa_status", "!=", "invalid"),
        ], order="create_date desc, id desc").filtered(lambda t: t._sepa_is_current_mode())
        token = tokens.filtered(lambda t: t.sepa_status == "valid")[:1] or tokens[:1]
        # Re-check an unconfirmed mandate before using it.
        if token and token.sepa_status != "valid":
            try:
                token._sepa_sync()
            except ValidationError as e:
                _logger.warning("SEPA mandate %s could not be synced: %s", token.provider_ref, e)
            if token.sepa_status == "invalid" or not token.active:
                return self._get_sepa_token_for_partner(partner, provider)
        return token or self._sepa_find_unknown_mandate(partner, provider)

    @api.model
    def _sepa_find_unknown_mandate(self, partner, provider):
        """Return a token for a valid mandate that exists at Mollie but not in
        Odoo (e.g. created in the Mollie dashboard). Mandates whose token was
        archived in Odoo are left alone: archiving stops the collection.
        """
        customer_id = provider._sepa_get_customer(partner)
        if not customer_id:
            return self.browse()
        try:
            mandates = provider._api_sepa_list_mandates(customer_id)
        except ValidationError as e:
            _logger.warning("SEPA mandates of %s could not be listed: %s", customer_id, e)
            return self.browse()
        known = set(self.sudo().with_context(active_test=False).search([
            ("provider_id", "=", provider.id),
            ("provider_ref", "in", [m.get("id") for m in mandates]),
        ]).mapped("provider_ref"))
        candidates = [
            m for m in mandates
            if m.get("method") == "directdebit"
            and m.get("status") in ("valid", "pending")
            and m.get("id") not in known
        ]
        candidates.sort(key=lambda m: (m.get("status") == "valid", m.get("createdAt") or ""))
        if not candidates:
            return self.browse()
        return provider._sepa_get_or_create_token(partner, candidates[-1])

    def _sepa_sync(self):
        for token in self:
            if not token.is_sepa_mandate or not token._sepa_is_current_mode():
                continue
            customer_id = (
                token.mollie_customer_id
                or token.provider_id._sepa_get_customer(token.partner_id)
            )
            if not customer_id:
                continue
            mandate = token.provider_id._api_sepa_get_mandate(
                customer_id, token.provider_ref
            )
            if mandate:
                token._sepa_update_from_mandate(mandate)

    def _sepa_update_from_mandate(self, mandate):
        for token in self.sudo():
            details = mandate.get("details", {})
            vals = {
                "sepa_status": mandate.get("status") or token.sepa_status,
                "sepa_mandate_reference": mandate.get("mandateReference")
                or token.sepa_mandate_reference,
                "sepa_consumer_name": details.get("consumerName")
                or token.sepa_consumer_name,
                "sepa_consumer_account": details.get("consumerAccount")
                or token.sepa_consumer_account,
            }
            if mandate.get("signatureDate") and not token.sepa_signature_date:
                vals["sepa_signature_date"] = mandate["signatureDate"]
            iban = vals["sepa_consumer_account"] or ""
            if iban:
                vals["payment_details"] = f"****{iban[-4:]}"
            token.sudo().write(vals)
            if mandate.get("status") == "invalid" and token.active:
                token.sudo()._handle_sepa_revoked()

    def _handle_sepa_revoked(self):
        for token in self:
            token.partner_id.message_post(body=_(
                "The SEPA direct debit mandate %(ref)s is invalid at Mollie. "
                "Automatic collection is disabled for this customer until a "
                "new mandate is registered.",
                ref=token.sepa_mandate_reference or token.provider_ref,
            ))
            token.sudo().write({"active": False})
