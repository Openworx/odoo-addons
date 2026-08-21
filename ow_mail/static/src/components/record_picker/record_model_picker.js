/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { _t } from "@web/core/l10n/translation";

/**
 * "Other…" model picker for the create-record-from-email flow.
 *
 * Small dialog with a filter input over the mail-thread models the
 * current user may create (server-filtered list via
 * `/ow_mail/record/models`). Clicking an entry calls `props.onConfirm`
 * with the technical model name and closes the dialog.
 */
export class RecordModelPickerDialog extends Component {
    static template = "ow_mail.RecordModelPickerDialog";
    static components = { Dialog };
    static props = {
        models: Array, // [{model, name}]
        onConfirm: Function,
        close: Function,
    };

    setup() {
        this.local = useState({ filter: "" });
        this.title = _t("Create record from email");
    }

    get filteredModels() {
        const needle = this.local.filter.trim().toLowerCase();
        if (!needle) return this.props.models;
        return this.props.models.filter(
            (m) => m.name.toLowerCase().includes(needle) ||
                   m.model.toLowerCase().includes(needle));
    }

    onPick(model) {
        this.props.close();
        this.props.onConfirm(model);
    }
}
