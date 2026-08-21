"""HTTP controllers — all data endpoints are live IMAP proxies.

Every route is a live IMAP round-trip; no message bodies, headers, or
attachments are persisted in the database. Message identity is the composite
``(folder_id, uid)`` — there is no numeric message ID.

Ownership is asserted in every route via ``_get_folder`` / ``_get_account``
independent of ``ir.rule``. This defense-in-depth ensures that even if an
``ir.rule`` is accidentally widened or removed, cross-user access is still
rejected at the controller layer.
"""
import email
import logging
import re
from urllib.parse import quote

from odoo import _, http
from odoo.exceptions import UserError
from odoo.http import Response, request

from ..models import ow_mail_imap as imap_utils
from ..models.ow_mail_search import parse_query, has_operators, _imap_quote

_logger = logging.getLogger(__name__)


def _imap_err(message):
    """Wrap an error message in the standard ``{"error": ...}`` dict.

    All JSON routes return this shape on failure so the frontend can
    detect errors with a single ``if (result.error)`` check.
    """
    return {"error": str(message)}


def _content_disposition(disp, filename):
    """Build a safe Content-Disposition header value.

    Strips CR/LF (which break HTTP header serialization after header-folding
    in MIME filenames) and adds an RFC 5987 `filename*=UTF-8''…` variant so
    non-ASCII filenames survive the round-trip.
    """
    safe = re.sub(r'[\r\n"]+', "", filename or "").strip() or "file"
    ascii_fallback = safe.encode("ascii", "replace").decode("ascii")
    return (f'{disp}; filename="{ascii_fallback}"; '
            f"filename*=UTF-8''{quote(safe, safe='')}")


# MIME types safe to render inline in the Odoo origin. Anything else must
# be served as "attachment" with nosniff so the browser won't execute it.
# Request caps — prevent a single call from exhausting IMAP/worker time
# on a folder with 100k+ UIDs. Chosen high enough for ordinary inboxes.
_MAX_LIMIT = 200            # page size upper bound (/ow_mail/messages)
_MAX_ACTION_UIDS = 1000     # uid set upper bound (/ow_mail/message/action)
_MAX_FALLBACK_UIDS = 2000   # envelope-fetch upper bound for client-side filter

_INLINE_SAFE_PREFIXES = ("image/", "audio/")
_INLINE_SAFE_EXACT = {"application/pdf"}
# Always force attachment regardless of hint — these can XSS the Odoo origin.
_ATTACH_ONLY_EXACT = {
    "text/html", "application/xhtml+xml", "image/svg+xml",
    "application/javascript", "application/ecmascript",
    "text/javascript", "text/ecmascript",
    "application/xml", "text/xml",
}


def _safe_inline_ctype(ctype):
    """Return True iff *ctype* is safe to render inline in the Odoo origin.

    HTML, SVG, XML, and JavaScript types are blocked regardless of the
    ``Content-Disposition`` hint from the sender — they can execute scripts
    or load remote resources in the Odoo origin even inside an iframe. Images
    and audio are safe because browsers treat them as opaque binary data.
    PDFs are allowlisted because Odoo ships a sandboxed PDF viewer; all other
    application/* types default to attachment.
    """
    c = (ctype or "").split(";", 1)[0].strip().lower()
    if c in _ATTACH_ONLY_EXACT:
        return False
    if c in _INLINE_SAFE_EXACT:
        return True
    return any(c.startswith(p) for p in _INLINE_SAFE_PREFIXES)


def _security_headers(extra=None):
    """Defense-in-depth headers for anything served from /ow_mail/*.

    ``nosniff`` stops the browser from re-sniffing ``application/octet-stream``
    as HTML; CSP ``default-src 'none'; sandbox`` ensures that even if a file
    is (mis-)rendered as HTML it can't reach back into the Odoo origin or
    execute scripts.
    """
    headers = [
        ("X-Content-Type-Options", "nosniff"),
        ("Content-Security-Policy", "default-src 'none'; sandbox; base-uri 'none'"),
        ("Referrer-Policy", "no-referrer"),
    ]
    if extra:
        headers.extend(extra)
    return headers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_folder(folder_id):
    """Resolve *folder_id* and assert ownership.

    Project convention: controllers must not rely on ``ir.rule`` alone.
    This helper is defense-in-depth — if a rule is ever dropped or widened
    by mistake, every controller route still rejects cross-user access.
    """
    try:
        folder = request.env["ow.mail.folder"].browse(int(folder_id)).exists()
    except (TypeError, ValueError):
        return None
    if not folder:
        return None
    if folder.account_id.user_id.id != request.env.user.id:
        _logger.warning(
            "ow_mail: folder %s belongs to user %s but requested by %s",
            folder.id, folder.account_id.user_id.id, request.env.user.id,
        )
        return None
    return folder


def _get_account(account_id):
    """Resolve *account_id* and assert ownership — same contract as _get_folder."""
    try:
        acc = request.env["ow.mail.account"].browse(int(account_id)).exists()
    except (TypeError, ValueError):
        return None
    if not acc:
        return None
    if acc.user_id.id != request.env.user.id:
        _logger.warning(
            "ow_mail: account %s belongs to user %s but requested by %s",
            acc.id, acc.user_id.id, request.env.user.id,
        )
        return None
    return acc


def _envelope_to_dict(e, folder):
    """Convert a ``fetch_envelopes`` row into the wire dict sent to the frontend.

    ``from_name`` / ``from_email`` are split by ``imap_utils.parse_envelope``
    rather than returned as a combined RFC 5322 address because the list view
    renders them separately (avatar initial, tooltip). ``tag_ids`` are resolved
    from IMAP custom keywords so the frontend never needs to know keyword names.
    """
    return {
        "folder_id": folder.id,
        "account_id": folder.account_id.id,
        "uid": e["uid"],
        "subject": e["subject"],
        "from_name": e["from_name"],
        "from_email": e["from_email"],
        "preview": e.get("preview", ""),
        "date": e["date"],
        "message_id": e.get("message_id", ""),
        "in_reply_to": e.get("in_reply_to", ""),
        "references": e.get("references", ""),
        "flags_seen": e["flags_seen"],
        "flags_flagged": e["flags_flagged"],
        "flags_answered": e["flags_answered"],
        "has_attachments": e.get("has_attachments", False),
        "tag_ids": _tag_ids_from_keywords(e["keywords"]),
        "size": e["size"],
    }


def _annotate_known_partners(msgs):
    """Stamp each envelope wire dict with ``partner_id`` when the sender
    address matches a ``res.partner`` (one batched query per page, matching
    on ``email_normalized`` like the single-message endpoint). The frontend
    renders a "Customer" badge for messages whose sender is a known contact;
    no match → ``partner_id`` is False.
    """
    emails = {(m.get("from_email") or "").strip().lower() for m in msgs}
    emails.discard("")
    if not emails:
        return msgs
    partners = request.env["res.partner"].search_read(
        [("email_normalized", "in", list(emails))],
        ["id", "email_normalized"],
    )
    by_email = {p["email_normalized"]: p["id"] for p in partners}
    for m in msgs:
        m["partner_id"] = by_email.get(
            (m.get("from_email") or "").strip().lower(), False)
    return msgs


def _tag_keyword_lookup(name):
    """Resolve a label name to its IMAP keyword string, or None.

    Tries a case-insensitive name match first, then falls back to a direct
    ``imap_keyword`` match. Scoped to the current user via ``ir.rule``, so
    this can safely be passed as ``tag_lookup`` to ``parse_query``.
    """
    if not name:
        return None
    Tag = request.env["ow.mail.tag"]
    t = Tag.search([("name", "=ilike", name)], limit=1)
    if not t:
        t = Tag.search([("imap_keyword", "=", name)], limit=1)
    return t.imap_keyword or None if t else None


def _search_criteria(filter, search=None, tag_keyword=None):
    """Build an IMAP SEARCH criteria string + any envelope-level post-filters.

    Returns ``(criteria_str, post_filters, dsl_active)``:
    * ``criteria_str`` — IMAP SEARCH criteria; ``"ALL"`` if nothing applied.
    * ``post_filters`` — dict of envelope-level filters (e.g.
      ``{"has_attachment": True}``) that IMAP can't express.
    * ``dsl_active`` — True when the search string contained at least
      one DSL operator; the caller uses this to skip the legacy
      substring fallback so strict operators like ``from:`` stay strict.
    """
    parts = []
    post_filters = {}
    dsl_active = False
    if filter == "unread":
        parts.append("UNSEEN")
    elif filter == "starred":
        parts.append("FLAGGED")
    # incoming/outgoing handled at folder-kind level client-side
    if tag_keyword:
        parts.append(f"KEYWORD {tag_keyword}")
    if search:
        dsl_active = has_operators(search)
        if dsl_active:
            dsl_criteria, post_filters = parse_query(
                search, tag_lookup=_tag_keyword_lookup)
            if dsl_criteria:
                parts.append(dsl_criteria)
        else:
            # Legacy path: broad OR across SUBJECT/FROM/TO/TEXT. Value goes
            # through _imap_quote (same as the DSL) so embedded quotes and
            # backslashes are IMAP-quoted-string-escaped rather than stripped.
            # This closes the SEARCH-criteria injection vector where the old
            # strip-only filter let keywords/parentheses through into the
            # command (e.g. `foo" OR ALL (`).
            stripped = search.strip()
            if stripped and not any(ch in stripped for ch in ("\r", "\n", "\x00")):
                q = _imap_quote(stripped)
                parts.append(
                    f"OR OR OR SUBJECT {q} FROM {q} TO {q} TEXT {q}")
    return " ".join(parts) if parts else "ALL", post_filters, dsl_active


def _apply_post_filters(envelopes, post_filters):
    """Apply envelope-level filters that IMAP SEARCH cannot express.

    Some DSL predicates (e.g. ``has:attachment``) have no direct IMAP
    counterpart and are therefore deferred to a Python pass over the already-
    fetched envelopes. Only called when ``_search_criteria`` returned a
    non-empty ``post_filters`` dict.
    """
    if not post_filters:
        return envelopes
    out = envelopes
    if post_filters.get("has_attachment"):
        out = [e for e in out if e.get("has_attachments")]
    return out


def _client_filter(envelopes, search):
    """Post-filter envelopes client-side for substring matching.

    IMAP SEARCH does tokenized matching on many servers (Dovecot in
    particular), so searching for "overleg" won't find "werkoverleg".
    This applies a Python substring check on subject/from/to as fallback.
    Only used when the query has no DSL operators — otherwise callers
    want strict behaviour.
    """
    if not search:
        return envelopes
    q = search.lower()
    return [e for e in envelopes if
            q in (e.get("subject") or "").lower() or
            q in (e.get("from_name") or "").lower() or
            q in (e.get("from_email") or "").lower()]


def _rewrite_inline(html, folder_id, uid, inline_parts):
    """Rewrite ``cid:`` references in sanitized HTML to ``/ow_mail/inline/…`` URLs.

    The ``<iframe srcdoc>`` used by MessageViewer cannot resolve ``cid:`` URIs
    — they are only meaningful inside a MIME multipart structure. Each inline
    part's Content-ID is replaced with an absolute Odoo URL so the browser
    fetches it through the ``/ow_mail/inline`` route, which re-authenticates
    and streams the part bytes safely.
    """
    for inline in inline_parts:
        cid = inline.get("cid")
        if not cid:
            continue
        url = f"/ow_mail/inline/{folder_id}/{uid}/{cid}"
        html = re.sub(rf"cid:{re.escape(cid)}", url, html, flags=re.I)
    return html


def _trusted_sender_scope(user_id):
    """Return `ow.mail.trusted.sender` sudo()-scoped to user_id.

    Always apply this helper when bypassing the ir.rule via sudo(); the
    explicit user_id filter must never be skipped.
    """
    return request.env["ow.mail.trusted.sender"].sudo().with_context(
        _ow_trusted_scope_user=user_id)


def _is_sender_trusted(user_id, from_email):
    """Return True if the sender is on the user's safe-mode allow-list.

    Matches either the full address or the domain (e.g. trusting
    ``example.com`` covers all mail from that domain). When trusted, the
    ``MessageViewer`` shows remote images without the ``SafeModeBanner``
    prompt. Uses ``_trusted_sender_scope`` so the sudo() bypass is always
    accompanied by an explicit ``user_id`` filter.
    """
    if not from_email:
        return False
    TS = _trusted_sender_scope(user_id)
    domain = from_email.split("@")[-1].lower() if "@" in from_email else ""
    return bool(TS.search_count([
        ("user_id", "=", user_id),
        "|", ("email", "=", from_email.lower()), ("email", "=", domain),
    ]))


def _stamp_previews(conn, envs):
    """Best-effort: attach a ``preview`` snippet to each envelope dict.

    Must be called inside the open IMAP session of the folder the envelopes
    came from. Only ever runs on a single page (≤ ``_MAX_LIMIT`` messages),
    never on the full fallback set. Missing snippets stay ``""``.
    """
    if not envs:
        return envs
    previews = imap_utils.fetch_previews(conn, [e["uid"] for e in envs])
    for e in envs:
        e["preview"] = previews.get(e["uid"], "")
    return envs


def _tag_ids_from_keywords(keywords):
    """Map IMAP custom keywords back to local ``ow.mail.tag`` IDs.

    Tags are stored as IMAP keywords (``OwTag_<slug>``) on the mail server,
    never in the DB per-message. This converts the keyword list from a FLAGS
    response into the ``tag_ids`` list that the frontend expects, scoped to
    the current user via ``ir.rule``.
    """
    if not keywords:
        return []
    Tag = request.env["ow.mail.tag"]
    tags = Tag.search([("imap_keyword", "in", keywords)])
    return tags.ids


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

class OwMailController(http.Controller):

    @http.route("/ow_mail/bootstrap", type="json", auth="user")
    def bootstrap(self):
        """Return all data needed to mount the mail client in one call.

        Loads accounts + their folder trees + all tags. Performing a single
        bootstrap request avoids three serial round-trips on initial page
        load. If an account exists but has no folders yet (e.g. just
        connected), a live ``_refresh_folders`` is triggered inline so the
        first open always shows the folder tree.
        """
        Account = request.env["ow.mail.account"]
        # Defense-in-depth: explicit user_id filter on top of ir.rule, mirroring
        # _get_account / _get_folder. Keeps cross-user leakage impossible even
        # if the rule is ever widened or removed by mistake.
        accounts = Account.search([("user_id", "=", request.env.user.id)])
        # Refresh folder tree for accounts that have none yet
        for acc in accounts:
            if acc.state == "confirmed" and not acc.folder_ids:
                try:
                    acc._refresh_folders()
                except Exception as e:
                    _logger.warning("bootstrap folder refresh failed: %s", e)
        tags = request.env["ow.mail.tag"].search([])
        return {
            "accounts": [{
                "id": a.id,
                "name": a.name,
                "email": a.email,
                "color": a.color,
                "state": a.state,
                "error_message": a.error_message or "",
                "signature_html": a.signature_html or "",
                "signature_enabled": a.signature_enabled,
                "signature_placement": a.signature_placement or "below",
                "special": {
                    "inbox": a.inbox_folder_id.id or False,
                    "sent": a.sent_folder_id.id or False,
                    "drafts": a.drafts_folder_id.id or False,
                    "archive": a.archive_folder_id.id or False,
                    "spam": a.spam_folder_id.id or False,
                    "trash": a.trash_folder_id.id or False,
                },
                "unread_count": sum(a.folder_ids.filtered(
                    lambda f: f.kind not in ("trash", "spam")).mapped("unread_count")),
                "folders": [{
                    "id": f.id, "name": f.name, "full_path": f.full_path,
                    "kind": f.kind, "unread_count": f.unread_count, "total_count": f.total_count,
                    "subscribed": f.subscribed,
                    "parent_id": f.parent_id.id or False,
                } for f in a.folder_ids],
            } for a in accounts],
            "tags": [{"id": t.id, "name": t.name, "color": t.color,
                      "keyword": t.imap_keyword} for t in tags],
            "prefs": request.env["ow.mail.preferences"]._get_for_user()._to_wire(),
            "create_menu": request.env["ow.mail.record.link"].get_create_menu(),
        }

    @http.route("/ow_mail/prefs/save", type="json", auth="user")
    def prefs_save(self, vals):
        """Persist client preferences for the current user.

        Only whitelisted keys are accepted (mass-assignment guard, mirroring
        ``contacts_update``); values are type-coerced before the write.
        """
        def _coerce_theme(value):
            value = str(value)
            if value not in ("system", "light", "dark"):
                raise ValueError(value)
            return value

        allowed = {"mark_read_delay": int, "thread_view_default": bool,
                   "stacked_threads": bool, "theme": _coerce_theme}
        clean = {}
        for key, coerce in allowed.items():
            if key in (vals or {}):
                try:
                    clean[key] = coerce(vals[key])
                except (TypeError, ValueError):
                    return _imap_err(f"Invalid value for {key}")
        prefs = request.env["ow.mail.preferences"]._get_for_user()
        if clean:
            prefs.write(clean)
        return {"ok": True, "prefs": prefs._to_wire()}

    @http.route("/ow_mail/sync", type="json", auth="user")
    def sync(self, account_id=None):
        """Trigger a live IMAP LIST + STATUS refresh for one or all accounts.

        Called by the frontend polling loop and after actions that change
        folder counts (send, move, delete). Passing ``account_id`` scopes
        the refresh to a single account; omitting it refreshes everything.
        """
        # Explicit user_id filter — defense-in-depth; ir.rule also applies.
        domain = [("state", "!=", "draft"), ("user_id", "=", request.env.user.id)]
        if account_id:
            domain.append(("id", "=", account_id))
        accounts = request.env["ow.mail.account"].search(domain)
        accounts.action_sync()
        return {"ok": True}

    @http.route("/ow_mail/messages", type="json", auth="user")
    def messages(self, folder_id=None, filter="all", search=None, tag_id=None,
                 offset=0, limit=60, account_id=None,
                 sort_by="date", sort_order="desc"):
        """Return a paged list of message envelopes from a folder.

        Translates UI params into an IMAP SEARCH criteria, sorts via IMAP SORT
        (falling back to client-side when the server lacks the SORT extension),
        then fetches only the envelope headers for the requested page.

        ``filter`` accepts ``"all"``, ``"unread"``, or ``"starred"``.
        ``search`` goes through the DSL parser (``has_operators``) first; plain
        strings fall back to a broad IMAP OR across Subject/From/To/Body, with
        a Python substring pass to compensate for Dovecot tokenization gaps.
        Omitting ``folder_id`` triggers the All Mailboxes view via
        ``_messages_all_folders``.

        Returns ``{"total": int, "messages": [envelope_dict, ...]}``.
        """
        # Coerce and cap — callers can send junk or exhaust the server.
        try:
            limit = max(1, min(_MAX_LIMIT, int(limit)))
            offset = max(0, int(offset))
        except (TypeError, ValueError):
            limit, offset = 60, 0
        tag_keyword = None
        if tag_id:
            tag = request.env["ow.mail.tag"].browse(int(tag_id)).exists()
            if tag:
                tag_keyword = tag.imap_keyword
        criteria, post_filters, dsl_active = _search_criteria(
            filter, search, tag_keyword)

        # All Mailboxes: query across all subscribed inbox folders
        if not folder_id:
            return self._messages_all_folders(
                criteria, filter, search, tag_keyword,
                post_filters, dsl_active,
                sort_by, sort_order, int(offset), int(limit))

        folder = _get_folder(folder_id)
        if not folder:
            return _imap_err("Folder not found")
        try:
            with imap_utils.imap_session(folder.account_id, folder.full_path, readonly=True) as conn:
                uids = imap_utils.sort_uids(conn, criteria, sort_by, sort_order)
                # If IMAP SEARCH returned nothing, the legacy free-text
                # path re-fetches all UIDs and applies a substring match
                # (Dovecot tokenizes so "overleg" misses "werkoverleg").
                # DSL queries are strict — skip the fallback for them.
                if not uids and search and not dsl_active:
                    base_criteria, _pf, _ = _search_criteria(
                        filter, None, tag_keyword)
                    all_uids = imap_utils.sort_uids(conn, base_criteria, sort_by, sort_order)
                    if all_uids:
                        # Cap to keep worker/IMAP time bounded; sort order is
                        # desc-by-date so the newest _MAX_FALLBACK_UIDS win.
                        all_uids = all_uids[:_MAX_FALLBACK_UIDS]
                        all_envs = imap_utils.fetch_envelopes(conn, all_uids)
                        filtered = _client_filter(all_envs, search)
                        # Preserve sort order from all_uids
                        uid_order = {u: i for i, u in enumerate(all_uids)}
                        filtered.sort(key=lambda e: uid_order.get(e["uid"], 0))
                        total = len(filtered)
                        page_envs = filtered[int(offset): int(offset) + int(limit)]
                        _stamp_previews(conn, page_envs)
                        return {"total": total, "messages": _annotate_known_partners([
                            _envelope_to_dict(e, folder) for e in page_envs])}
                if post_filters:
                    # Post-filters need every envelope to decide; fetch
                    # all UIDs returned by IMAP first, then page. Cap so
                    # a 100k-folder doesn't block the worker.
                    capped = uids[:_MAX_FALLBACK_UIDS]
                    envs_all = imap_utils.fetch_envelopes(conn, capped)
                    filtered = _apply_post_filters(envs_all, post_filters)
                    uid_order = {u: i for i, u in enumerate(capped)}
                    filtered.sort(key=lambda e: uid_order.get(e["uid"], 0))
                    total = len(filtered)
                    page_envs = filtered[int(offset): int(offset) + int(limit)]
                    _stamp_previews(conn, page_envs)
                    return {"total": total, "messages": _annotate_known_partners([
                        _envelope_to_dict(e, folder) for e in page_envs])}
                total = len(uids)
                page = uids[int(offset): int(offset) + int(limit)]
                envs = imap_utils.fetch_envelopes(conn, page)
                _stamp_previews(conn, envs)
        except Exception as e:
            _logger.warning("messages fetch failed: %s", e)
            return _imap_err(e)
        # Order fetch_envelopes in same order as requested
        by_uid = {e["uid"]: e for e in envs}
        out = []
        for uid in page:
            e = by_uid.get(uid)
            if not e:
                continue
            out.append(_envelope_to_dict(e, folder))
        return {"total": total, "messages": _annotate_known_partners(out)}

    def _messages_all_folders(self, criteria, filter, search, tag_keyword,
                              post_filters, dsl_active,
                              sort_by, sort_order, offset, limit):
        """Unified search across Inbox + Sent folders of every confirmed account.

        Each folder is queried independently (separate IMAP sessions), results
        are merged in Python and then re-sorted so the combined list respects
        the requested sort order. Called when ``messages()`` receives no
        ``folder_id`` (the All Mailboxes virtual view).
        """
        accounts = request.env["ow.mail.account"].search(
            [("state", "=", "confirmed"),
             ("user_id", "=", request.env.user.id)])
        all_msgs = []
        for acc in accounts:
            folders = []
            if acc.inbox_folder_id:
                folders.append(acc.inbox_folder_id)
            if acc.sent_folder_id:
                folders.append(acc.sent_folder_id)
            for folder in folders:
                try:
                    with imap_utils.imap_session(acc, folder.full_path, readonly=True) as conn:
                        uids = imap_utils.sort_uids(conn, criteria, sort_by, sort_order)
                        # Legacy free-text fallback (see messages()).
                        if not uids and search and not dsl_active:
                            base_criteria, _pf, _ = _search_criteria(
                                filter, None, tag_keyword)
                            uids = imap_utils.sort_uids(conn, base_criteria, sort_by, sort_order)
                        # Cap per folder — _MAX_FALLBACK_UIDS is the upper
                        # bound across the two folders of one account.
                        uids = uids[:_MAX_FALLBACK_UIDS]
                        envs = imap_utils.fetch_envelopes(conn, uids)
                        if search and not dsl_active:
                            envs = _client_filter(envs, search)
                        envs = _apply_post_filters(envs, post_filters)
                        for e in envs:
                            all_msgs.append(_envelope_to_dict(e, folder))
                except Exception as e:
                    _logger.warning("all-mailboxes fetch failed for %s/%s: %s",
                                    acc.email, folder.full_path, e)
        # Sort merged results
        reverse = sort_order == "desc"
        sort_key = {"date": "date", "from": "from_name", "subject": "subject",
                    "size": "size"}.get(sort_by, "date")
        all_msgs.sort(key=lambda m: m.get(sort_key) or "", reverse=reverse)
        total = len(all_msgs)
        page = _annotate_known_partners(all_msgs[offset: offset + limit])
        self._stamp_previews_multi_folder(page)
        return {"total": total, "messages": page}

    def _stamp_previews_multi_folder(self, page_msgs):
        """Fetch preview snippets for one already-paged multi-folder message list.

        Previews are fetched *after* paging so the cost stays bounded at one
        short read-only session per distinct folder on the page (≤ 2 folders
        per account), each issuing 1–2 batched partial fetches. Any session
        failure leaves those previews empty.
        """
        by_folder = {}
        for m in page_msgs:
            by_folder.setdefault(m["folder_id"], []).append(m)
        for folder_id, msgs in by_folder.items():
            folder = _get_folder(folder_id)
            if not folder:
                continue
            try:
                with imap_utils.imap_session(
                        folder.account_id, folder.full_path, readonly=True) as conn:
                    previews = imap_utils.fetch_previews(
                        conn, [m["uid"] for m in msgs])
                for m in msgs:
                    m["preview"] = previews.get(m["uid"], "")
            except Exception as e:
                _logger.debug("all-mailboxes preview fetch failed for %s: %s",
                              folder.full_path, e)

    @http.route("/ow_mail/message/source/<int:folder_id>/<int:uid>",
                type="http", auth="user")
    def message_source(self, folder_id, uid):
        """Stream the raw RFC 822 bytes of a message as ``text/plain``.

        Used by the "View source" developer action. Served as plain text
        (not ``message/rfc822``) so the browser displays it inline rather
        than triggering a download or a native mail client open.
        """
        folder = _get_folder(folder_id)
        if not folder:
            return Response("Folder not found", status=404)
        try:
            with imap_utils.imap_session(folder.account_id, folder.full_path) as conn:
                _flags, raw = imap_utils.fetch_full(conn, uid)
        except Exception as e:
            _logger.warning("message source fetch failed: %s", e)
            return Response("IMAP error", status=502)
        if not raw:
            return Response("Message not found", status=404)
        return Response(
            raw,
            headers=[("Content-Type", "text/plain; charset=utf-8"),
                     ("Content-Disposition", "inline")],
        )

    @http.route("/ow_mail/calendar/from_invite", type="json", auth="user")
    def calendar_from_invite(self, folder_id, uid):
        """Parse an iCalendar invite from an email and return an ``ir.actions.act_window``.

        Fetches the full message, extracts the first ``text/calendar`` MIME part via
        ``imap_utils.extract_invite``, resolves attendee emails to ``res.partner``
        records, and returns a new-form action pre-filled with the event details.
        Returns an IMAP error dict if no calendar part is found.
        """
        folder = _get_folder(folder_id)
        if not folder:
            return _imap_err("Folder not found")
        try:
            with imap_utils.imap_session(folder.account_id, folder.full_path,
                                         readonly=True) as conn:
                _flags, raw = imap_utils.fetch_full(conn, uid)
        except Exception as e:
            _logger.warning("calendar from_invite fetch failed: %s", e)
            return _imap_err(e)
        if not raw:
            return _imap_err("Message not found")
        msg = email.message_from_bytes(raw)
        invite = imap_utils.extract_invite(msg, default_tz=request.env.user.tz)
        if not invite:
            return _imap_err("No calendar invite found")

        partner_ids = []
        for att in invite.get("attendees") or []:
            p = request.env["res.partner"]._ow_find_by_email(att.get("email"))
            if p:
                partner_ids.append(p.id)

        context = {
            "default_name": invite.get("summary") or "Invitation",
            "default_location": invite.get("location") or "",
            "default_description": invite.get("description") or "",
        }
        if invite.get("dtstart_iso"):
            context["default_start"] = invite["dtstart_iso"].replace("T", " ")[:19]
        if invite.get("dtend_iso"):
            context["default_stop"] = invite["dtend_iso"].replace("T", " ")[:19]
        if partner_ids:
            context["default_partner_ids"] = [(6, 0, partner_ids)]
        return {
            "type": "ir.actions.act_window",
            "res_model": "calendar.event",
            "views": [[False, "form"]],
            "target": "new",
            "context": context,
        }

    @http.route("/ow_mail/record/prefill", type="json", auth="user")
    def record_prefill(self, folder_id, uid, key=None, model=None):
        """Server-computed ``default_*`` context for the create-record dialog.

        Exactly one of ``key`` (curated dropdown entry) or ``model`` (from
        the "Other…" picker) must be given; both are re-validated against
        the installed + create-allowed filters, so the client can never
        smuggle in an arbitrary model or context. Mirrors the
        ``calendar_from_invite`` pattern: fetch once, parse + sanitize
        server-side, return prefill data for the FormViewDialog.
        """
        folder = _get_folder(folder_id)
        if not folder:
            return _imap_err("Folder not found")
        Link = request.env["ow.mail.record.link"]
        extra_context = None
        if key:
            entry = Link.menu_entry(key)
            if not entry:
                return _imap_err("Unknown create action")
            model_name = entry["model"]
            title = str(entry["label"])
            extra_context = entry.get("extra_context")
        elif model:
            row = next((r for r in Link.get_creatable_models()
                        if r["model"] == model), None)
            if not row:
                return _imap_err("Model not available")
            model_name, title = model, row["name"]
        else:
            return _imap_err("Missing key or model")

        try:
            with imap_utils.imap_session(folder.account_id, folder.full_path,
                                         readonly=True) as conn:
                _flags, raw = imap_utils.fetch_full(conn, int(uid))
        except Exception as e:
            _logger.warning("record prefill fetch failed: %s", e)
            return _imap_err(e)
        if not raw:
            return _imap_err("Message not found")
        msg = email.message_from_bytes(raw)
        envelope = imap_utils.parse_envelope(msg)
        text, html, _inline, _atts = imap_utils.extract_parts(msg)
        # No _rewrite_inline here: /ow_mail/inline URLs embedded in a record
        # description would 404 for every other user of that record.
        sanitized, _has_remote = imap_utils.sanitize_and_detect(html or "")
        if not sanitized and text:
            sanitized = "<pre>%s</pre>" % text.replace("<", "&lt;")
        context = Link.prefill_context(model_name, envelope, sanitized,
                                       extra_context=extra_context)
        return {"model": model_name, "context": context, "title": title}

    @http.route("/ow_mail/record/attach", type="json", auth="user")
    def record_attach(self, folder_id, uid, model, res_id,
                      include_attachments=True):
        """Post the email into the chatter of *model*/*res_id*.

        Called by the FormViewDialog's ``onRecordSaved`` callback right
        after the record is created, and reusable for any explicit
        attach. All heavy lifting (fetch, sanitize, duplicate guard,
        Message-ID stamping) lives in ``ow.mail.record.link``.
        """
        folder = _get_folder(folder_id)
        if not folder:
            return _imap_err("Folder not found")
        try:
            message = request.env["ow.mail.record.link"].attach_email(
                folder, int(uid), model, int(res_id),
                include_attachments=bool(include_attachments))
        except UserError as e:
            return _imap_err(e)
        except Exception as e:
            _logger.warning("record attach failed: %s", e)
            return _imap_err(e)
        record = request.env[model].browse(int(res_id))
        return {
            "ok": True,
            "mail_message_id": message.id,
            "model": model,
            "res_id": record.id,
            "display_name": record.display_name,
        }

    @http.route("/ow_mail/record/models", type="json", auth="user")
    def record_models(self):
        """Models for the "Other…" picker — fetched lazily when it opens
        (the ir.model scan is the expensive part, so it stays out of
        bootstrap)."""
        return {"models": request.env["ow.mail.record.link"]
                .get_creatable_models()}

    @http.route("/ow_mail/message", type="json", auth="user")
    def message(self, folder_id, uid, peek=False):
        """Fetch and render a full message for the MessageViewer.

        Performs a single ``UID FETCH RFC822``, then: parses headers + MIME
        tree, sanitizes the HTML body (``sanitize_and_detect``), rewrites
        ``cid:`` references to ``/ow_mail/inline`` URLs, and checks the sender
        against the trust list. Sets ``\\Seen`` as a side-effect when the
        message was previously unseen — unless ``peek`` is truthy, in which
        case the session is read-only and the read-state is left untouched
        (used by the "mark read after N seconds" preference; the client marks
        the message read later via ``message_action``). The returned
        ``flags.seen`` always reflects the actual server state so the client
        knows whether a delayed mark-read is needed.

        ``has_remote_content`` is True only when remote URLs were detected
        *and* the sender is not trusted, so trusted senders always load images
        without the SafeModeBanner prompt.
        """
        folder = _get_folder(folder_id)
        if not folder:
            return _imap_err("Folder not found")
        peek = bool(peek)
        was_seen = True
        try:
            with imap_utils.imap_session(folder.account_id, folder.full_path,
                                         readonly=peek) as conn:
                flags_str, raw = imap_utils.fetch_full(conn, uid)
                if raw is None:
                    return _imap_err("Message not found")
                was_seen = bool(flags_str and "\\Seen" in flags_str)
                # Mark seen (historic default behaviour, skipped when peeking)
                if not peek and not was_seen and flags_str:
                    try:
                        conn.uid("STORE", str(uid), "+FLAGS", "(\\Seen)")
                    except Exception:
                        pass
        except Exception as e:
            _logger.warning("message fetch failed: %s", e)
            return _imap_err(e)

        msg = email.message_from_bytes(raw)
        env = imap_utils.parse_envelope(msg)
        text, html, inline, atts = imap_utils.extract_parts(msg)
        sanitized, has_remote = imap_utils.sanitize_and_detect(html)
        sanitized = _rewrite_inline(sanitized, folder.id, uid, inline)
        trusted = _is_sender_trusted(folder.account_id.user_id.id, env["from_email"])
        invite = imap_utils.extract_invite(msg, default_tz=request.env.user.tz)

        partner = request.env["res.partner"]._ow_find_by_email(env["from_email"])

        # flags_str is the raw FETCH response line (e.g. ``1 (UID 1 FLAGS
        # (\Seen OwTag_X) BODY[] {n}``) — extract the FLAGS group before
        # splitting, otherwise surrounding tokens pollute the keyword list
        # and custom tags never resolve.
        flags_m = re.search(r"FLAGS \(([^)]*)\)", flags_str or "")
        keywords = [f for f in (flags_m.group(1).split() if flags_m else [])
                    if f and not f.startswith("\\")]
        return {
            "folder_id": folder.id,
            "uid": int(uid),
            "subject": env["subject"],
            "from_name": env["from_name"],
            "from_email": env["from_email"],
            "to": env["to"],
            "cc": env["cc"],
            "date": env["date"].isoformat() if env["date"] else None,
            "message_id": env["message_id"],
            "in_reply_to": env["in_reply_to"],
            "references": env["references"],
            "html": sanitized or f"<pre>{(text or '').replace('<', '&lt;')}</pre>",
            "text": text or "",
            "has_remote_content": has_remote and not trusted,
            "trusted": trusted,
            "attachments": [{
                "section": a["section"],
                "name": a["filename"],
                "mimetype": a["mimetype"],
                "size": a["size"],
                "url": f"/ow_mail/attachment/{folder.id}/{uid}/{a['section']}",
                "download_url": f"/ow_mail/attachment/{folder.id}/{uid}/{a['section']}?download=1",
                "is_inline": False,
            } for a in atts],
            "partner_id": partner.id or False,
            "linked_records": request.env["ow.mail.record.link"]
                .linked_records(folder, int(uid), env["message_id"]),
            "folder_kind": folder.kind,
            "invite": invite,
            "flags": {
                "seen": True if not peek else was_seen,
                "flagged": "\\Flagged" in (flags_str or ""),
                "answered": "\\Answered" in (flags_str or ""),
            },
            "tag_ids": _tag_ids_from_keywords(keywords),
        }

    @http.route("/ow_mail/thread", type="json", auth="user")
    def thread(self, account_id, message_ids):
        """Fetch all messages in a conversation thread for a single account.

        ``search_thread_uids`` matches in both directions: the given ids
        themselves (ancestors) and messages whose References header carries
        one of them (descendants) — so the stacked view is complete from any
        member of the thread, root included.

        Only Inbox and Sent are searched because a thread is composed of
        received messages (Inbox) and authored replies (Sent); searching every
        folder would be redundant and expensive. ``seen_keys`` deduplicates
        results because some servers place a self-reply in both folders.
        Results are sorted chronologically (oldest-first) regardless of the
        caller's sort preference, since thread view always reads top-to-bottom.
        """
        acc = _get_account(account_id)
        if not acc:
            return _imap_err("Account not found")
        # Search Inbox + Sent for messages matching any of the Message-IDs
        folder_ids = [
            f_id for f_id in [acc.inbox_folder_id.id, acc.sent_folder_id.id] if f_id
        ]
        results = []
        seen_keys = set()
        for fid in folder_ids:
            folder = _get_folder(fid)
            if not folder:
                continue
            try:
                with imap_utils.imap_session(acc, folder.full_path, readonly=True) as conn:
                    uids = imap_utils.search_thread_uids(conn, message_ids)
                    if not uids:
                        continue
                    for uid in uids:
                        key = f"{fid}:{uid}"
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        flags_str, raw = imap_utils.fetch_full(conn, uid)
                        if raw is None:
                            continue
                        msg = email.message_from_bytes(raw)
                        env = imap_utils.parse_envelope(msg)
                        text, html, inline, atts = imap_utils.extract_parts(msg)
                        sanitized, has_remote = imap_utils.sanitize_and_detect(html)
                        sanitized = _rewrite_inline(sanitized, fid, uid, inline)
                        trusted = _is_sender_trusted(acc.user_id.id, env["from_email"])
                        results.append({
                            "folder_id": fid,
                            "uid": int(uid),
                            "subject": env["subject"],
                            "from_name": env["from_name"],
                            "from_email": env["from_email"],
                            "to": env["to"],
                            "cc": env["cc"],
                            "date": env["date"].isoformat() if env["date"] else None,
                            "message_id": env["message_id"],
                            "in_reply_to": env["in_reply_to"],
                            "references": env["references"],
                            "html": sanitized or f"<pre>{(text or '').replace('<', '&lt;')}</pre>",
                            "text": text or "",
                            "has_remote_content": has_remote and not trusted,
                            "trusted": trusted,
                            "attachments": [{
                                "section": a["section"],
                                "name": a["filename"],
                                "mimetype": a["mimetype"],
                                "size": a["size"],
                                "url": f"/ow_mail/attachment/{fid}/{uid}/{a['section']}",
                            } for a in atts],
                            "folder_kind": folder.kind,
                            "linked_records":
                                request.env["ow.mail.record.link"]
                                .linked_records(folder, int(uid),
                                                env["message_id"]),
                        })
            except Exception as e:
                _logger.warning("thread search failed for folder %s: %s", fid, e)
        # Sort chronologically
        results.sort(key=lambda m: m["date"] or "")
        return {"messages": results}

    @http.route("/ow_mail/inline/<int:folder_id>/<int:uid>/<path:cid>",
                type="http", auth="user")
    def inline(self, folder_id, uid, cid, **kw):
        """Serve a cid-referenced inline part.

        Restricted to parts where ``walk_parts`` returns ``kind == "inline"``
        and whose Content-Type matches an image/audio allowlist — stops an
        attacker from crafting a message where an HTML/SVG/JS *attachment*
        carries a Content-ID equal to one referenced from the body and ends
        up executing in the Odoo origin.
        """
        folder = _get_folder(folder_id)
        if not folder:
            return request.not_found()
        try:
            with imap_utils.imap_session(folder.account_id, folder.full_path, readonly=True) as conn:
                _flags, raw = imap_utils.fetch_full(conn, uid)
                if raw is None:
                    return request.not_found()
                msg = email.message_from_bytes(raw)
                for part, _section, kind in imap_utils.walk_parts(msg):
                    if kind != "inline":
                        continue
                    part_cid = (part.get("Content-ID") or "").strip("<>").strip()
                    if part_cid != cid:
                        continue
                    ctype = part.get_content_type()
                    if not _safe_inline_ctype(ctype):
                        _logger.info(
                            "blocked inline part with unsafe ctype=%s folder=%s uid=%s",
                            ctype, folder_id, uid,
                        )
                        return request.not_found()
                    payload = part.get_payload(decode=True) or b""
                    filename = (imap_utils.decode_header_value(part.get_filename())
                                or part_cid or "inline")
                    return Response(payload, headers=_security_headers([
                        ("Content-Type", ctype),
                        ("Content-Disposition", _content_disposition("inline", filename)),
                        ("Cache-Control", "private, max-age=300"),
                    ]))
        except Exception:
            _logger.exception("inline fetch failed folder=%s uid=%s cid=%s",
                              folder_id, uid, cid)
            return request.not_found()
        return request.not_found()

    @http.route("/ow_mail/attachment/<int:folder_id>/<int:uid>/<string:section>",
                type="http", auth="user")
    def attachment(self, folder_id, uid, section, download=False, **kw):
        """Serve an attachment part.

        ``Content-Type`` comes from the sender, so unsafe types (HTML, SVG,
        JS, XHTML) are always forced to ``attachment`` disposition and
        relabelled ``application/octet-stream``. The browser's nosniff +
        the sandbox CSP then prevent any fallback to HTML rendering in the
        Odoo origin even if the user saves the file and opens it.
        """
        folder = _get_folder(folder_id)
        if not folder:
            return request.not_found()
        try:
            with imap_utils.imap_session(folder.account_id, folder.full_path, readonly=True) as conn:
                _flags, raw = imap_utils.fetch_full(conn, uid)
                if raw is None:
                    return request.not_found()
                msg = email.message_from_bytes(raw)
                for part, sec, _kind in imap_utils.walk_parts(msg):
                    if sec != section:
                        continue
                    payload = part.get_payload(decode=True) or b""
                    filename = imap_utils.decode_header_value(part.get_filename()) or "file"
                    raw_ctype = part.get_content_type()
                    base = (raw_ctype or "").split(";", 1)[0].strip().lower()
                    # Inline rendering only when explicitly allowed AND safe.
                    if download or not _safe_inline_ctype(raw_ctype):
                        disp = "attachment"
                    else:
                        disp = "inline"
                    # Sanitize the Content-Type: force octet-stream for any
                    # type that could execute in the Odoo origin even if the
                    # browser ignores the attachment disposition.
                    if base in _ATTACH_ONLY_EXACT:
                        served_ctype = "application/octet-stream"
                    else:
                        served_ctype = raw_ctype
                    return Response(payload, headers=_security_headers([
                        ("Content-Type", served_ctype),
                        ("Content-Disposition", _content_disposition(disp, filename)),
                    ]))
        except Exception:
            _logger.exception("attachment fetch failed folder=%s uid=%s section=%s",
                              folder_id, uid, section)
            return request.not_found()
        return request.not_found()

    @http.route("/ow_mail/attachment/prepare", type="json", auth="user")
    def attachment_prepare(self, folder_id, uid):
        """Copy all attachments from an IMAP message to Odoo ir.attachment.

        Used when forwarding a message or editing a draft to pre-populate
        the compose window with the original attachments.
        """
        folder = _get_folder(folder_id)
        if not folder:
            return _imap_err("Folder not found")

        out = []
        try:
            with imap_utils.imap_session(folder.account_id, folder.full_path, readonly=True) as conn:
                _flags, raw = imap_utils.fetch_full(conn, uid)
                if not raw:
                    return _imap_err("Message not found")

                msg = email.message_from_bytes(raw)
                for part, _section, kind in imap_utils.walk_parts(msg):
                    if kind == "attachment":
                        payload = part.get_payload(decode=True)
                        if not payload:
                            continue
                        filename = imap_utils.decode_header_value(part.get_filename()) or "attachment"
                        raw_mime = (part.get_content_type() or "application/octet-stream").split(";", 1)[0].strip().lower()
                        served_mime = ("application/octet-stream"
                                       if raw_mime in self._UPLOAD_RELABEL else raw_mime)

                        att = request.env["ir.attachment"].create({
                            "name": filename,
                            "raw": payload,
                            "mimetype": served_mime,
                            "res_model": "ow.mail.compose",
                            "res_id": 0,
                        })
                        out.append({
                            "id": att.id,
                            "name": att.name,
                            "size": len(payload),
                            "mimetype": att.mimetype,
                        })
        except Exception as e:
            _logger.exception("Failed to prepare attachments for folder=%s uid=%s", folder_id, uid)
            return _imap_err(str(e))

        return out

    @http.route("/ow_mail/send", type="json", auth="user")
    def send(self, account_id, to, cc=None, bcc=None, subject="", body_html="",
             attachment_ids=None, in_reply_to=None, references=None):
        """Send a message via SMTP and APPEND a copy to the Sent folder.

        Delegates to ``ow.mail.account.send_mail``. Returns ``{"ok": True}``
        on success. When SMTP succeeded but the Sent-folder APPEND failed,
        returns ``{"ok": True, "warning": "..."}`` so the frontend can show a
        non-blocking notice without treating the send as a failure.
        """
        acc = _get_account(account_id)
        if not acc:
            return _imap_err("Invalid account")
        try:
            result = acc.send_mail(
                to=to, cc=cc, bcc=bcc, subject=subject, body_html=body_html,
                attachment_ids=attachment_ids or [],
                in_reply_to=in_reply_to, references=references)
        except Exception as e:
            return _imap_err(str(e))
        # Propagate the Sent-APPEND warning when SMTP succeeded but the
        # copy to Sent didn't — the client shows a non-blocking notice.
        if isinstance(result, dict) and result.get("sent_append_failed"):
            return {"ok": True, "warning": _(
                "Message sent, but saving to Sent folder failed: %s"
            ) % result["sent_append_failed"]}
        return {"ok": True}

    @http.route("/ow_mail/draft", type="json", auth="user")
    def save_draft(self, account_id, to="", cc="", bcc="", subject="", body_html="",
                   attachment_ids=None, in_reply_to=None, references=None,
                   replace_uid=None):
        """APPEND a draft message to the account's Drafts folder via IMAP.

        No DB record is created; the draft lives exclusively on the IMAP
        server and is visible in the Drafts folder like any other message.
        ``replace_uid`` names a previous autosave of the same compose window;
        it is expunged (best-effort) after the new APPEND succeeds so
        repeated autosaves keep exactly one copy. Returns the new draft UID
        (or ``None`` when the server reports no APPENDUID and the Message-ID
        fallback search fails) so the client can chain the next replace.
        Returns an error if no Drafts folder is configured for the account.
        """
        acc = _get_account(account_id)
        if not acc:
            return _imap_err("Invalid account")
        if not acc.drafts_folder_id:
            return _imap_err("No Drafts folder configured")
        try:
            replace_uid = int(replace_uid) if replace_uid else None
        except (TypeError, ValueError):
            replace_uid = None
        try:
            new_uid = acc.save_draft(
                to=to, cc=cc, bcc=bcc, subject=subject, body_html=body_html,
                attachment_ids=attachment_ids or [],
                in_reply_to=in_reply_to, references=references,
                replace_uid=replace_uid)
        except Exception as e:
            return _imap_err(str(e))
        return {"ok": True, "uid": new_uid,
                "folder_id": acc.drafts_folder_id.id}

    @http.route("/ow_mail/draft/discard", type="json", auth="user")
    def discard_draft(self, account_id, uid):
        """Expunge one draft from the account's Drafts folder (best-effort).

        Called after a successful send to remove the autosaved copy. Only
        ever targets the account's own Drafts folder — the caller cannot
        name an arbitrary folder.
        """
        acc = _get_account(account_id)
        if not acc:
            return _imap_err("Invalid account")
        try:
            acc.discard_draft(int(uid))
        except Exception as e:
            _logger.info("draft discard failed for account %s uid %s: %s",
                         acc.id, uid, e)
            return _imap_err(str(e))
        return {"ok": True}

    @http.route("/ow_mail/message/action", type="json", auth="user")
    def message_action(self, folder_id, uids, action, payload=None):
        """Apply a bulk action to one or more messages identified by UID.

        Multiplexes the following actions in a single IMAP session:

        * ``mark_read`` / ``mark_unread`` — ``UID STORE +/-FLAGS (\\Seen)``.
        * ``toggle_flag`` — toggles ``\\Flagged``; if ``payload.state`` is
          provided the flag is set/cleared to that value unconditionally,
          otherwise per-message current state is fetched first.
        * ``move`` — ``UID MOVE`` to ``payload.folder_id`` (same account only);
          falls back to COPY + \\Deleted + EXPUNGE on servers without MOVE.
        * ``delete`` — moves to Trash if configured; otherwise hard-deletes
          with ``\\Deleted`` + EXPUNGE.
        * ``add_tag`` / ``remove_tag`` — single ``UID STORE +/-FLAGS`` of one
          tag's IMAP keyword on the whole uid set (bulk-bar tagging).
        * ``trust_sender`` — adds ``payload.emails`` to ``ow.mail.trusted.sender``
          for the current user (safe-mode allow-list, no IMAP side-effect).
        * ``set_tags`` — diffs the requested ``payload.tag_ids`` against
          current IMAP keywords and issues ``+FLAGS`` / ``-FLAGS`` for the
          delta; only ``OwTag_`` keywords are touched, system flags are left
          intact.

        The UID set is capped at ``_MAX_ACTION_UIDS`` to keep IMAP command
        size and worker time bounded.
        """
        folder = _get_folder(folder_id)
        if not folder:
            return _imap_err("Folder not found")
        payload = payload or {}
        # Cap uid list to keep the IMAP command size and worker time bounded.
        if not isinstance(uids, (list, tuple)):
            return _imap_err("uids must be a list")
        if len(uids) > _MAX_ACTION_UIDS:
            return _imap_err(
                f"Too many messages in one request (max {_MAX_ACTION_UIDS})")
        uid_set = ",".join(str(int(u)) for u in uids)
        if not uid_set:
            return {"ok": False}
        try:
            with imap_utils.imap_session(folder.account_id, folder.full_path) as conn:
                if action == "mark_read":
                    conn.uid("STORE", uid_set, "+FLAGS", "(\\Seen)")
                elif action == "mark_unread":
                    conn.uid("STORE", uid_set, "-FLAGS", "(\\Seen)")
                elif action == "toggle_flag":
                    # Fetch current flags; for simplicity always +FLAGS if requested state
                    target = payload.get("state")
                    if target is None:
                        # Toggle per-uid
                        typ, data = conn.uid("FETCH", uid_set, "(FLAGS)")
                        flagged_map = {}
                        for part in data or []:
                            if not isinstance(part, (bytes, bytearray)):
                                continue
                            line = part.decode(errors="replace")
                            m = re.search(r"UID (\d+).*?FLAGS \(([^)]*)\)", line)
                            if m:
                                flagged_map[int(m.group(1))] = "\\Flagged" in m.group(2)
                        for uid in uids:
                            op = "-FLAGS" if flagged_map.get(int(uid)) else "+FLAGS"
                            conn.uid("STORE", str(uid), op, "(\\Flagged)")
                    else:
                        op = "+FLAGS" if target else "-FLAGS"
                        conn.uid("STORE", uid_set, op, "(\\Flagged)")
                elif action == "move":
                    target = _get_folder(payload.get("folder_id") or 0)
                    if not target:
                        return _imap_err("Target folder missing")
                    if target.account_id.id != folder.account_id.id:
                        return _imap_err("Cross-account move is not supported")
                    target_mbox = imap_utils.imap_mbox_quote(target.full_path)
                    try:
                        conn.uid("MOVE", uid_set, target_mbox)
                    except Exception:
                        conn.uid("COPY", uid_set, target_mbox)
                        conn.uid("STORE", uid_set, "+FLAGS", "(\\Deleted)")
                        conn.expunge()
                elif action == "delete":
                    trash = folder.account_id.trash_folder_id
                    if trash and trash != folder:
                        trash_mbox = imap_utils.imap_mbox_quote(trash.full_path)
                        try:
                            conn.uid("MOVE", uid_set, trash_mbox)
                        except Exception:
                            conn.uid("COPY", uid_set, trash_mbox)
                            conn.uid("STORE", uid_set, "+FLAGS", "(\\Deleted)")
                            conn.expunge()
                    else:
                        conn.uid("STORE", uid_set, "+FLAGS", "(\\Deleted)")
                        conn.expunge()
                elif action in ("add_tag", "remove_tag"):
                    # Bulk tagging: single +/-FLAGS on the whole uid set — no
                    # per-uid FETCH needed (unlike set_tags, which diffs the
                    # complete keyword set of one message).
                    tag = request.env["ow.mail.tag"].browse(
                        int(payload.get("tag_id") or 0)).exists()
                    if not tag or tag.user_id.id != request.env.user.id:
                        return _imap_err("Tag not found")
                    if not tag.imap_keyword:
                        return _imap_err("Tag has no IMAP keyword")
                    op = "+FLAGS" if action == "add_tag" else "-FLAGS"
                    conn.uid("STORE", uid_set, op, f"({tag.imap_keyword})")
                elif action == "trust_sender":
                    uid = request.env.user.id
                    TS = _trusted_sender_scope(uid)
                    for email_addr in payload.get("emails") or []:
                        if not email_addr:
                            continue
                        if not TS.search([("user_id", "=", uid),
                                          ("email", "=", email_addr.lower())], limit=1):
                            TS.create({"user_id": uid,
                                       "email": email_addr.lower()})
                elif action == "set_tags":
                    tag_ids = payload.get("tag_ids") or []
                    tags = request.env["ow.mail.tag"].browse(tag_ids)
                    # Fetch existing keywords for each uid, then diff
                    typ, data = conn.uid("FETCH", uid_set, "(FLAGS)")
                    per_uid = {}
                    for part in data or []:
                        if not isinstance(part, (bytes, bytearray)):
                            continue
                        line = part.decode(errors="replace")
                        # UID and FLAGS order differs per server (Dovecot:
                        # "UID n FLAGS (...)", GreenMail: "FLAGS (...) UID n")
                        # — match them independently so removal works on both.
                        m_uid = re.search(r"UID (\d+)", line)
                        m_flags = re.search(r"FLAGS \(([^)]*)\)", line)
                        if m_uid and m_flags:
                            kw = [f for f in m_flags.group(1).split()
                                  if f and not f.startswith("\\")]
                            per_uid[int(m_uid.group(1))] = kw
                    target_kws = [t.imap_keyword for t in tags if t.imap_keyword]
                    all_ow_kws = [t.imap_keyword for t in
                                   request.env["ow.mail.tag"].search([]) if t.imap_keyword]
                    for uid in uids:
                        cur = set(per_uid.get(int(uid), []))
                        to_add = [k for k in target_kws if k not in cur]
                        to_del = [k for k in (cur & set(all_ow_kws)) if k not in target_kws]
                        if to_add:
                            conn.uid("STORE", str(uid), "+FLAGS", f"({' '.join(to_add)})")
                        if to_del:
                            conn.uid("STORE", str(uid), "-FLAGS", f"({' '.join(to_del)})")
                else:
                    return _imap_err(f"Unknown action: {action}")
        except Exception as e:
            _logger.warning("action %s failed: %s", action, e)
            return _imap_err(e)
        # Refresh counts
        try:
            folder.refresh_counts()
        except Exception:
            pass
        return {"ok": True}

    # ---------------- Folder management ----------------

    @http.route("/ow_mail/folder/create", type="json", auth="user")
    def folder_create(self, account_id, name, parent_id=None):
        """Create a new IMAP mailbox under an optional parent.

        Delegates to ``imap_create_folder``, which issues ``CREATE`` on the
        server and then calls ``_refresh_folders`` so the local cache stays
        in sync. ``parent_id`` must belong to the same account.
        """
        account = _get_account(account_id)
        if not account:
            return _imap_err("Account not found")
        parent_path = ""
        if parent_id:
            parent = _get_folder(parent_id)
            if not parent or parent.account_id.id != account.id:
                return _imap_err("Parent folder not found")
            parent_path = parent.full_path
        try:
            account.imap_create_folder(parent_path, name)
        except Exception as e:
            return _imap_err(e)
        return {"ok": True}

    @http.route("/ow_mail/folder/rename", type="json", auth="user")
    def folder_rename(self, folder_id, new_name):
        """Rename the leaf segment of a folder's path via IMAP RENAME.

        Only the leaf name changes; the parent hierarchy is preserved.
        Delegates to ``imap_rename_leaf`` which also refreshes the local
        folder cache to reflect the new ``full_path``.
        """
        folder = _get_folder(folder_id)
        if not folder:
            return _imap_err("Folder not found")
        name = (new_name or "").strip()
        if not name:
            return _imap_err("Folder name is required")
        account = folder.account_id
        try:
            account.imap_rename_leaf(folder.full_path, name)
        except Exception as e:
            return _imap_err(e)
        return {"ok": True}

    @http.route("/ow_mail/folder/move", type="json", auth="user")
    def folder_move(self, folder_id, new_parent_id=None):
        """Move a folder to a new parent (or to the root) via IMAP RENAME.

        ``new_parent_id=None`` moves the folder to the top level of the
        hierarchy. Moving a folder into itself is rejected. The target parent
        must belong to the same account — cross-account IMAP RENAME is not
        meaningful.
        """
        folder = _get_folder(folder_id)
        if not folder:
            return _imap_err("Folder not found")
        account = folder.account_id
        new_parent_path = ""
        if new_parent_id:
            parent = _get_folder(new_parent_id)
            if not parent or parent.account_id.id != account.id:
                return _imap_err("Target parent not found")
            if parent.id == folder.id:
                return _imap_err("Cannot move folder into itself")
            new_parent_path = parent.full_path
        try:
            account.imap_move_folder(folder.full_path, new_parent_path)
        except Exception as e:
            return _imap_err(e)
        return {"ok": True}

    @http.route("/ow_mail/folder/delete", type="json", auth="user")
    def folder_delete(self, folder_id):
        """Delete a mailbox from the IMAP server and remove it from the local cache.

        Delegates to ``imap_delete_folder``. The folder must be empty on most
        servers; callers should offer to empty it first or use ``folder_empty``
        before deleting.
        """
        folder = _get_folder(folder_id)
        if not folder:
            return _imap_err("Folder not found")
        try:
            folder.account_id.imap_delete_folder(folder.full_path)
        except Exception as e:
            return _imap_err(e)
        return {"ok": True}

    @http.route("/ow_mail/folder/subscribe", type="json", auth="user")
    def folder_subscribe(self, folder_id, subscribed):
        """Toggle the IMAP SUBSCRIBE/UNSUBSCRIBE state for a folder.

        Subscribed folders appear in the sidebar; unsubscribed ones are hidden
        but still exist on the server. The ``subscribed`` field on the local
        ``ow.mail.folder`` cache is updated by ``imap_set_subscribed`` so the
        UI reflects the change without a full sync.
        """
        folder = _get_folder(folder_id)
        if not folder:
            return _imap_err("Folder not found")
        try:
            folder.account_id.imap_set_subscribed(folder.full_path, bool(subscribed))
        except Exception as e:
            return _imap_err(e)
        return {"ok": True}

    @http.route("/ow_mail/account/quota", type="json", auth="user")
    def account_quota(self, account_id):
        """Fetch mailbox quota via IMAP GETQUOTA / GETQUOTAROOT.

        Returns the raw result from ``imap_quota()``; the shape depends on
        what the server supports. Not all servers implement RFC 2087; the
        frontend should handle a missing or empty result gracefully.
        """
        account = _get_account(account_id)
        if not account:
            return _imap_err("Account not found")
        try:
            return account.imap_quota()
        except Exception as e:
            return _imap_err(e)

    @http.route("/ow_mail/folder/empty", type="json", auth="user")
    def folder_empty(self, folder_id):
        """Permanently delete all messages in a folder via IMAP.

        Intended for Trash / Spam folders. Delegates to ``imap_empty_folder``
        which flags all messages ``\\Deleted`` and calls EXPUNGE. This is
        irreversible — the frontend must confirm before calling.
        """
        folder = _get_folder(folder_id)
        if not folder:
            return _imap_err("Folder not found")
        try:
            folder.account_id.imap_empty_folder(folder.full_path)
        except Exception as e:
            return _imap_err(e)
        return {"ok": True}

    # Compose attachment upload ---------------------------------------------
    _MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MiB; matches most SMTP limits
    # Mimetypes we refuse to store verbatim — they can trigger unwanted
    # rendering paths if ever served inline. We still accept the file, but
    # relabel to octet-stream so later /attachment serves it as a download.
    _UPLOAD_RELABEL = frozenset({
        "text/html", "application/xhtml+xml", "image/svg+xml",
        "application/javascript", "application/ecmascript",
        "text/javascript", "text/ecmascript",
    })

    @http.route("/ow_mail/attachment/upload", type="http", auth="user",
                methods=["POST"], csrf=True)
    def upload(self, ufile=None, **kw):
        """Store a compose-time attachment.

        Requires CSRF (handled by Odoo's web layer — the client reads
        ``session.csrf_token`` and includes it in the FormData). The
        attachment is pinned to the uploading user via ``create_uid`` and
        ``res_model='ow.mail.compose'`` / ``res_id=0`` so the orphan cron
        can clean it up.
        """
        import json
        if ufile is None:
            return Response(json.dumps({"error": "No file"}), status=400,
                            content_type="application/json")
        data = ufile.read()
        if not data:
            return Response(json.dumps({"error": "Empty file"}), status=400,
                            content_type="application/json")
        if len(data) > self._MAX_UPLOAD_BYTES:
            return Response(json.dumps({
                "error": f"File too large (max {self._MAX_UPLOAD_BYTES // (1024*1024)} MiB)"
            }), status=413, content_type="application/json")
        # Sanitize filename — strip CR/LF + path separators so it can't
        # traverse directories or break headers when echoed back.
        safe_name = re.sub(r'[\r\n"/\\]+', "_",
                           (ufile.filename or "attachment")).strip() or "attachment"
        safe_name = safe_name[:200]
        # Normalize mimetype (browser-supplied, not trusted for behaviour
        # but useful as a hint for the recipient's mail client).
        raw_mime = (ufile.mimetype or "application/octet-stream").split(";", 1)[0].strip().lower()
        served_mime = ("application/octet-stream"
                       if raw_mime in self._UPLOAD_RELABEL else raw_mime)
        att = request.env["ir.attachment"].create({
            "name": safe_name, "raw": data, "mimetype": served_mime,
            "res_model": "ow.mail.compose", "res_id": 0,
        })
        return json.dumps({"id": att.id, "name": att.name, "size": len(data),
                           "mimetype": att.mimetype})

    # -----------------------------------------------------------------------
    # Contacts
    # -----------------------------------------------------------------------

    def _contact_to_dict(self, c):
        """Serialize an ``ow.mail.contact`` record to the wire dict.

        Used by both ``contacts_list`` and ``contacts_suggest`` so both
        endpoints return the same field shape. ``last_used`` is serialized
        as an ISO 8601 string so the suggest endpoint can sort candidates
        by string comparison client-side.
        """
        return {
            "id": c.id,
            "name": c.name,
            "email": c.email,
            "company": c.company or "",
            "phone": c.phone or "",
            "note": c.note or "",
            "partner_id": c.partner_id.id if c.partner_id else False,
            "last_used": c.last_used and c.last_used.isoformat() or None,
        }

    @http.route("/ow_mail/contacts/list", type="json", auth="user")
    def contacts_list(self, search=None, limit=500):
        """Return the full contact list, optionally filtered by a search string.

        Used by the Contacts panel to display all saved addresses. The ``ir.rule``
        on ``ow.mail.contact`` ensures each user only sees their own contacts.
        """
        domain = []
        if search:
            s = search.strip()
            domain = ["|", "|",
                     ("name", "ilike", s),
                     ("email", "ilike", s),
                     ("company", "ilike", s)]
        records = request.env["ow.mail.contact"].search(domain, limit=limit)
        return {"contacts": [self._contact_to_dict(c) for c in records]}

    @http.route("/ow_mail/contacts/suggest", type="json", auth="user")
    def contacts_suggest(self, query, limit=8):
        """Return address-book suggestions for the compose To/CC/BCC typeahead.

        Searches the user's own ``ow.mail.contact`` records first; remaining
        slots are filled from ``res.partner`` (company-wide contacts). Dedup
        by normalized email ensures the same address doesn't appear twice.
        ``source`` in each result indicates ``"own"`` vs ``"partner"`` so the
        frontend can visually distinguish them.
        """
        q = (query or "").strip()
        if len(q) < 1:
            return {"suggestions": []}
        out = []
        seen = set()
        own = request.env["ow.mail.contact"].search(
            ["|", ("name", "ilike", q), ("email", "ilike", q)],
            limit=limit,
        )
        for c in own:
            key = (c.email or "").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append({
                "source": "own",
                "id": c.id,
                "name": c.name,
                "email": c.email,
                "company": c.company or "",
            })
        remaining = max(0, limit - len(out))
        if remaining:
            partners = request.env["res.partner"].search(
                ["&", ("email", "!=", False),
                 "|", ("name", "ilike", q), ("email", "ilike", q)],
                limit=remaining * 2,
            )
            for p in partners:
                key = (p.email or "").lower().strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                out.append({
                    "source": "partner",
                    "id": p.id,
                    "name": p.name or p.email,
                    "email": p.email,
                    "company": p.parent_id.name if p.parent_id else "",
                })
                if len(out) >= limit:
                    break
        return {"suggestions": out}

    @http.route("/ow_mail/contacts/create", type="json", auth="user")
    def contacts_create(self, name, email, company=None, phone=None,
                        note=None, partner_id=None):
        """Create a new personal contact, or return the existing one if email matches.

        Email is the unique key (case-insensitive). When a contact already
        exists for that address, returns it with ``"already": True`` so the
        caller can decide whether to update instead.
        """
        if not email or not email.strip():
            return _imap_err("Email is required")
        email_n = email.strip().lower()
        existing = request.env["ow.mail.contact"].search(
            [("email", "=", email_n)], limit=1,
        )
        if existing:
            return {"contact": self._contact_to_dict(existing), "already": True}
        vals = {
            "name": (name or "").strip() or email_n,
            "email": email_n,
            "company": company or False,
            "phone": phone or False,
            "note": note or False,
        }
        if partner_id:
            vals["partner_id"] = int(partner_id)
        c = request.env["ow.mail.contact"].create(vals)
        return {"contact": self._contact_to_dict(c), "already": False}

    @http.route("/ow_mail/contacts/update", type="json", auth="user")
    def contacts_update(self, id, vals):
        """Update writable fields on a contact.

        Only fields in the explicit allowlist are accepted to prevent mass
        assignment of system fields (e.g. ``user_id``, ``create_uid``).
        ``partner_id`` is validated to exist before being written.
        """
        c = request.env["ow.mail.contact"].browse(int(id)).exists()
        if not c:
            return _imap_err("Contact not found")
        allowed = {"name", "email", "company", "phone", "note", "partner_id"}
        clean = {k: v for k, v in (vals or {}).items() if k in allowed}
        if clean.get("partner_id"):
            pid = int(clean["partner_id"])
            if not request.env["res.partner"].browse(pid).exists():
                return _imap_err("Partner not found")
            clean["partner_id"] = pid
        try:
            c.write(clean)
        except Exception as e:
            return _imap_err(e)
        return {"contact": self._contact_to_dict(c)}

    @http.route("/ow_mail/contacts/delete", type="json", auth="user")
    def contacts_delete(self, ids):
        """Delete one or more contacts by ID.

        The ``ir.rule`` on ``ow.mail.contact`` ensures users can only delete
        their own records; browsing a foreign ID silently produces an empty
        recordset, so no cross-user delete is possible.
        """
        records = request.env["ow.mail.contact"].browse(
            [int(i) for i in ids or []]
        ).exists()
        records.unlink()
        return {"ok": True}

    @http.route("/ow_mail/contacts/touch", type="json", auth="user")
    def contacts_touch(self, emails):
        """Update the ``last_used`` timestamp on contacts matching the given addresses.

        Called after a message is sent to keep the suggest endpoint's recency
        ranking fresh. Operates in bulk so a send with many recipients only
        hits the DB once.
        """
        norm = [(e or "").strip().lower() for e in emails or [] if e]
        if not norm:
            return {"ok": True}
        records = request.env["ow.mail.contact"].search(
            [("email", "in", norm)]
        )
        if records:
            records.touch()
        return {"ok": True}
