import { Thread } from "@mail/core/common/thread_model";
import { fields } from "@mail/model/export";
import { isMobileOS } from "@web/core/browser/feature_detection";
import { _t } from "@web/core/l10n/translation";
import { rpc } from "@web/core/network/rpc";
import { patch } from "@web/core/utils/patch";

import { OW_AI_CLIENT_IDENTIFIER } from "@ow_ai/core/common/ow_ai_client_identifier";

/**
 * Apply the loop state acknowledged by an advance/resume RPC, unless the bus
 * already delivered a newer state meanwhile.
 *
 * @param {import("models").OwAiSession} session
 * @param {string} loopStateBefore
 * @param {{loop_state?: string}} [acknowledgement]
 */
function applyOwAiAcknowledgement(session, loopStateBefore, acknowledgement) {
    if (acknowledgement?.loop_state && session.exists() && session.loop_state === loopStateBefore) {
        session.loop_state = acknowledgement.loop_state;
    }
}

/**
 * An AI chat is a `discuss.channel` of type `ow_ai_chat` (a `Thread` record on
 * Odoo 19): the agent and the sessions are published on it only
 * (`discuss.channel._store_ow_ai_fields`).
 *
 * @type {import("models").Thread}
 */
const threadPatch = {
    setup() {
        super.setup(...arguments);
        /** only published on `ow_ai_chat` channels */
        this.ow_ai_agent_id = fields.One("ow.ai.agent");
        this.ow_ai_session_ids = fields.Many("ow.ai.session", { inverse: "channel_id" });
        /**
         * The composer of an AI chat is disabled while the assistant answers
         * (not while a card or a client tool waits: posting aborts those).
         * Once the answer is there, the focus goes back to the composer,
         * unless the user moved on to something else meanwhile (a disabled
         * textarea loses the focus to the body).
         */
        this.owAiComposerLocked = fields.Attr(false, {
            compute() {
                return this.isOwAiChat && Boolean(this.owAiSession?.isComposerLocked);
            },
            onUpdate() {
                if (this.owAiComposerLocked || this.isAiWaitingForUser || isMobileOS()) {
                    return;
                }
                const active = document.activeElement;
                if (!active || active === document.body) {
                    this.composer.autofocus++;
                }
            },
            eager: true,
        });
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
    /** AI chats behave like direct chats: unread counter, "@" prefix, chat window. */
    get isChatChannel() {
        return super.isChatChannel || this.isOwAiChat;
    },
    /** Renaming an AI chat renames the channel (below), not a per-member custom name. */
    get supportsCustomChannelName() {
        return !this.isOwAiChat && super.supportsCustomChannelName;
    },
    get displayName() {
        if (this.isOwAiChat && !this.name && this.ow_ai_agent_id?.name) {
            return this.ow_ai_agent_id.name;
        }
        return super.displayName;
    },
    /** @override */
    get composerDisabled() {
        return this.owAiComposerLocked || super.composerDisabled;
    },
    /**
     * The user may rename an AI chat (if the server lets them write the
     * channel): like a channel, through `channel_rename`.
     *
     * @override
     * @param {string} name
     */
    async rename(name) {
        const newName = name.trim();
        if (!this.isOwAiChat || !this.is_editable) {
            return super.rename(...arguments);
        }
        if (newName && newName !== this.displayName) {
            this.name = newName;
            await this.store.env.services.orm.call(
                "discuss.channel",
                "channel_rename",
                [[this.id]],
                { name: newName }
            );
        }
    },
    /** Delete the chat on the server (`/ow_ai/session/delete_chat`), then locally. */
    async deleteOwAiChat() {
        await rpc("/ow_ai/session/delete_chat", { channel_id: this.id });
        if (this.exists()) {
            this.delete(); // also closes the chat window
        }
    },

    /**
     * In an AI chat, a posted message starts the assistant's turn. Only a turn
     * waiting on the model (or a message being sent) blocks it: a pending card
     * or client tool call is aborted by the server, which also unsticks a chat
     * whose blocking client tool this tab never ran.
     *
     * @override
     */
    async post(body, postData = {}, extraData = {}) {
        if (!this.isOwAiChat) {
            return super.post(...arguments);
        }
        const session = this.owAiSession;
        if (session?.isComposerLocked) {
            // the composer is disabled meanwhile: only a race can get here
            this.store.env.services.notification.add(
                _t("The assistant is still answering. Please wait."),
                { type: "warning" }
            );
            return;
        }
        if (session) {
            session.isSubmitting = true;
        }
        try {
            const message = await super.post(...arguments);
            if (message) {
                await this.requestOwAiAdvance(message, session);
            }
            return message;
        } finally {
            if (session?.exists()) {
                session.isSubmitting = false;
            }
        }
    },

    /**
     * Ask the server to answer `message` (`/ow_ai/session/advance`). Errors are
     * shown as a notification.
     *
     * @param {import("models").Message} message
     * @param {import("models").OwAiSession} [session]
     */
    async requestOwAiAdvance(message, session = this.owAiSession) {
        const loopStateBefore = session?.loop_state;
        try {
            const acknowledgement = await rpc("/ow_ai/session/advance", {
                channel_id: this.id,
                message_id: message.id,
                session_config: session ? { ...session.config } : null,
                current_view_info: null,
                client_identifier: OW_AI_CLIENT_IDENTIFIER,
            });
            if (session) {
                applyOwAiAcknowledgement(session, loopStateBefore, acknowledgement);
            }
            return acknowledgement;
        } catch (error) {
            this.store.env.services.notification.add(
                error?.data?.message || error?.message || _t("The assistant could not be reached."),
                { type: "danger" }
            );
        }
    },

    /**
     * Answer the interaction the session waits on (`/ow_ai/session/resume`):
     * `response` is `{ kind, value }` with kind `confirmation`, `question`,
     * `skip`, `client_result` or `client_error`. Errors are thrown.
     *
     * @param {{kind: string, value?: any}} response
     * @param {Object} [options]
     * @param {import("models").OwAiSession} [options.session] defaults to the chat's session
     * @param {string} [options.resumeToken] defaults to the session's pending token
     * @returns {Promise<{interactionConsumed: boolean, loop_state: string}|undefined>}
     */
    async requestOwAiResume(
        response,
        { session = this.owAiSession, resumeToken = session?.pendingResumeToken } = {}
    ) {
        if (!session) {
            return;
        }
        const loopStateBefore = session.loop_state;
        session.isSubmitting = true;
        try {
            const acknowledgement = await rpc("/ow_ai/session/resume", {
                channel_id: this.id,
                session_id: session.id,
                resume_token: resumeToken,
                response,
                // the rest of the turn's client tools run in the answering tab
                client_identifier: OW_AI_CLIENT_IDENTIFIER,
            });
            applyOwAiAcknowledgement(session, loopStateBefore, acknowledgement);
            return acknowledgement;
        } finally {
            if (session.exists()) {
                session.isSubmitting = false;
            }
        }
    },
};

patch(Thread.prototype, threadPatch);
