import { Component, signal } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { user } from "@web/core/user";
import { useService } from "@web/core/utils/hooks";

import { OW_AI_OPEN_RECORD_CHAT, OW_AI_USER_GROUP } from "@ow_ai/web/ow_ai_chat_launcher_service";

/**
 * The "Ask AI" systray button (AI users only): on a form view, a chat about
 * its record (see the form controller patch); anywhere else, a plain chat.
 */
export class OwAiSystrayAskAi extends Component {
    static template = "ow_ai.SystrayAskAi";

    setup() {
        super.setup();
        this.action = useService("action");
        this.launcher = useService("ow_ai.chat_launcher");
        this.isAiUser = signal(false);
        user.hasGroup(OW_AI_USER_GROUP).then((isAiUser) => this.isAiUser.set(isAiUser));
    }

    onClick() {
        if (this.action.currentController?.view?.type === "form") {
            const request = { handled: false };
            this.env.bus.trigger(OW_AI_OPEN_RECORD_CHAT, request);
            if (request.handled) {
                return;
            }
        }
        this.launcher.launchChat({ interfaceKey: "systray" });
    }
}

registry
    .category("systray")
    .add("ow_ai.systray_ask_ai", { Component: OwAiSystrayAskAi }, { sequence: 30 });
