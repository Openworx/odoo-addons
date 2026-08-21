from odoo import _, api, fields, models
from odoo.exceptions import UserError


class OwMailAttachRecord(models.TransientModel):
    """Attach an email to an existing record ("chain" toolbar action).

    Thin UI shell: all fetch/post/stamp logic lives in
    ``ow.mail.record.link.attach_email`` so the create-record flow and this
    wizard share one implementation (single IMAP fetch, server-side
    sanitizing, Message-ID duplicate guard).
    """
    _name = "ow.mail.attach.record"
    _description = "Attach Mail to Record"

    folder_id = fields.Integer(required=True)
    uid = fields.Integer(required=True)
    subject = fields.Char(readonly=True)
    from_name = fields.Char(readonly=True)
    from_email = fields.Char(readonly=True)
    date = fields.Char(readonly=True)
    body_html = fields.Html(readonly=True)
    has_attachments = fields.Boolean(readonly=True)
    include_attachments = fields.Boolean(string="Include Attachments", default=True)

    res_model_id = fields.Many2one("ir.model", string="Model", required=True,
                                   ondelete="cascade")
    res_id = fields.Integer(string="Record ID", required=True)

    # For selection of record
    record_ref = fields.Reference(selection="_selection_target_model",
                                  string="Target Record")

    @api.model
    def _selection_target_model(self):
        # Attach targets: readable mail-thread models only (posting rights
        # are enforced by message_post at confirm). The unfiltered ir.model
        # list this used to return was both unusable and an ACL smell.
        return [(row["model"], row["name"])
                for row in self.env["ow.mail.record.link"]
                .get_creatable_models(mode="read")]

    @api.onchange("record_ref")
    def _onchange_record_ref(self):
        if self.record_ref:
            self.res_model_id = self.env["ir.model"].search(
                [("model", "=", self.record_ref._name)], limit=1)
            self.res_id = self.record_ref.id

    def action_confirm(self):
        self.ensure_one()
        if not self.res_model_id or not self.res_id:
            raise UserError(_("Please select a target record."))
        folder = self.env["ow.mail.folder"].browse(self.folder_id).exists()
        # Defense-in-depth ownership assert, mirroring the controller's
        # _get_folder: a crafted default_folder_id in context must not let
        # a user attach (and thereby read) someone else's mail.
        if not folder or folder.account_id.user_id != self.env.user:
            raise UserError(_("Folder not found."))
        self.env["ow.mail.record.link"].attach_email(
            folder, self.uid, self.res_model_id.model, self.res_id,
            include_attachments=self.include_attachments)
        return {"type": "ir.actions.act_window_close"}
