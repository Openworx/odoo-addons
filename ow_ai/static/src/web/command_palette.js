import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { user } from "@web/core/user";
import { useService } from "@web/core/utils/hooks";

import { OW_AI_USER_GROUP } from "@ow_ai/web/ow_ai_chat_launcher_service";

registry.category("command_categories").add("ow_ai", { name: _t("OW AI") }, { sequence: 20 });

/**
 * AI users get "Open AI chat" and, once they typed something, "Ask AI: <what
 * they typed>", which opens a chat and posts it as the first message.
 */
registry.category("command_provider").add("ow_ai", {
    async provide({ searchValue = "" } = {}) {
        const launcher = useService("ow_ai.chat_launcher");
        if (!(await user.hasGroup(OW_AI_USER_GROUP))) {
            return [];
        }
        const commands = [
            {
                name: _t("Open AI chat"),
                category: "ow_ai",
                action() {
                    launcher.launchChat();
                },
            },
        ];
        const query = searchValue.trim();
        if (query) {
            commands.unshift({
                // the raw search value, so that the palette's fuzzy lookup keeps it
                name: _t("Ask AI: %s", searchValue),
                category: "ow_ai",
                action() {
                    launcher.launchChat({ userMessage: query });
                },
            });
        }
        return commands;
    },
});
