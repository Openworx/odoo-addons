import { Message } from "@mail/core/common/message_model";
import { patch } from "@web/core/utils/patch";

/**
 * What the assistant posts in an AI chat (`engine/html_output.py`,
 * `models/ow_ai_session_post.py`), told apart by message type and subtype:
 * - comment + note: an agent step (`o_ow_ai_agent_step`);
 * - notification + note: a tool summary (`o_ow_ai_tool_summary`);
 * - comment + discussion: an answer, or a card (`o_ow_ai_input_card`);
 * - notification + discussion: a chat note (`data-oe-type="ow_ai_note|ow_ai_preview"`).
 * Notifications are not hidden (mail's `notificationHidden` stays false):
 * tool summaries and chat notes render as notification messages.
 *
 * @type {import("models").Message}
 */
const messagePatch = {
    get owAiAgentAuthored() {
        const channel = this.channel_id;
        const author = this.author_id;
        if (!channel || !author) {
            return false;
        }
        return Boolean(
            channel.ow_ai_agent_id?.partner_id?.eq(author) ||
                channel.ow_ai_session_ids.some((session) =>
                    session.agent_id?.partner_id?.eq(author)
                )
        );
    },
    get owAiIsAgentStep() {
        return Boolean(this.owAiAgentAuthored && this.message_type === "comment" && this.isNote);
    },
    get owAiIsToolSummary() {
        return Boolean(
            this.owAiAgentAuthored && this.message_type === "notification" && this.isNote
        );
    },
    get owAiIsUserInputCard() {
        return (
            this.owAiAgentAuthored &&
            this.message_type === "comment" &&
            Boolean(this.bodyEl?.querySelector(".o_ow_ai_input_card"))
        );
    },
    get owAiIsAnswer() {
        return Boolean(
            this.owAiAgentAuthored &&
                this.message_type === "comment" &&
                this.isDiscussion &&
                !this.owAiIsUserInputCard
        );
    },
};

patch(Message.prototype, messagePatch);
