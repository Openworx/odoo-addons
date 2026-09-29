import { Message } from "@mail/core/common/message_model";
import { fields } from "@mail/model/export";
import { createDocumentFragmentFromContent } from "@web/core/utils/html";
import { patch } from "@web/core/utils/patch";

/**
 * @typedef {Object} OwAiToolSummaryInfo
 * @property {string} text the summary's text
 * @property {string|undefined} icon the icon's `fa-*` classes (`<i class="fa fa-search">`: `fa-search`)
 * @property {string|undefined} callId the tool call's id (`data-id`)
 * @property {number|undefined} eventId the session event of the call (`data-oe-id`)
 */

/**
 * @typedef {Object} OwAiBodyInfo
 * @property {boolean} hasInputCard the body is the history copy of a card (`o_ow_ai_input_card`)
 * @property {OwAiToolSummaryInfo|undefined} toolSummary the body's `o_ow_ai_tool_summary` block
 * @property {string} text the body's whole text
 */

/**
 * The `o_ow_ai_*` blocks of a message body (`engine/html_output.py`), read
 * from the body's HTML in an inert document (Odoo 19's Message has no
 * rendered `bodyEl`).
 *
 * @param {string} body
 * @returns {OwAiBodyInfo}
 */
function parseOwAiBody(body) {
    const doc = createDocumentFragmentFromContent(body);
    const summaryEl = doc.querySelector(".o_ow_ai_tool_summary");
    const iconClasses = [...(summaryEl?.querySelector("i")?.classList ?? [])];
    return {
        hasInputCard: Boolean(doc.querySelector(".o_ow_ai_input_card")),
        toolSummary: summaryEl && {
            text: summaryEl.textContent.trim(),
            // only the `fa-*` classes (`html_output.py::tool_summary_markup`)
            icon: iconClasses.filter((name) => name.startsWith("fa-")).join(" ") || undefined,
            callId: summaryEl.dataset.id || undefined,
            eventId: Number(summaryEl.dataset.oeId) || undefined,
        },
        text: doc.body.textContent.trim(),
    };
}

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
    setup() {
        super.setup(...arguments);
        /**
         * The body's `o_ow_ai_*` blocks, parsed once per body (a lazy compute,
         * like mail's `edited`/`hasLink`): only read for the assistant's messages.
         *
         * @type {OwAiBodyInfo}
         */
        this.owAiBody = fields.Attr(undefined, {
            compute() {
                return parseOwAiBody(this.body);
            },
        });
    },
    get owAiAgentAuthored() {
        const chat = this.thread;
        const author = this.author_id;
        if (!chat || !author) {
            return false;
        }
        return Boolean(
            chat.ow_ai_agent_id?.partner_id?.eq(author) ||
                chat.ow_ai_session_ids.some((session) => session.agent_id?.partner_id?.eq(author))
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
        return Boolean(
            this.owAiAgentAuthored &&
                this.message_type === "comment" &&
                this.owAiBody.hasInputCard
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
