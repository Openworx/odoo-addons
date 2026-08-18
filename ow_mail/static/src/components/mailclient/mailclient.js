/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Sidebar } from "../sidebar/sidebar";
import { MessageList } from "../message_list/message_list";
import { MessageViewer } from "../message_viewer/message_viewer";
import { ComposeWindow } from "../compose_window/compose_window";
import { Contacts } from "../contacts/contacts";
import { ShortcutsDialog } from "../shortcuts_dialog/shortcuts_dialog";
import { registerMailHotkeys } from "./hotkeys";

/** `localStorage` key used to persist the sidebar collapsed state across page loads. */
const LS_KEY = "ow_mail.sidebar_collapsed";

/**
 * Root component for the OW Mail client action.
 *
 * Implements a three-pane layout: `Sidebar` (account/folder navigation) |
 * `MessageList` | `MessageViewer`. A floating stack of `ComposeWindow`
 * instances overlays the layout. On mobile the three panes collapse to a
 * single visible pane controlled by `isMobileDetail`.
 *
 * Registered as the `ow_mail.mailclient` client action so Odoo's action
 * manager mounts it when the user opens the mail menu item.
 */
export class Mailclient extends Component {
    static template = "ow_mail.Mailclient";
    static components = { Sidebar, MessageList, MessageViewer, ComposeWindow, Contacts };

    /**
     * Wire up the mail service, read the persisted sidebar-collapse preference
     * from `localStorage`, register keyboard shortcuts via `registerMailHotkeys`,
     * and fetch initial data. `onWillStart` blocks rendering until `bootstrap()`
     * has loaded accounts, folders, and tags. If the action was opened with a
     * `search` param (e.g. from a notification link), the search is applied
     * immediately after bootstrap.
     */
    setup() {
        this.mail = useService("ow_mail");
        this.dialog = useService("dialog");
        this.action = useService("action");
        this.state = useState(this.mail.state);
        this.ui = useState({ sidebarCollapsed: localStorage.getItem(LS_KEY) === "1" });
        registerMailHotkeys(this);
        onWillStart(async () => {
            await this.mail.bootstrap();
            const search = this.props.action?.params?.search;
            if (search) {
                await this.mail.searchAll(search);
            }
        });
    }

    /**
     * Whether the onboarding empty-state should be shown instead of the
     * list/viewer panes: bootstrap finished and the user has no usable
     * account yet (none at all, or none past the draft/error state).
     * @returns {boolean}
     */
    get needsOnboarding() {
        if (!this.state.bootstrapped || this.state.view !== "mail") {
            return false;
        }
        return !this.state.accounts.some(
            (a) => a.state === "confirmed" && a.folders.length);
    }

    /** First account-level error message, for the onboarding panel. */
    get onboardingError() {
        const broken = this.state.accounts.find(
            (a) => a.state === "error" && a.error_message);
        return broken ? broken.error_message : "";
    }

    /** Launch the Connect Mailbox wizard; it returns to the client on success. */
    onConnectMailbox() {
        this.action.doAction("ow_mail.action_ow_mail_connect_wizard");
    }

    /** Open the keyboard-shortcut cheatsheet dialog (hotkey `?`). */
    openShortcutsDialog() {
        this.dialog.add(ShortcutsDialog, {});
    }

    /**
     * Toggle the sidebar between collapsed and expanded on desktop.
     * Persists the choice to `localStorage` so it survives page reloads.
     */
    toggleSidebar() {
        this.ui.sidebarCollapsed = !this.ui.sidebarCollapsed;
        try {
            localStorage.setItem(LS_KEY, this.ui.sidebarCollapsed ? "1" : "0");
        } catch {}
    }

    /** Open the sidebar as a slide-over overlay on mobile. */
    openSidebar() {
        this.state.sidebarOverlayOpen = true;
    }

    /** Close the mobile sidebar overlay. */
    closeSidebar() {
        this.state.sidebarOverlayOpen = false;
    }

    /**
     * Handle the mobile back-navigation gesture.
     * In the contacts view, deselects the active contact and hides the detail
     * panel. In the mail view, clears the selected message so the list pane
     * becomes visible again.
     */
    onBack() {
        if (this.state.view === "contacts") {
            this.mail.selectContact(null);
            this.state.contactsDetailOpen = false;
        } else {
            this.mail.clearSelection();
        }
    }

    /**
     * Whether the UI is in a "detail view" state on mobile — i.e. only the
     * detail pane (message viewer or contact detail) should be visible and
     * the list pane should be hidden. Drives the mobile 3→1 pane transition
     * so the user always sees exactly one full-width pane at a time.
     * @returns {boolean}
     */
    get isMobileDetail() {
        const s = this.state;
        return (s.view === "mail" && !!s.selectedMessage)
            || (s.view === "contacts" && s.contactsDetailOpen);
    }

    /**
     * The title shown in the mobile top bar. Reflects the current pane so
     * the user knows where they are when the sidebar and list are hidden.
     * @returns {string}
     */
    get mobileTitle() {
        const s = this.state;
        if (s.view === "contacts") {
            return s.contactsDetailOpen ? "Contact" : "Contacts";
        }
        if (s.selectedMessage && s.selectedMessage.subject) {
            return s.selectedMessage.subject;
        }
        return "Mail";
    }
}

registry.category("actions").add("ow_mail.mailclient", Mailclient);
