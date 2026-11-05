# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
import re

from odoo import _, fields, models
from odoo.exceptions import UserError

try:
    from stdnum import iban as iban_validator
except Exception:  # noqa: B902 - optional, may be broken
    iban_validator = None

IBAN_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{10,30}$")


class MollieSepaMandateWizard(models.TransientModel):
    _name = "mollie.sepa.mandate.wizard"
    _description = "Register SEPA Direct Debit Mandate"

    partner_id = fields.Many2one(
        "res.partner", string="Customer", required=True, readonly=True
    )
    bank_id = fields.Many2one(
        "res.partner.bank",
        string="Bank Account",
        required=True,
        domain="[('partner_id', 'child_of', partner_id)]",
        help="The customer's IBAN that will be debited through this mandate.",
    )
    iban = fields.Char(related="bank_id.acc_number", string="IBAN")
    consumer_name = fields.Char(required=True)
    signature_date = fields.Date(
        string="Date of Signature",
        required=True,
        default=fields.Date.context_today,
        help="The date on which the customer signed (or accepted) the SEPA "
        "Direct Debit mandate.",
    )
    mandate_reference = fields.Char(
        string="Mandate Reference",
        required=True,
        readonly=False,
        help="A unique reference identifying this mandate. Banks may decline "
        "direct debits when the reference is reused.",
    )
    provider_id = fields.Many2one(
        "payment.provider",
        string="Provider",
        required=True,
        domain="[('code', '=', 'mollie'), ('sepa_enabled', '=', True)]",
    )

    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        partner = self.env["res.partner"].browse(
            self.env.context.get("default_partner_id")
        )
        if partner and "bank_id" in fields_list and not res.get("bank_id"):
            banks = partner.bank_ids.filtered(
                lambda b: b.acc_type == "iban" and b.active
            )
            if not banks:
                banks = partner.bank_ids.filtered(lambda b: b.active)
            if banks:
                res["bank_id"] = banks[:1].id
        provider = self.env["payment.provider"]._sepa_get_enabled_providers(
            company=partner.company_id or self.env.company
        ) or self.env["payment.provider"]._sepa_get_enabled_providers()
        if provider and "provider_id" in fields_list:
            res["provider_id"] = provider[:1].id
        if partner and "mandate_reference" in fields_list and not res.get(
            "mandate_reference"
        ):
            # Reuse the reference printed on the mandate form, if still unused.
            res["mandate_reference"] = (
                partner.commercial_partner_id._sepa_unused_form_reference()
                or provider[:1]._sepa_next_mandate_reference()
            )
        return res

    def action_create_mandate(self):
        self.ensure_one()
        provider = self.provider_id
        iban = (self.iban or "").replace(" ", "").upper()
        self._check_iban(iban)
        if self.signature_date > fields.Date.context_today(self):
            raise UserError(_("The signature date cannot be in the future."))

        existing = self.env["payment.token"].sudo().with_context(active_test=False).search([
            ("provider_id", "=", provider.id),
            ("sepa_mandate_reference", "=", self.mandate_reference),
        ], limit=1)
        if existing:
            raise UserError(_(
                "A mandate with reference %s already exists (customer %s). "
                "Mandate references must be unique.",
                self.mandate_reference, existing.partner_id.display_name,
            ))

        customer_id = provider._sepa_get_or_create_customer(self.partner_id)
        if not customer_id:
            raise UserError(_("The Mollie customer could not be created. "
                              "Check the provider's API key and logs."))

        # Raises a ValidationError with Mollie's message when refused.
        mandate = provider._api_sepa_create_mandate(
            customer_id,
            consumer_name=self.consumer_name,
            iban=iban,
            signature_date=self.signature_date,
            mandate_reference=self.mandate_reference,
            bic=self._get_bic(),
        )
        if not mandate or not mandate.get("id"):
            raise UserError(_(
                "Mollie refused to create the mandate. Check the IBAN, the "
                "consumer name and the signature date, and see the payment "
                "logs for the exact error."
            ))

        token = provider._sepa_get_or_create_token(self.partner_id, mandate)
        token.sudo().write({"sepa_signature_date": self.signature_date})
        self.partner_id.message_post(body=_(
            "SEPA Direct Debit mandate %(ref)s registered at Mollie for IBAN "
            "%(iban)s (status: %(status)s).",
            ref=self.mandate_reference,
            iban=f"****{iban[-4:]}",
            status=mandate.get("status", "pending"),
        ))
        return {"type": "ir.actions.act_window_close"}

    def _get_bic(self):
        self.ensure_one()
        bank = self.bank_id.bank_id
        return (bank.bic if bank and "bic" in bank._fields else False) or False

    def _check_iban(self, iban):
        self.ensure_one()
        if iban_validator is not None:
            try:
                iban_validator.validate(iban)
            except Exception as e:  # noqa: B902
                raise UserError(_("Invalid IBAN: %s", e)) from e
        elif len(iban) < 15 or not IBAN_RE.match(iban):
            raise UserError(_("This does not look like a valid IBAN: %s", iban))
