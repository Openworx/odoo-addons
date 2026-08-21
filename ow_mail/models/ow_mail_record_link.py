"""Create-record-from-email helper.

Single home for the record integration:

* :meth:`get_create_menu` — the curated toolbar dropdown (Contact, Meeting,
  Task, Lead, Opportunity, Sales Order, Helpdesk Ticket), filtered to models
  that are installed *and* creatable by the current user;
* :meth:`get_creatable_models` — the "Other…" picker list (all mail-thread
  models the user may create, minus a technical blocklist);
* :meth:`prefill_context` — generic ``default_*`` mapping from an email
  envelope onto any target model (no hardcoded per-app knowledge beyond the
  explicit special cases at the bottom);
* :meth:`attach_email` — materialise the email into the chatter of an
  existing record, stamping the RFC Message-ID for the linked-records panel;
* :meth:`linked_records` — reverse lookup: which records carry this email.

No hard dependencies: optional apps (crm, project, sale, helpdesk) are
probed via the registry (``model in self.env``) and the user's ACLs
(``ir.model.access._get_allowed_models``) at runtime.
"""
import base64
import email
import logging
import re
from datetime import timedelta

import pytz
from markupsafe import Markup

from odoo import _, _lt, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import html2plaintext

from . import ow_mail_imap as imap_utils

_logger = logging.getLogger(__name__)

# Curated toolbar dropdown. The client only ever sends back the ``key`` —
# never a model name or context — so a crafted RPC cannot inject defaults.
CREATE_MENU = [
    {"key": "contact", "model": "res.partner",
     "label": _lt("Contact"), "icon": "fa-user"},
    {"key": "meeting", "model": "calendar.event",
     "label": _lt("Meeting"), "icon": "fa-calendar"},
    {"key": "task", "model": "project.task",
     "label": _lt("Task"), "icon": "fa-check-square-o"},
    {"key": "lead", "model": "crm.lead",
     "label": _lt("Lead"), "icon": "fa-star-o",
     "extra_context": {"default_type": "lead"}},
    {"key": "opportunity", "model": "crm.lead",
     "label": _lt("Opportunity"), "icon": "fa-star",
     "extra_context": {"default_type": "opportunity"}},
    {"key": "sale_order", "model": "sale.order",
     "label": _lt("Sales Order"), "icon": "fa-shopping-cart"},
    {"key": "ticket", "model": "helpdesk.ticket",
     "label": _lt("Helpdesk Ticket"), "icon": "fa-life-ring"},
]

# Technical surface the "Other…" picker must never offer, even when the
# user technically holds create rights (mirrors the blocklist idiom of
# ow_mcp_server, copied — not imported — to keep ow_mail dependency-free).
BLOCKLIST_RE = re.compile(
    r"^(ir\.|base\.|base_|bus\.|auth_|res\.users|res\.groups|res\.config"
    r"|mail\.|fetchmail\.|ow\.mail\.)"
)

# Body lands in the first of these fields that exists on the target model
# and is an html/text field. Explicit allowlist — a substring scan over
# field names would hit unrelated fields like ``note_ids``.
BODY_FIELDS = ("description", "note", "comment")

# Sender-address fallback for models without a declared ``_primary_email``
# (e.g. OCA helpdesk.ticket, which stores it in ``partner_email``).
EMAIL_FIELDS = ("partner_email", "email_from", "email")


class OwMailRecordLink(models.AbstractModel):
    _name = "ow.mail.record.link"
    _description = "OW Mail Record Integration Helper"

    # ------------------------------------------------------------------
    # Menu / picker
    # ------------------------------------------------------------------

    @api.model
    def get_create_menu(self):
        """Curated dropdown entries: installed models the user may create."""
        allowed = self.env["ir.model.access"]._get_allowed_models("create")
        menu = []
        for entry in CREATE_MENU:
            model = entry["model"]
            if model not in self.env or model not in allowed:
                continue
            menu.append({
                "key": entry["key"],
                "model": model,
                "label": str(entry["label"]),
                "icon": entry["icon"],
            })
        return menu

    @api.model
    def menu_entry(self, key):
        """Resolve a dropdown ``key`` to its raw CREATE_MENU entry —
        re-applying the installed + create-allowed filter so a crafted RPC
        with a valid key but missing rights is still rejected."""
        allowed = self.env["ir.model.access"]._get_allowed_models("create")
        for entry in CREATE_MENU:
            if (entry["key"] == key and entry["model"] in self.env
                    and entry["model"] in allowed):
                return entry
        return None

    @api.model
    def get_creatable_models(self, mode="create"):
        """Mail-thread models the user has *mode* access on.

        ``create`` feeds the "Other…" picker of the create flow; the attach
        wizard's Reference selection uses ``read`` (posting rights are
        enforced later by ``message_post``).
        """
        allowed = self.env["ir.model.access"]._get_allowed_models(mode)
        rows = self.env["ir.model"].sudo().search_read(
            [("is_mail_thread", "=", True), ("transient", "=", False)],
            ["model", "name"], order="name")
        result = []
        for row in rows:
            model = row["model"]
            if model not in self.env:
                continue
            if model not in allowed or BLOCKLIST_RE.match(model):
                continue
            Model = self.env[model]
            if Model._abstract or Model._transient:
                continue
            result.append({"model": model, "name": row["name"]})
        return result

    # ------------------------------------------------------------------
    # Prefill
    # ------------------------------------------------------------------

    @api.model
    def prefill_context(self, model_name, envelope, body_html,
                        extra_context=None):
        """Map an email envelope onto ``default_*`` context keys.

        Generic, duck-typed mapping — works for any mail-thread model
        without per-app dependencies. ``envelope`` is the dict shape of
        :func:`ow_mail_imap.parse_envelope`; ``body_html`` must already be
        sanitized by the caller.
        """
        Model = self.env[model_name]
        ctx = {}

        # 1. Record name <- subject. Candidates: _rec_name first, then a
        # plain "name" field (covers models whose _rec_name is a sequence,
        # like OCA helpdesk.ticket's readonly "number"). A candidate is
        # skipped when it is readonly or already has a default —
        # conservative: this also covers sequence-backed names like
        # sale.order's "New".
        rec_name = Model._rec_name or "name"
        for candidate in dict.fromkeys((rec_name, "name")):
            name_field = Model._fields.get(candidate)
            if (name_field is not None
                    and name_field.type in ("char", "text")
                    and not name_field.readonly
                    and not Model.default_get([candidate]).get(candidate)):
                ctx[f"default_{candidate}"] = envelope.get("subject") or ""
                break

        # 2. Primary email field <- sender address (crm.lead.email_from,
        # res.partner.email, ...). Models without a declared _primary_email
        # fall back to the explicit EMAIL_FIELDS allowlist.
        primary_email = Model._mail_get_primary_email_field() \
            if hasattr(Model, "_mail_get_primary_email_field") else None
        if not primary_email:
            primary_email = next(
                (f for f in EMAIL_FIELDS
                 if Model._fields.get(f) is not None
                 and Model._fields[f].type == "char"
                 and not Model._fields[f].readonly), None)
        if primary_email and envelope.get("from_email"):
            ctx[f"default_{primary_email}"] = envelope["from_email"]

        # 3. Customer link when the sender is a known partner.
        partner_field = Model._fields.get("partner_id")
        if (partner_field is not None
                and getattr(partner_field, "comodel_name", None) == "res.partner"):
            partner = self.env["res.partner"]._ow_find_by_email(
                envelope.get("from_email"))
            if partner:
                ctx["default_partner_id"] = partner.id

        # 4. Body -> first allowlisted html/text field.
        for field_name in BODY_FIELDS:
            body_field = Model._fields.get(field_name)
            if body_field is None or body_field.readonly:
                continue
            if body_field.type == "html":
                ctx[f"default_{field_name}"] = body_html or ""
                break
            if body_field.type == "text":
                ctx[f"default_{field_name}"] = html2plaintext(body_html or "")
                break

        # 5. Special cases on top of the generic pass.
        if model_name == "res.partner":
            # A contact is the *sender*, not the email: name from the
            # display name, and never the email body as internal notes.
            ctx["default_name"] = (envelope.get("from_name")
                                   or envelope.get("from_email") or "")
            ctx.pop("default_comment", None)
        elif model_name == "calendar.event":
            start, stop = self._default_event_window()
            ctx.setdefault("default_start", start)
            ctx.setdefault("default_stop", stop)

        ctx.update(extra_context or {})
        return ctx

    @api.model
    def _default_event_window(self):
        """Next full hour (user timezone), one hour long, as naive-UTC strings."""
        tz = pytz.timezone(self.env.user.tz or "UTC")
        now_local = fields.Datetime.now().replace(tzinfo=pytz.UTC).astimezone(tz)
        start_local = (now_local + timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0)
        start_utc = start_local.astimezone(pytz.UTC).replace(tzinfo=None)
        fmt = "%Y-%m-%d %H:%M:%S"
        return (start_utc.strftime(fmt),
                (start_utc + timedelta(hours=1)).strftime(fmt))

    # ------------------------------------------------------------------
    # Attach
    # ------------------------------------------------------------------

    @api.model
    def _fetch_raw(self, folder, uid):
        """Fetch the raw RFC 822 bytes over IMAP. Seam for unit tests."""
        with imap_utils.imap_session(folder.account_id, folder.full_path,
                                     readonly=True) as conn:
            _flags, raw = imap_utils.fetch_full(conn, int(uid))
        return raw

    @api.model
    def attach_email(self, folder, uid, model_name, res_id,
                     include_attachments=True):
        """Post the email (body + attachments) into a record's chatter.

        Fetches the message once, sanitizes server-side, guards against
        duplicates (Message-ID first, legacy uid/folder pair second), posts
        via ``message_post`` and stamps the source identifiers on the new
        ``mail.message``. Returns that message.
        """
        uid = int(uid)
        if model_name not in self.env:
            raise UserError(_("Unknown model: %s") % model_name)
        Model = self.env[model_name]
        if Model._abstract or Model._transient:
            raise UserError(_("Cannot attach email to %s.") % model_name)
        record = Model.browse(int(res_id)).exists()
        if not record or not record.has_access("read"):
            raise UserError(_("The selected record does not exist."))

        raw = self._fetch_raw(folder, uid)
        if not raw:
            raise UserError(_("Message not found on the mail server."))
        msg = email.message_from_bytes(raw)
        envelope = imap_utils.parse_envelope(msg)

        self._check_duplicate(folder, uid, model_name, record.id,
                              envelope.get("message_id"))

        _text, html, _inline, _atts = imap_utils.extract_parts(msg)
        sanitized, _has_remote = imap_utils.sanitize_and_detect(html or "")
        if not sanitized and _text:
            sanitized = "<pre>%s</pre>" % _text.replace("<", "&lt;")

        author = self.env["res.partner"]._ow_find_by_email(
            envelope.get("from_email"))
        email_from = (
            author.email_formatted
            if author and author.email_formatted
            else f"{envelope.get('from_name')} <{envelope.get('from_email')}>"
            if envelope.get("from_name") and envelope.get("from_email")
            else envelope.get("from_email") or ""
        )
        date_str = (envelope["date"].strftime("%Y-%m-%d %H:%M")
                    if envelope.get("date") else "")
        header_html = Markup(
            "<b>From:</b> %s &lt;%s&gt;<br/><b>Date:</b> %s<br/>"
            "<b>Subject:</b> %s<br/><br/>") % (
            envelope.get("from_name") or "", envelope.get("from_email") or "",
            date_str, envelope.get("subject") or "")
        body = header_html + Markup(sanitized or "")

        attachment_ids = []
        if include_attachments:
            attachment_ids = self._create_attachments(msg, model_name,
                                                      record.id)

        message = record.message_post(
            body=body,
            message_type="email",
            author_id=author.id if author else self.env.user.partner_id.id,
            email_from=email_from,
            attachment_ids=attachment_ids,
        )
        # sudo: mail.message rows aren't generally writable by regular
        # users; the stamps are internal bookkeeping, not user content.
        message.sudo().write({
            "ow_mail_uid": uid,
            "ow_mail_folder_id": folder.id,
            "ow_mail_message_id": envelope.get("message_id") or False,
        })
        if author:
            record.message_subscribe(partner_ids=author.ids)
        return message

    @api.model
    def _check_duplicate(self, folder, uid, model_name, res_id, message_id):
        Message = self.env["mail.message"].sudo()
        base = [("model", "=", model_name), ("res_id", "=", res_id)]
        duplicate = message_id and Message.search_count(
            base + [("ow_mail_message_id", "=", message_id)], limit=1)
        if not duplicate:
            # Legacy stamps (pre-Message-ID rows) only carry the uid/folder
            # pair — still a duplicate of the same source email.
            duplicate = Message.search_count(
                base + [("ow_mail_uid", "=", uid),
                        ("ow_mail_folder_id", "=", folder.id)], limit=1)
        if duplicate:
            raise UserError(
                _("This message has already been attached to this record."))

    @api.model
    def _create_attachments(self, msg, model_name, res_id):
        attachment_ids = []
        for part, _section, kind in imap_utils.walk_parts(msg):
            if kind != "attachment":
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            filename = imap_utils.decode_header_value(
                part.get_filename()) or "attachment"
            attachment = self.env["ir.attachment"].create({
                "name": filename,
                "datas": base64.b64encode(payload),
                "res_model": model_name,
                "res_id": res_id,
            })
            attachment_ids.append(attachment.id)
        return attachment_ids

    # ------------------------------------------------------------------
    # Linked-records lookup
    # ------------------------------------------------------------------

    @api.model
    def linked_records(self, folder, uid, message_id, limit=20):
        """Records this email was attached to, readable by the current user.

        Message-ID match first (survives IMAP moves), uid/folder pair as
        fallback for pre-upgrade stamps. ``mail.message`` is searched with
        sudo on purpose: its ``_search`` does expensive per-record access
        post-filtering, and we do our own document-level filtering below
        (model registry + ACL read + ir.rules via ``search``).
        """
        uid = int(uid)
        pair = [("ow_mail_uid", "=", uid), ("ow_mail_folder_id", "=", folder.id)]
        if message_id:
            domain = ["|", ("ow_mail_message_id", "=", message_id), "&"] + pair
        else:
            domain = pair
        rows = self.env["mail.message"].sudo().search_read(
            domain, ["model", "res_id"], limit=200)

        seen, grouped = set(), {}
        for row in rows:
            key = (row["model"], row["res_id"])
            if not row["model"] or not row["res_id"] or key in seen:
                continue
            seen.add(key)
            grouped.setdefault(row["model"], []).append(row["res_id"])

        allowed = self.env["ir.model.access"]._get_allowed_models("read")
        result = []
        for model_name, ids in grouped.items():
            if model_name not in self.env or model_name not in allowed:
                continue
            # search (not browse) so ir.rule record-level filtering applies
            for record in self.env[model_name].search(
                    [("id", "in", ids)], limit=limit):
                result.append({
                    "model": model_name,
                    "res_id": record.id,
                    "display_name": record.display_name,
                })
                if len(result) >= limit:
                    return result
        return result
