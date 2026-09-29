import { Component, useState } from "@odoo/owl";

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
    static props = {
        /** the agent steps and tool summaries (`mail.message` records), in order */
        messages: { type: Array, element: Object },
        open: { type: Boolean, optional: true },
    };
    static defaultProps = { open: false };

    setup() {
        super.setup();
        /** `undefined` until the user folds/unfolds the block */
        this.state = useState({ userOpen: undefined });
    }

    get isOpen() {
        return this.state.userOpen ?? this.props.open;
    }

    toggle() {
        this.state.userOpen = !this.isOpen;
    }
}
