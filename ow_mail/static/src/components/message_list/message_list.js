/** @odoo-module **/

import { Component, useEffect, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { AvatarInitials } from "../avatar_initials/avatar_initials";
import { owColor } from "../../utils/colors";

/**
 * Message list pane — displays a paginated, filterable, sortable list of
 * message envelopes for the selected folder or tag.
 *
 * Selection model: `local.checked` is a plain object keyed by the composite
 * `folder_id:uid` string (produced by `keyOf(m)`). Using the composite key rather
 * than just `uid` keeps selections unambiguous in the unified All Mailboxes view,
 * where messages from different IMAP accounts may share the same UID integer.
 *
 * Thread view: when `this.mail.state.threadView` is active, messages are grouped
 * client-side by thread root (via `buildThreads()` in the service). Clicking a
 * thread row with multiple messages expands it inline; single-message threads
 * open directly.
 *
 * Drag-drop: dragging a message row serializes `{folder_id, uids, subject}` to
 * the `application/x-ow-mail` DataTransfer MIME type. The sidebar folder rows
 * act as drop targets and read this payload to perform an IMAP MOVE.
 */
export class MessageList extends Component {
    static template = "ow_mail.MessageList";
    static components = { AvatarInitials };

    /**
     * Returns the filter chip options shown above the list.
     *
     * The `to:me` option requires the current account's email address at
     * activation time (see `onFilter`) because the IMAP SEARCH criterion must
     * contain a literal address, not the string "me".
     *
     * @returns {{ id: string, label: string, icon: string }[]}
     */
    get filters() {
        return [
            { id: "all", label: _t("All"), icon: "fa-envelope-o" },
            { id: "unread", label: _t("Unread"), icon: "fa-envelope" },
            { id: "starred", label: _t("Flagged"), icon: "fa-flag-o" },
            { id: "tome", label: _t("To me"), icon: "fa-paper-plane-o" },
            { id: "hasattach", label: _t("Has files"), icon: "fa-paperclip" },
        ];
    }

    /**
     * Resolves the email address of the account that owns the currently selected
     * folder. Used by `onFilter("tome")` to build the literal `to:<email>` IMAP
     * search string, since the server has no "me" keyword.
     *
     * @returns {string} The account email, or an empty string when no account is found.
     */
    _currentAccountEmail() {
        const accId = this.state.selection.accountId;
        const acc = this.state.accounts.find((a) => a.id === accId) || this.state.accounts[0];
        return (acc && acc.email) || "";
    }

    /**
     * Returns the label for the active filter chip so the dropdown trigger can
     * display the current selection rather than the static "Filter" placeholder.
     *
     * @returns {string}
     */
    activeFilterLabel() {
        const active = this.filters.find((f) => this.isFilterActive(f.id));
        return active ? active.label : _t("Filter");
    }

    /**
     * Returns the sort field options for the sort dropdown.
     *
     * @returns {{ id: string, label: string, icon: string }[]}
     */
    get sortOptions() {
        return [
            { id: "date", label: _t("Date"), icon: "fa-calendar" },
            { id: "from", label: _t("From"), icon: "fa-user" },
            { id: "subject", label: _t("Subject"), icon: "fa-tag" },
            { id: "size", label: _t("Size"), icon: "fa-database" },
        ];
    }

    setup() {
        this.mail = useService("ow_mail");
        this.notification = useService("notification");
        this.state = useState(this.mail.state);
        this.local = useState({ search: this.state.selection.search || "", checked: {}, expandedThreads: {} });
        // Keep the search box in sync when the service resets the query
        // (folder/tag switch clears the search — the input must follow).
        useEffect(
            (search) => {
                if ((search || "") !== this.local.search) {
                    this.local.search = search || "";
                }
            },
            () => [this.state.selection.search]
        );
    }

    /**
     * Returns the composite identity key for a message envelope.
     * The key format `folder_id:uid` is the canonical message identity used
     * throughout the frontend — there is no persistent numeric record id.
     *
     * @param {{ folder_id: number, uid: number }} m
     * @returns {string}
     */
    keyOf(m) {
        return `${m.folder_id}:${m.uid}`;
    }

    /**
     * Resolve a message's `tag_ids` to tag objects from the service state.
     * Unknown ids (deleted tags whose keyword is still on the server) are
     * silently dropped.
     * @param {{ tag_ids: number[] }} m
     * @returns {Array<{id: number, name: string, color: number}>}
     */
    tagsOf(m) {
        if (!m.tag_ids || !m.tag_ids.length) {
            return [];
        }
        const byId = new Map(this.state.tags.map((t) => [t.id, t]));
        return m.tag_ids.map((id) => byId.get(id)).filter(Boolean);
    }

    /**
     * Inline style for a tag chip: solid palette color with white text.
     * @param {{ color: number }} tag
     * @returns {string}
     */
    tagChipStyle(tag) {
        return `background: ${owColor(tag.color)}; color: #fff;`;
    }

    /**
     * Whether a row needs its chips line (any tag or the Customer badge).
     * @param {{ tag_ids: number[], partner_id: number|false }} m
     * @returns {boolean}
     */
    hasChips(m) {
        return Boolean(m.partner_id || (m.tag_ids && m.tag_ids.length));
    }

    /**
     * Whether the currently selected folder is the Drafts special folder.
     * Determines row interaction: double-click opens a draft in the compose
     * window instead of the read-only message viewer.
     *
     * @returns {boolean}
     */
    get isDraftsFolder() {
        const fid = this.state.selection.folderId;
        if (!fid) return false;
        return this.state.accounts.some((a) => a.special && a.special.drafts === fid);
    }

    /**
     * Opens a message in the viewer on single click.
     * Does NOT open the compose window for drafts — that requires a double-click.
     *
     * @param {{ folder_id: number, uid: number }} m
     */
    onSelect(m) {
        this.mail.openMessage(m.folder_id, m.uid);
    }

    /**
     * Keyboard activation of a focused list row (Enter/Space) so the list
     * is operable without a mouse.
     *
     * @param {KeyboardEvent} ev
     * @param {{ folder_id: number, uid: number }} m
     */
    onRowKey(ev, m) {
        if (ev.key === "Enter" || ev.key === " ") {
            ev.preventDefault();
            this.onSelect(m);
        }
    }

    /**
     * Opens a draft in the compose window on double-click.
     * Only active when `isDraftsFolder` is true; no-ops otherwise.
     *
     * @async
     * @param {{ folder_id: number, uid: number }} m
     */
    async onDblClick(m) {
        if (this.isDraftsFolder) {
            await this.mail.editDraft(m.folder_id, m.uid);
        }
    }

    /**
     * Whether a message row should render as selected (highlighted).
     *
     * @param {{ folder_id: number, uid: number }} m
     * @returns {boolean}
     */
    isSelected(m) {
        return this.state.selectedKey === this.keyOf(m);
    }

    /**
     * Formats a message date for compact list display.
     * Same-day messages show time only (HH:MM); all other dates fall back to the
     * locale default via `toLocaleDateString()`.
     *
     * @param {string} iso - ISO 8601 date string from the envelope.
     * @returns {string}
     */
    formatDate(iso) {
        if (!iso) return "";
        const d = new Date(iso);
        const now = new Date();
        const sameDay = d.toDateString() === now.toDateString();
        if (sameDay) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        return d.toLocaleDateString();
    }

    /**
     * Toggles the starred (\\Flagged) flag on a single message without opening it.
     * Stops propagation so the row click handler does not also fire.
     *
     * @async
     * @param {MouseEvent} ev
     * @param {{ folder_id: number, uid: number, flags_flagged: boolean }} m
     */
    async onToggleFlag(ev, m) {
        ev.stopPropagation();
        await this.mail.runAction(m.folder_id, [m.uid], "toggle_flag",
            { state: !m.flags_flagged });
    }

    _findAccount(folderId) {
        return this.state.accounts.find((a) =>
            a.folders.some((f) => f.id === folderId)
        ) || this.state.accounts[0];
    }

    /**
     * Opens the message in the viewer and immediately opens a reply compose window.
     * The message must be fetched first so the reply can be pre-filled with headers
     * (In-Reply-To, References, quoted body).
     *
     * @async
     * @param {MouseEvent} ev
     * @param {{ folder_id: number, uid: number }} m
     */
    async onQuickReply(ev, m) {
        ev.stopPropagation();
        await this.mail.openMessage(m.folder_id, m.uid);
        this.mail.openReply(this.state.selectedMessage);
    }

    /**
     * Opens the message in the viewer and immediately opens a forward compose window.
     * The message must be fetched first so the forward can be pre-filled with the
     * original body and attachments.
     *
     * @async
     * @param {MouseEvent} ev
     * @param {{ folder_id: number, uid: number }} m
     */
    async onQuickForward(ev, m) {
        ev.stopPropagation();
        await this.mail.openMessage(m.folder_id, m.uid);
        this.mail.openForward(this.state.selectedMessage);
    }

    /**
     * Archive a single message.
     *
     * @async
     * @param {MouseEvent} ev
     * @param {{ folder_id: number, uid: number }} m
     */
    async onArchive(ev, m) {
        ev.stopPropagation();
        await this.mail.archiveMessages(m.folder_id, [m.uid]);
    }

    /**
     * Deletes a single message: moves it to Trash, or expunges it permanently
     * when already in the Trash folder.
     *
     * @async
     * @param {MouseEvent} ev
     * @param {{ folder_id: number, uid: number }} m
     */
    async onDelete(ev, m) {
        ev.stopPropagation();
        await this.mail.runAction(m.folder_id, [m.uid], "delete", {});
    }

    /**
     * Initiates a drag operation for a message row.
     * Serializes `{folder_id, uids, subject}` as JSON into the
     * `application/x-ow-mail` DataTransfer MIME type so that sidebar folder
     * drop targets can perform an IMAP MOVE with the correct source folder.
     *
     * @param {DragEvent} ev
     * @param {{ folder_id: number, uid: number, subject: string }} m
     */
    onDragStart(ev, m) {
        const payload = {
            folder_id: m.folder_id,
            uids: [m.uid],
            subject: m.subject,
        };
        ev.dataTransfer.setData("application/x-ow-mail", JSON.stringify(payload));
        ev.dataTransfer.effectAllowed = "move";
        ev.currentTarget.classList.add("o-ow-dragging");
    }

    /**
     * Cleans up the visual drag state added by `onDragStart`.
     *
     * @param {DragEvent} ev
     */
    onDragEnd(ev) {
        ev.currentTarget.classList.remove("o-ow-dragging");
    }

    /**
     * Triggers a search immediately when the user presses Enter.
     *
     * @param {KeyboardEvent} ev
     */
    onSearchKey(ev) {
        if (ev.key === "Enter") {
            this.mail.setSearch(this.local.search);
        }
    }

    /**
     * Debounced search: waits 300 ms after the last keystroke before issuing
     * the IMAP SEARCH round-trip, avoiding a request per character.
     */
    onSearchInput() {
        clearTimeout(this._searchTimer);
        this._searchTimer = setTimeout(() => {
            this.mail.setSearch(this.local.search);
        }, 300);
    }

    /**
     * Clears the search query and reloads the unfiltered message list.
     */
    onClearSearch() {
        this.local.search = "";
        this.mail.setSearch("");
    }

    /**
     * Activates a filter chip. Only one filter is active at a time.
     *
     * `tome` and `hasattach` are implemented as search queries rather than
     * server-side filter flags because the IMAP server needs a literal TO
     * address or a HASATTACHMENT criterion — they cannot be expressed as simple
     * flag filters.
     *
     * @param {string} id - Filter id from the `filters` array.
     */
    onFilter(id) {
        if (id === "tome") {
            const email = this._currentAccountEmail();
            const q = email ? `to:${email}` : "to:me";
            this.local.search = q;
            this.state.selection.filter = "all";
            this.mail.setSearch(q);
            return;
        }
        if (id === "hasattach") {
            const q = "has:attachment";
            this.local.search = q;
            this.state.selection.filter = "all";
            this.mail.setSearch(q);
            return;
        }
        this.local.search = "";
        this.state.selection.search = "";
        this.mail.setFilter(id);
    }

    /**
     * Returns whether a given filter id is currently the active one.
     * `tome` and `hasattach` are detected from the search string because they
     * are stored as search queries rather than filter flags.
     *
     * @param {string} id
     * @returns {boolean}
     */
    isFilterActive(id) {
        const search = this.state.selection.search || "";
        if (id === "tome") return /^to:\S+/.test(search);
        if (id === "hasattach") return search === "has:attachment";
        if (search) return false;
        return this.state.selection.filter === id;
    }

    /**
     * Sets the sort field; preserves the current sort direction.
     *
     * @param {Event} ev - Change event from the sort `<select>` element.
     */
    onSortSelect(ev) {
        this.mail.setSort(ev.target.value, this.state.sortOrder);
    }

    /**
     * Flips the sort direction between ascending and descending.
     * The new order is persisted to localStorage by the service.
     */
    onToggleSortOrder() {
        this.mail.setSort(this.state.sortBy, this.state.sortOrder === "desc" ? "asc" : "desc");
    }

    /* ---- thread view ---- */

    /**
     * Toggles conversation grouping on/off and clears any expanded thread state
     * so stale expansions from the previous view do not bleed through.
     * The preference is persisted to localStorage by the service.
     */
    onToggleThreadView() {
        this.local.expandedThreads = {};
        this.mail.toggleThreadView();
    }

    /**
     * Returns whether a thread row is currently expanded to show child messages.
     *
     * @param {string} threadId
     * @returns {boolean}
     */
    isThreadExpanded(threadId) {
        return !!this.local.expandedThreads[threadId];
    }

    /**
     * Expands or collapses a thread row. Single-message threads skip the
     * expand/collapse and open the message directly.
     *
     * @param {{ threadId: string, messages: object[] }} thread
     */
    onToggleThread(thread) {
        if (thread.messages.length <= 1) {
            this.onSelect(thread.messages[0]);
            return;
        }
        if (this.local.expandedThreads[thread.threadId]) {
            delete this.local.expandedThreads[thread.threadId];
        } else {
            this.local.expandedThreads[thread.threadId] = true;
        }
    }

    /**
     * Opens a specific message from within an expanded thread row.
     *
     * @param {{ folder_id: number, uid: number }} m
     */
    onSelectThreadMessage(m) {
        this.mail.openMessage(m.folder_id, m.uid);
    }

    /* ---- selection / bulk ---- */

    /**
     * Toggles the checkbox state for a message in the bulk-selection set.
     * Messages are stored in `local.checked` keyed by `folder_id:uid` so that
     * the All Mailboxes view can hold checked messages from multiple accounts.
     *
     * @param {MouseEvent} ev
     * @param {{ folder_id: number, uid: number }} m
     */
    onToggleCheck(ev, m) {
        ev.stopPropagation();
        const key = this.keyOf(m);
        if (this.local.checked[key]) {
            delete this.local.checked[key];
        } else {
            this.local.checked[key] = m;
        }
    }

    /**
     * Returns whether a message is currently checked for bulk action.
     *
     * @param {{ folder_id: number, uid: number }} m
     * @returns {boolean}
     */
    isChecked(m) {
        return !!this.local.checked[this.keyOf(m)];
    }

    /**
     * Total number of checked messages across all folders.
     *
     * @returns {number}
     */
    get checkedCount() {
        return Object.keys(this.local.checked).length;
    }

    /**
     * Flat array of all checked message envelope objects.
     *
     * @returns {object[]}
     */
    get checkedMessages() {
        return Object.values(this.local.checked);
    }

    /**
     * True when every message on the current page is checked.
     * Used to control the select-all checkbox state.
     *
     * @returns {boolean}
     */
    get allChecked() {
        return this.state.messages.length > 0 &&
            this.state.messages.every((m) => this.local.checked[this.keyOf(m)]);
    }

    /**
     * Selects all visible messages when not all are checked; clears all when
     * every visible message is already checked (toggle behaviour).
     */
    onToggleAll() {
        if (this.allChecked) {
            this.local.checked = {};
        } else {
            const checked = {};
            for (const m of this.state.messages) {
                checked[this.keyOf(m)] = m;
            }
            this.local.checked = checked;
        }
    }

    /**
     * Clears the entire checked-message selection.
     */
    clearChecked() {
        this.local.checked = {};
    }

    /**
     * Groups checked message UIDs by their source folder_id so that bulk
     * operations can issue one IMAP command per mailbox rather than one per
     * message, which is required for correct server-side batch processing.
     *
     * @returns {Object.<string, number[]>} Map of folder_id (string) to uid array.
     */
    _checkedUidsByFolder() {
        const byFolder = {};
        for (const m of this.checkedMessages) {
            if (!byFolder[m.folder_id]) byFolder[m.folder_id] = [];
            byFolder[m.folder_id].push(m.uid);
        }
        return byFolder;
    }

    /**
     * Archive all checked messages, dispatching one IMAP action per source folder.
     * Clears the selection when done.
     *
     * @async
     */
    async onBulkArchive() {
        const byFolder = this._checkedUidsByFolder();
        for (const [folderId, uids] of Object.entries(byFolder)) {
            await this.mail.archiveMessages(parseInt(folderId, 10), uids);
        }
        this.clearChecked();
    }

    /**
     * Deletes all checked messages, dispatching one IMAP action per source folder.
     * Clears the selection when done.
     *
     * @async
     */
    async onBulkDelete() {
        const byFolder = this._checkedUidsByFolder();
        for (const [folderId, uids] of Object.entries(byFolder)) {
            await this.mail.runAction(parseInt(folderId, 10), uids, "delete", {});
        }
        this.clearChecked();
    }

    /**
     * Marks all checked messages as read, dispatching one IMAP action per source folder.
     * Clears the selection when done.
     *
     * @async
     */
    async onBulkMarkRead() {
        const byFolder = this._checkedUidsByFolder();
        for (const [folderId, uids] of Object.entries(byFolder)) {
            await this.mail.runAction(parseInt(folderId, 10), uids, "mark_read", {});
        }
        this.clearChecked();
    }

    /**
     * Marks all checked messages as unread, dispatching one IMAP action per source folder.
     * Clears the selection when done.
     *
     * @async
     */
    async onBulkMarkUnread() {
        const byFolder = this._checkedUidsByFolder();
        for (const [folderId, uids] of Object.entries(byFolder)) {
            await this.mail.runAction(parseInt(folderId, 10), uids, "mark_unread", {});
        }
        this.clearChecked();
    }

    /**
     * Flags (stars) all checked messages, dispatching one IMAP action per source folder.
     * Always sets the `\\Flagged` flag — does not clear it if already set.
     * Clears the selection when done.
     *
     * @async
     */
    async onBulkToggleFlag() {
        const byFolder = this._checkedUidsByFolder();
        for (const [folderId, uids] of Object.entries(byFolder)) {
            await this.mail.runAction(parseInt(folderId, 10), uids, "toggle_flag", { state: true });
        }
        this.clearChecked();
    }

    /**
     * Moves all checked messages to the selected target folder.
     * Messages already in the target folder are skipped to avoid a no-op IMAP MOVE.
     * Clears the selection when done.
     *
     * @async
     * @param {Event} ev - Change event from the move-target `<select>` element.
     */
    async onBulkMove(ev) {
        const folderId = parseInt(ev.target.value, 10);
        if (!folderId) return;
        ev.target.value = "";
        const byFolder = this._checkedUidsByFolder();
        for (const [srcId, uids] of Object.entries(byFolder)) {
            if (parseInt(srcId, 10) === folderId) continue;
            await this.mail.runAction(parseInt(srcId, 10), uids, "move", { folder_id: folderId });
        }
        this.clearChecked();
    }

    /**
     * Inline style for a tag's colored dot in the bulk Label dropdown.
     * @param {{ color: number }} tag
     * @returns {string}
     */
    tagDotStyle(tag) {
        return `background: ${owColor(tag.color)};`;
    }

    /**
     * Add or remove one tag on all checked messages, dispatching one IMAP
     * action per source folder (single `+/-FLAGS` on the uid set server-side).
     * Clears the selection when done.
     *
     * @async
     * @param {number} tagId
     * @param {"add"|"remove"} mode
     */
    async onBulkTag(tagId, mode) {
        const byFolder = this._checkedUidsByFolder();
        const action = mode === "remove" ? "remove_tag" : "add_tag";
        for (const [folderId, uids] of Object.entries(byFolder)) {
            await this.mail.runAction(parseInt(folderId, 10), uids, action,
                { tag_id: tagId });
        }
        this.clearChecked();
    }

    /**
     * Opens a compose window pre-filled with subject lines of all checked messages.
     * Only subject lines are included — full content is not fetched — so a
     * notification informs the user that each message must be forwarded individually
     * to include its body.
     *
     * @async
     */
    async onBulkForward() {
        const subjects = this.checkedMessages.map((m) => m.subject).filter(Boolean);
        const label = subjects.length ? subjects[0] : "selected messages";
        this.mail.openCompose({
            subject: `Fwd: ${label}${subjects.length > 1 ? ` (+${subjects.length - 1})` : ""}`,
            body: "<p>Forwarding " + this.checkedMessages.length + " message(s).</p>",
        });
        this.notification.add(
            "Note: bulk forward attaches subject lines only. Open each message to forward full content.",
            { type: "info" }
        );
        this.clearChecked();
    }

    /**
     * Folders available as move targets for the current account.
     * Excludes the folder currently being viewed to prevent no-op moves.
     *
     * @returns {{ id: number, name: string }[]}
     */
    get moveTargetFolders() {
        const acc = this.state.accounts.find((a) => a.id === this.state.selection.accountId);
        if (!acc) return [];
        return acc.folders.filter((f) => f.subscribed && f.id !== this.state.selection.folderId);
    }

    /* ---- pagination ---- */

    /**
     * Total number of pages based on server-reported message count and page size.
     * Always at least 1 so pagination controls remain rendered on empty folders.
     *
     * @returns {number}
     */
    get totalPages() {
        return Math.max(1, Math.ceil(this.state.totalMessages / this.state.pageSize));
    }

    /**
     * 1-based index of the first message on the current page, for the "x–y of z" display.
     *
     * @returns {number}
     */
    get rangeStart() {
        return (this.state.page - 1) * this.state.pageSize + 1;
    }

    /**
     * 1-based index of the last message on the current page, capped at the total.
     *
     * @returns {number}
     */
    get rangeEnd() {
        return Math.min(this.state.page * this.state.pageSize, this.state.totalMessages);
    }

    /**
     * Array of page numbers rendered as pagination buttons.
     * Currently returns all pages; elision with '...' can be added here when
     * the total exceeds 7 without touching the template.
     *
     * @returns {number[]}
     */
    get pageList() {
        const pages = [];
        for (let i = 1; i <= this.totalPages; i++) pages.push(i);
        return pages;
    }

    /** Navigate to the first page. */
    onFirstPage() { this.mail.goToPage(1); }

    /** Navigate to the previous page. */
    onPrevPage() { this.mail.goToPage(this.state.page - 1); }

    /** Navigate to the next page. */
    onNextPage() { this.mail.goToPage(this.state.page + 1); }

    /** Navigate to the last page. */
    onLastPage() { this.mail.goToPage(this.totalPages); }

    /**
     * Jumps to a specific page chosen from the page number list.
     *
     * @param {Event} ev - Change event from the page `<select>` element.
     */
    onPageSelect(ev) {
        const val = parseInt(ev.target.value, 10);
        if (val) this.mail.goToPage(val);
    }
}
