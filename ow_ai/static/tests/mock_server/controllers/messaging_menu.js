import { messagingMenuHelpers } from "@mail/../tests/mock_server/controllers/discuss/messaging_menu";

import { patch } from "@web/core/utils/patch";

// Mirrors `controllers/messaging_menu.py` (OwAiMessagingMenuController): the
// `ow-ai-chat` tab and its `ow-ai-chat-unread` filter, on top of mail's tabs.

patch(messagingMenuHelpers, {
    _get_menu_tab_domain(env, tab_id) {
        if (tab_id === "ow-ai-chat") {
            return [
                ["channel_type", "=", "ow_ai_chat"],
                ["self_member_id.is_pinned", "=", true],
            ];
        }
        return super._get_menu_tab_domain(env, tab_id);
    },
    _get_menu_tab_filter_domain(env, tab_id, filter_id) {
        if (tab_id === "ow-ai-chat" && filter_id === "ow-ai-chat-unread") {
            return [["self_member_id.is_unread", "=", true]];
        }
        return super._get_menu_tab_filter_domain(env, tab_id, filter_id);
    },
});
