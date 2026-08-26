from odoo import api, models


class AccountMoveSend(models.AbstractModel):
    """``account.move.send`` is the abstract model shared by
    ``account.move.send.wizard`` (single send), ``account.move.send.batch.wizard``
    (batch send) and the asynchronous cron send. Overriding it here covers all
    three paths at once.
    """

    _inherit = 'account.move.send'

    @api.model
    def _get_default_mail_partner_ids(self, move, mail_template, mail_lang):
        partners = super()._get_default_mail_partner_ids(move, mail_template, mail_lang)

        # Ordering matters: super() normally ends on ``.filtered('email')``, so a
        # customer without an email is already gone from ``partners`` by now. The
        # invoice contact is therefore resolved from ``move`` directly -- resolving
        # it from super()'s output would leave exactly that customer unswapped, and
        # the invoice would end up with no recipient at all.
        invoice_partners = partners.browse()
        for customer in move.partner_id | move.commercial_partner_id:
            resolved = customer.sudo()._get_invoice_email_partner()
            if resolved != customer:
                # ``with_env`` keeps a sudo recordset out of the wizard field.
                invoice_partners |= resolved.with_env(partners.env)
                partners -= customer

        recipients = partners | invoice_partners

        # Mirror super()'s own closing line. ``account.move.action_invoice_sent()``
        # sets this context precisely so that partners without an email survive and
        # the wizard can raise its "Partner(s) should have an email address" alert.
        # Re-applying the filter here would silently swallow that warning.
        if self.env.context.get('allow_partners_without_mail'):
            return recipients
        return recipients.filtered('email')

    @api.model
    def _send_mail(self, move, mail_template, **kwargs):
        """Keep the superseded customer from receiving the mail as a follower.

        Swapping the "To" field is not enough on its own: this method posts the
        invoice email as an ``mt_comment`` message, and ``message_post``
        notifies the record's *followers* on top of ``partner_ids``. The
        customer company is an auto-subscribed follower of its own invoice, so
        in stock Odoo it is simply deduplicated against the "To" field -- but
        once the invoice contact takes that slot, the company would receive a
        second, separate mail.

        The superseded partner is flagged in the context and dropped again in
        ``account.move._notify_get_recipients``. Follower records themselves are
        left untouched: the company stays a follower and keeps seeing the
        invoice, it just does not receive this particular email.
        """
        partner_ids = set(kwargs.get('partner_ids') or [])
        superseded = move.partner_id.browse()
        for customer in move.partner_id | move.commercial_partner_id:
            resolved = customer.sudo()._get_invoice_email_partner()
            if resolved != customer and resolved.id in partner_ids:
                superseded |= customer
        if superseded:
            move = move.with_context(
                ow_invoice_mail_superseded_partner_ids=superseded.ids)
        return super()._send_mail(move, mail_template, **kwargs)


class AccountMove(models.Model):
    _inherit = 'account.move'

    def _notify_get_recipients(self, message, msg_vals=False, **kwargs):
        recipients = super()._notify_get_recipients(message, msg_vals=msg_vals, **kwargs)
        superseded = self.env.context.get('ow_invoice_mail_superseded_partner_ids')
        if not superseded:
            return recipients
        superseded = set(superseded)
        return [r for r in recipients if r['id'] not in superseded]


class AccountMoveSendWizard(models.TransientModel):
    _inherit = 'account.move.send.wizard'

    # The @api.depends must be re-declared: Odoo resolves a computed field's
    # dependencies from the outermost method in the MRO -- ours. An override
    # without the decorator would leave the field with no dependencies and it
    # would silently stop recomputing.
    @api.depends('template_id', 'lang')
    def _compute_mail_partners(self):
        super()._compute_mail_partners()
        for wizard in self:
            # The template branch already went through
            # _get_default_mail_partner_ids above. Only the no-template
            # fallback needs patching: it hardcodes commercial_partner_id and
            # never renders a template at all.
            if wizard.template_id or not wizard.move_id:
                continue
            commercial_partner = wizard.move_id.commercial_partner_id
            resolved = commercial_partner.sudo()._get_invoice_email_partner()
            if resolved != commercial_partner:
                wizard.mail_partner_ids = resolved.with_env(self.env)
