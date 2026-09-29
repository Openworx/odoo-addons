import { DiscussChannel } from "@mail/discuss/core/common/discuss_channel_model";
import { fields } from "@mail/model/export";
import { rpc } from "@web/core/network/rpc";
import { patch } from "@web/core/utils/patch";

/** @type {import("models").DiscussChannel} */
const discussChannelPatch = {
    setup() {
        super.setup(...arguments);
        /** only published on `ow_ai_chat` channels */
        this.ow_ai_agent_id = fields.One("ow.ai.agent");
        this.ow_ai_session_ids = fields.Many("ow.ai.session", { inverse: "channel_id" });
    },
    get isOwAiChat() {
        return this.channel_type === "ow_ai_chat";
    },
    /** The root session of the chat: the newest one, like the server (`_order = 'id desc'`). */
    get owAiSession() {
        let newest;
        for (const session of this.ow_ai_session_ids) {
            if (!newest || session.id > newest.id) {
                newest = session;
            }
        }
        return newest;
    },
    get isAiGenerating() {
        return Boolean(this.owAiSession?.isGenerating);
    },
    get isAiWaitingForUser() {
        return Boolean(this.owAiSession?.isWaitingForUser);
    },
    /** The user may rename an AI chat (if the server lets them write the channel). */
    get allowedToRenameChannelTypes() {
        return [...super.allowedToRenameChannelTypes, "ow_ai_chat"];
    },
    /** AI chats behave like direct chats: unread counter, "@" prefix, chat window. */
    get chatChannelTypes() {
        return [...super.chatChannelTypes, "ow_ai_chat"];
    },
    get computedDisplayName() {
        if (this.isOwAiChat && !this.name && this.ow_ai_agent_id?.name) {
            return this.ow_ai_agent_id.name;
        }
        return super.computedDisplayName;
    },
    /** Delete the chat on the server (`/ow_ai/session/delete_chat`), then locally. */
    async deleteOwAiChat() {
        await rpc("/ow_ai/session/delete_chat", { channel_id: this.id });
        if (this.exists()) {
            this.delete(); // also closes the chat window
        }
    },
};

patch(DiscussChannel.prototype, discussChannelPatch);
