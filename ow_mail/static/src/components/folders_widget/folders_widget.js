/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

/** MIME type used for drag-drop payloads when reordering folders within the widget. */
const FOLDER_MIME = "application/x-ow-folder";

/** Maps folder `kind` to its Font Awesome icon class for use in the widget tree. */
const SPECIAL_ICONS = {
    inbox: "fa-inbox",
    sent: "fa-paper-plane",
    drafts: "fa-file-o",
    archive: "fa-archive",
    spam: "fa-fire",
    trash: "fa-trash-o",
    custom: "fa-folder-o",
};

/** Display order for folder kinds when sorting a mixed-kind list. */
const KIND_ORDER = ["inbox", "drafts", "sent", "archive", "spam", "trash", "custom"];

/**
 * Infer the IMAP path delimiter from a flat list of folder records.
 * The server stores `full_path` as a raw IMAP path string (e.g.
 * `"INBOX/Work/Project"` or `"INBOX.Work.Project"`). The delimiter is not
 * stored explicitly, so the client must detect it by scanning for the first
 * `"/"` or `"."` occurrence. Falls back to `"/"` when no delimiter is found.
 * @param {Array<{ full_path: string }>} folders
 * @returns {string}
 */
function detectDelim(folders) {
    for (const f of folders) {
        if (f.full_path.includes("/")) return "/";
        if (f.full_path.includes(".")) return ".";
    }
    return "/";
}

/**
 * Build a nested tree structure from a flat array of folder records.
 * The delimiter is detected via `detectDelim` so that `"INBOX/Work/Project"`
 * becomes a child node of `"INBOX/Work"`. Within each level the sort order
 * is: special folders first (by `KIND_ORDER`) then alphabetically by name,
 * ensuring Inbox/Drafts/Sent always appear above custom folders.
 * @param {Array<{ full_path: string, kind: string, name: string }>} folders
 * @returns {{ roots: Array, delim: string }}
 */
function buildTree(folders) {
    const delim = detectDelim(folders);
    const byPath = new Map();
    const roots = [];
    const items = folders.map((f) => ({ ...f, children: [], parent: null }));
    for (const it of items) byPath.set(it.full_path, it);
    for (const it of items) {
        const parts = it.full_path.split(delim);
        parts.pop();
        const parentPath = parts.join(delim);
        const parent = parentPath && byPath.get(parentPath);
        if (parent) {
            it.parent = parent;
            parent.children.push(it);
        } else {
            roots.push(it);
        }
    }
    const sortFn = (a, b) => {
        const oa = KIND_ORDER.indexOf(a.kind);
        const ob = KIND_ORDER.indexOf(b.kind);
        if (oa !== ob) return oa - ob;
        return (a.name || "").localeCompare(b.name || "");
    };
    const walk = (list) => {
        list.sort(sortFn);
        list.forEach((n) => walk(n.children));
    };
    walk(roots);
    return { roots, delim };
}

/**
 * Account settings form widget for folder management.
 *
 * Embedded in the `ow.mail.account` form view (not the main mail UI). Lets
 * the user create, rename, move, delete, empty, and subscribe/unsubscribe
 * IMAP folders on a single account. Folder reorganization (drag-drop or move)
 * uses IMAP RENAME under the hood. After any mutating IMAP call, the widget
 * calls `_reloadRecord` to re-read the account's One2many folder list from
 * the server so the tree stays in sync.
 */
export class FoldersWidget extends Component {
    static template = "ow_mail.FoldersWidget";
    static props = {
        "*": true,
    };

    /**
     * Bootstrap the service if it hasn't loaded yet (the widget can be
     * rendered before the main Mailclient action has run `bootstrap()`).
     * Local `ui` state tracks the selected folder, search query, and
     * current drag-drop target.
     */
    setup() {
        this.mail = useService("ow_mail");
        this.state = useState(this.mail.state);
        this.ui = useState({
            selectedFolderId: null,
            search: "",
            dropTargetId: null,
        });
        onWillStart(async () => {
            if (!this.state.accounts.length) {
                await this.mail.bootstrap();
            }
        });
    }

    /** The Odoo record id of the account this widget is embedded in. */
    get accountId() {
        return this.props.record && this.props.record.resId;
    }

    /** The reactive account object from the mail service state. */
    get account() {
        return this.state.accounts.find((a) => a.id === this.accountId);
    }

    /** The nested tree for the current account, built by `buildTree`. */
    get tree() {
        const acc = this.account;
        if (!acc) return { roots: [], delim: "/" };
        return buildTree(acc.folders);
    }

    /** The folder object that is currently selected in the widget, or `null`. */
    get selectedFolder() {
        const acc = this.account;
        if (!acc) return null;
        return acc.folders.find((f) => f.id === this.ui.selectedFolderId) || null;
    }

    /**
     * A depth-annotated flat list derived from `tree.roots`, filtered by the
     * current search query. Used to render the subscribe-toggle list where a
     * flat traversal is easier to work with than the nested tree.
     */
    get flatTree() {
        return this.flatten(this.tree.roots);
    }

    /**
     * Recursively flatten a tree node list into `[{ node, depth }]` entries,
     * applying the current search filter. When a node matches, it and its
     * children are included at their natural depth. When a node does not
     * match, the children are still traversed (at the parent's depth) so
     * matching descendants are not dropped.
     * @param {Array} nodes
     * @param {number} [depth=0]
     * @param {Array} [out=[]]
     * @returns {Array<{ node: Object, depth: number }>}
     */
    flatten(nodes, depth = 0, out = []) {
        for (const n of nodes) {
            if (this.matchesSearch(n)) {
                out.push({ node: n, depth });
                this.flatten(n.children, depth + 1, out);
            } else {
                this.flatten(n.children, depth, out);
            }
        }
        return out;
    }

    /**
     * Return `true` when a folder node matches the current search query
     * (case-insensitive substring match on `full_path` or `name`).
     * Always returns `true` when the search box is empty.
     * @param {{ full_path: string, name: string }} node
     * @returns {boolean}
     */
    matchesSearch(node) {
        const q = (this.ui.search || "").toLowerCase();
        if (!q) return true;
        return (node.full_path || "").toLowerCase().includes(q)
            || (node.name || "").toLowerCase().includes(q);
    }

    /**
     * Return the Font Awesome icon class for a folder kind.
     * @param {string} kind
     * @returns {string}
     */
    iconForKind(kind) {
        return SPECIAL_ICONS[kind] || "fa-folder-o";
    }

    /**
     * Reload the account record in the form view after an IMAP mutation.
     * The folder list is a One2many on the account; after a create/rename/
     * delete/move the server-side tree has changed, so a full record reload
     * is required to pull fresh folder data into the widget.
     * @async
     */
    async _reloadRecord() {
        try {
            await this.props.record.load();
        } catch {}
    }

    // ---------- Folder actions ----------

    /**
     * Prompt for a name and create a new IMAP folder, either at the account
     * root or inside the currently selected folder.
     * @async
     */
    async onNew() {
        const acc = this.account;
        if (!acc) return;
        const parent = this.selectedFolder;
        const where = parent ? `inside "${parent.full_path}"` : `in ${acc.email}`;
        const name = window.prompt(`New folder ${where}:`);
        if (!name) return;
        await this.mail.createFolder(acc.id, name, parent ? parent.id : null);
        await this._reloadRecord();
    }

    /**
     * Prompt for a new name and issue an IMAP RENAME for the selected folder.
     * @async
     */
    async onRename() {
        const f = this.selectedFolder;
        if (!f) return;
        const name = window.prompt("Rename folder:", f.name);
        if (!name || name === f.name) return;
        await this.mail.renameFolder(f.id, name);
        await this._reloadRecord();
    }

    /**
     * Ask for confirmation then permanently delete the selected folder via
     * IMAP DELETE. The confirmation guard is important because IMAP DELETE
     * cannot be undone and also removes all messages in the folder.
     * @async
     */
    async onDelete() {
        const f = this.selectedFolder;
        if (!f) return;
        if (!window.confirm(`Delete "${f.full_path}"? This cannot be undone.`)) return;
        await this.mail.deleteFolder(f.id);
        this.ui.selectedFolderId = null;
        await this._reloadRecord();
    }

    /**
     * Ask for confirmation then permanently delete all messages in the
     * selected folder without removing the folder itself. Primarily used for
     * Trash and Spam — "empty" rather than "delete".
     * @async
     */
    async onEmpty() {
        const f = this.selectedFolder;
        if (!f) return;
        if (!window.confirm(`Empty "${f.full_path}"? All messages will be permanently deleted.`)) return;
        await this.mail.emptyFolder(f.id);
    }

    /**
     * Mark a folder as selected in the widget UI.
     * @param {{ id: number }} folder
     */
    onSelectFolder(folder) {
        this.ui.selectedFolderId = folder.id;
    }

    /**
     * Toggle the subscription state of a folder.
     * Unsubscribed folders are hidden in the main Sidebar; subscribing re-adds
     * them to the user's visible folder list.
     * @async
     * @param {Event} ev
     * @param {{ id: number, subscribed: boolean }} node
     */
    async onToggleSubscribe(ev, node) {
        ev.stopPropagation();
        await this.mail.subscribeFolder(node.id, !node.subscribed);
    }

    // ---------- Drag & drop ----------

    /**
     * Begin a folder drag. Only custom folders may be moved; special folders
     * (inbox, sent, …) cannot be reorganized via drag-drop.
     * @param {DragEvent} ev
     * @param {{ id: number, kind: string }} node
     */
    onDragStart(ev, node) {
        if (node.kind !== "custom") {
            ev.preventDefault();
            return;
        }
        ev.dataTransfer.setData(FOLDER_MIME, JSON.stringify({ folder_id: node.id }));
        ev.dataTransfer.effectAllowed = "move";
    }

    /**
     * Highlight a folder row as a valid drop target when a `FOLDER_MIME`
     * drag is in progress.
     * @param {DragEvent} ev
     * @param {{ id: number }} node
     */
    onDragOver(ev, node) {
        const types = ev.dataTransfer && ev.dataTransfer.types;
        if (!types || !Array.prototype.includes.call(types, FOLDER_MIME)) return;
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "move";
        this.ui.dropTargetId = node.id;
    }

    /**
     * Clear the drop-target highlight when the drag leaves a folder row.
     * @param {DragEvent} ev
     * @param {{ id: number }} node
     */
    onDragLeave(ev, node) {
        if (this.ui.dropTargetId === node.id) this.ui.dropTargetId = null;
    }

    /**
     * Move a folder by dropping it onto another folder.
     * Issues an IMAP RENAME that changes the path delimiter prefix, making
     * the dragged folder a child of the drop target. The new path is
     * constructed server-side using the account's detected delimiter.
     * @async
     * @param {DragEvent} ev
     * @param {{ id: number }} node  - The new parent folder
     */
    async onDrop(ev, node) {
        ev.preventDefault();
        this.ui.dropTargetId = null;
        const raw = ev.dataTransfer.getData(FOLDER_MIME);
        if (!raw) return;
        let data;
        try { data = JSON.parse(raw); } catch { return; }
        if (!data.folder_id || data.folder_id === node.id) return;
        await this.mail.moveFolder(data.folder_id, node.id);
        await this._reloadRecord();
    }

    /**
     * Highlight the root drop zone (the area outside any folder row) when a
     * folder drag is in progress. Dropping here promotes the folder to the
     * account root level.
     * @param {DragEvent} ev
     */
    onDragOverRoot(ev) {
        const types = ev.dataTransfer && ev.dataTransfer.types;
        if (!types || !Array.prototype.includes.call(types, FOLDER_MIME)) return;
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "move";
        this.ui.dropTargetId = "__root__";
    }

    /**
     * Promote a folder to the account root by dropping it on the empty root
     * zone. Issues an IMAP RENAME that strips any parent path prefix, using
     * the account's path delimiter.
     * @async
     * @param {DragEvent} ev
     */
    async onDropRoot(ev) {
        ev.preventDefault();
        this.ui.dropTargetId = null;
        const raw = ev.dataTransfer.getData(FOLDER_MIME);
        if (!raw) return;
        let data;
        try { data = JSON.parse(raw); } catch { return; }
        if (!data.folder_id) return;
        await this.mail.moveFolder(data.folder_id, null);
        await this._reloadRecord();
    }
}

registry.category("view_widgets").add("ow_folders_tree", {
    component: FoldersWidget,
});
