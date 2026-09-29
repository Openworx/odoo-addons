import { Component, t, useProps } from "@odoo/owl";

/**
 * The on/off switch drawn next to a composer "AI chat settings" action; the
 * action itself (the whole menu item) does the toggling.
 */
export class OwAiToggleIndicator extends Component {
    static template = "ow_ai.ToggleIndicator";

    setup() {
        super.setup();
        this.props = useProps({ checked: t.boolean() });
    }
}
