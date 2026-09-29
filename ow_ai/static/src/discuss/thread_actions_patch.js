import { ACTION_TAGS } from "@mail/core/common/action";
import { registerThreadAction, ThreadAction } from "@mail/core/common/thread_actions";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";

/**
 * The only thread actions of an AI chat: window handling, search, rename,
 * favorites, attachments and deleting the chat. Invitations, members, calls,
 * notification settings, leaving… make no sense with an assistant.
 */
const OW_AI_THREAD_ACTIONS = new Set([
    "close",
    "fold-chat-window",
    "expand-discuss",
    "search-messages",
    "rename-thread",
    "add-to-favorites",
    "remove-from-favorites",
    "attachments",
    "ow_ai_delete_chat",
]);

registerThreadAction("ow_ai_delete_chat", {
    condition: ({ channel, owner }) => Boolean(channel?.isOwAiChat) && !owner.env.inMeetingView,
    icon: "delete",
    name: _t("Delete Chat"),
    onSelected: ({ channel, store }) => {
        store.env.services.dialog.add(ConfirmationDialog, {
            title: _t("Delete Chat"),
            body: _t("Delete this chat and all its messages? This cannot be undone."),
            confirmLabel: _t("Delete"),
            confirmClass: "btn-danger",
            confirm: async () => {
                try {
                    await channel.deleteOwAiChat();
                } catch (error) {
                    store.env.services.notification.add(
                        error?.data?.message || _t("The chat could not be deleted."),
                        { type: "danger" }
                    );
                }
            },
            cancel: () => {},
        });
    },
    sequence: 30,
    sequenceGroup: 40,
    tags: ACTION_TAGS.DANGER,
});

patch(ThreadAction.prototype, {
    _condition({ channel }) {
        if (
            channel?.isOwAiChat &&
            !this.definition.isMoreAction &&
            !OW_AI_THREAD_ACTIONS.has(this.id)
        ) {
            return false;
        }
        return super._condition(...arguments);
    },
});
