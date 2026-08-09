/** @odoo-module **/

import { Component, useRef, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { owColor, OW_PALETTE, OW_COLOR_FALLBACK } from "../../utils/colors";

/** MIME type used to identify drag-drop payloads originating from MessageList. */
const DND_MIME = "application/x-ow-mail";

/** Maps folder `kind` to its Font Awesome icon class. Unlisted kinds fall back to a plain folder icon. */
const SPECIAL_ICONS = {
    inbox: "fa-inbox",
    sent: "fa-paper-plane",
    drafts: "fa-file-o",
    archive: "fa-archive",
    spam: "fa-ban",
    trash: "fa-trash-o",
};

/**
 * 12-entry color palette for account badge and header accents.
 * Index 0 is intentionally null (no color) so accounts with `color == 0`
 * render without an accent. Colors cycle by account position (index mod 12),
 * not by account id, giving stable-looking results regardless of which
 * accounts are loaded.
 */
const ACCOUNT_PALETTE = [
    null,
    "#F06050",
    "#F4A460",
    "#F7CD1F",
    "#6CC1ED",
    "#814968",
    "#EB7E7F",
    "#2C8397",
    "#475577",
    "#D6145F",
    "#30C381",
    "#9365B8",
];

/**
 * Three-column navigation sidebar.
 *
 * Renders the account list, the folder tree for each account, and the global
 * tag list. Handles drag-drop message moves: `MessageList` sets a
 * `DND_MIME`-typed payload on the drag event; dropping onto a folder row here
 * issues an IMAP MOVE via `runAction('move')`. Also works as a slide-over
 * drawer on mobile (controlled by `state.sidebarOverlayOpen`).
 */
export class Sidebar extends Component {
    static template = "ow_mail.Sidebar";
    static props = {
        collapsed: { type: Boolean, optional: true },
        toggleSidebar: { type: Function, optional: true },
    };

    /**
     * Wire up the central mail service, expose `state` and `dnd` as reactive
     * objects. `dnd.targetId` tracks which folder is currently highlighted
     * during a drag; `ui.syncingAccountId` guards against double-tap on the
     * sync button.
     */
    setup() {
        this.mail = useService("ow_mail");
        this.dialog = useService("dialog");
        this.state = useState(this.mail.state);
        this.dnd = useState({ targetId: null });
        this.ui = useState({ syncingAccountId: null, newTagName: "", colorPickerTagId: null });
        this.newTagInput = useRef("newTagInput");
    }

    /**
     * Return the Font Awesome class for a folder kind, e.g. `"fa-inbox"`.
     * Falls back to `"fa-folder-o"` for custom/unknown kinds.
     * @param {string} kind
     */
    iconForKind(kind) {
        return SPECIAL_ICONS[kind] || "fa-folder-o";
    }

    /**
     * Resolve the accent color for an account from `ACCOUNT_PALETTE`.
     * Uses `account.color` as a palette index (mod 12). Returns `null` when
     * the account has no assigned color (index 0).
     * @param {{ color: number }} account
     * @returns {string|null}
     */
    accountColor(account) {
        return ACCOUNT_PALETTE[account.color || 0] || null;
    }

    /**
     * Inline style for the account header row — adds a colored left border
     * when the account has an accent color, or an empty string otherwise.
     * @param {{ color: number }} account
     * @returns {string}
     */
    accountHeaderStyle(account) {
        const c = this.accountColor(account);
        return c ? `border-left: 4px solid ${c}; padding-left: 8px;` : "";
    }

    /**
     * Inline style for the small colored dot shown next to an account name.
     * @param {{ color: number }} account
     * @returns {string}
     */
    accountDotStyle(account) {
        const c = this.accountColor(account);
        return c ? `background: ${c};` : "";
    }

    /**
     * Format an unread/total count for display in a badge.
     * Numbers ≥ 10 000 are abbreviated to `"10k"` etc. to avoid overflow.
     * @param {number} n
     * @returns {string}
     */
    formatCount(n) {
        if (!n) return "";
        if (n >= 10000) return Math.floor(n / 1000) + "k";
        if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "k";
        return String(n);
    }

    /**
     * Build the tooltip string for a folder row.
     * Shows `"account@email — Full/Path (N unread)"` so the user can inspect
     * counts without cluttering every row with a badge — only the active
     * folder badge is shown inline; everything else surfaces on hover.
     * @param {{ email: string }} account
     * @param {{ full_path: string, name: string, unread_count: number }} folder
     * @returns {string}
     */
    folderTooltip(account, folder) {
        const label = folder.full_path || folder.name;
        const count = folder.unread_count ? ` (${folder.unread_count} unread)` : "";
        return `${account.email} — ${label}${count}`;
    }

    /**
     * Close the mobile slide-over drawer after a navigation action so the
     * user lands directly in the selected view.
     */
    _closeMobileDrawer() {
        if (this.state.viewport === "mobile") {
            this.state.sidebarOverlayOpen = false;
        }
    }

    /** Open a new compose window and dismiss the mobile drawer. */
    onCompose() {
        this.mail.openCompose({});
        this._closeMobileDrawer();
    }

    /** Switch to the unified All Mailboxes view (no account/folder filter). */
    onAllMailboxes() {
        this.mail.setView("mail");
        this.mail.selectFolder(null, null);
        this._closeMobileDrawer();
    }

    /** Switch to the Contacts pane. */
    onContacts() {
        this.mail.setView("contacts");
        this._closeMobileDrawer();
    }

    /**
     * Select a specific folder in an account and switch to the mail view.
     * @param {number} accountId
     * @param {number} folderId
     */
    onFolder(accountId, folderId) {
        this.mail.setView("mail");
        this.mail.selectFolder(accountId, folderId);
        this._closeMobileDrawer();
    }

    /**
     * Trigger an IMAP LIST + STATUS refresh for one account.
     * The `syncingAccountId` guard prevents a second concurrent sync if the
     * user double-taps the sync button before the first request completes.
     * @async
     * @param {number} accountId
     */
    async onSyncAccount(accountId) {
        if (this.ui.syncingAccountId) return;
        this.ui.syncingAccountId = accountId;
        try {
            await this.mail.sync(accountId);
        } finally {
            this.ui.syncingAccountId = null;
        }
    }

    /**
     * Filter the message list to all messages carrying the given tag keyword.
     * Clicking the already-active tag clears the filter again (toggle).
     * @param {number} tagId
     */
    onTag(tagId) {
        this.mail.setView("mail");
        this.mail.selectTag(this.state.selection.tagId === tagId ? null : tagId);
        this._closeMobileDrawer();
    }

    /**
     * Inline style for a tag's colored dot in the sidebar tag list.
     * @param {{ color: number }} tag
     * @returns {string}
     */
    tagDotStyle(tag) {
        return `background: ${owColor(tag.color)};`;
    }

    /**
     * The full color palette as `{index, color}` swatch entries for the
     * tag color picker. Index 0 renders as neutral gray ("no color").
     * @returns {Array<{index: number, color: string}>}
     */
    get palette() {
        return OW_PALETTE.map((c, index) => ({ index, color: c || OW_COLOR_FALLBACK }));
    }

    /**
     * Toggle the color picker popover for a tag (opened by clicking its dot).
     * @param {{ id: number }} tag
     */
    onTagDot(tag) {
        this.ui.colorPickerTagId = this.ui.colorPickerTagId === tag.id ? null : tag.id;
    }

    /** Close the tag color picker popover. */
    closeColorPicker() {
        this.ui.colorPickerTagId = null;
    }

    /** Swallow clicks inside the picker so the row's filter toggle doesn't fire. */
    onPickerClick() {}

    /**
     * Persist a new palette color for a tag and close the picker.
     * @async
     * @param {{ id: number }} tag
     * @param {number} colorIndex
     */
    async onPickColor(tag, colorIndex) {
        this.ui.colorPickerTagId = null;
        await this.mail.setTagColor(tag.id, colorIndex);
    }

    /** Create a tag from the "New tag…" input and clear the field. */
    async onCreateTag() {
        const name = this.ui.newTagName;
        this.ui.newTagName = "";
        // Clear the DOM value directly as well — the t-model patch alone
        // leaves the typed text in the focused input.
        if (this.newTagInput.el) {
            this.newTagInput.el.value = "";
        }
        await this.mail.createTag(name);
    }

    /**
     * Submit the new-tag input on Enter.
     * @param {KeyboardEvent} ev
     */
    onNewTagKey(ev) {
        if (ev.key === "Enter") {
            this.onCreateTag();
        }
    }

    /**
     * Delete a tag definition after an explicit confirmation dialog. The
     * IMAP keywords remain on the server; only the local label/color
     * mapping disappears.
     * @param {{ id: number, name: string }} tag
     */
    onDeleteTag(tag) {
        this.dialog.add(ConfirmationDialog, {
            title: _t("Delete tag"),
            body: _t('Delete the tag "%s"? Messages keep the label on the mail server, but it will no longer be shown or filterable.', tag.name),
            confirmLabel: _t("Delete"),
            cancelLabel: _t("Cancel"),
            confirm: () => this.mail.deleteTag(tag.id),
            cancel: () => {},
        });
    }

    /**
     * Return whether the given folder is the currently selected one.
     * @param {number} folderId
     * @returns {boolean}
     */
    isActiveFolder(folderId) {
        return this.state.selection.folderId === folderId;
    }

    /**
     * Return the special folders for an account in display order
     * (Inbox → Sent → Drafts → Archive → Spam → Trash).
     * Folders missing from `account.special` are omitted.
     * @param {{ special: Object, folders: Array }} account
     * @returns {Array}
     */
    specialFolders(account) {
        const order = ["inbox", "sent", "drafts", "archive", "spam", "trash"];
        const map = account.special || {};
        return order
            .filter((k) => map[k])
            .map((k) => account.folders.find((f) => f.id === map[k]))
            .filter(Boolean);
    }

    /**
     * Return `[{id, name, kind, unread_count, depth, ...}]` for the unified
     * folder tree of *account*. Specials come first in the fixed order
     * (Inbox, Sent, Drafts, Archive, Spam, Trash) and any custom folder
     * whose parent_id points at one of them is nested under it. Remaining
     * custom folders (no parent, or parent not visible) are appended at
     * the bottom, also as a nested tree. Unsubscribed folders are hidden.
     * @param {{ folders: Array, special: Object }} account
     * @returns {Array}
     */
    folderTree(account) {
        const visible = account.folders.filter((f) => f.subscribed !== false);
        const byId = new Map(visible.map((f) => [f.id, f]));
        const emitted = new Set();
        const childrenOf = (parentId) => visible
            .filter((f) => f.parent_id === parentId && !emitted.has(f.id))
            .sort((a, b) => a.name.localeCompare(b.name));
        const out = [];
        const walk = (node, depth, kind) => {
            if (emitted.has(node.id)) return;
            emitted.add(node.id);
            out.push({ ...node, depth, kind: kind || node.kind });
            for (const c of childrenOf(node.id)) walk(c, depth + 1, null);
        };
        const specials = this.specialFolders(account);
        for (const sp of specials) walk(sp, 0, sp.kind);
        // Custom roots: no parent, or parent not visible AND not a special
        // we already rendered (otherwise they'd appear twice).
        const customRoots = visible
            .filter((f) => !emitted.has(f.id) &&
                (!f.parent_id || !byId.has(f.parent_id)))
            .sort((a, b) => a.name.localeCompare(b.name));
        for (const r of customRoots) walk(r, 0, null);
        return out;
    }

    /**
     * Allow a drop when the dragged item carries a `DND_MIME` payload
     * (i.e. it came from `MessageList`). Sets the visual drop-target highlight.
     * @param {DragEvent} ev
     * @param {{ id: number }} folder
     */
    onDragOver(ev, folder) {
        const types = ev.dataTransfer && ev.dataTransfer.types;
        if (!types || !Array.prototype.includes.call(types, DND_MIME)) return;
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "move";
        this.dnd.targetId = folder.id;
    }

    /**
     * Clear the drop-target highlight when the drag leaves a folder row.
     * @param {DragEvent} ev
     * @param {{ id: number }} folder
     */
    onDragLeave(ev, folder) {
        if (this.dnd.targetId === folder.id) {
            this.dnd.targetId = null;
        }
    }

    /**
     * Handle a message drop onto a folder row.
     * Deserializes the `DND_MIME` JSON payload (set by `MessageList`) to
     * retrieve the source `folder_id` and `uids`, then calls
     * `runAction('move')` which issues an IMAP UID MOVE to the target folder.
     * Drops onto the same folder or malformed payloads are silently ignored.
     * @async
     * @param {DragEvent} ev
     * @param {{ id: number }} folder  - The drop-target folder
     */
    async onDrop(ev, folder) {
        ev.preventDefault();
        this.dnd.targetId = null;
        let payload;
        try {
            payload = JSON.parse(ev.dataTransfer.getData(DND_MIME) || "{}");
        } catch {
            return;
        }
        if (!payload.folder_id || !payload.uids || !payload.uids.length) return;
        if (payload.folder_id === folder.id) return;
        await this.mail.runAction(payload.folder_id, payload.uids, "move",
            { folder_id: folder.id });
    }

    /**
     * Return a flat, depth-annotated list of custom (non-special) folders for
     * an account, sorted alphabetically at each level. Unsubscribed folders
     * are excluded — the user explicitly hid them in Settings. Children whose
     * parent is not visible (e.g. a subscribed child of an unsubscribed parent)
     * are promoted to root level (depth 0).
     * @param {{ folders: Array, special: Object }} account
     * @returns {Array<{ depth: number }>}
     */
    customFolders(account) {
        const specialIds = new Set(Object.values(account.special || {}).filter(Boolean));
        const visible = account.folders.filter(
            (f) => !specialIds.has(f.id) && f.subscribed !== false);
        const byId = new Map(visible.map((f) => [f.id, f]));
        const out = [];
        const walk = (node, depth) => {
            out.push({ ...node, depth });
            const children = visible
                .filter((f) => f.parent_id === node.id)
                .sort((a, b) => a.name.localeCompare(b.name));
            for (const c of children) walk(c, depth + 1);
        };
        const roots = visible
            .filter((f) => !f.parent_id || !byId.has(f.parent_id))
            .sort((a, b) => a.name.localeCompare(b.name));
        for (const r of roots) walk(r, 0);
        return out;
    }
}
