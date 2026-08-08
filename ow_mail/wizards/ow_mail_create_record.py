from odoo import _, api, fields, models
from odoo.exceptions import UserError
from markupsafe import Markup
import logging

_logger = logging.getLogger(__name__)

class OwMailCreateRecord(models.TransientModel):
    _name = "ow.mail.create.record"
    _description = "Create Record from Mail"

    folder_id = fields.Integer(required=True)
    uid = fields.Integer(required=True)
    subject = fields.Char(readonly=True)
    from_name = fields.Char(readonly=True)
    from_email = fields.Char(readonly=True)
    date = fields.Char(readonly=True)
    body_html = fields.Html(readonly=True)
    
    res_model_id = fields.Many2one(
        "ir.model", 
        string="Model", 
        required=True, 
        domain=[("is_mail_thread", "=", True)]
    )

    def action_confirm(self):
        self.ensure_one()
        if not self.res_model_id:
            raise UserError(_("Please select a model."))

        model_name = self.res_model_id.model
        TargetModel = self.env[model_name]
        
        vals = {}
        if 'name' in TargetModel._fields:
            vals['name'] = self.subject

        # Check if the selected model has a field "partner_id"
        if 'partner_id' in TargetModel._fields:
            partner = self.env['res.partner'].search([('email_normalized', '=', self.from_email)], limit=1)
            if not partner:
                partner = self.env['res.partner'].search([('email', '=ilike', self.from_email)], limit=1)
            if partner:
                vals['partner_id'] = partner.id

        # Check for description, note, or body fields
        target_field = False
        for field_name in ['description', 'note', 'body']:
            if field_name in TargetModel._fields:
                target_field = field_name
                break
        
        if not target_field:
            # Check with ilike as requested: "Check if the new model has a field ilike 'description' or 'note' or 'body'."
            # In Odoo, field names are usually exact, but let's look for fields containing these strings.
            all_fields = TargetModel._fields.keys()
            for f in all_fields:
                if 'description' in f or 'note' in f or 'body' in f:
                    # Prefer exact matches if multiple found, but here we just take the first one that matches
                    target_field = f
                    break

        context_msg_body = False
        if target_field:
            vals[target_field] = self.body_html
        else:
            context_msg_body = self.body_html
        # We don't create the record here because we want to open the form view for the user to add more info.
        # "If user sets model and confirms, open the form view for new record woith context in current tab."
        
        context = dict(self.env.context)
        for key, value in vals.items():
            context[f'default_{key}'] = value
        
        # Pass information for post-creation logic
        context['ow_mail_create_record'] = True
        context['ow_mail_folder_id'] = self.folder_id
        context['ow_mail_uid'] = self.uid

        if self.res_model_id.model == 'account.move':
            context['move_type'] = 'in_invoice'

        _logger.info(f"Context: {context}")
        if context_msg_body:
            context['ow_mail_context_msg_body'] = context_msg_body

        return {
            'type': 'ir.actions.act_window',
            'res_model': model_name,
            'views': [[False, 'form']],
            'target': 'current',
            'context': context,
        }
