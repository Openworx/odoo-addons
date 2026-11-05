# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
from odoo import _, api, fields, models
from odoo.exceptions import AccessError
from odoo.tools import consteq

from odoo.addons.payment import utils as payment_utils

from .. import const


class ResPartner(models.Model):
    _inherit = "res.partner"

    mollie_sepa_customer_ids = fields.One2many(
        "mollie.sepa.customer", "partner_id", string="Mollie Customers", readonly=True,
    )
    sepa_form_reference = fields.Char(
        string="Printed Mandate Reference", readonly=True, copy=False,
        help="Mandate reference printed on the last SEPA mandate form.",
    )
    sepa_collect_enabled = fields.Boolean(
        string="SEPA Collection",
        help="Open invoices of this customer are included in SEPA collection "
        "batches. Enabled automatically when a mandate is registered.",
    )
    sepa_mandate_count = fields.Integer(
        string="SEPA Mandate Count", compute="_compute_sepa_mandate_count"
    )
    has_sepa_mandate = fields.Boolean(
        string="Has SEPA Mandate", compute="_compute_sepa_mandate_count"
    )
    sepa_token_ids = fields.One2many(
        string="SEPA Mandates", comodel_name="payment.token",
        compute="_compute_sepa_mandate_count"
    )

    @api.model
    def _commercial_fields(self):
        return super()._commercial_fields() + ["sepa_collect_enabled"]

    def _compute_sepa_mandate_count(self):
        Token = self.env["payment.token"]
        for partner in self:
            if not partner.id:
                partner.sepa_token_ids = Token
                partner.sepa_mandate_count = 0
                partner.has_sepa_mandate = False
                continue
            mandates = Token.sudo().search([
                ("partner_id", "child_of", partner.commercial_partner_id.id),
                ("provider_code", "=", "mollie"),
                ("provider_ref", "=like", "mdt_%"),
                ("active", "=", True),
            ])
            partner.sepa_token_ids = mandates
            partner.sepa_mandate_count = len(mandates)
            partner.has_sepa_mandate = bool(
                mandates.filtered(lambda t: t.sepa_status != "invalid")
            )

    def action_create_sepa_mandate(self):
        self.ensure_one()
        return {
            "name": _("Register SEPA Direct Debit Mandate"),
            "type": "ir.actions.act_window",
            "res_model": "mollie.sepa.mandate.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_partner_id": self.commercial_partner_id.id,
                "default_consumer_name": self.commercial_partner_id.display_name,
            },
        }

    def _sepa_unused_form_reference(self):
        """Return the printed mandate reference if no mandate uses it yet."""
        self.ensure_one()
        reference = self.sepa_form_reference
        if not reference:
            return False
        used = self.env["payment.token"].sudo().with_context(active_test=False).search_count([
            ("sepa_mandate_reference", "=", reference),
        ])
        return False if used else reference

    def _sepa_form_provider(self):
        Provider = self.env["payment.provider"].sudo()
        return (
            Provider._sepa_get_enabled_providers(company=self.company_id or self.env.company)
            or Provider._sepa_get_enabled_providers()
        )[:1]

    def action_print_sepa_mandate_form(self):
        self.ensure_one()
        partner = self.commercial_partner_id
        if not partner._sepa_unused_form_reference():
            partner.sudo().sepa_form_reference = partner._sepa_form_provider()._sepa_next_mandate_reference()
        return self.env.ref("ow_payment_mollie_sepa.action_report_sepa_mandate").report_action(partner)

    def _sepa_mandate_form_values(self):
        self.ensure_one()
        provider = self._sepa_form_provider()
        bank = self.bank_ids.filtered(lambda b: b.acc_type == "iban")[:1]
        return {
            "company": provider.company_id or self.env.company,
            "creditor_identifier": provider.sepa_creditor_identifier,
            "reference": self.sepa_form_reference,
            "iban": bank.acc_number,
            "holder": bank.acc_holder_name or (bank and self.name),
        }

    def _sepa_mandate_access_token(self):
        self.ensure_one()
        return payment_utils.generate_access_token(self.id, "sepa-mandate", env=self.env)

    def _sepa_check_mandate_access_token(self, access_token):
        self.ensure_one()
        return bool(access_token) and consteq(access_token, self._sepa_mandate_access_token())

    def get_sepa_mandate_url(self):
        """Personal link to grant a SEPA mandate (used by the request e-mail).

        Accountants only: the link lets anyone holding it attach a bank account
        to this customer.
        """
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group("account.group_account_manager")):
            raise AccessError(_("Only accountants can create SEPA mandate links."))
        partner = self.commercial_partner_id
        token = partner._sepa_mandate_access_token()
        return f"{partner.get_base_url()}/sepa/mandate/{partner.id}/{token}"

    def action_send_sepa_mandate_request(self):
        self.ensure_one()
        partner = self.commercial_partner_id
        template = self.env.ref("ow_payment_mollie_sepa.mail_template_sepa_mandate_request")
        return {
            "name": _("Request SEPA Mandate"),
            "type": "ir.actions.act_window",
            "res_model": "mail.compose.message",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_model": "res.partner",
                "default_res_ids": partner.ids,
                "default_template_id": template.id,
                "default_composition_mode": "comment",
                "default_email_layout_xmlid": "mail.mail_notification_light",
            },
        }

    def action_sync_sepa_mandates(self):
        """Sync mandates for partners that already have a Mollie customer."""
        self.ensure_one()
        partner = self.commercial_partner_id
        providers = self.env["payment.provider"]._sepa_get_enabled_providers(
            company=partner.company_id or self.env.company
        ) or self.env["payment.provider"]._sepa_get_enabled_providers()
        synced = self.env["payment.token"]
        for provider in providers:
            customer_id = provider._sepa_get_customer(partner)
            if not customer_id:
                continue
            for mandate in provider._api_sepa_list_mandates(customer_id):
                if mandate.get("method") != const.SEPA_MOLLIE_METHOD_ID:
                    continue  # e.g. PayPal mandates are not SEPA-relevant
                synced |= provider._sepa_get_or_create_token(partner, mandate)
        partner.message_post(body=_(
            "SEPA mandates synced from Mollie (%d found).", len(synced)
        ))
        return True

    def action_view_sepa_mandates(self):
        self.ensure_one()
        return {
            "name": _("SEPA Mandates"),
            "type": "ir.actions.act_window",
            "res_model": "payment.token",
            "view_mode": "list,form",
            "domain": [
                ("partner_id", "child_of", self.commercial_partner_id.id),
                ("provider_code", "=", "mollie"),
                ("provider_ref", "=like", "mdt_%"),
            ],
        }
