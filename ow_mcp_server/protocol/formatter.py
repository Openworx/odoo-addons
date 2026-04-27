"""LLM-friendly serialization of Odoo recordsets.

Turns ORM field values into JSON shapes that are easy for language models to
read: many2one as {id, name}, x2many as a capped list of the same,
html stripped to plain text, dates as ISO 8601, binaries as metadata only.
"""
import base64
import re


_HTML_TAG_RE = re.compile(r'<[^>]+>')


def serialize_records(records, fields, max_text_len, max_preview):
    """Serialize `records` returning a list of dicts with LLM-friendly values.

    Args:
        records: a recordset (any model).
        fields:  iterable of field names to include; 'id' is always added.
        max_text_len: truncate text/html at this many chars.
        max_preview: cap x2many previews at this many entries.
    """
    out = []
    model_fields = records._fields
    for rec in records:
        row = {'id': rec.id}
        for fname in fields:
            if fname == 'id':
                continue
            f = model_fields.get(fname)
            if f is None:
                continue
            row[fname] = _format_value(rec, f, rec[fname], max_text_len, max_preview)
        out.append(row)
    return out


def _format_value(rec, f, val, max_text_len, max_preview):
    ftype = f.type
    if val is False and ftype not in ('boolean',):
        return None
    if val is None:
        return None

    if ftype == 'many2one':
        return {'id': val.id, 'name': val.display_name} if val else None

    if ftype in ('one2many', 'many2many'):
        total = len(val)
        preview = [{'id': r.id, 'name': r.display_name} for r in val[:max_preview]]
        if total > max_preview:
            return {'preview': preview, 'total': total, 'truncated': True}
        return preview

    if ftype == 'binary':
        if not val:
            return None
        # Odoo Binary fields return base64-encoded bytes; decode to report real size.
        try:
            raw = base64.b64decode(val)
        except Exception:
            raw = val if isinstance(val, (bytes, bytearray)) else b''
        return {'size': len(raw), 'truncated': True}

    if ftype == 'html':
        text = _HTML_TAG_RE.sub('', str(val)).strip()
        return _truncate(text, max_text_len)

    if ftype == 'text':
        return _truncate(str(val), max_text_len)

    if ftype == 'monetary':
        currency = rec[f.get_currency_field(rec)] if hasattr(f, 'get_currency_field') else None
        cur_name = currency.name if currency else None
        return {'amount': val, 'currency': cur_name}

    if ftype == 'datetime':
        # Odoo stores datetimes naive-UTC; emit ISO 8601 with Z.
        return val.strftime('%Y-%m-%dT%H:%M:%SZ')

    if ftype == 'date':
        return val.isoformat()

    # boolean, integer, float, char, selection → pass through
    return val


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + '…'
