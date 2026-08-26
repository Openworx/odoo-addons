from odoo import models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    def _get_invoice_email_partner(self):
        """Return the contact that invoice emails should be addressed to.

        Returns the dedicated "Invoice Address" child contact
        (``res.partner.type == 'invoice'``) when one exists and carries its own
        email. Returns ``self`` in every other case.

        ``address_get(['invoice'])`` degrades to a 'contact' child and then to
        the partner itself when no invoice address exists. That fallback is
        exactly what must not be acted on, hence the explicit ``type`` check:
        without it, a customer with no invoice address at all would silently be
        redirected to an arbitrary contact child. The ``email`` check keeps a
        mailable recipient from being traded for an unmailable one.
        """
        self.ensure_one()
        if not isinstance(self.id, int):
            # NewId: virtual record during an onchange, nothing to resolve.
            return self

        invoice_partner_id = self.address_get(['invoice']).get('invoice')
        if not invoice_partner_id or invoice_partner_id == self.id:
            return self

        invoice_partner = self.browse(invoice_partner_id)
        if invoice_partner.type != 'invoice' or not invoice_partner.email:
            return self

        return invoice_partner
