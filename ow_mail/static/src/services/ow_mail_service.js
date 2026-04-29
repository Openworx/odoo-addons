/** @odoo-module **/

import { registry } from "@web/core/registry";
import { rpc } from "@web/core/network/rpc";
import { reactive } from "@odoo/owl";

/**
 * Compose the stable `folder_id:uid` identity key for a message envelope.
 *
 * Message identity in ow_mail is a composite of the IMAP folder DB id and the
 * IMAP UID (an unsigned 32-bit integer assigned by the server). There is no
 * numeric ORM record id — never reintroduce one without adding a persistent
 * model first.
 *
 * @param {{folder_id: number, uid: number}} m - Envelope object with at minimum folder_id and uid.
 * @returns {string} Key in the form `"<folder_id>:<uid>"`.
 */
function msgKey(m) {
    return `${m.folder_id}:${m.uid}`;
}

function _stripRePrefix(subject) {
    return (subject || "").replace(/^(re|fwd|fw)\s*:\s*/i, "").trim();
}

function _threadRoot(m) {
    // First Message-ID in References is the conversation root
    if (m.references) {
        const first = m.references.trim().split(/\s+/)[0];
        if (first) return first;
    }
    if (m.in_reply_to) return m.in_reply_to;
    return m.message_id || msgKey(m);
}

/**
 * Group a flat array of message envelopes into conversation threads.
 *
 * Threading uses RFC 2822 `References` / `In-Reply-To` headers to identify the
 * conversation root: the first Message-ID in `References` wins, falling back to
 * `In-Reply-To`, then the message's own `Message-ID`, and finally the
 * `folder_id:uid` key as last resort (makes every orphan its own thread).
 *
 * Each returned thread object carries:
 * - `threadId` — the root Message-ID used as the grouping key
 * - `messages` — chronologically sorted member envelopes
 * - `latestDate`, `from_name`, `from_email` — taken from the newest message
 * - `has_unread`, `has_flagged`, `has_attachments` — OR across all members
 * - `subject` — Re:/Fwd: prefix stripped for display
 *
 * The threads array itself is sorted descending by `latestDate`, mirroring the
 * default sort order of the flat list.
 *
 * @param {Array<object>} messages - Flat array of envelope objects (one IMAP page).
 * @returns {Array<object>} Threads sorted newest-first.
 */
function buildThreads(messages) {
    const groups = {};
    for (const m of messages) {
        const root = _threadRoot(m);
        if (!groups[root]) {
            groups[root] = { threadId: root, messages: [], subject: _stripRePrefix(m.subject) };
        }
        groups[root].messages.push(m);
    }
    const threads = Object.values(groups);
    // Sort messages within each thread chronologically
    for (const t of threads) {
        t.messages.sort((a, b) => (a.date || "").localeCompare(b.date || ""));
        const latest = t.messages[t.messages.length - 1];
        t.latestDate = latest.date;
        t.from_name = latest.from_name;
        t.from_email = latest.from_email;
        t.has_unread = t.messages.some((m) => !m.flags_seen);
        t.has_flagged = t.messages.some((m) => m.flags_flagged);
        t.has_attachments = t.messages.some((m) => m.has_attachments);
    }
    // Sort threads by latest date (desc by default)
    threads.sort((a, b) => (b.latestDate || "").localeCompare(a.latestDate || ""));
    return threads;
}

/**
 * Central OWL service for the ow_mail client.
 *
 * Registered as `"ow_mail"` and consumed via `useService("ow_mail")`.
 * Owns the single reactive `state` object that drives the entire three-pane UI.
 * All server communication is live IMAP proxied via JSON-RPC to
 * `ow_mail/controllers/main.py` — no message bodies are persisted in the DB.
 *
 * ## Reactive state
 * Components read `useState(this.mail.state)`. Every field listed below
 * triggers OWL re-renders when mutated:
 *
 * - `accounts` — array of account objects returned by `/ow_mail/bootstrap`,
 *   each containing `id`, `name`, `email`, `special` (special-folder id map),
 *   `folders` (flat list), `unread_count`, `signature_*`.
 * - `tags` — array of `{id, name, color, imap_keyword}` tag objects.
 * - `selection` — `{accountId, folderId, tagId, filter, search}` — current
 *   view context. `folderId === null` means "All Mailboxes" cross-account view.
 * - `messages` — current page of envelope objects from `/ow_mail/messages`.
 * - `totalMessages` — total matching count (for pagination).
 * - `page`, `pageSize` — current page index (1-based) and fixed page size.
 * - `selectedKey` — `"folder_id:uid"` of the open message; `null` = none open.
 * - `selectedMessage` — full parsed message object from `/ow_mail/message`,
 *   including `html`, `text`, `attachments`, flags, headers.
 * - `composeWindows` — array of active `{id, accountId, to, cc, bcc, subject,
 *   body, ...}` draft objects. Multiple windows can coexist (floating stack).
 * - `sortBy` — envelope sort field, persisted to localStorage.
 * - `sortOrder` — `"asc"` | `"desc"`, persisted to localStorage.
 * - `threadView` — when `true`, `messages` is also grouped into `threads`;
 *   persisted to localStorage.
 * - `threads` — result of `buildThreads(messages)` when threadView is on.
 * - `threadMessages` — messages belonging to the open message's conversation.
 * - `loading` — `true` while the message list RPC is in-flight.
 * - `contacts`, `contactsLoading`, `contactsSearch`, `selectedContactId`,
 *   `contactsDetailOpen` — contacts pane state.
 * - `view` — active pane: `'mail'` | `'contacts'`.
 * - `viewport` — `'desktop'` | `'tablet'` | `'mobile'`; set by matchMedia.
 * - `sidebarOverlayOpen` — whether the mobile sidebar overlay is visible.
 *
 * ## Sequence guard
 * `_msgSeq` is incremented before every `openMessage` call. Both `openMessage`
 * and `loadThread` capture `_msgSeq` at call time and compare it after each
 * `await`; if the value changed (user clicked another mail) the response is
 * silently discarded, preventing a stale body from clobbering the current view.
 *
 * ## Bus subscriptions
 * - `ow_mail/new_mail` — shows a notification + browser Notification API popup.
 * - `ow_mail/refresh` — re-runs `bootstrap()` to pick up server-pushed changes
 *   (e.g. new mail detected by the IMAP cron).
 */
export const owMailService = {
    dependencies: ["notification", "bus_service"],
    async start(env, { notification, bus_service }) {
        const PAGE_SIZE = 50;
        const LS_SORT_BY = "ow_mail.sortBy";
        const LS_SORT_ORDER = "ow_mail.sortOrder";
        const LS_THREAD_VIEW = "ow_mail.threadView";

        /**
         * Read a value from localStorage, returning `fallback` on SecurityError
         * (private-browsing mode or cross-origin iframe) or when the key is absent.
         *
         * @param {string} key
         * @param {string} fallback
         * @returns {string}
         */
        function _lsGet(key, fallback) {
            try { return localStorage.getItem(key) || fallback; } catch { return fallback; }
        }
        /**
         * Write a value to localStorage, silently swallowing SecurityError so that
         * sort/thread preferences degrade gracefully in restricted contexts.
         *
         * @param {string} key
         * @param {string} val
         */
        function _lsSet(key, val) {
            try { localStorage.setItem(key, val); } catch {}
        }

        const state = reactive({
            accounts: [],
            tags: [],
            selection: { accountId: null, folderId: null, tagId: null, filter: "all", search: "" },
            messages: [],
            totalMessages: 0,
            page: 1,
            pageSize: PAGE_SIZE,
            selectedKey: null,
            selectedMessage: null,
            composeWindows: [],
            loading: false,
            sortBy: _lsGet(LS_SORT_BY, "date"),
            sortOrder: _lsGet(LS_SORT_ORDER, "desc"),
            threadView: _lsGet(LS_THREAD_VIEW, "0") === "1",
            threads: [],
            threadMessages: [],
            view: "mail", // "mail" | "contacts"
            contacts: [],
            contactsLoading: false,
            contactsSearch: "",
            selectedContactId: null,
            contactsDetailOpen: false,
            viewport: "desktop", // "desktop" | "tablet" | "mobile"
            sidebarOverlayOpen: false,
        });

        // Viewport tracking via matchMedia — runs in browsers only.
        if (typeof window !== "undefined" && window.matchMedia) {
            const mqMobile = window.matchMedia("(max-width: 767.98px)");
            const mqTablet = window.matchMedia("(max-width: 991.98px)");
            const applyViewport = () => {
                state.viewport = mqMobile.matches
                    ? "mobile"
                    : mqTablet.matches
                    ? "tablet"
                    : "desktop";
                if (state.viewport !== "mobile") state.sidebarOverlayOpen = false;
            };
            applyViewport();
            mqMobile.addEventListener("change", applyViewport);
            mqTablet.addEventListener("change", applyViewport);
        }

        /**
         * Perform the initial data load: fetch accounts, folder tree, and tags from
         * `/ow_mail/bootstrap`, then select the Inbox of the first account and load
         * the first page of messages.
         *
         * On re-bootstrap (bus refresh or `sync()` call), the existing folder
         * selection is preserved — only the first bootstrap call sets the Inbox
         * as the default view.
         *
         * Also called on `ow_mail/refresh` bus events to pick up server-side changes
         * (new accounts, folder renames, unread count corrections).
         *
         * @async
         */
        async function bootstrap() {
            const data = await rpc("/ow_mail/bootstrap");
            state.accounts = data.accounts;
            state.tags = data.tags;
            if (!state.selection.folderId && data.accounts.length) {
                const first = data.accounts[0];
                state.selection.accountId = first.id;
                state.selection.folderId =
                    first.special.inbox || (first.folders[0] && first.folders[0].id);
            }
            await refreshList();
        }

        /**
         * Re-fetch the message page that matches the current `state.selection`.
         *
         * Sets `state.loading` for the duration of the RPC, then writes
         * `state.messages` and `state.totalMessages`. When `threadView` is on it
         * also rebuilds `state.threads` via `buildThreads`.
         *
         * Bails out early (before the network call) only when no accounts have
         * been loaded yet — i.e. before the first `bootstrap()`. An all-mailboxes
         * view (`folderId === null`) is explicitly supported.
         *
         * @async
         */
        async function refreshList() {
            // Only bail when no accounts loaded yet (pre-bootstrap)
            if (!state.selection.folderId && !state.selection.tagId && !state.accounts.length) {
                state.messages = [];
                state.totalMessages = 0;
                return;
            }
            state.loading = true;
            try {
                const offset = (state.page - 1) * PAGE_SIZE;
                const res = await rpc("/ow_mail/messages", {
                    folder_id: state.selection.folderId || null,
                    filter: state.selection.filter,
                    search: state.selection.search || null,
                    tag_id: state.selection.tagId || null,
                    offset,
                    limit: PAGE_SIZE,
                    sort_by: state.sortBy,
                    sort_order: state.sortOrder,
                });
                if (res.error) {
                    notification.add("Mailbox error: " + res.error, { type: "warning" });
                    state.messages = [];
                    state.totalMessages = 0;
                } else {
                    state.messages = res.messages;
                    state.totalMessages = res.total;
                    state.threads = state.threadView ? buildThreads(res.messages) : [];
                }
            } finally {
                state.loading = false;
            }
        }

        /**
         * Switch the active view to a specific folder, clearing the tag filter,
         * resetting to page 1, and deselecting any open message.
         *
         * Passing `accountId = null, folderId = null` activates the cross-account
         * "All Mailboxes" view.
         *
         * @async
         * @param {number|null} accountId
         * @param {number|null} folderId
         */
        async function selectFolder(accountId, folderId) {
            state.selection.accountId = accountId;
            state.selection.folderId = folderId;
            state.selection.tagId = null;
            state.page = 1;
            state.selectedKey = null;
            state.selectedMessage = null;
            await refreshList();
        }

        /**
         * Switch the active view to a virtual tag-filtered mailbox (cross-folder
         * IMAP KEYWORD search). Clears the folder selection, resets to page 1,
         * and deselects any open message.
         *
         * @async
         * @param {number} tagId - DB id of the `ow.mail.tag` record.
         */
        async function selectTag(tagId) {
            state.selection.tagId = tagId;
            state.page = 1;
            state.selectedKey = null;
            state.selectedMessage = null;
            await refreshList();
        }

        /**
         * Apply an envelope filter to the current folder/tag view.
         *
         * Valid values map to IMAP search criteria in the controller:
         * `"all"` (no filter), `"unread"` (`UNSEEN`), `"flagged"` (`FLAGGED`).
         * Always resets to page 1 to avoid stale offsets.
         *
         * @async
         * @param {string} filter - Filter identifier.
         */
        async function setFilter(filter) {
            state.selection.filter = filter;
            state.page = 1;
            await refreshList();
        }

        /**
         * Update the free-text search query for the current folder/tag view.
         *
         * The controller translates `q` into an IMAP `OR SUBJECT … FROM … BODY …`
         * criterion. Always resets to page 1.
         *
         * @async
         * @param {string} q - Search string; falsy clears the search.
         */
        async function setSearch(q) {
            state.selection.search = q || "";
            state.page = 1;
            await refreshList();
        }

        /**
         * Perform a cross-account full-text search by switching to the All Mailboxes
         * view (`folderId = null`) and setting the search query.
         *
         * Clears all active filters, folder, and tag selections so the controller
         * issues the search across every configured IMAP account.
         *
         * @async
         * @param {string} query - Search string; falsy clears the search.
         */
        async function searchAll(query) {
            state.view = "mail";
            state.selection.accountId = null;
            state.selection.folderId = null;
            state.selection.tagId = null;
            state.selection.filter = "all";
            state.selection.search = query || "";
            state.page = 1;
            state.selectedKey = null;
            state.selectedMessage = null;
            await refreshList();
        }

        /**
         * Resolve the parent account for a folder id.
         *
         * Falls back to `state.accounts[0]` when the folder is not found — useful
         * for compose windows opened from the Sent or Drafts folder of an account
         * that hasn't loaded yet, or for edge cases during bootstrap.
         *
         * @param {number} folderId
         * @returns {object|undefined} Account object; falls back to the first account,
         *   or `undefined` when no accounts are loaded yet.
         */
        function _accountForFolder(folderId) {
            return state.accounts.find((a) =>
                a.folders.some((f) => f.id === folderId)
            ) || state.accounts[0];
        }

        /**
         * Move keyboard focus to the next (`delta = 1`) or previous (`delta = -1`)
         * message in `state.messages` and open it.
         *
         * When no message is currently selected, `delta > 0` opens the first message
         * and `delta < 0` opens the last. Does nothing when the resulting index would
         * be out of bounds, preventing wrap-around.
         *
         * @async
         * @param {number} delta - Direction: `1` for next, `-1` for previous.
         */
        async function moveSelection(delta) {
            const msgs = state.messages;
            if (!msgs.length) return;
            const key = state.selectedKey;
            let idx = key ? msgs.findIndex((m) => msgKey(m) === key) : -1;
            idx = idx < 0 ? (delta > 0 ? 0 : msgs.length - 1) : idx + delta;
            if (idx < 0 || idx >= msgs.length) return;
            const m = msgs[idx];
            await openMessage(m.folder_id, m.uid);
        }

        /**
         * HTML-escape untrusted envelope strings before splicing them into the
         * compose body's `innerHTML`.
         *
         * Covers `from_name`, `subject`, `to`, and `date` fields from the envelope.
         * The message body (`m.html`) is already sanitized server-side via
         * `imap_utils.sanitize_and_detect` and is safe to interpolate directly.
         *
         * @param {string} s - Raw string, potentially containing HTML metacharacters.
         * @returns {string} HTML-safe string.
         */
        function _escHtml(s) {
            return String(s || "").replace(/[&<>"']/g, (c) => ({
                "&": "&amp;", "<": "&lt;", ">": "&gt;",
                '"': "&quot;", "'": "&#39;",
            })[c]);
        }

        /**
         * Return the account's signature HTML for insertion into a compose body.
         *
         * Returns an empty string when the signature feature is disabled
         * (`signature_enabled === false`) or when no account is provided, so callers
         * can always concatenate the result unconditionally.
         *
         * @param {object|null|undefined} acc - Account object from `state.accounts`.
         * @returns {string} Signature HTML or `""`.
         */
        function _sig(acc) {
            return (acc && acc.signature_enabled && acc.signature_html) || "";
        }

        /**
         * Assemble the initial compose body for a reply or forward.
         *
         * Respects the account's `signature_placement` setting:
         * - `"above"` — signature appears between the composer cursor and the quote.
         * - anything else (default `"below"`) — signature appears after the quote.
         *
         * Always starts with an empty `<p>` so the cursor lands above the quote
         * regardless of placement.
         *
         * @param {object|null|undefined} acc - Account object (for signature settings).
         * @param {string} quoteHtml - Pre-built blockquote HTML for the original message.
         * @returns {string} Full compose body HTML.
         */
        function _bodyWithQuote(acc, quoteHtml) {
            const sig = _sig(acc);
            if (acc && acc.signature_placement === "above") {
                return `<p><br/></p>${sig ? sig + "<br/>" : ""}${quoteHtml}`;
            }
            return `<p><br/></p>${quoteHtml}${sig ? "<br/>" + sig : ""}`;
        }

        /**
         * Open a compose window pre-populated for a reply or reply-all.
         *
         * Sets `To` to the original sender's address. When `all = true` (reply-all),
         * also populates `CC` with the original message's CC list. Prepends `"Re: "`
         * to the subject if not already present. Threads the new message by setting
         * `In-Reply-To` and `References` headers so IMAP clients can group it.
         *
         * @param {object} m - Full message object (`state.selectedMessage`).
         * @param {object} [opts]
         * @param {boolean} [opts.all=false] - When `true`, populate CC for reply-all.
         */
        function openReply(m, { all = false } = {}) {
            if (!m) return;
            const acc = _accountForFolder(m.folder_id);
            const who = _escHtml(m.from_name || m.from_email);
            const when = _escHtml(m.date || "");
            const quote = `<p>On ${when}, <em>${who}</em> wrote:</p>` +
                `<blockquote style="border-left:2px solid #ccc;padding-left:10px;">${m.html || ""}</blockquote>`;
            openCompose({
                accountId: acc ? acc.id : null,
                to: m.from_email,
                cc: all ? m.cc : "",
                subject: /^re:/i.test(m.subject || "") ? m.subject : `Re: ${m.subject || ""}`,
                body: _bodyWithQuote(acc, quote),
                in_reply_to: m.message_id,
                references: [m.references, m.message_id].filter(Boolean).join(" "),
            });
        }

        /**
         * Open a compose window pre-populated for a forward.
         *
         * Prepends `"Fwd: "` to the subject if not already present. Wraps the
         * original message in a standardized forwarded-message header block and
         * inlines the sanitized HTML body. Does not pre-fill `To` — the user must
         * supply the recipient.
         *
         * @param {object} m - Full message object (`state.selectedMessage`).
         */
        function openForward(m) {
            if (!m) return;
            const acc = _accountForFolder(m.folder_id);
            const from = _escHtml(m.from_name || "");
            const fromEmail = _escHtml(m.from_email || "");
            const date = _escHtml(m.date || "");
            const subj = _escHtml(m.subject || "");
            const to = _escHtml(m.to || "");
            const fwdBlock = `<p>---------- Forwarded message ----------</p>` +
                `<p>From: ${from} &lt;${fromEmail}&gt;<br/>` +
                `Date: ${date}<br/>Subject: ${subj}<br/>To: ${to}</p>` +
                `<blockquote>${m.html || ""}</blockquote>`;
            openCompose({
                accountId: acc ? acc.id : null,
                subject: /^fwd:/i.test(m.subject || "") ? m.subject : `Fwd: ${m.subject || ""}`,
                body: _bodyWithQuote(acc, fwdBlock),
            });
        }

        /**
         * Move the currently open message to the account's Archive folder.
         *
         * Shows a warning if no archive folder is configured (the account's
         * `special.archive` id is absent) or if the message is already in the
         * archive. Clears `selectedMessage` and `selectedKey` on success so
         * the message viewer closes.
         *
         * @async
         */
        async function archiveSelected() {
            const m = state.selectedMessage;
            if (!m) return;
            const acc = _accountForFolder(m.folder_id);
            const archiveId = acc && acc.special && acc.special.archive;
            if (!archiveId || archiveId === m.folder_id) {
                notification.add("No archive folder configured", { type: "warning" });
                return;
            }
            await runAction(m.folder_id, [m.uid], "move", { folder_id: archiveId });
            state.selectedMessage = null;
            state.selectedKey = null;
        }

        /**
         * Delete the currently open message via the generic action dispatcher.
         *
         * The server-side handler moves the message to Trash if a Trash folder is
         * configured, or performs a permanent IMAP `EXPUNGE` otherwise. Clears
         * `selectedMessage` and `selectedKey` on success.
         *
         * @async
         */
        async function deleteSelected() {
            const m = state.selectedMessage;
            if (!m) return;
            await runAction(m.folder_id, [m.uid], "delete");
            state.selectedMessage = null;
            state.selectedKey = null;
        }

        /**
         * Toggle the IMAP `\Flagged` flag on the currently open message.
         *
         * Optimistically updates `selectedMessage.flags.flagged` in local state
         * after the server call so the star icon reflects the change immediately.
         *
         * @async
         */
        async function toggleStarSelected() {
            const m = state.selectedMessage;
            if (!m) return;
            await runAction(m.folder_id, [m.uid], "toggle_flag",
                { state: !m.flags.flagged });
            m.flags.flagged = !m.flags.flagged;
        }

        /**
         * Toggle the IMAP `\Seen` flag on the currently open message.
         *
         * Issues either `mark_read` or `mark_unread` depending on current state.
         * Optimistically flips `selectedMessage.flags.seen` in local state.
         *
         * @async
         */
        async function toggleReadSelected() {
            const m = state.selectedMessage;
            if (!m) return;
            const action = m.flags.seen ? "mark_unread" : "mark_read";
            await runAction(m.folder_id, [m.uid], action, {});
            m.flags.seen = !m.flags.seen;
        }

        /**
         * Imperatively focus the search input and select its existing text.
         *
         * Used by keyboard shortcut handlers (e.g. pressing `/`) to jump focus
         * to the search box without the component needing to expose a ref.
         */
        function focusSearch() {
            const el = document.querySelector(".o-ow-search-input");
            if (el) {
                el.focus();
                el.select && el.select();
            }
        }

        /** Navigate to the All Mailboxes unified view (no folder selected). @async */
        async function goAllMailboxes() {
            await selectFolder(null, null);
        }

        /**
         * Navigate to the Inbox of the currently selected account.
         *
         * Falls back to the first account when no account is selected yet.
         * No-op if the current account has no mapped Inbox special folder.
         *
         * @async
         */
        async function goInbox() {
            const acc = state.accounts.find((a) => a.id === state.selection.accountId)
                || state.accounts[0];
            if (!acc) return;
            const inboxId = acc.special && acc.special.inbox;
            if (inboxId) await selectFolder(acc.id, inboxId);
        }

        /**
         * Create an Odoo calendar event from an ICS invite attachment in this message.
         *
         * The controller parses the ICS payload, creates (or updates) an
         * `calendar.event` record, and returns its id and name. Returns `null` on
         * error after showing a danger notification.
         *
         * @async
         * @param {number} folderId
         * @param {number} uid - IMAP UID of the message carrying the ICS attachment.
         * @returns {object|null} `{event_id, event_name}` on success, `null` on error.
         */
        async function addInviteToCalendar(folderId, uid) {
            const res = await rpc("/ow_mail/calendar/from_invite",
                { folder_id: folderId, uid });
            if (res && res.error) {
                notification.add("Could not open invite: " + res.error,
                    { type: "danger" });
                return null;
            }
            return res;
        }

        /**
         * Update the envelope sort parameters and re-fetch the current page.
         *
         * The new values are persisted to localStorage so the preference survives
         * page reloads. Always resets to page 1 to avoid stale offsets.
         *
         * @async
         * @param {string} field - Sort field name (e.g. `"date"`, `"from"`, `"subject"`).
         * @param {string} order - `"asc"` or `"desc"`.
         */
        async function setSort(field, order) {
            state.sortBy = field;
            state.sortOrder = order;
            _lsSet(LS_SORT_BY, field);
            _lsSet(LS_SORT_ORDER, order);
            state.page = 1;
            await refreshList();
        }

        /**
         * Toggle conversation-grouping mode on or off.
         *
         * When enabled, `state.threads` is rebuilt from the current `state.messages`
         * via `buildThreads`. The setting is persisted to localStorage (`"1"` / `"0"`)
         * so it survives page reloads.
         */
        function toggleThreadView() {
            state.threadView = !state.threadView;
            _lsSet(LS_THREAD_VIEW, state.threadView ? "1" : "0");
            state.threads = state.threadView ? buildThreads(state.messages) : [];
        }

        // Sequence counter for the message-open race guard. Bumped on every
        // openMessage call. Both openMessage and loadThread capture the value at
        // call time and compare after each await — a mismatch means the user has
        // already opened a different message, so the stale response is discarded.
        let _msgSeq = 0;

        /**
         * Fetch related messages for the open message's conversation and store them
         * in `state.threadMessages`.
         *
         * Collects all Message-IDs referenced by the open message (`message_id`,
         * `in_reply_to`, and each id in `references`), then calls `/ow_mail/thread`
         * to search those ids across the account's folders.
         *
         * If only one unique id exists the fetch is skipped and `threadMessages`
         * is set to just the open message. Silently falls back to `[msg]` on error.
         *
         * Uses the sequence guard (`reqSeq` vs `_msgSeq`): if the user has clicked
         * a different message by the time any `await` resolves, the write to state
         * is skipped. `openMessage` always passes its `mySeq` as `reqSeq`.
         *
         * @async
         * @param {number} folderId - Folder id of the currently open message.
         * @param {number} uid - IMAP UID of the currently open message.
         * @param {number|null} [reqSeq=null] - Sequence number captured by the caller;
         *   defaults to the current `_msgSeq` if omitted.
         */
        async function loadThread(folderId, uid, reqSeq = null) {
            const mySeq = reqSeq !== null ? reqSeq : _msgSeq;
            const msg = state.selectedMessage;
            if (!msg) return;
            const allIds = new Set();
            if (msg.message_id) allIds.add(msg.message_id);
            if (msg.in_reply_to) allIds.add(msg.in_reply_to);
            if (msg.references) {
                msg.references.split(/\s+/).forEach((id) => { if (id) allIds.add(id); });
            }
            if (allIds.size <= 1) {
                if (mySeq !== _msgSeq) return;
                state.threadMessages = [msg];
                return;
            }
            const acc = state.accounts.find((a) =>
                a.folders.some((f) => f.id === folderId)
            );
            if (!acc) {
                if (mySeq !== _msgSeq) return;
                state.threadMessages = [msg];
                return;
            }
            try {
                const res = await rpc("/ow_mail/thread", {
                    account_id: acc.id,
                    message_ids: [...allIds],
                });
                if (mySeq !== _msgSeq) return;
                if (res.error) {
                    state.threadMessages = [msg];
                } else {
                    state.threadMessages = res.messages || [msg];
                }
            } catch {
                if (mySeq !== _msgSeq) return;
                state.threadMessages = [msg];
            }
        }

        /**
         * Navigate to a specific page of the current message list.
         *
         * Clamps the target to the valid range `[1, totalPages]` and is a no-op when
         * the requested page equals the current one.
         *
         * @async
         * @param {number} p - Target 1-based page number.
         */
        async function goToPage(p) {
            const totalPages = Math.max(1, Math.ceil(state.totalMessages / PAGE_SIZE));
            const target = Math.max(1, Math.min(p, totalPages));
            if (target === state.page) return;
            state.page = target;
            await refreshList();
        }

        /**
         * Fetch and display a message's full body.
         *
         * Increments `_msgSeq` immediately (sequence guard) and sets `selectedKey`
         * so the list row highlights before the RPC returns. After the fetch:
         * - If the sequence no longer matches, the result is discarded (stale click).
         * - On success, writes `selectedMessage` and optimistically marks the
         *   envelope `flags_seen = true`, decrementing `unread_count` on the parent
         *   folder and account for instant sidebar feedback.
         * - Auto-triggers `loadThread` when the message has `References` or
         *   `In-Reply-To` headers; otherwise sets `threadMessages = [data]`.
         *
         * @async
         * @param {number} folderId
         * @param {number} uid - IMAP UID.
         */
        async function openMessage(folderId, uid) {
            const mySeq = ++_msgSeq;
            state.selectedKey = `${folderId}:${uid}`;
            state.selectedMessage = null;
            state.threadMessages = [];
            const data = await rpc("/ow_mail/message", { folder_id: folderId, uid });
            if (mySeq !== _msgSeq) return;          // user clicked another mail
            if (data.error) {
                notification.add("Could not load message: " + data.error, { type: "danger" });
                state.selectedKey = null;
                return;
            }
            state.selectedMessage = data;
            const m = state.messages.find((x) => x.folder_id === folderId && x.uid === uid);
            if (m && !m.flags_seen) {
                m.flags_seen = true;
                // Decrement unread count in sidebar (folder + account)
                for (const acc of state.accounts) {
                    const folder = acc.folders.find((f) => f.id === folderId);
                    if (folder) {
                        if (folder.unread_count > 0) folder.unread_count--;
                        if (acc.unread_count > 0) acc.unread_count--;
                        break;
                    }
                }
            }
            // Auto-load thread if message is part of a conversation
            if (data.references || data.in_reply_to) {
                await loadThread(folderId, uid, mySeq);
            } else {
                state.threadMessages = [data];
            }
        }

        /**
         * Open an existing draft message in a compose window for editing.
         *
         * Fetches the full message body from IMAP (same as `openMessage`) and
         * populates a new compose window with its headers and body. Each save
         * appends a new draft to the Drafts folder; the old draft is not
         * automatically deleted — the caller is responsible for removing it
         * after the user sends or explicitly discards the draft.
         *
         * @async
         * @param {number} folderId
         * @param {number} uid - IMAP UID of the draft message.
         */
        async function editDraft(folderId, uid) {
            const data = await rpc("/ow_mail/message", { folder_id: folderId, uid });
            if (data.error) {
                notification.add("Could not load draft: " + data.error, { type: "danger" });
                return;
            }
            const acc = state.accounts.find((a) =>
                a.folders.some((f) => f.id === folderId)
            ) || state.accounts[0];
            openCompose({
                accountId: acc ? acc.id : null,
                to: data.to || "",
                cc: data.cc || "",
                subject: data.subject || "",
                body: data.html || data.text || "",
                in_reply_to: data.in_reply_to || null,
                references: data.references || null,
                draftFolderId: folderId,
                draftUid: uid,
            });
        }

        /**
         * Trigger a full IMAP `LIST` + `STATUS` refresh for one account (or all
         * accounts when `accountId` is omitted), then re-run `bootstrap` to apply
         * the updated folder tree and unread counts.
         *
         * Shows an info notification before the call to give immediate feedback
         * since the round-trip can take a few seconds on large mailboxes.
         *
         * @async
         * @param {number|null} [accountId=null] - Omit to refresh all accounts.
         */
        async function sync(accountId = null) {
            notification.add("Refreshing…", { type: "info" });
            await rpc("/ow_mail/sync", { account_id: accountId });
            await bootstrap();
        }

        /**
         * Dispatch a generic IMAP action for one or more messages.
         *
         * Maps to `POST /ow_mail/message/action`. Supported actions:
         * `mark_read`, `mark_unread`, `toggle_flag`, `move`, `delete`,
         * `trust_sender`, `set_tags`. Shows a danger notification on error and
         * always refreshes the message list on success.
         *
         * @async
         * @param {number} folderId - Source folder id.
         * @param {number[]} uids - IMAP UIDs to act on.
         * @param {string} action - Action identifier.
         * @param {object} [payload={}] - Action-specific parameters (e.g. `{folder_id}` for move).
         */
        async function runAction(folderId, uids, action, payload = {}) {
            const res = await rpc("/ow_mail/message/action", {
                folder_id: folderId, uids, action, payload,
            });
            if (res.error) {
                notification.add("Action failed: " + res.error, { type: "danger" });
                return;
            }
            await refreshList();
        }

        /**
         * Create a new floating compose window and push it onto `state.composeWindows`.
         *
         * Generates a unique id via `Date.now() + Math.random()`. Falls back to the
         * first account when `init.accountId` is not supplied. If no `body` is given,
         * `defaultBody` is called to insert the account's signature placeholder.
         *
         * Multiple compose windows can coexist; each is independently minimizable
         * and expandable.
         *
         * @param {object} [init={}] - Initial values for the compose window.
         * @param {number} [init.accountId]
         * @param {string} [init.to]
         * @param {string} [init.cc]
         * @param {string} [init.bcc]
         * @param {string} [init.subject]
         * @param {string} [init.body] - HTML body; defaults to signature placeholder.
         * @param {string} [init.in_reply_to] - Message-ID for threading.
         * @param {string} [init.references] - Space-separated Reference ids.
         * @param {number} [init.draftFolderId] - Folder id of the draft being edited.
         * @param {number} [init.draftUid] - UID of the draft being edited.
         */
        function openCompose(init = {}) {
            const id = Date.now() + Math.random();
            const accountId = init.accountId || (state.accounts[0] && state.accounts[0].id);
            state.composeWindows.push({
                id,
                accountId,
                to: init.to || "",
                cc: init.cc || "",
                bcc: init.bcc || "",
                subject: init.subject || "",
                body: init.body || defaultBody(accountId),
                in_reply_to: init.in_reply_to || null,
                references: init.references || null,
                attachments: [],
                minimized: false,
                expanded: false,
                draftFolderId: init.draftFolderId || null,
                draftUid: init.draftUid || null,
            });
        }

        /**
         * Return the default HTML body for a new blank compose window.
         *
         * Consists of an empty paragraph (cursor position) followed by the
         * account's signature HTML (or nothing when the signature is disabled).
         *
         * @param {number} accountId
         * @returns {string} HTML string.
         */
        function defaultBody(accountId) {
            const acc = state.accounts.find((a) => a.id === accountId);
            return "<p><br/></p>" + _sig(acc);
        }

        /**
         * Remove a compose window from `state.composeWindows` by its id.
         *
         * @param {number} id - The unique id assigned in `openCompose`.
         */
        function closeCompose(id) {
            state.composeWindows = state.composeWindows.filter((w) => w.id !== id);
        }

        /**
         * Send a message via SMTP and IMAP-APPEND it to the Sent folder.
         *
         * On success, calls `touchRecipients` to update last_used on the contacted
         * addresses (for recency-ranked autocomplete), closes the compose window,
         * and refreshes the message list. Shows a warning notification if the server
         * reports a partial failure (e.g. SMTP delivery succeeded but the Sent-folder
         * IMAP APPEND failed).
         *
         * @async
         * @param {object} win - The compose window object from `state.composeWindows`.
         */
        async function sendCompose(win) {
            const res = await rpc("/ow_mail/send", {
                account_id: win.accountId,
                to: win.to,
                cc: win.cc,
                bcc: win.bcc,
                subject: win.subject,
                body_html: win.body,
                attachment_ids: win.attachments.map((a) => a.id),
                in_reply_to: win.in_reply_to,
                references: win.references,
            });
            if (res.error) {
                notification.add("Send failed: " + res.error, { type: "danger" });
                return;
            }
            if (res.warning) {
                notification.add(res.warning, { type: "warning" });
            } else {
                notification.add("Message sent", { type: "success" });
            }
            touchRecipients([win.to, win.cc, win.bcc]);
            closeCompose(win.id);
            await refreshList();
        }

        /**
         * Save the current compose window contents as a draft via IMAP APPEND.
         *
         * The controller appends the message to the account's Drafts folder with
         * the `\Draft` flag. Closes the compose window and refreshes the list on
         * success so the new draft appears immediately if the Drafts folder is active.
         *
         * @async
         * @param {object} win - The compose window object from `state.composeWindows`.
         */
        async function saveDraft(win) {
            const res = await rpc("/ow_mail/draft", {
                account_id: win.accountId,
                to: win.to,
                cc: win.cc,
                bcc: win.bcc,
                subject: win.subject,
                body_html: win.body,
                attachment_ids: win.attachments.map((a) => a.id),
                in_reply_to: win.in_reply_to,
                references: win.references,
            });
            if (res.error) {
                notification.add("Draft save failed: " + res.error, { type: "danger" });
                return;
            }
            notification.add("Draft saved", { type: "success" });
            closeCompose(win.id);
            await refreshList();
        }

        /**
         * Execute a folder-management RPC and re-run `bootstrap` on success.
         *
         * Centralizes error handling for all folder operations: shows a danger
         * notification on error and returns `false`; on success re-bootstraps to
         * refresh the folder tree and returns `true`.
         *
         * @async
         * @param {string} path - RPC endpoint path.
         * @param {object} body - Request body.
         * @returns {boolean} `true` on success, `false` on error.
         */
        async function folderRpc(path, body) {
            const res = await rpc(path, body);
            if (res && res.error) {
                notification.add("Folder error: " + res.error, { type: "danger" });
                return false;
            }
            await bootstrap();
            return true;
        }
        /**
         * Create a new IMAP folder under an optional parent.
         *
         * @param {number} accountId
         * @param {string} name - Leaf name for the new folder.
         * @param {number|null} [parentId=null] - Parent folder id; `null` creates at root.
         * @returns {Promise<boolean>}
         */
        const createFolder = (accountId, name, parentId = null) =>
            folderRpc("/ow_mail/folder/create",
                { account_id: accountId, name, parent_id: parentId });
        /**
         * Rename the leaf segment of an IMAP folder path.
         *
         * The controller issues `RENAME` only on the final path component, preserving
         * the folder's position in the hierarchy.
         *
         * @param {number} folderId
         * @param {string} newName - New leaf name.
         * @returns {Promise<boolean>}
         */
        const renameFolder = (folderId, newName) =>
            folderRpc("/ow_mail/folder/rename",
                { folder_id: folderId, new_name: newName });
        /**
         * Move a folder to a new parent by issuing an IMAP `RENAME` on the full path.
         *
         * @param {number} folderId
         * @param {number|null} newParentId - Target parent folder id; `null` moves to root.
         * @returns {Promise<boolean>}
         */
        const moveFolder = (folderId, newParentId) =>
            folderRpc("/ow_mail/folder/move",
                { folder_id: folderId, new_parent_id: newParentId });
        /**
         * Permanently delete an IMAP folder and all its contents.
         *
         * @param {number} folderId
         * @returns {Promise<boolean>}
         */
        const deleteFolder = (folderId) =>
            folderRpc("/ow_mail/folder/delete", { folder_id: folderId });
        /**
         * Mark all messages in a folder as `\Deleted` and issue `EXPUNGE`.
         *
         * @param {number} folderId
         * @returns {Promise<boolean>}
         */
        const emptyFolder = (folderId) =>
            folderRpc("/ow_mail/folder/empty", { folder_id: folderId });
        /**
         * Subscribe or unsubscribe from an IMAP folder.
         *
         * Subscribed folders appear in the sidebar; unsubscribing hides them without
         * deleting them.
         *
         * @param {number} folderId
         * @param {boolean} subscribed - `true` to subscribe, `false` to unsubscribe.
         * @returns {Promise<boolean>}
         */
        const subscribeFolder = (folderId, subscribed) =>
            folderRpc("/ow_mail/folder/subscribe",
                { folder_id: folderId, subscribed });
        /**
         * Fetch IMAP quota information for an account via `GETQUOTA`.
         *
         * Returns a normalised object even when the server does not support
         * `QUOTA` (RFC 2087): `{supported: false, percent: 0, used: 0, total: 0}`.
         *
         * @async
         * @param {number} accountId
         * @returns {Promise<{supported: boolean, percent: number, used: number, total: number}>}
         */
        async function fetchQuota(accountId) {
            const res = await rpc("/ow_mail/account/quota", { account_id: accountId });
            if (res && res.error) return { supported: false, percent: 0, used: 0, total: 0 };
            return res;
        }

        // --- Contacts --------------------------------------------------------
        /**
         * Switch the active top-level pane between `'mail'` and `'contacts'`.
         *
         * When switching to `'contacts'` for the first time (empty contacts list),
         * triggers an initial `loadContacts()` fetch automatically.
         *
         * @param {'mail'|'contacts'} view
         */
        function setView(view) {
            state.view = view;
            if (view === "contacts" && state.contacts.length === 0) {
                loadContacts();
            }
        }

        /**
         * Fetch the contact list from the server, optionally filtered by a search string.
         *
         * Sets `state.contactsLoading` for the duration of the request. The result
         * replaces `state.contacts` entirely (no pagination — contacts list is
         * expected to be small relative to mailbox size).
         *
         * @async
         * @param {string} [search=""] - Filter string; empty string returns all contacts.
         */
        async function loadContacts(search = "") {
            state.contactsLoading = true;
            state.contactsSearch = search;
            try {
                const res = await rpc("/ow_mail/contacts/list", { search: search || null });
                state.contacts = res.contacts || [];
            } finally {
                state.contactsLoading = false;
            }
        }

        /**
         * Create a new `ow.mail.contact` record.
         *
         * Shows an info notification when the email address is already in the
         * contact list (`res.already`). Reloads the contact list preserving the
         * current search string if the contacts pane is active or the list is
         * already populated.
         *
         * @async
         * @param {object} vals - Field values for the new contact (name, email, …).
         * @returns {Promise<object|null>} Created contact object, or `null` on error.
         */
        async function createContact(vals) {
            const res = await rpc("/ow_mail/contacts/create", vals);
            if (res.error) {
                notification.add("Save failed: " + res.error, { type: "danger" });
                return null;
            }
            if (res.already) {
                notification.add("Already in contacts", { type: "info" });
            } else {
                notification.add("Contact saved", { type: "success" });
            }
            if (state.contacts.length || state.view === "contacts") {
                await loadContacts(state.contactsSearch);
            }
            return res.contact;
        }

        /**
         * Update fields on an existing `ow.mail.contact` record.
         *
         * @async
         * @param {number} id - Contact record id.
         * @param {object} vals - Fields to update.
         * @returns {Promise<object|null>} Updated contact object, or `null` on error.
         */
        async function updateContact(id, vals) {
            const res = await rpc("/ow_mail/contacts/update", { id, vals });
            if (res.error) {
                notification.add("Update failed: " + res.error, { type: "danger" });
                return null;
            }
            await loadContacts(state.contactsSearch);
            return res.contact;
        }

        /**
         * Delete one or more contact records.
         *
         * Clears `state.selectedContactId` if the selected contact is among those
         * deleted. Reloads the contact list after a successful delete.
         *
         * @async
         * @param {number[]} ids - Contact record ids to delete.
         */
        async function deleteContact(ids) {
            const res = await rpc("/ow_mail/contacts/delete", { ids });
            if (res.error) {
                notification.add("Delete failed: " + res.error, { type: "danger" });
                return;
            }
            if (state.selectedContactId && ids.includes(state.selectedContactId)) {
                state.selectedContactId = null;
            }
            await loadContacts(state.contactsSearch);
        }

        /**
         * Query the contacts + sent-history index for recipient autocomplete.
         *
         * Returns up to N `{name, email, last_used}` suggestions ranked by recency.
         * Returns an empty array immediately for blank/whitespace queries to avoid
         * unnecessary RPCs.
         *
         * @async
         * @param {string} query - Partial name or email typed by the user.
         * @returns {Promise<Array<{name: string, email: string}>>} Matching suggestions.
         */
        async function suggestRecipients(query) {
            if (!query || !query.trim()) return [];
            const res = await rpc("/ow_mail/contacts/suggest", { query });
            return res.suggestions || [];
        }

        /**
         * Extract bare email addresses from a raw RFC 2822 address string.
         *
         * Handles both `"Name" <addr>` and bare `addr` forms separated by
         * commas. Semicolon-separated lists are NOT supported — use a
         * pre-split if the input may contain semicolons. Returns lowercase
         * addresses only.
         *
         * @param {string|null|undefined} raw - Raw address string from a header field.
         * @returns {string[]} Array of lowercase email addresses.
         */
        function _parseEmails(raw) {
            if (!raw) return [];
            const out = [];
            const re = /([^<,]+)?<([^>]+)>|([^,\s]+@[^,\s]+)/g;
            let m;
            while ((m = re.exec(raw)) !== null) {
                const addr = (m[2] || m[3] || "").trim().toLowerCase();
                if (addr) out.push(addr);
            }
            return out;
        }

        /**
         * Update `last_used` timestamps on contacts matching the given addresses.
         *
         * Called automatically after a successful `sendCompose` with the `to`, `cc`,
         * and `bcc` fields so that frequently mailed contacts rank higher in
         * `suggestRecipients`. The RPC failure is silently ignored — this is a
         * best-effort recency update, not critical path.
         *
         * @async
         * @param {string[]} parts - Array of raw address strings (may contain name+angle-bracket form).
         */
        async function touchRecipients(parts) {
            const emails = [];
            for (const p of parts) emails.push(..._parseEmails(p));
            if (!emails.length) return;
            try { await rpc("/ow_mail/contacts/touch", { emails }); } catch {}
        }

        /**
         * Open the detail panel for a contact and set it as the selected contact.
         *
         * Passing `null` or a falsy value effectively deselects the current contact
         * and collapses the detail panel.
         *
         * @param {number|null} id - Contact record id.
         */
        function selectContact(id) {
            state.selectedContactId = id;
            state.contactsDetailOpen = !!id;
        }

        /**
         * Clear the currently open message, closing the message viewer pane.
         *
         * Resets `selectedKey`, `selectedMessage`, and `threadMessages` to their
         * empty defaults. Used when the user navigates away from a message without
         * opening a new one (e.g. folder switch while a message is open).
         */
        function clearSelection() {
            state.selectedKey = null;
            state.selectedMessage = null;
            state.threadMessages = [];
        }

        // --- New-mail bus notifications ---
        bus_service.subscribe("ow_mail/new_mail", (payload) => {
            notification.add(payload.message, {
                title: payload.account_name || "New Mail",
                type: "info",
                sticky: false,
            });
            // Browser notification (requires permission)
            if (typeof Notification !== "undefined" && Notification.permission === "granted") {
                try {
                    new Notification(payload.account_name || "New Mail", {
                        body: payload.message,
                        icon: "/ow_mail/static/description/icon.png",
                    });
                } catch {}
            }
            // State refresh is driven by the paired ow_mail/refresh event
        });
        // --- Silent mailbox refresh driven by the server cron ---
        bus_service.subscribe("ow_mail/refresh", () => {
            bootstrap();
        });
        // Request browser notification permission on first load
        if (typeof Notification !== "undefined" && Notification.permission === "default") {
            Notification.requestPermission();
        }

        return {
            state, bootstrap, refreshList, selectFolder, selectTag, setFilter, setSearch, searchAll, setSort,
            toggleThreadView, loadThread, goToPage,
            openMessage, editDraft, sync, runAction, openCompose, closeCompose, sendCompose, saveDraft,
            moveSelection, openReply, openForward,
            archiveSelected, deleteSelected, toggleStarSelected, toggleReadSelected,
            focusSearch, goAllMailboxes, goInbox, addInviteToCalendar,
            createFolder, renameFolder, moveFolder, deleteFolder, emptyFolder,
            subscribeFolder, fetchQuota,
            setView, loadContacts, createContact, updateContact, deleteContact,
            suggestRecipients, touchRecipients, selectContact, clearSelection,
            msgKey,
        };
    },
};

registry.category("services").add("ow_mail", owMailService);
