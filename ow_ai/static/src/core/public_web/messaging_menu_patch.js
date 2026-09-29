import { MessagingMenu } from "@mail/core/public_web/messaging_menu";
import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";

/** Id of the AI tab: the `channel_type` of the chats it lists (`store.menuThreads`). */
export const OW_AI_CHAT_TAB = "ow_ai_chat";

/**
 * The "AI" tab of the messaging menu (AI users only): the user's AI chats,
 * counted when unread. Mobile shows it with mail's tabs (`_tabs`), desktop in
 * the header (`web/messaging_menu_patch.xml`).
 *
 * @type {import("@mail/core/public_web/messaging_menu").MessagingMenu}
 */
const messagingMenuPatch = {
    /** @returns {{id: string, icon: string, label: string, sequence: number, counter: number}|undefined} */
    get owAiTab() {
        if (!this.store.has_access_ow_ai) {
            return undefined;
        }
        return {
            counter: this.store.discuss.owAiChats.reduce(
                (count, thread) =>
                    thread.self_member_id?.message_unread_counter > 0 ? count + 1 : count,
                0
            ),
            icon: "fa fa-magic",
            id: OW_AI_CHAT_TAB,
            label: _t("OW AI"),
            sequence: 30,
        };
    },
    /** @override */
    get _tabs() {
        const tabs = super._tabs;
        const owAiTab = this.owAiTab;
        if (owAiTab) {
            tabs.push(owAiTab);
        }
        return tabs;
    },
};

patch(MessagingMenu.prototype, messagingMenuPatch);
