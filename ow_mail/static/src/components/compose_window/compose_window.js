/** @odoo-module **/

import { Component, useState, useRef, onMounted, onWillDestroy } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { RecipientInput } from "./recipient_input";

/** Milliseconds between silent draft autosaves of a dirty compose window. */
const AUTOSAVE_INTERVAL_MS = 30000;

/**
 * Tags stripped entirely from user-composed HTML before sending.
 * Their mere presence in the compose body can execute, navigate, or fetch
 * remote content if a regression in server-side `sanitize_and_detect` ever
 * lets them through. The compose `contenteditable` lives in the Odoo
 * same-origin context (not in a sandboxed iframe), so this is the last
 * defence before `innerHTML` insertion. Mirrors `models/ow_mail_imap.py`
 * `_DROP_TAGS` but tuned for the editing context (`style` is allowed for
 * inline formatting via `execCommand`).
 */
const _DROP_TAGS = new Set([
    "script", "iframe", "object", "embed", "link", "meta", "base",
    "form", "frame", "frameset",
]);

/**
 * URL schemes that execute code and must never appear in URL-bearing
 * attributes. Matches `javascript:`, `vbscript:`, and `file:` (with
 * optional leading whitespace).
 */
const _DANGER_SCHEME_RX = /^\s*(?:javascript|vbscript|file):/i;

/**
 * Distinguishes dangerous `data:` URIs (anything that is not an image) from
 * safe ones. A `data:image/...` URI is allowed (inline images); all other
 * `data:` subtypes (HTML, scripts, SVG-with-scripts, etc.) are stripped.
 */
const _DATA_NONIMAGE_RX = /^\s*data:(?!image\/)/i;

/**
 * HTML attributes that can carry URLs and therefore must be checked against
 * `_DANGER_SCHEME_RX` and `_DATA_NONIMAGE_RX` before the message is sent.
 */
const _URL_ATTRS = ["src", "srcset", "href", "action", "formaction",
                    "data", "poster", "background", "ping"];

/**
 * Strip executable content from user-composed HTML before sending.
 *
 * Uses `DOMParser` rather than regex so that nested or malformed HTML is
 * handled correctly by the browser's own parser. The following vectors are
 * blocked:
 *
 * - **Script tags** and other dangerous elements (`_DROP_TAGS`): `<script>`,
 *   `<iframe>`, `<object>`, `<embed>`, `<link>`, `<meta>`, `<base>`,
 *   `<form>`, `<frame>`, `<frameset>` — removed entirely including children.
 * - **Event handlers** (`on*` attributes): stripped from every element.
 * - **Dangerous URL schemes**: `javascript:`, `vbscript:`, and `file:` in
 *   URL-bearing attributes (`_URL_ATTRS`) are removed.
 * - **`data:` non-image URIs**: `data:text/html`, `data:application/...`,
 *   `data:image/svg+xml` with embedded scripts, etc. are removed; plain
 *   `data:image/*` URIs (inline images) are preserved.
 * - **`<meta>` redirects** and **`<link>` external stylesheets**: removed via
 *   `_DROP_TAGS`.
 *
 * @param {string} html - Raw HTML from the `contenteditable` compose body.
 * @returns {string} Sanitized `innerHTML` of the parsed document body, safe
 *   to insert into the DOM or transmit as the message body.
 */
function _sanitizeComposeHtml(html) {
    if (!html) return "";
    const doc = new DOMParser().parseFromString(String(html), "text/html");
    const drop = [];
    for (const el of doc.querySelectorAll("*")) {
        const tag = el.tagName.toLowerCase();
        if (_DROP_TAGS.has(tag)) {
            drop.push(el);
            continue;
        }
        // Strip event-handler attrs (on*).
        for (const attr of [...el.attributes]) {
            if (attr.name.toLowerCase().startsWith("on")) {
                el.removeAttribute(attr.name);
            }
        }
        // Block dangerous URL schemes in URL-bearing attrs.
        for (const a of _URL_ATTRS) {
            const v = el.getAttribute(a);
            if (v && (_DANGER_SCHEME_RX.test(v) || _DATA_NONIMAGE_RX.test(v))) {
                el.removeAttribute(a);
            }
        }
    }
    for (const el of drop) el.remove();
    return doc.body ? doc.body.innerHTML : "";
}

/**
 * Floating, minimizable compose window component.
 *
 * Multiple instances can be open simultaneously (one per entry in
 * `mail.state.composeWindows`). Each window is identified by a unique
 * `win.id` and the parent `MailClient` component renders one
 * `ComposeWindow` per entry.
 *
 * The message body is a plain `contenteditable` div with a
 * `document.execCommand` toolbar — intentionally minimal and kept separate
 * from Odoo's built-in WYSIWYG editor, which requires a real `fields.Html`
 * record backed by an ORM model.
 *
 * `_sanitizeComposeHtml` is called on send and draft save to strip any XSS
 * content the user may have pasted into the body from an external source.
 */
export class ComposeWindow extends Component {
    static template = "ow_mail.ComposeWindow";
    static components = { RecipientInput };
    static props = { win: Object };

    /**
     * Update the "To" recipient string from a `RecipientInput` update event.
     *
     * `RecipientInput` calls this via the `onUpdate` prop whenever its value
     * changes (parent-owns-state pattern: the child is stateless with respect
     * to the current value; the parent holds the authoritative copy in
     * `props.win`).
     *
     * @param {string} v - New recipient string (comma-separated RFC 5322 addresses).
     */
    onUpdateTo(v) { this.props.win.to = v; this.markDirty(); }

    /**
     * Update the "CC" recipient string from a `RecipientInput` update event.
     *
     * @param {string} v - New recipient string (comma-separated RFC 5322 addresses).
     */
    onUpdateCc(v) { this.props.win.cc = v; this.markDirty(); }

    /**
     * Update the "BCC" recipient string from a `RecipientInput` update event.
     *
     * @param {string} v - New recipient string (comma-separated RFC 5322 addresses).
     */
    onUpdateBcc(v) { this.props.win.bcc = v; this.markDirty(); }

    /**
     * OWL lifecycle setup hook.
     *
     * Hooks used: `useService` (ow_mail service), `useState` (shared mail
     * state and component-local drag state), `useRef` (body contenteditable),
     * `onMounted`.
     *
     * `onMounted` positions the window and populates the initial body by
     * writing `this.mail.defaultBody()` (or the pre-filled `win.body`) into
     * the `contenteditable` via `_sanitizeComposeHtml` to guard against
     * pre-filled bodies that may contain unsafe HTML (e.g. quoted replies).
     */
    setup() {
        this.mail = useService("ow_mail");
        this.state = useState(this.mail.state);
        // imgSel: overlay-geometry (relative to the compose-inner card) for
        // the image-resize toolbar; the selected <img> itself lives in
        // this._selImg (plain DOM ref — never mutated with marker classes,
        // so the saved HTML stays clean).
        this.local = useState({ dragActive: false, imgSel: null });
        this.bodyRef = useRef("body");
        this.innerRef = useRef("inner");
        this._selImg = null;
        this._imgDrag = null;
        this._savedRange = null;
        this._dragCount = 0;
        onMounted(() => {
            if (this.bodyRef.el) {
                this.bodyRef.el.innerHTML = _sanitizeComposeHtml(this.props.win.body);
            }
        });
        // Silent periodic autosave — the service skips pristine/empty/busy
        // windows, so an idle ticker is cheap.
        this._autosaveTimer = setInterval(
            () => this.mail.autosaveDraft(this.props.win),
            AUTOSAVE_INTERVAL_MS);
        // Drag-resize listeners live on the document so the pointer can
        // leave the handle while dragging.
        this._onImgDragMove = this._onImgDragMove.bind(this);
        this._onImgDragEnd = this._onImgDragEnd.bind(this);
        document.addEventListener("pointermove", this._onImgDragMove);
        document.addEventListener("pointerup", this._onImgDragEnd);
        onWillDestroy(() => {
            clearInterval(this._autosaveTimer);
            document.removeEventListener("pointermove", this._onImgDragMove);
            document.removeEventListener("pointerup", this._onImgDragEnd);
        });
    }

    /** Flag the window as having unsaved changes (drives autosave + guards). */
    markDirty() {
        this.props.win.dirty = true;
    }

    /**
     * Footer status line: last autosave time or an unsaved-changes hint.
     * @returns {string}
     */
    get saveStatus() {
        const win = this.props.win;
        if (win.saving) {
            return _t("Saving…");
        }
        if (win.dirty) {
            return _t("Unsaved changes");
        }
        if (win.lastSavedAt) {
            const time = new Date(win.lastSavedAt).toLocaleTimeString(
                [], { hour: "2-digit", minute: "2-digit" });
            return _t("Draft saved at %s", time);
        }
        return "";
    }

    /**
     * Capture the current `window.getSelection()` range if it lies inside the
     * compose body `contenteditable`.
     *
     * Toolbar buttons are rendered outside the `contenteditable`, so clicking
     * them moves browser focus away from the editor and collapses the
     * selection. `_saveSelection` must be called on toolbar `mousedown`
     * (before focus moves) so that `_restoreSelection` can put the caret back
     * before `execCommand` fires — `execCommand` operates on the active
     * selection and does nothing if the selection is empty or outside the
     * editable region.
     */
    _saveSelection() {
        const sel = window.getSelection();
        if (!sel || !sel.rangeCount || !this.bodyRef.el) return;
        const range = sel.getRangeAt(0);
        if (this.bodyRef.el.contains(range.commonAncestorContainer)) {
            this._savedRange = range.cloneRange();
        }
    }

    /**
     * Restore the previously saved selection range into the compose body.
     *
     * Focuses the `contenteditable` first, then re-applies the cloned range
     * from `_saveSelection`. Must be called immediately before any
     * `execCommand` invocation that depends on selection context (e.g.
     * `insertHTML`, `createLink`, `insertImage`).
     */
    _restoreSelection() {
        if (!this.bodyRef.el) return;
        this.bodyRef.el.focus();
        if (this._savedRange) {
            const sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(this._savedRange);
        }
    }

    /**
     * Sync the body HTML from the `contenteditable` element back into
     * `props.win.body` on each keystroke.
     *
     * Because OWL reactive state owns the authoritative copy of the compose
     * body (for save/send), and the `contenteditable` is the live editor, the
     * two must be kept in sync on every `input` event.
     */
    /** Sync the contenteditable HTML into the window state + mark dirty. */
    _syncBody() {
        if (!this.bodyRef.el) return;
        this.props.win.body = this.bodyRef.el.innerHTML;
        this.markDirty();
    }

    onBodyInput() {
        this._syncBody();
        // Typing invalidates the image-selection overlay geometry.
        this.deselectImg();
    }

    // ------------------------------------------------------------------
    // Image paste + resize
    // ------------------------------------------------------------------

    /**
     * Intercept pastes that carry image files (screenshots, copied images).
     *
     * Without this, Chrome inserts `blob:` URLs that only resolve inside
     * the current browser session — the image would be broken in the saved
     * draft and for every recipient. Image files are converted to inline
     * `data:` URIs via the same path as the toolbar's insert-image button.
     * Text/HTML pastes are left to the browser.
     *
     * @param {ClipboardEvent} ev
     */
    async onPaste(ev) {
        const files = [...((ev.clipboardData && ev.clipboardData.files) || [])]
            .filter((f) => f.type.startsWith("image/"));
        if (!files.length) return;
        ev.preventDefault();
        for (const f of files) {
            await this._insertImageFile(f);
        }
    }

    /**
     * Read an image file and insert it at the caret as an inline data URI.
     * Shared by the toolbar insert button and the paste handler.
     *
     * @param {File} file
     */
    async _insertImageFile(file) {
        const dataUrl = await new Promise((resolve, reject) => {
            const r = new FileReader();
            r.onload = () => resolve(r.result);
            r.onerror = reject;
            r.readAsDataURL(file);
        });
        const safe = String(dataUrl).replace(/"/g, "&quot;");
        this.exec("insertHTML",
            `<img src="${safe}" style="max-width:100%;height:auto;" alt=""/>`);
    }

    /**
     * Body click: clicking an image selects it (overlay with size toolbar +
     * drag handle); clicking anywhere else deselects.
     *
     * @param {MouseEvent} ev
     */
    onBodyClick(ev) {
        if (ev.target && ev.target.tagName === "IMG") {
            this._selectImg(ev.target);
        } else {
            this.deselectImg();
        }
    }

    /**
     * Compute the overlay geometry (selection border, toolbar, drag handle)
     * for *img*, relative to the compose-inner card.
     *
     * @param {HTMLImageElement} img
     */
    _selectImg(img) {
        if (!this.innerRef.el) return;
        this._selImg = img;
        const inner = this.innerRef.el.getBoundingClientRect();
        const r = img.getBoundingClientRect();
        this.local.imgSel = {
            top: r.top - inner.top,
            left: r.left - inner.left,
            width: r.width,
            height: r.height,
            barTop: Math.max(2, r.top - inner.top - 34),
        };
    }

    /** Clear the image selection overlay. */
    deselectImg() {
        this._selImg = null;
        this.local.imgSel = null;
    }

    /**
     * Apply a preset width to the selected image (`"50%"` … or `null` for
     * the original size). Percentages scale with the recipient's viewport,
     * which survives mail clients better than fixed pixels.
     *
     * @param {string|null} width
     */
    setImgWidth(width) {
        const img = this._selImg;
        if (!img) return;
        if (width) {
            img.style.width = width;
            img.style.height = "auto";
            img.style.maxWidth = "100%";
        } else {
            img.style.width = "";
            img.style.height = "";
        }
        this._syncBody();
        this._selectImg(img); // overlay opnieuw uitlijnen op de nieuwe maat
    }

    /** Remove the selected image from the body. */
    removeSelectedImg() {
        const img = this._selImg;
        if (!img) return;
        img.remove();
        this.deselectImg();
        this._syncBody();
    }

    /**
     * Start a corner-handle drag. Width follows the pointer in pixels and
     * is converted to a body-relative percentage on release.
     *
     * @param {PointerEvent} ev
     */
    onImgHandleDown(ev) {
        if (!this._selImg) return;
        ev.preventDefault();
        ev.stopPropagation();
        this._imgDrag = {
            startX: ev.clientX,
            startWidth: this._selImg.getBoundingClientRect().width,
        };
    }

    _onImgDragMove(ev) {
        if (!this._imgDrag || !this._selImg || !this.bodyRef.el) return;
        const maxW = this.bodyRef.el.clientWidth - 8;
        const w = Math.min(maxW, Math.max(
            40, this._imgDrag.startWidth + (ev.clientX - this._imgDrag.startX)));
        this._selImg.style.width = `${Math.round(w)}px`;
        this._selImg.style.height = "auto";
        this._selectImg(this._selImg);
    }

    _onImgDragEnd() {
        if (!this._imgDrag || !this._selImg || !this.bodyRef.el) {
            this._imgDrag = null;
            return;
        }
        this._imgDrag = null;
        const bodyW = this.bodyRef.el.clientWidth || 1;
        const pct = Math.round(
            (this._selImg.getBoundingClientRect().width / bodyW) * 100);
        this.setImgWidth(pct >= 98 ? "100%" : `${Math.max(5, pct)}%`);
    }

    /**
     * Sanitize the body, then delegate to the mail service to send the
     * message via SMTP and append to the Sent folder.
     *
     * Sanitization (`_sanitizeComposeHtml`) happens here, immediately before
     * the payload leaves the browser, to strip any XSS content the user may
     * have pasted. Does nothing if the "To" field is empty.
     *
     * @async
     */
    async onSend() {
        this.onBodyInput();
        if (!this.props.win.to) return;
        await this.mail.sendCompose(this.props.win);
    }

    /**
     * Sanitize the body and save a draft via the mail service.
     *
     * Sanitization runs before the payload is serialized so that the stored
     * draft body is already clean.
     *
     * @async
     */
    async onDraft() {
        this.onBodyInput();
        await this.mail.saveDraft(this.props.win);
    }

    /**
     * Close this compose window.
     *
     * With unsaved changes the user is asked whether to save a draft first;
     * declining keeps any earlier autosaved copy (safer than deleting it)
     * but discards the changes made since.
     *
     * @async
     */
    async onClose() {
        const win = this.props.win;
        if (win.dirty) {
            this.onBodyInput();
            if (window.confirm(_t("Save this draft before closing?"))) {
                const ok = await this.mail.saveDraft(win);
                if (!ok) {
                    return; // save failed — keep the window open
                }
                return;      // saveDraft already closed the window
            }
        }
        this.mail.closeCompose(win.id);
    }

    /**
     * Toggle the minimized state of the window.
     *
     * When minimized the window collapses to a title-bar strip. Minimizing
     * also clears the expanded flag so the window cannot be simultaneously
     * minimized and expanded.
     */
    toggleMin() {
        this.deselectImg();
        this.props.win.minimized = !this.props.win.minimized;
        if (this.props.win.minimized) this.props.win.expanded = false;
    }

    /**
     * Toggle the expanded (fullscreen) mode of the window.
     *
     * When expanded the window grows to fill more of the viewport. Expanding
     * also clears the minimized flag.
     */
    toggleExpand() {
        this.props.win.expanded = !this.props.win.expanded;
        if (this.props.win.expanded) this.props.win.minimized = false;
    }

    /**
     * Handle the file-input `change` event from the attachment button.
     *
     * Delegates to `_uploadFiles`, then resets the input value so the same
     * file can be attached again if needed.
     *
     * @async
     * @param {Event} ev - Native `change` event from the hidden `<input type="file">`.
     */
    async onAttach(ev) {
        await this._uploadFiles(ev.target.files);
        ev.target.value = "";
    }

    /**
     * Upload one or more files to the server and push the resulting
     * `ir.attachment` metadata objects onto the compose window's attachment
     * list.
     *
     * Sends a `multipart/form-data` POST to `/ow_mail/attachment/upload` for
     * each file (one request per file). On success the server returns a JSON
     * object with at least `{id, name, mimetype, size}`; on failure an
     * in-app `danger` notification is shown and the file is skipped.
     *
     * Uploaded attachments are stored server-side with
     * `res_model='ow.mail.compose'` and `res_id=0` until the message is sent,
     * at which point the SMTP controller pins them to the real message.
     * Orphaned uploads are periodically purged by `_cron_gc_orphan_uploads`.
     *
     * Note: `odoo.csrf_token` is read from the global `odoo` object (Odoo 18
     * no longer exposes the token via `@web/session`).
     *
     * @async
     * @param {FileList|File[]} files - Files to upload.
     */
    async _uploadFiles(files) {
        const notification = this.env.services.notification;
        for (const f of files) {
            const fd = new FormData();
            fd.append("ufile", f);
            // Odoo 18: csrf_token lives on the global, not on @web/session.
            // Mirrors web/static/src/core/file_upload/file_upload_service.js.
            fd.append("csrf_token", odoo.csrf_token);
            const res = await fetch("/ow_mail/attachment/upload",
                                    { method: "POST", body: fd });
            if (!res.ok) {
                let msg = `Upload failed (${res.status})`;
                try {
                    const err = await res.json();
                    if (err.error) msg = err.error;
                } catch (_) {}
                notification.add(msg, { type: "danger" });
                continue;
            }
            const data = await res.json();
            if (data.error) {
                notification.add(data.error, { type: "danger" });
                continue;
            }
            this.props.win.attachments.push(data);
            this.markDirty();
        }
    }

    /**
     * Handle the `dragenter` event on the compose window drop zone.
     *
     * Ignores drags that do not carry files. Increments `_dragCount` (rather
     * than using a simple boolean) to handle the case where `dragenter` fires
     * again on a child element before `dragleave` fires on the parent — a
     * naive boolean would flicker the drop-active state on every nested
     * element boundary.
     *
     * @param {DragEvent} ev
     */
    onDragEnter(ev) {
        if (!ev.dataTransfer || !Array.from(ev.dataTransfer.types || []).includes("Files")) {
            return;
        }
        ev.preventDefault();
        this._dragCount++;
        this.local.dragActive = true;
    }

    /**
     * Handle the `dragover` event to allow dropping files.
     *
     * Sets `dropEffect` to `"copy"` to show the copy cursor.
     *
     * @param {DragEvent} ev
     */
    onDragOver(ev) {
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "copy";
    }

    /**
     * Handle the `dragleave` event.
     *
     * Decrements `_dragCount`; only clears `dragActive` when the counter
     * reaches zero, meaning the drag has left the outermost drop-zone element
     * (not just crossed an internal child boundary).
     *
     * @param {DragEvent} ev
     */
    onDragLeave(ev) {
        ev.preventDefault();
        this._dragCount = Math.max(0, this._dragCount - 1);
        if (this._dragCount === 0) this.local.dragActive = false;
    }

    /**
     * Handle the `drop` event: upload all dropped files.
     *
     * Resets `_dragCount` and `dragActive` unconditionally (the drag is over
     * regardless of counter state), then delegates to `_uploadFiles`.
     *
     * @async
     * @param {DragEvent} ev
     */
    async onDrop(ev) {
        ev.preventDefault();
        this._dragCount = 0;
        this.local.dragActive = false;
        const files = ev.dataTransfer.files;
        if (files && files.length) {
            await this._uploadFiles(files);
        }
    }

    /**
     * Remove an attachment from the compose window's attachment list.
     *
     * Filters by `id`. Does **not** delete the corresponding `ir.attachment`
     * record on the server — orphan cleanup is handled by
     * `_cron_gc_orphan_uploads`.
     *
     * @param {number} id - `ir.attachment` id to remove.
     */
    removeAttachment(id) {
        this.props.win.attachments = this.props.win.attachments.filter((a) => a.id !== id);
        this.markDirty();
    }

    /**
     * Thin wrapper around `document.execCommand` that saves/restores the
     * selection and syncs the body state afterward.
     *
     * Selection save/restore is necessary because toolbar buttons are outside
     * the `contenteditable`; the act of clicking them moves focus and
     * collapses the selection before `execCommand` fires.
     *
     * Note: `execCommand` is deprecated but is kept intentionally — the
     * compose window is "intentionally minimal" and upgrading to a full
     * WYSIWYG editor would require a real `fields.Html`-backed ORM record.
     *
     * @param {string} cmd - `execCommand` command name (e.g. `"bold"`, `"insertHTML"`).
     * @param {string|null} [value=null] - Optional command value.
     */
    exec(cmd, value = null) {
        document.execCommand(cmd, false, value);
        this.onBodyInput();
    }

    /**
     * Apply a block-level format (h1, h2, p, blockquote, etc.) to the
     * current selection via `execCommand("formatBlock")`.
     *
     * Resets the `<select>` back to `"p"` after applying so that the control
     * always shows a neutral state rather than the last-chosen value.
     *
     * @param {Event} ev - `change` event from the format-block `<select>`.
     */
    onFormatBlock(ev) {
        const tag = ev.target.value;
        if (tag) this.exec("formatBlock", `<${tag}>`);
        ev.target.value = "p";
    }

    /**
     * Apply a font size (1–7, legacy HTML `size` attribute) to the current
     * selection via `execCommand("fontSize")`.
     *
     * Resets the `<select>` to an empty value afterward.
     *
     * @param {Event} ev - `change` event from the font-size `<select>`.
     */
    onFontSize(ev) {
        const size = ev.target.value;
        if (size) this.exec("fontSize", size);
        ev.target.value = "";
    }

    /**
     * Apply a foreground text color to the current selection via
     * `execCommand("foreColor")`.
     *
     * @param {Event} ev - `input` or `change` event from the color `<input type="color">`.
     */
    onTextColor(ev) {
        this.exec("foreColor", ev.target.value);
    }

    /**
     * Apply a background highlight color to the current selection via
     * `execCommand("hiliteColor")`.
     *
     * @param {Event} ev - `input` or `change` event from the highlight `<input type="color">`.
     */
    onHighlight(ev) {
        this.exec("hiliteColor", ev.target.value);
    }

    /**
     * Prompt the user for a URL and insert a hyperlink around the current
     * selection via `execCommand("createLink")`.
     *
     * Uses the browser-native `window.prompt()` dialog (not an Odoo dialog)
     * to keep the compose window intentionally minimal.
     *
     * The entered URL is validated against a scheme allowlist (`http:`,
     * `https:`, `mailto:`, `tel:`). Dangerous schemes (`javascript:`,
     * `vbscript:`, `file:`) are rejected with a warning notification to
     * prevent self-XSS when the link is clicked in a saved/forwarded draft.
     */
    insertLink() {
        const url = window.prompt("Link URL");
        if (!url) return;
        // Scheme whitelist — block javascript:/vbscript:/file: which would
        // turn an accidental paste into self-XSS the moment the link is
        // clicked in the saved/forwarded draft.
        if (!/^(?:https?|mailto|tel):/i.test(url.trim())) {
            this.env.services.notification.add(
                "Link URL must start with http:, https:, mailto: or tel:.",
                { type: "warning" });
            return;
        }
        this.exec("createLink", url.trim());
    }

    /**
     * Handle `mousedown` on images inside the compose body.
     *
     * Saves the current selection before the mousedown event can steal focus
     * away from the `contenteditable`. This preserves the insertion point so
     * that subsequent `execCommand` calls (e.g. triggered by resize handles)
     * still operate on the correct position.
     */
    onImageMousedown() {
        this._saveSelection();
    }

    /**
     * Handle image file selection and insert the image into the compose body.
     *
     * Reads the selected file as a `data:` URL via `FileReader`, restores the
     * saved selection (which was captured on the image-picker button's
     * `mousedown`), and inserts an `<img>` tag at the cursor position via
     * `execCommand("insertHTML")`.
     *
     * Only `image/*` MIME types are accepted; the input value is cleared after
     * processing so the same file can be re-inserted.
     *
     * @async
     * @param {Event} ev - `change` event from the hidden image `<input type="file">`.
     */
    async onInsertImage(ev) {
        const file = ev.target.files && ev.target.files[0];
        ev.target.value = "";
        if (!file || !file.type.startsWith("image/")) return;
        this._restoreSelection();
        await this._insertImageFile(file);
    }

    /**
     * Insert the active account's signature HTML at the end of the compose
     * body.
     *
     * Moves the cursor to the end of the `contenteditable` content, then
     * inserts two line breaks followed by `acc.signature_html` via
     * `execCommand("insertHTML")`. Does nothing if the account has no
     * signature or if the body ref is not yet mounted.
     */
    onInsertSignature() {
        const acc = this.state.accounts.find(
            (a) => a.id === Number(this.props.win.accountId)
        );
        if (!acc || !acc.signature_html) {
            return;
        }
        if (!this.bodyRef.el) {
            return;
        }

        // Move cursor to end
        this.bodyRef.el.focus();
        const sel = window.getSelection();
        if (sel) {
            try {
                sel.selectAllChildren(this.bodyRef.el);
                sel.collapseToEnd();
            } catch (e) {}
        }
        this.exec("insertHTML", "<br/><br/>" + acc.signature_html);
    }
}
