import { Thread } from "@mail/core/common/thread";
import { patch } from "@web/core/utils/patch";

import { OW_AI_WAITING_FOR_USER_STATES } from "@ow_ai/core/common/ow_ai_session_model";
import { OwAiChatIntro } from "@ow_ai/discuss/ow_ai_chat_intro";
import { OwAiStepsGroup } from "@ow_ai/discuss/ow_ai_steps_group";
import { OwAiThreadStatus } from "@ow_ai/discuss/ow_ai_thread_status";
import { OwAiUserInputRequest } from "@ow_ai/discuss/ow_ai_user_input_request";

Object.assign(Thread.components, {
    OwAiChatIntro,
    OwAiStepsGroup,
    OwAiThreadStatus,
    OwAiUserInputRequest,
});

/**
 * @typedef {Object} OwAiStepsLayout
 * @property {import("models").Message[]} messages the messages to render one by one
 * @property {Map<number, {messages: import("models").Message[], open: boolean}>} groups
 *  the "Steps taken" block rendered in place of the message with that id
 */

/** @type {import("@mail/core/common/thread").Thread} */
const threadPatch = {
    get isOwAiChat() {
        return Boolean(this.props.thread.isOwAiChat);
    },

    get owAiSession() {
        return this.props.thread.owAiSession;
    },

    /** Steps are shown when the chat says so (`show_agent_steps`, off by default), and in debug mode. */
    get owAiShowSteps() {
        return Boolean(this.owAiSession?.config?.show_agent_steps) || Boolean(this.env.debug);
    },

    /** The card the session waits on, shown at the bottom of the chat. */
    get owAiUserInputRequest() {
        const session = this.owAiSession;
        if (!session?.userInputRequest) {
            return undefined;
        }
        return OW_AI_WAITING_FOR_USER_STATES.includes(session.loop_state)
            ? session.userInputRequest
            : undefined;
    },

    /**
     * Fold each run of agent steps/tool summaries into one "Steps taken"
     * block, and leave out the history copy of the card that is live at the
     * bottom of the chat. A block is unfolded while the assistant is still
     * working on it: nothing but notes after it, and the session generating.
     *
     * @param {import("models").Message[]} messages the ordered messages of the thread
     * @returns {OwAiStepsLayout}
     */
    owAiLayout(messages) {
        const groups = new Map();
        if (!this.isOwAiChat) {
            return { messages, groups };
        }
        const showSteps = this.owAiShowSteps;
        const liveCard = this.owAiLiveCardMessage(messages);
        const visible = [];
        let group;
        let lastGroup;
        let commentAfterLastGroup = false;
        for (const message of messages) {
            if (message.owAiIsAgentStep || message.owAiIsToolSummary) {
                if (!showSteps) {
                    continue;
                }
                if (group) {
                    group.messages.push(message);
                    continue;
                }
                group = { messages: [message], open: false };
                groups.set(message.id, group);
                visible.push(message);
                lastGroup = group;
                commentAfterLastGroup = false;
                continue;
            }
            if (liveCard?.eq(message)) {
                continue;
            }
            group = undefined;
            if (message.message_type !== "notification") {
                commentAfterLastGroup = true;
            }
            visible.push(message);
        }
        if (lastGroup && !commentAfterLastGroup && this.props.thread.isAiGenerating) {
            lastGroup.open = true;
        }
        return { messages: visible, groups };
    },

    /**
     * The newest card message while its request is live: the card component
     * shows it (with its answers), so the history copy would be a duplicate.
     *
     * @param {import("models").Message[]} messages
     */
    owAiLiveCardMessage(messages) {
        if (!this.owAiUserInputRequest) {
            return undefined;
        }
        for (let index = messages.length - 1; index >= 0; index--) {
            const message = messages[index];
            if (message.owAiIsUserInputCard) {
                return message;
            }
            if (message.isSelfAuthored) {
                return undefined;
            }
        }
        return undefined;
    },

    /** Messages after a "Steps taken" block show their author again. */
    isSquashed(msg, prevMsg) {
        if (this.isOwAiChat && (prevMsg?.owAiIsAgentStep || prevMsg?.owAiIsToolSummary)) {
            return false;
        }
        return super.isSquashed(...arguments);
    },

    /**
     * An AI chat starts with the agent's intro (`mail.Thread.startMessage`
     * slot, see `thread_patch.xml`): mail shows the start message of
     * channels, groups and chats only.
     */
    get showStartMessage() {
        return super.showStartMessage || (this.isOwAiChat && this.state.mountedAndLoaded);
    },
};

patch(Thread.prototype, threadPatch);
