# -*- coding: utf-8 -*-
"""Base-model mixin exposing generic ``_ow_ai_*`` helpers to every model.

These helpers let AI tools read and summarise arbitrary records without each
tool re-implementing field selection / serialisation. ``_ow_ai_default_fields``
picks a small, generic set of fields by name-matching; Task 2 (automation
tools per app) is expected to override ``_ow_ai_extra_fields`` per model (as
``ow_ai/models/res_partner.py`` already does for ``res.partner``) to add
model-specific fields without duplicating this logic.
"""
from __future__ import annotations

from odoo import fields, models
from odoo.tools.mail import html2plaintext

from ..utils.serialize import serialize_record, serialize_records, truncate_text

_MAX_DEFAULT_FIELDS = 25
_MAX_DATE_FIELDS = 6
_CHATTER_LIMIT = 10
_CHATTER_BODY_LIMIT = 500


class Base(models.AbstractModel):
    _inherit = 'base'

    # Per-model extension point: subclasses may set this to a list of extra
    # field names to always include in `_ow_ai_default_fields()`.
    _ow_ai_extra_fields: list = []

    def _ow_ai_default_fields(self):
        """Return a small, generic list of field names for `_ow_ai_read`.

        Picks name-like fields, common status/relation fields, amount
        totals, description-like fields and any `_ow_ai_extra_fields`
        declared by the model. Only fields that actually exist on the model
        and are not binary/image/x2many-to-mail fields are kept. Capped at
        25 names.
        """
        field_defs = self._fields
        candidates = []

        def add(name):
            if name and name in field_defs and name not in candidates:
                candidates.append(name)

        rec_name = getattr(self, '_rec_name', None) or 'name'
        add(rec_name)
        for name in ('display_name', 'name', 'ref', 'code'):
            add(name)

        add('active')
        for name in ('state', 'stage_id'):
            add(name)
        for name in ('user_id', 'partner_id', 'company_id'):
            add(name)

        date_matches = [name for name in field_defs if 'date' in name and not name.startswith('_')]
        for name in sorted(date_matches)[:_MAX_DATE_FIELDS]:
            add(name)

        for name in ('amount_total', 'amount_untaxed'):
            add(name)
        for name in ('description', 'note'):
            add(name)

        for name in self._ow_ai_extra_fields:
            add(name)

        result = []
        for name in candidates:
            field = field_defs.get(name)
            if field is None or field.type in ('binary', 'image'):
                continue
            if field.type in ('one2many', 'many2many') and field.comodel_name == 'mail.message':
                continue
            result.append(name)

        return result[:_MAX_DEFAULT_FIELDS]

    def _ow_ai_read(self, field_names=None):
        """Serialise `self` (a recordset) for the LLM. See `serialize_records`."""
        return serialize_records(self.env, self, field_names)

    def _ow_ai_chatter_summary(self, limit=_CHATTER_LIMIT):
        """Summarise the last `limit` chatter messages, newest first.

        Returns `None` when the model has no `message_ids` (does not inherit
        `mail.thread`). Runs through `self.env`, so record rules on
        `mail.message` are respected as usual.
        """
        if 'message_ids' not in self._fields:
            return None

        messages = self.message_ids.filtered(lambda m: m.message_type in ('comment', 'email'))
        messages = messages.sorted(key=lambda m: m.date or m.create_date, reverse=True)[:limit]

        summary = []
        for message in messages:
            summary.append({
                'date': fields.Datetime.to_string(message.date) if message.date else None,
                'author': message.author_id.display_name if message.author_id else message.email_from,
                'type': message.message_type,
                'body': truncate_text(html2plaintext(message.body or ''), _CHATTER_BODY_LIMIT),
            })
        return summary

    def _ow_ai_record_context(self):
        """Build the "Ask AI about this record" snapshot for `self` (one record)."""
        self.ensure_one()
        base_url = self.get_base_url()
        default_fields = self._ow_ai_default_fields()
        return {
            'model': self._name,
            'model_label': self._description,
            'id': self.id,
            'display_name': self.display_name,
            'url': f"{base_url}/odoo/{self._name}/{self.id}",
            'fields': serialize_record(self.env, self, default_fields),
            'chatter': self._ow_ai_chatter_summary(),
        }
