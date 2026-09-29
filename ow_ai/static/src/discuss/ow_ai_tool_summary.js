import { Component, proxy, t, useProps } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { useService } from "@web/core/utils/hooks";

/**
 * The server names tool icons with Odoo 20 icon names (Material Symbols,
 * e.g. `search`), carried as `<i class="oi" data-icon="search">` -- the
 * same convention `view_button.js` uses for a button's `icon=`. This is a
 * pass-through: `DEFAULT_ICON` only covers a summary with no icon at all.
 */
const DEFAULT_ICON = "settings";

/**
 * One tool call of the assistant, from its summary message
 * (`<div class="o_ow_ai_tool_summary" data-id="<call id>" data-oe-id="<event id>">`):
 * a compact row; internal users can unfold the call's arguments
 * (`ow.ai.session.event.get_tool_params`, fetched once).
 */
export class OwAiToolSummary extends Component {
    static template = "ow_ai.ToolSummary";

    setup() {
        super.setup();
        this.store = useService("mail.store");
        this.orm = useService("orm");
        this.props = useProps({ message: t.instanceOf(this.store["mail.message"]) });
        this.state = proxy({ open: false, loading: false, params: undefined, error: undefined });
    }

    get summaryEl() {
        return this.props.message.bodyEl?.querySelector(".o_ow_ai_tool_summary");
    }

    get text() {
        return (this.summaryEl ?? this.props.message.bodyEl)?.textContent.trim() ?? "";
    }

    get icon() {
        const iconEl = this.summaryEl?.querySelector("i");
        return iconEl?.dataset.icon || DEFAULT_ICON;
    }

    get callId() {
        return this.summaryEl?.dataset.id || undefined;
    }

    get eventId() {
        return Number(this.summaryEl?.dataset.oeId) || undefined;
    }

    /** Arguments are shown to internal users only (the server checks access too). */
    get canShowDetails() {
        return Boolean(this.store.self_user?.share === false && this.callId && this.eventId);
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
