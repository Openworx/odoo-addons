/** @odoo-module **/

import { Component, useState, onWillStart } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

/**
 * Contact management pane.
 *
 * Provides CRUD for `ow.mail.contact` records — a separate model from
 * `res.partner`. Supports a live search with 200 ms debounce so keystrokes
 * don't fire a server call on every character. Contact records are owned
 * per-user and are not shared across accounts.
 */
export class Contacts extends Component {
    static template = "ow_mail.Contacts";

    /**
     * Initialise local state for search input, edit mode (`"new"` | `"edit"` |
     * `null`), and the draft being edited. Loads contacts from the server on
     * first render if the store is empty.
     */
    setup() {
        this.mail = useService("ow_mail");
        this.state = useState(this.mail.state);
        this.local = useState({
            search: this.state.contactsSearch || "",
            editing: null,
            draft: this._emptyDraft(),
            searchTimer: null,
        });
        onWillStart(async () => {
            if (!this.state.contacts.length) {
                await this.mail.loadContacts();
            }
        });
    }

    /**
     * Return a blank contact object used to initialize the new-record form
     * and to reset the draft after a save or cancel. Having a single source
     * of truth for the empty shape avoids stale field values when toggling
     * between new/edit modes.
     * @returns {{ id: null, name: string, email: string, company: string, phone: string, note: string }}
     */
    _emptyDraft() {
        return { id: null, name: "", email: "", company: "", phone: "", note: "" };
    }

    /**
     * The currently selected contact object from the store, or `null` when
     * no contact is selected.
     * @returns {Object|null}
     */
    get selected() {
        const id = this.state.selectedContactId;
        if (!id) return null;
        return this.state.contacts.find((c) => c.id === id) || null;
    }

    /**
     * Handle typing in the search box. Updates local state immediately for
     * responsive UI and debounces the server request by 200 ms to avoid
     * a round-trip on every keystroke.
     * @param {InputEvent} ev
     */
    onSearchInput(ev) {
        const v = ev.target.value;
        this.local.search = v;
        if (this.local.searchTimer) clearTimeout(this.local.searchTimer);
        this.local.searchTimer = setTimeout(() => {
            this.mail.loadContacts(v);
        }, 200);
    }

    /**
     * Select a contact from the list. Exits edit mode so the detail panel
     * shows the saved record rather than the draft form.
     * @param {{ id: number }} contact
     */
    onSelect(contact) {
        this.mail.selectContact(contact.id);
        this.local.editing = null;
    }

    /** Switch to new-contact mode, resetting the draft form to empty fields. */
    onNew() {
        this.local.editing = "new";
        this.local.draft = this._emptyDraft();
        this.state.contactsDetailOpen = true;
    }

    /**
     * Switch to edit mode for the selected contact, pre-filling the draft
     * form with the current field values.
     */
    onEdit() {
        const c = this.selected;
        if (!c) return;
        this.local.editing = "edit";
        this.local.draft = {
            id: c.id,
            name: c.name || "",
            email: c.email || "",
            company: c.company || "",
            phone: c.phone || "",
            note: c.note || "",
        };
        this.state.contactsDetailOpen = true;
    }

    /**
     * Discard unsaved changes and return to read mode.
     * Hides the detail panel if no contact is selected (i.e. the user was
     * creating a new contact and cancelled).
     */
    onCancelEdit() {
        this.local.editing = null;
        this.local.draft = this._emptyDraft();
        if (!this.selected) this.state.contactsDetailOpen = false;
    }

    /**
     * Persist the draft. Calls `updateContact` when editing an existing record
     * (draft has an `id`) or `createContact` for a new one. After saving,
     * the draft is reset and edit mode is cleared. A missing email address
     * blocks save since email is the required unique identifier.
     * @async
     */
    async onSave() {
        const d = this.local.draft;
        if (!d.email.trim()) return;
        if (this.local.editing === "edit" && d.id) {
            await this.mail.updateContact(d.id, {
                name: d.name, email: d.email, company: d.company,
                phone: d.phone, note: d.note,
            });
        } else {
            const created = await this.mail.createContact({
                name: d.name, email: d.email, company: d.company,
                phone: d.phone, note: d.note,
            });
            if (created) this.mail.selectContact(created.id);
        }
        this.local.editing = null;
        this.local.draft = this._emptyDraft();
    }

    /**
     * Ask for confirmation then delete the selected contact.
     * The service receives an `ids` array rather than a single id to leave
     * the door open for future batch deletion without a signature change.
     * @async
     */
    async onDelete() {
        const c = this.selected;
        if (!c) return;
        if (!window.confirm(`Delete contact "${c.name || c.email}"?`)) return;
        await this.mail.deleteContact([c.id]);
        this.local.editing = null;
    }

    /**
     * Open a compose window pre-addressed to the selected contact.
     * Formats the `To:` header as `"Name" <email>` when a display name is
     * available, or bare `email` otherwise.
     */
    onCompose() {
        const c = this.selected;
        if (!c) return;
        const to = c.name ? `"${c.name}" <${c.email}>` : c.email;
        this.mail.openCompose({ to });
    }

}
