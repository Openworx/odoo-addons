/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { useService } from "@web/core/utils/hooks";

/**
 * Modal dialog for inspecting the raw RFC 822 source of a message.
 *
 * Fetches the unprocessed email bytes from
 * `GET /ow_mail/message/source/<folder_id>/<uid>` on mount and renders them
 * in a scrollable pre-formatted view. Useful for debugging headers, MIME
 * structure, or verifying DKIM signatures. Also exposes an "open in new tab"
 * action so the user can save or search the source outside the dialog.
 */
export class MessageSourceDialog extends Component {
    static template = "ow_mail.MessageSourceDialog";
    static components = { Dialog };
    static props = {
        folderId: Number,
        uid: Number,
        subject: { type: String, optional: true },
        close: Function,
    };

    /**
     * Fetch the raw message source before the dialog renders.
     * Loading and error state are tracked so the template can show a spinner
     * or an error message instead of blank content.
     */
    setup() {
        this.mail = useService("ow_mail");
        this.state = useState({ text: "", loading: true, error: "" });
        onWillStart(async () => {
            try {
                const res = await fetch(
                    `/ow_mail/message/source/${this.props.folderId}/${this.props.uid}`
                );
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                this.state.text = await res.text();
            } catch (e) {
                this.state.error = String(e);
            } finally {
                this.state.loading = false;
            }
        });
    }

    /**
     * The dialog title. Shows `"Original — <Subject>"` when the subject is
     * known, or the generic fallback `"Original source"`.
     * @returns {string}
     */
    get title() {
        return this.props.subject
            ? `Original — ${this.props.subject}`
            : "Original source";
    }

    /**
     * Open the raw source URL in a new browser tab (with `noopener`).
     * Allows the user to use the browser's native Save As / Find in page
     * features on the full source text.
     */
    openInTab() {
        window.open(
            `/ow_mail/message/source/${this.props.folderId}/${this.props.uid}`,
            "_blank",
            "noopener"
        );
    }

    /** Theme scope classes for content rendered outside .o-ow-mail. */
    get themeClass() {
        return this.mail.state.darkMode ? "o-ow-themed o-ow-dark" : "o-ow-themed";
    }
}
