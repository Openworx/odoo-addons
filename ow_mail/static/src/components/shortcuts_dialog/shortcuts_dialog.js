/** @odoo-module **/

import { Component } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { _t } from "@web/core/l10n/translation";

/**
 * Read-only cheatsheet of every mail-client keyboard shortcut.
 * Opened with `?` (see hotkeys.js) or from the settings dialog.
 * The rows mirror the bindings registered in `registerMailHotkeys` —
 * keep both in sync when adding a shortcut.
 */
export class ShortcutsDialog extends Component {
    static template = "ow_mail.ShortcutsDialog";
    static components = { Dialog };
    static props = { close: Function };

    setup() {
        this.title = _t("Keyboard Shortcuts");
        this.groups = [
            {
                label: _t("Navigation"),
                rows: [
                    { keys: "j / k", label: _t("Next / previous message") },
                    { keys: "Esc", label: _t("Close the open message") },
                    { keys: "/", label: _t("Focus the search box") },
                    { keys: "?", label: _t("Show this cheatsheet") },
                ],
            },
            {
                label: _t("Actions"),
                rows: [
                    { keys: "c", label: _t("Compose a new message") },
                    { keys: "r", label: _t("Reply") },
                    { keys: "a", label: _t("Reply all") },
                    { keys: "f", label: _t("Forward") },
                    { keys: "e", label: _t("Archive") },
                    { keys: "Del / #", label: _t("Delete (move to Trash)") },
                    { keys: "s", label: _t("Toggle star") },
                    { keys: "u", label: _t("Toggle read / unread") },
                ],
            },
        ];
    }
}
