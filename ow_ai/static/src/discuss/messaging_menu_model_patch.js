import {
    MENU_TABS,
    MessagingMenu,
} from "@mail/core/public_web/messaging_menu/messaging_menu_model";
import { fields } from "@mail/model/export";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { patch } from "@web/core/utils/patch";

/** Id of the AI tab, known server-side by `controllers/messaging_menu.py`. */
export const OW_AI_CHAT_TAB = "ow-ai-chat";
MENU_TABS.OW_AI_CHAT = OW_AI_CHAT_TAB;

/**
 * The "AI" tab of the messaging menu and of the Discuss sidebar (AI users
 * only): the user's AI chats, counted when unread, and a "New chat" button
 * (in the web client, where the launcher exists).
 */
patch(MessagingMenu.prototype, {
    setup() {
        super.setup(...arguments);
        this.owAiChatTab = fields.One("MessagingMenuTab", {
            compute() {
                if (!this.store.has_access_ow_ai) {
                    return;
                }
                // the launcher service only exists in the web client (not on the public page);
                // the registry, unlike the services, is complete when this computes
                const canLaunch = registry.category("services").contains("ow_ai.chat_launcher");
                return {
                    id: OW_AI_CHAT_TAB,
                    recordType: "discuss.channel",
                    includesChannel: (channel) =>
                        channel.channel_type === "ow_ai_chat" &&
                        Boolean(channel.self_member_id?.is_pinned),
                    icon: "wand_stars",
                    sequence: 20,
                    label: _t("OW AI"),
                    emptyState: {
                        title: _t("No AI chat yet"),
                        subtitle: _t("Ask the assistant about your data, or have it do the work."),
                    },
                    filters: [
                        {
                            id: "ow-ai-chat-unread",
                            text: _t("Unread"),
                            includesChannel: (channel) => channel.isUnread,
                        },
                    ],
                    actions: canLaunch
                        ? [
                              {
                                  id: "ow_ai_new_chat",
                                  icon: "add",
                                  text: _t("New chat"),
                                  title: _t("New AI chat"),
                                  onClick: () =>
                                      this.store.env.services["ow_ai.chat_launcher"].launchChat(),
                              },
                          ]
                        : [],
                };
            },
            eager: true,
        });
    },
});
