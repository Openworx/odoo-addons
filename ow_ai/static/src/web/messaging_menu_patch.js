import { MessagingMenu } from "@mail/core/public_web/messaging_menu";
import { patch } from "@web/core/utils/patch";

import { OW_AI_CHAT_TAB } from "@ow_ai/core/public_web/messaging_menu_patch";

/**
 * The "AI" tab in the web client: its header filter on desktop and its
 * "New chat" button (the launcher only exists in the web client), see
 * `messaging_menu_patch.xml`.
 *
 * @type {import("@mail/core/public_web/messaging_menu").MessagingMenu}
 */
const messagingMenuPatch = {
    get isOwAiTabActive() {
        return Boolean(this.owAiTab) && this.store.discuss.activeTab === OW_AI_CHAT_TAB;
    },
    onClickOwAiTab() {
        this.store.discuss.activeTab = OW_AI_CHAT_TAB;
    },
    onClickOwAiNewChat() {
        this.env.services["ow_ai.chat_launcher"].launchChat();
        if (!this.ui.isSmall && !this.env.inDiscussApp) {
            this.dropdown.close();
        }
    },
};

patch(MessagingMenu.prototype, messagingMenuPatch);
