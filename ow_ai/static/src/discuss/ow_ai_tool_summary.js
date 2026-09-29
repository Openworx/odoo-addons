import { Component, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { useService } from "@web/core/utils/hooks";

/**
 * The server writes a tool's icon as Font Awesome classes
 * (`<i class="fa fa-search">`, `engine/html_output.py::ICON_CLASSES`; Odoo 19
 * renders `fa-*` classes, not `data-icon`). This is a pass-through:
 * `DEFAULT_ICON` only covers a summary with no icon at all.
 */
const DEFAULT_ICON = "fa-cog";

/**
 * One tool call of the assistant, from its summary message
 * (`<div class="o_ow_ai_tool_summary" data-id="<call id>" data-oe-id="<event id>">`):
 * a compact row; internal users can unfold the call's arguments
 * (`ow.ai.session.event.get_tool_params`, fetched once).
 */
export class OwAiToolSummary extends Component {
    static template = "ow_ai.ToolSummary";
    static props = {
        /** the tool summary (a `mail.message` record) */
        message: { type: Object },
    };

    setup() {
        super.setup();
        this.store = useService("mail.store");
        this.orm = useService("orm");
        this.state = useState({ open: false, loading: false, params: undefined, error: undefined });
    }

    /** The summary block of the message body (`message.owAiBody`, parsed once per body). */
    get summary() {
        return this.props.message.owAiBody.toolSummary;
    }

    get text() {
        return this.summary?.text ?? this.props.message.owAiBody.text;
    }

    get icon() {
        return this.summary?.icon || DEFAULT_ICON;
    }

    get callId() {
        return this.summary?.callId;
    }

    get eventId() {
        return this.summary?.eventId;
    }

    /** Arguments are shown to internal users only (the server checks access too). */
    get canShowDetails() {
        return Boolean(
            this.store.self.main_user_id?.share === false && this.callId && this.eventId
        );
    }

    get paramsText() {
        return JSON.stringify(this.state.params ?? {}, null, 2);
    }

    async toggle() {
        if (!this.canShowDetails) {
            return;
        }
        this.state.open = !this.state.open;
        if (!this.state.open || this.state.params !== undefined || this.state.loading) {
            return;
        }
        this.state.loading = true;
        this.state.error = undefined;
        try {
            this.state.params = await this.orm.silent.call(
                "ow.ai.session.event",
                "get_tool_params",
                [[this.eventId], this.callId]
            );
        } catch (error) {
            this.state.error =
                error?.data?.message || _t("The details of this step could not be loaded.");
        } finally {
            this.state.loading = false;
        }
    }
}
