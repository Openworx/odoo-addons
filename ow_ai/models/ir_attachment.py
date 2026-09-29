# -*- coding: utf-8 -*-
"""Turn an ``ir.attachment`` record into LLM message parts.

Images are resized and sent as inline data; PDFs are sent as inline data
as-is (page trimming is reserved for later); other files fall back to their
indexed text content (or a "no readable text" note) so the LLM always gets
*something* usable without ever receiving raw, un-introspectable bytes for
huge or unknown file types.
"""
from __future__ import annotations

import base64

from odoo import models
from odoo.tools.image import ImageProcess

from ..engine.types import inline_data_part, text_part
from ..utils.serialize import truncate_text

_IMAGE_MIMETYPES = {'image/png', 'image/jpeg', 'image/webp', 'image/gif'}
_MAX_RAW_BYTES = 25 * 1024 * 1024  # never load raw content above this size


class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    def _ow_ai_to_parts(self, *, max_image_px=1568, max_pdf_pages=None, max_chars=20000):
        """Return the LLM message parts (list of TextPart/InlineDataPart) for `self`.

        `max_pdf_pages` is reserved for future page-trimming of large PDFs;
        currently unused (the whole PDF is sent inline).
        """
        self.ensure_one()
        self.check_access('read')

        if self.file_size and self.file_size > _MAX_RAW_BYTES:
            return [self._ow_ai_no_text_part()]

        mimetype = (self.mimetype or '').split(';')[0].strip().lower()

        if mimetype in _IMAGE_MIMETYPES:
            return [self._ow_ai_image_part(mimetype, max_image_px)]

        if mimetype == 'application/pdf':
            return [inline_data_part(mimetype, _b64(self.raw), filename=self.name)]

        if self.index_content:
            text = f"Content of attachment '{self.name}':\n{truncate_text(self.index_content, max_chars)}"
            return [text_part(text)]

        return [self._ow_ai_no_text_part()]

    def _ow_ai_no_text_part(self):
        size = self.file_size or 0
        mimetype = self.mimetype or 'unknown'
        return text_part(f"Attachment '{self.name}' ({mimetype}, {size} bytes) has no readable text.")

    def _ow_ai_image_part(self, mimetype, max_image_px):
        raw = self.raw or b''
        processed = ImageProcess(raw)
        processed.resize(max_width=max_image_px, max_height=max_image_px)
        output = processed.image_quality() or raw
        return inline_data_part(mimetype, _b64(output), filename=self.name)


def _b64(data) -> str:
    return base64.b64encode(bytes(data or b'')).decode('ascii')
