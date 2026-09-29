import { fields, Record } from "@mail/model/export";
import { _t } from "@web/core/l10n/translation";
import { rpc } from "@web/core/network/rpc";

import { runOwAiClientTool } from "@ow_ai/core/common/ow_ai_client_tool_registry";

/** Loop states in which the assistant is working on the turn. */
export const OW_AI_GENERATING_STATES = ["waiting_model", "waiting_client_result"];
/** Loop states in which a card waits for the user's answer. */
export const OW_AI_WAITING_FOR_USER_STATES = ["waiting_confirmation", "waiting_answer"];
/** Loop states in which the session can be resumed with its resume token. */
export const OW_AI_INTERACTION_STATES = [...OW_AI_WAITING_FOR_USER_STATES, "waiting_client_result"];

/** An `ow.ai.session`: the state of the assistant's turn in an AI chat. */
export class OwAiSession extends Record {
    static _name = "ow.ai.session";
    static id = "id";

    /** @type {number} */
    id;
    channel_id = fields.One("discuss.channel", { inverse: "ow_ai_session_ids" });
    agent_id = fields.One("ow.ai.agent");
    composer_id = fields.One("ow.ai.composer");
    /** @type {string|false} */
    res_model;
    /** @type {number|false} */
    res_id;
    config = fields.Attr({ auto_confirm: false, show_agent_steps: false });
    /** @type {"ready"|"waiting_model"|"waiting_confirmation"|"waiting_answer"|"waiting_client_result"} */
    loop_state = "ready";
    /**
     * Only published while in an interaction state: the server omits it
     * otherwise, so a previous value may linger (see `pendingResumeToken`).
     *
     * @type {string|false|undefined}
     */
    resume_token;
    userInputRequest = fields.One("ow.ai.user.input.request", {
        inverse: "session",
        onDelete: (request) => request?.delete(),
    });
    /** @type {{name: string, params: Object, resumeToken: string}|false|undefined} */
    clientToolRequest = fields.Attr();
    /** @type {string|false|undefined} */
    toolStatus;
    /** Whether this tab is sending the user's message or answer (advance/resume RPC). */
    isSubmitting = false;
    /**
     * Prompt buttons of the composer the chat was launched from
     * (`action_launch_chat`): set client-side by the launcher, never published.
     *
     * @type {{name: string, prompt: string}[]}
     */
    promptButtons = fields.Attr([]);
    /** Resume token of the client tool request this tab is running. */
    runningClientToolToken;
    /** Resume token of the last client tool request this tab sent the result of. */
    lastClientToolToken;

    get isGenerating() {
        return this.isSubmitting || OW_AI_GENERATING_STATES.includes(this.loop_state);
    }

    /** The user cannot post: the message is being sent or the model is answering. */
    get isComposerLocked() {
        return this.isSubmitting || this.loop_state === "waiting_model";
    }

    get isWaitingForUser() {
        return !this.isSubmitting && OW_AI_WAITING_FOR_USER_STATES.includes(this.loop_state);
    }

    /** The token resuming the current interaction, if the session is in one. */
    get pendingResumeToken() {
        if (!OW_AI_INTERACTION_STATES.includes(this.loop_state)) {
            return undefined;
        }
        return (
            this.userInputRequest?.resumeToken ||
            this.clientToolRequest?.resumeToken ||
            this.resume_token ||
            undefined
        );
    }

    /**
     * Change the chat's settings (`/ow_ai/session/config`), e.g.
     * `{ auto_confirm: true }`; the server publishes the new state as well.
     *
     * @param {{auto_confirm?: boolean, show_agent_steps?: boolean}} values
     */
    async updateConfig(values) {
        const config = await rpc("/ow_ai/session/config", {
            channel_id: this.channel_id.id,
            config: values,
        });
        if (this.exists() && config) {
            this.config = { ...this.config, ...config };
        }
        return config;
    }

    /**
     * Run the client tools the session waits on, then resume the session with
     * their results (`client_result`) or the first error (`client_error`).
     * Defaults to the request published in the Store (`clientToolRequest`);
     * the `ow_ai.client_tools` service passes the bus notification's instead.
     * A request (resume token) runs once per tab: again only if sending its
     * result failed.
     *
     * @param {{tools: {name: string, params?: Object}[], resumeToken: string}} [request]
     */
    async processPendingClientTool(request = this._storeClientToolRequest()) {
        const { tools = [], resumeToken } = request ?? {};
        const thread = this.channel_id?.thread;
        if (
            !resumeToken ||
            !thread ||
            [this.runningClientToolToken, this.lastClientToolToken].includes(resumeToken)
        ) {
            return;
        }
        this.runningClientToolToken = resumeToken;
        try {
            await this._runClientTools(thread, tools, resumeToken);
        } finally {
            if (this.runningClientToolToken === resumeToken) {
                this.runningClientToolToken = undefined;
            }
        }
    }

    async _runClientTools(thread, tools, resumeToken) {
        let response;
        try {
            const results = [];
            for (const tool of tools) {
                const result = await runOwAiClientTool(this.store.env, tool);
                results.push(result === undefined ? "Done." : result);
            }
            response = { kind: "client_result", value: results };
        } catch (error) {
            response = { kind: "client_error", value: error?.message || String(error) };
        }
        try {
            await thread.requestOwAiResume(response, { session: this, resumeToken });
            this.lastClientToolToken = resumeToken;
        } catch (error) {
            this.store.env.services.notification.add(
                error?.data?.message ||
                    error?.message ||
                    _t("The result of the assistant's action could not be sent."),
                { type: "danger" }
            );
        }
    }

    _storeClientToolRequest() {
        const request = this.clientToolRequest;
        if (!request || this.loop_state !== "waiting_client_result") {
            return undefined;
        }
        return {
            tools: [{ name: request.name, params: request.params }],
            resumeToken: request.resumeToken,
        };
    }
}

OwAiSession.register();
