import { Component, proxy, t, useProps } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

import { OwAiToolSummary } from "@ow_ai/discuss/ow_ai_tool_summary";

/**
 * Consecutive agent steps and tool summaries of an AI chat, folded into one
 * "Steps taken" block (built by the thread patch). `open` is the default
 * state (unfolded while the assistant is still working on them); a click
 * overrides it for as long as the block exists.
 */
export class OwAiStepsGroup extends Component {
    static template = "ow_ai.StepsGroup";
    static components = { OwAiToolSummary };

    setup() {
        super.setup();
        this.store = useService("mail.store");
        this.props = useProps({
            messages: t.array(t.instanceOf(this.store["mail.message"])),
            open: t.boolean().optional(false),
        });
        /** `undefined` until the user folds/unfolds the block */
        this.state = proxy({ userOpen: undefined });
    }

    get isOpen() {
        return this.state.userOpen ?? this.props.open;
    }

    toggle() {
        this.state.userOpen = !this.isOpen;
    }
}
