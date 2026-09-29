import { Thread } from "@mail/core/common/thread";
import { usePlugin } from "@odoo/owl";
import { DebugModePlugin } from "@web/core/debug_mode_plugin";
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
    setup() {
        super.setup(...arguments);
        this.owAiDebugMode = usePlugin(DebugModePlugin);
    },

    get isOwAiChat() {
        return Boolean(this.channel?.isOwAiChat);
    },

    get owAiSession() {
        return this.channel?.owAiSession;
    },

    /** Steps are shown when the chat says so (`show_agent_steps`, off by default), and in debug mode. */
    get owAiShowSteps() {
        return Boolean(this.owAiSession?.config?.show_agent_steps) || this.owAiDebugMode.isActive();
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
        if (lastGroup && !commentAfterLastGroup && this.channel.isAiGenerating) {
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

    /**
     * Whether `message` is not rendered as a message of its own: an agent step
     * or a tool summary (folded into a "Steps taken" block, or hidden), or the
     * history copy of the live card.
     *
     * @param {import("models").Message} message
     * @param {import("models").Thread} thread
     */
    owAiIsFolded(message, thread) {
        return Boolean(
            message.owAiIsAgentStep ||
                message.owAiIsToolSummary ||
                this.owAiLiveCardMessage(thread.messages)?.eq(message)
        );
    },

    /**
     * Mail keeps a thread that is at its bottom following new messages by
     * scrolling to the first newer message (`applyScrollContextually`), and
     * waits for that message's element (`messageRefs`) before moving its
     * markers (`newestPersistentMessage`). A folded message never gets one, so
     * mail would wait forever: scroll to the first newer message that is
     * rendered instead, or to the bottom when none is.
     *
     * @override
     */
    applyScrollContextually(thread) {
        const firstNewer = this.isOwAiChat ? this.owAiFirstNewerFoldedMessage(thread) : undefined;
        if (!firstNewer) {
            return super.applyScrollContextually(...arguments);
        }
        const nextShown = thread.messages.find(
            (message) =>
                message.id > firstNewer.id &&
                !message.isNotification &&
                !message.is_transient &&
                !this.owAiIsFolded(message, thread)
        );
        if (nextShown) {
            const messageEl = this.messageRefs.get(nextShown.id)?.();
            if (!messageEl) {
                return false; // not rendered yet, like mail
            }
            this.applyScrollContextuallyNewerChannelMessages(thread, messageEl);
            return true;
        }
        const scrollable = this.scrollableRef();
        this.setScroll(scrollable.scrollHeight - scrollable.clientHeight, {
            smooth: thread.scrollTop.includes("smooth"),
        });
        return true;
    },

    /**
     * The first newer message of mail's "at the bottom, newer channel
     * messages" case (the same conditions as `applyScrollContextually`), when
     * it is folded.
     *
     * @param {import("models").Thread} thread
     */
    owAiFirstNewerFoldedMessage(thread) {
        const olderMessages = thread.oldestPersistentMessage?.id < this.oldestPersistentMessage?.id;
        const newerMessages = thread.newestPersistentMessage?.id > this.newestPersistentMessage?.id;
        const atBottom =
            typeof thread.scrollTop === "string" && thread.scrollTop.includes("bottom");
        if (
            this.props.order !== "asc" ||
            !newerMessages ||
            !atBottom ||
            !this.channel ||
            (this.snapshot && (olderMessages || this.loadNewer)) ||
            this.env.messageHighlight?.highlightedMessageId
        ) {
            return undefined;
        }
        const firstNewer = this.channel.getFirstNewerMessage({
            from_message_id: this.newestPersistentMessage.id + 1,
        });
        return firstNewer && this.owAiIsFolded(firstNewer, thread) ? firstNewer : undefined;
    },

    /** Messages after a "Steps taken" block show their author again. */
    isSquashed(msg, prevMsg) {
        if (this.isOwAiChat && (prevMsg?.owAiIsAgentStep || prevMsg?.owAiIsToolSummary)) {
            return false;
        }
        return super.isSquashed(...arguments);
    },

    /** An AI chat starts with the agent's intro (`mail.Thread.startMessage` slot). */
    get startMessageChannelTypes() {
        return [...super.startMessageChannelTypes, "ow_ai_chat"];
    },
};

patch(Thread.prototype, threadPatch);
