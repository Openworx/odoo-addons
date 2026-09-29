import { _t } from "@web/core/l10n/translation";
import { useBus } from "@web/core/utils/hooks";
import { patch } from "@web/core/utils/patch";
import { FormController } from "@web/views/form/form_controller";

import { OW_AI_OPEN_RECORD_CHAT } from "@ow_ai/web/ow_ai_chat_launcher_service";

/** Record chats: "Ask AI" in the systray and "Ask AI about this record" in the action menu. */
patch(FormController.prototype, {
    setup() {
        super.setup(...arguments);
        useBus(this.env.bus, OW_AI_OPEN_RECORD_CHAT, ({ detail: request }) => {
            if (this.owAiCanChatAboutRecord) {
                if (request) {
                    request.handled = true;
                }
                this.owAiOpenRecordChat();
            }
        });
    },

    /** The form of the current action (not a dialog's), for an AI user, on a real record model. */
    get owAiCanChatAboutRecord() {
        return Boolean(
            this.env.services["mail.store"]?.has_access_ow_ai &&
                this.env.services["ow_ai.chat_launcher"] &&
                !this.env.inDialog &&
                this.model.root.resModel !== "res.config.settings"
        );
    },

    /** Save the record if needed (like before any action menu item), then chat about it. */
    async owAiOpenRecordChat() {
        if (!(await this.shouldExecuteAction({}))) {
            return;
        }
        const record = this.model.root;
        if (!record.resId) {
            return;
        }
        return this.env.services["ow_ai.chat_launcher"].launchChat({
            interfaceKey: "record_chat",
            recordModel: record.resModel,
            recordId: record.resId,
            channelTitle: record.data.display_name || null,
        });
    },

    getStaticActionMenuItems() {
        return {
            ...super.getStaticActionMenuItems(...arguments),
            owAiAskAboutRecord: {
                isAvailable: () => this.owAiCanChatAboutRecord,
                sequence: 25,
                icon: "wand_stars",
                description: _t("Ask AI about this record"),
                callback: () => this.owAiOpenRecordChat(),
            },
        };
    },
});
