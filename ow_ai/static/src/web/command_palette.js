import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { user } from "@web/core/user";

import { OW_AI_USER_GROUP } from "@ow_ai/web/ow_ai_chat_launcher_service";

registry.category("command_categories").add("ow_ai", { name: _t("OW AI") }, { sequence: 20 });

/**
 * AI users get "Open AI chat" and, once they typed something, "Ask AI: <what
 * they typed>", which opens a chat and posts it as the first message.
 */
registry.category("command_provider").add("ow_ai", {
    /**
     * @param {import("@web/env").OdooEnv} env
     * @param {{ searchValue?: string }} options
     */
    async provide(env, { searchValue = "" } = {}) {
        const launcher = env.services["ow_ai.chat_launcher"];
        if (!launcher || !(await user.hasGroup(OW_AI_USER_GROUP))) {
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
