/** @odoo-module **/

import { Component, onPatched, useEffect, useRef, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { owColor } from "../../utils/colors";
import { SafeModeBanner } from "../safe_mode_banner/safe_mode_banner";
import { AvatarInitials } from "../avatar_initials/avatar_initials";
import { MessageSourceDialog } from "../message_source/message_source";

const BLANK_PIXEL = "data:image/gif;base64,R0lGODlhAQABAAAAACw=";
const XLINK_NS = "http://www.w3.org/1999/xlink";
const URL_ATTRS = ["src", "srcset", "href", "poster", "background",
                   "data", "action", "formaction", "ping"];
const REMOTE_SCHEME_RX = /^\s*(?:https?:)?\/\//i;
const CSS_URL_REMOTE_RX = /url\s*\(\s*(["']?)\s*(?:https?:)?\/\/[^"')]*\1\s*\)/gi;
const CSS_IMPORT_REMOTE_RX = /@import\s+(?:url\()?\s*["']?(?:https?:)?\/\/[^"');]*["']?\)?\s*;?/gi;

/**
 * Strip external URL references from a CSS string.
 *
 * Removes `@import` rules that reference remote hosts and replaces
 * `url(https?://…)` values with the transparent-GIF placeholder. This
 * prevents tracking/XSS vectors hidden inside `background-image` or
 * `@import` inside inline `style` attributes and `<style>` blocks.
 *
 * @param {string} css - Raw CSS text (inline style value or `<style>` block).
 * @returns {string} CSS with remote `@import` rules removed and remote
 *   `url()` values replaced with a transparent-GIF placeholder.
 */
function _stripRemoteCss(css) {
    return css
        .replace(CSS_IMPORT_REMOTE_RX, "")
        .replace(CSS_URL_REMOTE_RX, `url("${BLANK_PIXEL}")`);
}

/**
 * Heuristic to classify an `<img>` element as a tracking pixel.
 *
 * Beacon pixels are almost always 1×1 (or at most a few pixels). The check
 * inspects the `width`/`height` HTML attributes and inline `style` for
 * dimensions ≤ 5 px. This lets `stripRemote` count trackers separately so the
 * SafeModeBanner can say "N tracking pixels blocked" rather than a generic
 * message.
 *
 * Limitation: only inline dimension attributes and `style` are evaluated —
 * computed CSS (e.g. `width` set by a class rule) is not visible here because
 * the element lives in a detached DOMParser document, not a live browser
 * layout. `stripRemote` still blocks the remote URL regardless of this flag.
 *
 * @param {Element} el - A DOM element to test; returns `false` immediately for
 *   non-`<img>` elements.
 * @returns {boolean} `true` when `el` looks like a 1×1 tracking pixel.
 */
function _isTrackingPixel(el) {
    if (el.tagName.toLowerCase() !== "img") return false;
    const w = parseInt(el.getAttribute("width") || "", 10);
    const h = parseInt(el.getAttribute("height") || "", 10);
    if ((!isNaN(w) && w <= 5) || (!isNaN(h) && h <= 5)) return true;
    const style = el.getAttribute("style") || "";
    return /(?:width|height)\s*:\s*[012345](?:\.\d+)?\s*(?:px|;|$)/i.test(style);
}

/**
 * Defense-in-depth client-side remote content blocker.
 *
 * Server-side `sanitize_and_detect` (lxml) is the primary detection layer and
 * sets `has_remote_content` on the message. This function is the actual
 * blocking step: it runs at display time, just before the HTML is injected
 * into the iframe srcdoc, so even content that slips through server-side
 * processing never makes an outbound request.
 *
 * The document is parsed with `DOMParser` rather than processed with regex so
 * that attribute boundaries are always respected — naive regex substitution on
 * raw HTML strings leaked the original tracker hostname (see review
 * findings C#2 / C#7).
 *
 * Actions taken per element:
 * - `<link>`, `<object>`, `<embed>` with remote URLs: URL attributes removed.
 * - All other elements: remote URLs in `src`, `srcset`, `href`, `poster`,
 *   `background`, `data`, `action`, `formaction`, `ping`, and SVG
 *   `xlink:href` are replaced with `BLANK_PIXEL`.
 * - Inline `style` attributes and `<style>` blocks: processed by
 *   `_stripRemoteCss` to remove `@import` rules and `url()` references.
 *
 * @param {string} html - Raw HTML string (message body).
 * @returns {{ html: string, trackers: number, remoteItems: number }}
 *   `html` is the scrubbed HTML; `trackers` counts heuristically identified
 *   tracking pixels; `remoteItems` counts all other blocked resources.
 */
function stripRemote(html) {
    const doc = new DOMParser().parseFromString(html, "text/html");
    let trackers = 0;
    let remoteItems = 0;
    const markBlocked = (el, isTracker) => {
        el.setAttribute("data-ow-blocked", "1");
        if (isTracker) trackers += 1;
        else remoteItems += 1;
    };
    for (const el of doc.querySelectorAll("*")) {
        const tag = el.tagName.toLowerCase();
        // Drop externally-fetched structural tags entirely
        if (tag === "link" || tag === "object" || tag === "embed") {
            const anyUrl = el.getAttribute("href") || el.getAttribute("src") ||
                           el.getAttribute("data") || "";
            if (REMOTE_SCHEME_RX.test(anyUrl)) {
                markBlocked(el, false);
                el.removeAttribute("href");
                el.removeAttribute("src");
                el.removeAttribute("data");
            }
            continue;
        }
        let didBlock = false;
        // URL-bearing attrs in the HTML namespace
        for (const attr of URL_ATTRS) {
            const val = el.getAttribute(attr);
            if (val && REMOTE_SCHEME_RX.test(val)) {
                el.setAttribute(attr, BLANK_PIXEL);
                didBlock = true;
            }
        }
        // SVG xlink:href
        const xlink = el.getAttributeNS(XLINK_NS, "href");
        if (xlink && REMOTE_SCHEME_RX.test(xlink)) {
            el.setAttributeNS(XLINK_NS, "href", BLANK_PIXEL);
            didBlock = true;
        }
        // Inline style="background-image:url(...)"
        const style = el.getAttribute("style");
        if (style && /url\s*\(/i.test(style)) {
            const scrubbed = _stripRemoteCss(style);
            if (scrubbed !== style) {
                el.setAttribute("style", scrubbed);
                didBlock = true;
            }
        }
        if (didBlock) markBlocked(el, _isTrackingPixel(el));
    }
    // <style> blocks: rewrite remote @import and url()
    for (const styleEl of doc.querySelectorAll("style")) {
        const css = styleEl.textContent || "";
        const scrubbed = _stripRemoteCss(css);
        if (scrubbed !== css) {
            styleEl.textContent = scrubbed;
            remoteItems += 1;
        }
    }
    const outHtml = doc.body ? doc.body.innerHTML : doc.documentElement.outerHTML;
    return { html: outHtml, trackers, remoteItems };
}

/**
 * Main message-reading component for `ow_mail`.
 *
 * Displays a single email (or a collapsed thread) fetched live from IMAP.
 * The message body is rendered inside a sandboxed `<iframe srcdoc>` with
 * `sandbox="allow-same-origin"` — no scripts are executed.
 * `allow-same-origin` is required so the parent can read back
 * `contentDocument.body.scrollHeight` for auto-sizing and call
 * `contentWindow.print()` without a cross-origin error.  Using `srcdoc`
 * instead of a `src` URL keeps the content in the same origin and avoids a
 * second network round-trip.
 *
 * **Safe mode / remote content blocking**
 *
 * When `has_remote_content` is `true` and the sender is not trusted,
 * `stripRemote()` rewrites all remote URLs to a transparent-GIF placeholder
 * before the body is written to the iframe.  The `SafeModeBanner` shows above
 * the iframe in this state.  The user has two escapes:
 * - "Show once" (`onShowOnce`) — sets `local.showRemote = true` for this
 *   session only; not persisted.
 * - "Always trust" (`onTrustSender`) — calls the server `trust_sender` action
 *   which persists an `ow.mail.trusted.sender` record for the sender's domain.
 *   On the next open the server returns `trusted: true` and `stripRemote` is
 *   skipped entirely.
 *
 * **Thread view**
 *
 * When `this.mail.state.threadMessages` contains more than one message the
 * component switches to thread view.  Each thread message gets its own
 * `<iframe>` rendered by `_renderThreadIframes()`, which is called from
 * `onPatched` so it runs after OWL has flushed the DOM.  Individual messages
 * can be collapsed / expanded via `toggleThreadMsg`.
 */
export class MessageViewer extends Component {
    static template = "ow_mail.MessageViewer";
    static components = { SafeModeBanner, AvatarInitials };

    /**
     * OWL lifecycle hook — initialises services and reactive state.
     *
     * Services wired up: `ow_mail` (central mail store), `action` (Odoo
     * action manager), `dialog` (modal service).
     *
     * Reactive state:
     * - `this.state` — alias for `this.mail.state` via `useState`; changes
     *   here propagate to the shared service store.
     * - `this.local` — component-local state: `showRemote` (session-only
     *   remote-content override) and `expandedThread` (map of message key →
     *   boolean for thread collapse/expand).
     *
     * Effects:
     * - First `useEffect` on `[selectedMessage, showRemote]`: re-renders the
     *   main iframe body whenever the displayed message or the remote-content
     *   toggle changes.  Skipped in thread view (each thread iframe is managed
     *   by `_renderThreadIframes`).
     * - Second `useEffect` on `[threadMessages]`: resets `expandedThread` when
     *   the thread changes, keeping only keys that exist in the new thread and
     *   defaulting to the latest message when nothing is already expanded.
     *   Without this reset, stale keys from thread A would prevent thread B
     *   from rendering until the user manually expanded a message.
     * - `onPatched`: calls `_renderThreadIframes` after every DOM patch so
     *   newly-mounted thread iframes get their `srcdoc` written.
     */
    setup() {
        this.mail = useService("ow_mail");
        this.action = useService("action");
        this.dialog = useService("dialog");
        this.state = useState(this.mail.state);
        this.local = useState({ showRemote: false, expandedThread: {} });
        this.iframeRef = useRef("iframe");

        useEffect(
            (msg, showRemote) => {
                if (!this.isThreadView) {
                    this.renderBody(msg, showRemote);
                }
            },
            () => [this.state.selectedMessage, this.local.showRemote]
        );

        // When threadMessages changes, reset the expand-state to just the
        // latest message of the new thread. Without the reset, switching
        // from thread A to thread B would keep A's expanded keys in state —
        // none of which match B's messages — so nothing renders until the
        // user manually clicks a header.
        useEffect(
            (threadMsgs) => {
                if (!threadMsgs || threadMsgs.length <= 1) return;
                const latest = threadMsgs[threadMsgs.length - 1];
                const latestKey = `${latest.folder_id}:${latest.uid}`;
                const validKeys = new Set(threadMsgs.map(
                    (m) => `${m.folder_id}:${m.uid}`));
                const next = {};
                for (const k of Object.keys(this.local.expandedThread)) {
                    if (validKeys.has(k)) next[k] = true;
                }
                if (!Object.keys(next).length) {
                    next[latestKey] = true;
                }
                this.local.expandedThread = next;
            },
            () => [this.state.threadMessages]
        );

        // onPatched fires after every DOM patch, so whenever expandedThread
        // or threadMessages changes and the iframe element (re-)appears in
        // the DOM, we paint its srcdoc. _renderThreadIframes is idempotent
        // (skips elements whose dataset.rendered is already "1").
        onPatched(() => {
            if (this.isThreadView) {
                this._renderThreadIframes();
            }
        });
    }

    /**
     * Whether the service has a multi-message thread loaded.
     *
     * When `true` the template renders the thread-view layout (one collapsible
     * row per message) instead of the single-message layout.
     *
     * @returns {boolean}
     */
    get isThreadView() {
        return this.state.threadMessages && this.state.threadMessages.length > 1;
    }

    /**
     * Clean subject line for the thread header.
     *
     * Taken from the first (oldest) message in the thread with common reply/
     * forward prefixes (`Re:`, `Fwd:`, `Fw:`) stripped so the header shows the
     * original subject rather than a deeply nested "Re: Re: Re: …".
     *
     * @returns {string}
     */
    get threadSubject() {
        const msgs = this.state.threadMessages;
        if (!msgs || !msgs.length) return "";
        return (msgs[0].subject || "").replace(/^(re|fwd|fw)\s*:\s*/i, "");
    }

    /**
     * Whether a thread message is currently expanded (body visible).
     *
     * @param {{ folder_id: number, uid: number }} tm - Thread message object.
     * @returns {boolean}
     */
    isThreadMsgExpanded(tm) {
        return !!this.local.expandedThread[`${tm.folder_id}:${tm.uid}`];
    }

    /**
     * Toggle the collapsed/expanded state of a single thread message.
     *
     * Mutates `local.expandedThread` which triggers an OWL reactive re-render.
     * `onPatched` then calls `_renderThreadIframes` to write the `srcdoc` of
     * any newly-visible iframe after the DOM has been updated.
     *
     * @param {{ folder_id: number, uid: number }} tm - Thread message to toggle.
     */
    toggleThreadMsg(tm) {
        const key = `${tm.folder_id}:${tm.uid}`;
        if (this.local.expandedThread[key]) {
            delete this.local.expandedThread[key];
        } else {
            this.local.expandedThread[key] = true;
        }
        // onPatched renders the iframe once OWL has flushed the DOM.
    }

    /**
     * Inject HTML bodies into all visible thread-message iframes.
     *
     * Called from `onPatched` after every DOM patch so that iframes which just
     * became visible (because the user expanded a message row) receive their
     * `srcdoc`.  The method is idempotent: elements whose `dataset.rendered`
     * is already `"1"` are skipped, avoiding redundant re-injection.
     *
     * Safe-mode stripping is applied per-message: if `has_remote_content` is
     * set and neither `local.showRemote` nor `tm.trusted` is truthy, the body
     * is passed through `stripRemote` before injection.
     *
     * After the iframe loads, its height is clamped to
     * `[100 px, 600 px]` based on the inner document's `scrollHeight` to
     * avoid collapsed or infinitely-tall frames.
     */
    _renderThreadIframes() {
        for (const tm of this.state.threadMessages || []) {
            const key = `${tm.folder_id}:${tm.uid}`;
            if (!this.local.expandedThread[key]) continue;
            const el = document.querySelector(`[data-thread-iframe="${key}"]`);
            if (!el || el.dataset.rendered === "1") continue;
            el.dataset.rendered = "1";
            let html = tm.html || `<pre>${(tm.text || "").replace(/</g, "&lt;")}</pre>`;
            if (tm.has_remote_content && !this.local.showRemote && !tm.trusted) {
                html = stripRemote(html).html;
            }
            const doc = `<!doctype html><html><head><meta charset="utf-8"/>
                <base target="_blank"/>
                <style>body{font-family:system-ui,sans-serif;padding:12px;color:#222;}img{max-width:100%;}a{color:#0d6efd;}</style>
                </head><body>${html}</body></html>`;
            el.setAttribute("srcdoc", doc);
            // Auto-size iframe after load
            el.onload = () => {
                try {
                    const h = el.contentDocument.body.scrollHeight;
                    el.style.height = Math.min(Math.max(h + 20, 100), 600) + "px";
                } catch {}
            };
        }
    }

    /**
     * Format an ISO date string for display in thread message headers.
     *
     * @param {string} iso - ISO 8601 date string from the message envelope.
     * @returns {string} Locale-formatted date + short time, or `""` if absent.
     */
    formatThreadDate(iso) {
        if (!iso) return "";
        const d = new Date(iso);
        return d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
    }

    /**
     * Inject the sanitized message body into the main single-message iframe.
     *
     * `srcdoc` is used instead of a `src` URL for two reasons: (1) no extra
     * network request is needed — the HTML was already fetched via the
     * `/ow_mail/message` endpoint; (2) the iframe stays in the same origin,
     * which is required for `contentWindow.print()` in `onPrint`.
     *
     * When safe mode is active (`has_remote_content && !showRemote && !trusted`)
     * the body is passed through `stripRemote` before injection.
     *
     * A hidden `ow-print-header` div (with From/To/Subject/Date) is embedded
     * in the document so that printing the iframe produces a complete header
     * block without cluttering the on-screen view.
     *
     * @param {{ html?: string, text?: string, has_remote_content?: boolean,
     *           trusted?: boolean, subject?: string, from_name?: string,
     *           from_email?: string, to?: string, cc?: string,
     *           date?: string }} msg - The selected message object.
     * @param {boolean} showRemote - Whether the user has allowed remote content
     *   for this session (set by `onShowOnce`).
     */
    renderBody(msg, showRemote) {
        const iframe = this.iframeRef.el;
        if (!iframe || !msg) return;
        let html = msg.html || `<pre>${(msg.text || "").replace(/</g, "&lt;")}</pre>`;
        if (msg.has_remote_content && !showRemote && !msg.trusted) {
            html = stripRemote(html).html;
        }
        const esc = (s) => String(s || "").replace(/[&<>"]/g, (c) =>
            ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
        const printDate = msg.date ? new Date(msg.date).toLocaleString() : "";
        const header = `
            <div class="ow-print-header">
                <h2 style="margin:0 0 8px 0;font-size:18px;">${esc(msg.subject)}</h2>
                <div><strong>From:</strong> ${esc(msg.from_name)} &lt;${esc(msg.from_email)}&gt;</div>
                ${msg.to ? `<div><strong>To:</strong> ${esc(msg.to)}</div>` : ""}
                ${msg.cc ? `<div><strong>Cc:</strong> ${esc(msg.cc)}</div>` : ""}
                ${printDate ? `<div><strong>Date:</strong> ${esc(printDate)}</div>` : ""}
            </div>`;
        const doc = `<!doctype html><html><head><meta charset="utf-8"/>
            <base target="_blank"/>
            <style>
                body{font-family:system-ui,sans-serif;padding:12px;color:#222;}
                img{max-width:100%;}a{color:#0d6efd;}
                .ow-print-header{display:none;}
                @media print{
                    .ow-print-header{
                        display:block;
                        margin-bottom:16px;
                        padding-bottom:8px;
                        border-bottom:1px solid #ccc;
                        font-size:12px;
                        color:#333;
                    }
                }
            </style>
            </head><body>${header}${html}</body></html>`;
        iframe.setAttribute("srcdoc", doc);
    }

    /**
     * Allow remote content for this viewing session only.
     *
     * Sets `local.showRemote = true`, which causes the reactive `useEffect` on
     * `selectedMessage` / `showRemote` to re-fire and re-render the body
     * without calling `stripRemote`.  The preference is not persisted — it
     * resets the next time a message is opened.
     */
    onShowOnce() {
        this.local.showRemote = true;
    }

    /**
     * Permanently trust the sender and re-render with remote content visible.
     *
     * Trust chain:
     * 1. Calls the `trust_sender` action via `this.mail.runAction`, which
     *    POSTs to `/ow_mail/message/action`.
     * 2. The server creates an `ow.mail.trusted.sender` record scoped to the
     *    current user.
     * 3. `local.showRemote` is set to `true` so the current view re-renders
     *    immediately without waiting for the next message open.
     * 4. On the next open, the server sees the trust record and returns
     *    `trusted: true` on the message object, so `stripRemote` is skipped
     *    entirely at the source.
     */
    async onTrustSender() {
        const m = this.state.selectedMessage;
        if (!m) return;
        await this.mail.runAction(m.folder_id, [m.uid], "trust_sender",
            { emails: [m.from_email] });
        this.local.showRemote = true;
    }

    /**
     * Find the account that owns the currently displayed message.
     *
     * Walks `state.accounts` and checks whether any of their cached folders
     * matches `selectedMessage.folder_id`.  Falls back to the first account if
     * no match is found (e.g. during an in-flight folder refresh).
     *
     * @returns {object|null} Account object, or `null` when no message is selected.
     */
    _findAccount() {
        const m = this.state.selectedMessage;
        if (!m) return null;
        return this.state.accounts.find((a) =>
            a.folders.some((f) => f.id === m.folder_id)
        ) || this.state.accounts[0];
    }

    /**
     * Open a compose window pre-filled for replying to the selected message.
     *
     * Delegates to `this.mail.openReply`; the service handles quoting,
     * address pre-fill (Reply-To / From / Cc depending on `all`), and
     * floating window lifecycle.
     *
     * @param {boolean} [all=false] - When `true`, Reply-All is used and all
     *   original recipients are included in Cc.
     */
    onReply(all = false) {
        this.mail.openReply(this.state.selectedMessage, { all });
    }

    /**
     * Open a compose window pre-filled for forwarding the selected message.
     *
     * Reads `this.state.selectedMessage` and passes it to `this.mail.openForward`;
     * the service inlines the original body as a blockquote. Original
     * attachments are not re-attached — the compose window starts with an
     * empty attachment list.
     */
    onForward() {
        this.mail.openForward(this.state.selectedMessage);
    }

    /**
     * Open the `MessageSourceDialog` modal to inspect the raw RFC 822 source.
     *
     * Reads `folder_id`, `uid`, and `subject` from `this.state.selectedMessage`.
     * The dialog fetches the full message source from the server when it mounts.
     * Useful for debugging headers, DKIM, or sanitization issues.
     */
    onViewSource() {
        const m = this.state.selectedMessage;
        if (!m) return;
        this.dialog.add(MessageSourceDialog, {
            folderId: m.folder_id,
            uid: m.uid,
            subject: m.subject || "",
        });
    }

    /**
     * Format the date/time range of a calendar invite for display.
     *
     * Reads `dtstart_iso` and (optionally) `dtend_iso` from the parsed ICS
     * data on the selected message.  Both dates are formatted with full date
     * style and short time; an em-dash separates start and end when a range
     * is present.
     *
     * @returns {string} Human-readable date range, or `""` when no invite is
     *   present or `dtstart_iso` is missing.
     */
    formatInviteWhen() {
        const inv = this.state.selectedMessage && this.state.selectedMessage.invite;
        if (!inv || !inv.dtstart_iso) return "";
        const fmt = (iso) => {
            const d = new Date(iso);
            if (isNaN(d.getTime())) return iso;
            return d.toLocaleString([], { dateStyle: "full", timeStyle: "short" });
        };
        if (inv.dtend_iso) return `${fmt(inv.dtstart_iso)} — ${fmt(inv.dtend_iso)}`;
        return fmt(inv.dtstart_iso);
    }

    /**
     * Create an Odoo calendar event from the ICS data in the selected message.
     *
     * Reads `folder_id` and `uid` from `this.state.selectedMessage`. Calls
     * `this.mail.addInviteToCalendar` which POSTs to the server; the server
     * parses the ICS attachment and returns an `ir.actions.act_window` action
     * that opens the newly-created event form. The action is then executed via
     * the Odoo action manager so the user lands on the event.
     */
    async onAddToCalendar() {
        const m = this.state.selectedMessage;
        if (!m || !m.invite) return;
        const action = await this.mail.addInviteToCalendar(m.folder_id, m.uid);
        if (action) {
            await this.action.doAction(action);
        }
    }

    /**
     * Build the download URL for the raw ICS file attached to the message.
     *
     * Routes to the `GET /ow_mail/attachment/<folder_id>/<uid>/<section>`
     * controller with `?download=1` so the browser triggers a file download
     * rather than attempting to display the content inline.
     *
     * @returns {string} Absolute-path URL, or `""` when no invite or no MIME
     *   section reference is available on the message.
     */
    inviteIcsUrl() {
        const m = this.state.selectedMessage;
        if (!m || !m.invite || !m.invite.section) return "";
        return `/ow_mail/attachment/${m.folder_id}/${m.uid}/${m.invite.section}?download=1`;
    }

    /**
     * Open the "Create Record" wizard for the currently displayed message.
     */
    async onCreateRecord() {
        const msg = this.state.selectedMessage;
        if (!msg) return;

        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "ow.mail.create.record",
            views: [[false, "form"]],
            target: "new",
            context: {
                default_folder_id: msg.folder_id,
                default_uid: msg.uid,
                default_subject: msg.subject,
                default_from_name: msg.from_name,
                default_from_email: msg.from_email,
                default_date: msg.date,
                default_body_html: msg.html,
            },
        });
    }

    /**
     * Open the "Attach to Record" wizard for the currently displayed message.
     */
    async onAttach() {
        const msg = this.state.selectedMessage;
        if (!msg) return;

        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "ow.mail.attach.record",
            views: [[false, "form"]],
            target: "new",
            context: {
                default_folder_id: msg.folder_id,
                default_uid: msg.uid,
                default_subject: msg.subject,
                default_from_name: msg.from_name,
                default_from_email: msg.from_email,
                default_date: msg.date,
                default_body_html: msg.html,
                default_has_attachments: msg.attachments && msg.attachments.length > 0,
            },
        });
    }

    /**
     * Print the message by triggering the iframe's print dialog.
     *
     * In thread view, delegates to `_printThreadView` which assembles all
     * expanded thread messages into a dedicated print window. For a single
     * message, focuses the iframe's `contentWindow` first so the browser
     * print dialog targets only the message content rather than the full
     * Odoo shell. If accessing `contentWindow` throws (cross-origin
     * restriction in some edge cases), falls back to `window.print()`.
     *
     * The iframe document includes a hidden `.ow-print-header` block (injected
     * by `renderBody`) that becomes visible only in `@media print`, providing
     * From / To / Subject / Date context in the printed output.
     */
    onPrint() {
        if (this.isThreadView) {
            this._printThreadView();
            return;
        }

        const iframe = this.iframeRef.el;
        if (!iframe) {
            window.print();
            return;
        }

        try {
            iframe.contentWindow.focus();
            iframe.contentWindow.print();
        } catch (e) {
            window.print();
        }
    }

    /**
     * Print all expanded messages of the current thread.
     *
     * Collects the rendered bodies from the per-message thread iframes and
     * writes them, with a From/To/Cc/Date header per message, into a new
     * window that is then printed. Falls back to `window.print()` when a
     * popup cannot be opened.
     */
    _printThreadView() {
        const printWindow = window.open("", "_blank");
        if (!printWindow) {
            window.print();
            return;
        }

        const messages = [];
        for (const tm of this.state.threadMessages || []) {
            const key = `${tm.folder_id}:${tm.uid}`;
            if (!this.local.expandedThread[key]) continue;

            const iframe = document.querySelector(`[data-thread-iframe="${key}"]`);
            let bodyHtml = "";

            try {
                bodyHtml = iframe?.contentDocument?.body?.innerHTML || "";
            } catch (e) {
                // Iframe not readable (edge-case cross-origin); print an empty body.
            }

            messages.push(`
                <section class="ow-print-thread-message">
                    <h2>${this._escapeHtml(tm.subject || this.threadSubject || "")}</h2>
                    <div class="ow-print-meta">
                        <div><strong>From:</strong> ${this._escapeHtml(tm.from_name || "")} &lt;${this._escapeHtml(tm.from_email || "")}&gt;</div>
                        ${tm.to ? `<div><strong>To:</strong> ${this._escapeHtml(tm.to)}</div>` : ""}
                        ${tm.cc ? `<div><strong>Cc:</strong> ${this._escapeHtml(tm.cc)}</div>` : ""}
                        ${tm.date ? `<div><strong>Date:</strong> ${this._escapeHtml(this.formatThreadDate(tm.date))}</div>` : ""}
                    </div>
                    <div class="ow-print-body">
                        ${bodyHtml}
                    </div>
                </section>
            `);
        }

        if (!messages.length) {
            printWindow.close();
            return;
        }

        printWindow.document.open();
        printWindow.document.write(`<!doctype html>
            <html>
                <head>
                    <meta charset="utf-8"/>
                    <title>${this._escapeHtml(this.threadSubject || "Print message")}</title>
                    <style>
                        body {
                            font-family: system-ui, sans-serif;
                            color: #222;
                            padding: 24px;
                        }
                        img {
                            max-width: 100%;
                        }
                        a {
                            color: #0d6efd;
                        }
                        .ow-print-thread-message {
                            margin-bottom: 32px;
                            padding-bottom: 24px;
                            border-bottom: 1px solid #ccc;
                            break-inside: avoid;
                        }
                        .ow-print-thread-message h2 {
                            margin: 0 0 8px 0;
                            font-size: 18px;
                        }
                        .ow-print-meta {
                            margin-bottom: 16px;
                            font-size: 12px;
                            color: #333;
                        }
                    </style>
                </head>
                <body>
                    ${messages.join("")}
                </body>
            </html>`);
        printWindow.document.close();

        printWindow.focus();
        printWindow.print();
    }

    /**
     * Escape a value for safe interpolation into the print window's HTML.
     *
     * @param {*} value
     * @returns {string}
     */
    _escapeHtml(value) {
        return String(value || "").replace(/[&<>"]/g, (c) =>
            ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
        );
    }

    /**
     * Switch to the contacts view and open the existing `res.partner` record
     * linked to the message sender.
     *
     * Reads `partner_id` from `this.state.selectedMessage`. Only available
     * when the server has resolved `partner_id` (i.e. the sender's email
     * matched an existing partner).
     */
    async onOpenContact() {
        const m = this.state.selectedMessage;
        if (!m || !m.partner_id) return;
        await this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "res.partner",
            res_id: m.partner_id,
            views: [[false, "form"]],
            target: "current",
        });
    }

    /**
     * Open a new-record form to create an `ow.mail.contact` from the sender.
     *
     * Reads `from_name` and `from_email` from `this.state.selectedMessage`
     * and pre-fills them so the user only needs to review and save. Opens in
     * a modal (`target: "new"`) to avoid navigating away from the mailbox.
     */
    async onSaveContact() {
        const m = this.state.selectedMessage;
        if (!m || !m.from_email) return;
        await this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "ow.mail.contact",
            views: [[false, "form"]],
            target: "new",
            context: {
                default_name: m.from_name || "",
                default_email: m.from_email,
            },
        });
    }

    /**
     * Open a new-record form to create or update a `res.partner` from the sender.
     *
     * Reads `from_name` and `from_email` from `this.state.selectedMessage`.
     * Similar to `onSaveContact` but targets the core Odoo partner model,
     * making the sender available across the whole Odoo instance (CRM, Sales,
     * etc.). Opens in a modal to avoid navigating away from the mailbox.
     */
    async onSavePartner() {
        const m = this.state.selectedMessage;
        if (!m || !m.from_email) return;
        await this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "res.partner",
            views: [[false, "form"]],
            target: "new",
            context: {
                default_name: m.from_name || "",
                default_email: m.from_email,
            },
        });
    }

    /**
     * Whether the selected message is stored in a Drafts folder.
     *
     * Used to swap the toolbar between "Reply / Forward" (normal messages)
     * and "Edit draft" (draft messages).
     *
     * @returns {boolean}
     */
    get isDraft() {
        const m = this.state.selectedMessage;
        return m && m.folder_kind === "drafts";
    }

    /**
     * Re-open a draft message in a floating compose window for editing.
     *
     * Reads `folder_id` and `uid` from `this.state.selectedMessage`. Delegates
     * to `this.mail.editDraft` which fetches the draft body from IMAP and
     * pre-populates a new `ComposeWindow` instance.
     */
    onEditDraft() {
        const m = this.state.selectedMessage;
        if (!m) return;
        this.mail.editDraft(m.folder_id, m.uid);
    }

    /**
     * Whether the open message currently carries the given tag.
     * @param {number} tagId
     * @returns {boolean}
     */
    messageHasTag(tagId) {
        const m = this.state.selectedMessage;
        return Boolean(m && (m.tag_ids || []).includes(tagId));
    }

    /**
     * Inline style for a pill in the viewer tag strip: filled with the tag
     * color when active, neutral outline otherwise.
     * @param {{ id: number, color: number }} tag
     * @returns {string}
     */
    tagToggleStyle(tag) {
        const c = owColor(tag.color);
        if (this.messageHasTag(tag.id)) {
            return `background: ${c}; border-color: ${c}; color: #fff;`;
        }
        return "";
    }

    /**
     * Inline style for the colored dot inside an inactive tag pill.
     * @param {{ color: number }} tag
     * @returns {string}
     */
    tagDotStyle(tag) {
        return `background: ${owColor(tag.color)};`;
    }

    /**
     * Toggle a tag on the open message. Sends the complete new tag id set
     * to the `set_tags` action (server diffs IMAP keywords).
     * @async
     * @param {{ id: number }} tag
     */
    async onToggleTag(tag) {
        const m = this.state.selectedMessage;
        if (!m) return;
        const ids = new Set(m.tag_ids || []);
        if (ids.has(tag.id)) {
            ids.delete(tag.id);
        } else {
            ids.add(tag.id);
        }
        await this.mail.setMessageTags(m, [...ids]);
    }

    /**
     * Toggle the read/unread state of the selected message.
     *
     * Issues a `mark_read` or `mark_unread` action via the service (IMAP
     * `UID STORE ±\Seen`) and optimistically updates `m.flags.seen` in the
     * local reactive state so the UI reflects the change immediately without
     * waiting for a full message list refresh.
     */
    async onToggleRead() {
        const m = this.state.selectedMessage;
        if (!m) return;
        const action = m.flags.seen ? "mark_unread" : "mark_read";
        await this.mail.runAction(m.folder_id, [m.uid], action, {});
        m.flags.seen = !m.flags.seen;
    }

    /**
     * Toggle the starred/flagged state of the selected message.
     *
     * Issues a `toggle_flag` action via the service (IMAP `UID STORE ±\Flagged`)
     * and optimistically updates `m.flags.flagged` in local state.
     */
    async onToggleFlag() {
        const m = this.state.selectedMessage;
        if (!m) return;
        await this.mail.runAction(m.folder_id, [m.uid], "toggle_flag",
            { state: !m.flags.flagged });
        m.flags.flagged = !m.flags.flagged;
    }

    /**
     * Move the selected message to a different folder.
     *
     * Reads the target `folder_id` from the `<select>` change event value and
     * resets the select back to its empty placeholder immediately so the
     * dropdown doesn't appear "stuck" on the chosen folder.  After the server
     * `move` action completes, clears `selectedMessage` and `selectedKey` so
     * the viewer is dismissed (the message no longer exists in the current
     * folder).
     *
     * @param {Event} ev - The `change` event from the folder `<select>` element.
     */
    async onMove(ev) {
        const folderId = parseInt(ev.target.value, 10);
        if (!folderId) return;
        ev.target.value = "";
        const m = this.state.selectedMessage;
        if (!m || m.folder_id === folderId) return;
        await this.mail.runAction(m.folder_id, [m.uid], "move", { folder_id: folderId });
        this.state.selectedMessage = null;
        this.state.selectedKey = null;
    }

    /**
     * Folders available as move destinations for the selected message.
     *
     * Returns all folders belonging to the same account, minus the folder the
     * message currently lives in (moving to the same folder is a no-op and
     * would clutter the dropdown).
     *
     * @returns {Array<{ id: number, full_path: string, kind: string }>}
     */
    get moveTargetFolders() {
        const acc = this._findAccount();
        if (!acc) return [];
        const m = this.state.selectedMessage;
        return acc.folders.filter((f) => f.subscribed && (!m || f.id !== m.folder_id));
    }

    /**
     * Move the selected message to the Archive folder.
     */
    async onArchive() {
        const m = this.state.selectedMessage;
        if (!m) return;
        await this.mail.archiveSelected();
    }

    /**
     * Delete the selected message.
     *
     * Calls the `delete` action via the service, which either moves the
     * message to the Trash folder (if one is configured on the account) or
     * permanently expunges it.  After the action completes, clears
     * `selectedMessage` and `selectedKey` to dismiss the viewer.
     */
    async onDelete() {
        const m = this.state.selectedMessage;
        if (!m) return;
        await this.mail.runAction(m.folder_id, [m.uid], "delete");
        this.state.selectedMessage = null;
        this.state.selectedKey = null;
    }
}
