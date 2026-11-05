# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
from werkzeug.exceptions import NotFound

from odoo import http
from odoo.exceptions import UserError
from odoo.http import request

from odoo.addons.payment.controllers.post_processing import PaymentPostProcessing


class MollieSepaMandateController(http.Controller):
    """Standalone SEPA mandate request: a personal e-mailed link or the portal."""

    def _sepa_link_partner(self, partner_id, access_token):
        partner = request.env["res.partner"].sudo().browse(partner_id).exists()
        if not partner or not partner._sepa_check_mandate_access_token(access_token):
            raise NotFound()
        return partner.commercial_partner_id

    def _sepa_provider(self, partner):
        Provider = request.env["payment.provider"].sudo()
        return (
            Provider._sepa_get_enabled_providers(company=partner.company_id or request.env.company)
            or Provider._sepa_get_enabled_providers()
        )[:1]

    def _sepa_render(self, partner, landing_route, access_token=None, error=False, tx=None, new=None):
        provider = self._sepa_provider(partner)
        mandate = request.env["payment.token"]
        if provider:
            mandate = mandate.sudo().search([
                ("partner_id", "child_of", partner.id),
                ("provider_id", "=", provider.id),
                ("provider_ref", "=like", "mdt_%"),
                ("active", "=", True),
                ("sepa_status", "!=", "invalid"),
            ], order="create_date desc, id desc").filtered(lambda t: t._sepa_is_current_mode())[:1]
        transaction = request.env["payment.transaction"]
        if tx:
            try:
                transaction = transaction.sudo().browse(int(tx)).exists()
            except (TypeError, ValueError):
                transaction = transaction.browse()
            if (
                transaction.operation != "validation"
                or transaction.partner_id.commercial_partner_id != partner
            ):
                transaction = transaction.browse()
        tx_state = transaction.state if transaction else False
        if tx_state == "done" and transaction.token_id:
            status = "granted"
        elif tx_state in ("draft", "pending", "authorized", "done"):
            status = "processing"
        elif tx_state in ("cancel", "error"):
            status = "failed"
        else:
            status = "existing" if mandate else "none"
        methods = provider._sepa_mandate_methods() if provider else []
        return request.render("ow_payment_mollie_sepa.sepa_mandate_page", {
            "partner": partner,
            "company": provider.company_id or request.env.company,
            "methods": methods,
            "show_methods": bool(methods) and (
                status in ("none", "failed") or (status == "existing" and bool(new))
            ),
            "status": status,
            "mandate": transaction.token_id if status == "granted" else mandate,
            "access_token": access_token,
            "landing_route": landing_route,
            "error": error,
        })

    @http.route(
        "/sepa/mandate/<int:partner_id>/<string:access_token>",
        type="http", auth="public", website=True, sitemap=False,
    )
    def sepa_mandate_link(self, partner_id, access_token, error=None, tx=None, new=None, **kwargs):
        partner = self._sepa_link_partner(partner_id, access_token)
        landing_route = f"/sepa/mandate/{partner.id}/{access_token}"
        return self._sepa_render(partner, landing_route, access_token, bool(error), tx, new)

    @http.route("/my/sepa_mandate", type="http", auth="user", website=True, sitemap=False)
    def sepa_mandate_portal(self, error=None, tx=None, new=None, **kwargs):
        partner = request.env.user.partner_id.sudo().commercial_partner_id
        return self._sepa_render(partner, "/my/sepa_mandate", error=bool(error), tx=tx, new=new)

    @http.route(
        "/sepa/mandate/start", type="http", methods=["POST"], auth="public",
        website=True, sitemap=False,
    )
    def sepa_mandate_start(self, payment_method_id, partner_id=None, access_token=None, **kwargs):
        try:
            payment_method_id = int(payment_method_id)
            partner_id = int(partner_id) if partner_id else None
        except (TypeError, ValueError):
            raise NotFound()
        if partner_id:
            partner = self._sepa_link_partner(partner_id, access_token)
            landing_route = f"/sepa/mandate/{partner.id}/{access_token}"
        elif not request.env.user._is_public():
            partner = request.env.user.partner_id.sudo().commercial_partner_id
            landing_route = "/my/sepa_mandate"
        else:
            raise NotFound()
        provider = self._sepa_provider(partner)
        method = request.env["payment.method"].sudo().browse(payment_method_id).exists()
        if not provider:
            raise NotFound()
        try:
            tx = provider._sepa_create_mandate_transaction(partner, method, landing_route)
        except UserError:
            raise NotFound()
        tx.landing_route = f"{landing_route}?tx={tx.id}"
        rendering_values = tx._get_specific_rendering_values({})
        if not rendering_values.get("api_url"):
            return request.redirect(f"{landing_route}?error=1")
        PaymentPostProcessing.monitor_transaction(tx)
        return request.redirect(rendering_values["api_url"], local=False)
