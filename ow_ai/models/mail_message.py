# -*- coding: utf-8 -*-
"""Turn a ``mail.message`` into LLM message parts (mirrors ``ir.attachment``,
see ``models/ir_attachment.py::_ow_ai_to_parts``)."""
from __future__ import annotations

from odoo import models
from odoo.tools.mail import html2plaintext

from ..engine.types import text_part


class MailMessage(models.Model):
    _inherit = 'mail.message'

    def _ow_ai_to_parts(self) -> list:
        """Return the parts (text + inline_data) for `self`, one message.

        Attachments are read as the current user (``check_access('read')``);
        a message carrying attachments gets an extra text part naming them,
        ahead of each attachment's own parts.
        """
        self.ensure_one()
        parts = []
        text = html2plaintext(self.body or '')
        if text:
            parts.append(text_part(text))

        attachments = self.attachment_ids
        if attachments:
            attachments.check_access('read')
            names = ', '.join(attachments.mapped('name'))
            parts.append(text_part(self.env._("Attached files: %(names)s", names=names)))
            for attachment in attachments:
                parts.extend(attachment._ow_ai_to_parts())

        return parts
