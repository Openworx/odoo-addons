/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

/**
 * In-client preferences dialog (gear button in the sidebar footer).
 *
 * Edits the per-user `ow.mail.preferences` record through the ow_mail
 * service: mark-read delay, conversation-view default, plus shortcuts to
 * the account form (signature settings) and the keyboard cheatsheet.
 * Values are saved on "Save"; "Discard" leaves the stored preferences
 * untouched.
 */
export class SettingsDialog extends Component {
    static template = "ow_mail.SettingsDialog";
    static components = { Dialog };
    static props = {
        close: Function,
        openShortcuts: { type: Function, optional: true },
    };

    setup() {
        this.mail = useService("ow_mail");
        this.action = useService("action");
        this.state = useState(this.mail.state);
        this.local = useState({
            mark_read_delay: String(this.mail.state.prefs.mark_read_delay || 0),
            thread_view_default: this.mail.state.prefs.thread_view_default,
            stacked_threads: this.mail.state.prefs.stacked_threads,
            theme: this.mail.state.prefs.theme || "system",
        });
        this.title = _t("Mail Settings");
    }


    /** Theme scope classes for content rendered outside .o-ow-mail. */
    get themeClass() {
        return this.mail.state.darkMode ? "o-ow-themed o-ow-dark" : "o-ow-themed";
    }

    /** Options for the theme select. */
    get themeOptions() {
        return [
            { value: "system", label: _t("Follow Odoo theme / system") },
            { value: "light", label: _t("Light") },
            { value: "dark", label: _t("Dark") },
        ];
    }

    /** Options for the mark-read-delay select. */
    get delayOptions() {
        return [
            { value: "0", label: _t("Immediately") },
            { value: "2", label: _t("After 2 seconds") },
            { value: "5", label: _t("After 5 seconds") },
            { value: "-1", label: _t("Only manually") },
        ];
    }

    async onSave() {
        const ok = await this.mail.savePrefs({
            mark_read_delay: parseInt(this.local.mark_read_delay, 10) || 0,
            thread_view_default: !!this.local.thread_view_default,
            stacked_threads: !!this.local.stacked_threads,
            theme: this.local.theme,
        });
        if (ok) {
            this.props.close();
        }
    }

    /** Open the account form (signature, notifications, servers) in a dialog. */
    onOpenAccount() {
        const acc = this.state.accounts.find(
            (a) => a.id === this.state.selection.accountId
        ) || this.state.accounts[0];
        if (!acc) {
            return;
        }
        this.props.close();
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "ow.mail.account",
            res_id: acc.id,
            views: [[false, "form"]],
            target: "new",
        });
    }

    onShowShortcuts() {
        this.props.close();
        if (this.props.openShortcuts) {
            this.props.openShortcuts();
        }
    }
}
