import { MessageAction, registerMessageAction } from "@mail/core/common/message_actions";
import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";

/** The only message actions of an AI chat; mail's others are hidden there. */
const OW_AI_MESSAGE_ACTIONS = new Set([
    "copy-message",
    "ow_ai_send_as_message",
    "ow_ai_log_note",
    "ow_ai_use_this",
]);

/** @param {{message: import("models").Message, thread?: import("models").Thread}} params */
function getOwAiChannel({ message, thread }) {
    const channel = thread?.channel ?? message?.channel_id;
    return channel?.isOwAiChat ? channel : undefined;
}

/**
 * The record an answer can be posted on: the chat's own record
 * (`res_model`/`res_id`), in the web client only (the full composer is a
 * window action).
 */
function getOwAiRecord(params) {
    const session = getOwAiChannel(params)?.owAiSession;
    if (
        !params.message.owAiIsAnswer ||
        !session?.res_model ||
        !session.res_id ||
        !params.store.env.services.action
    ) {
        return undefined;
    }
    return { model: session.res_model, id: session.res_id };
}

/** Open mail's full composer on the chat's record, prefilled with the answer. */
function openOwAiFullComposer(params, { isNote }) {
    const { model, id } = getOwAiRecord(params);
    return params.store.env.services.action.doAction({
        name: isNote ? _t("Log note") : _t("Send message"),
        type: "ir.actions.act_window",
        res_model: "mail.compose.message",
        view_mode: "form",
        views: [[false, "form"]],
        target: "new",
        context: {
            default_body: params.message.body,
            default_composition_mode: "comment",
            default_email_add_signature: false,
            default_model: model,
            default_res_ids: [id],
            default_subtype_xmlid: isNote ? "mail.mt_note" : "mail.mt_comment",
        },
    });
}

registerMessageAction("ow_ai_send_as_message", {
    condition: (params) => Boolean(getOwAiRecord(params)),
    icon: "send",
    name: _t("Send as message"),
    onSelected: (params) => openOwAiFullComposer(params, { isNote: false }),
    sequence: 86,
});
registerMessageAction("ow_ai_log_note", {
    condition: (params) => Boolean(getOwAiRecord(params)),
    icon: "sticky_note_2",
    name: _t("Log as note"),
    onSelected: (params) => openOwAiFullComposer(params, { isNote: true }),
    sequence: 87,
});
/** Insert the answer where the chat was opened from: in a later phase. */
registerMessageAction("ow_ai_use_this", {
    condition: false,
    icon: "check",
    name: _t("Use this"),
    sequence: 88,
});

patch(MessageAction.prototype, {
    /** In an AI chat, only copy and the assistant's own actions. */
    _condition(params) {
        if (
            getOwAiChannel(params) &&
            !this.definition.isMoreAction &&
            !OW_AI_MESSAGE_ACTIONS.has(this.id)
        ) {
            return false;
        }
        return super._condition(...arguments);
    },
});
