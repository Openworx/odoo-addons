import { prettifyMessageText } from "@mail/utils/common/format";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";

/** The group of the users who may use the assistant. */
export const OW_AI_USER_GROUP = "ow_ai.group_ai_user";
/**
 * Asks the active form view to launch a chat about its record. The payload
 * is `{ handled: false }`: the form sets `handled` when it takes over.
 */
export const OW_AI_OPEN_RECORD_CHAT = "OW_AI:OPEN_RECORD_CHAT";

/**
 * @typedef {Object} OwAiLaunchOptions
 * @property {"systray"|"record_chat"} [interfaceKey="systray"] the composer's interface
 * @property {string|null} [recordModel] model of the record the chat is about
 * @property {number|null} [recordId] id of the record the chat is about
 * @property {string|null} [channelTitle] chat title (default: the record's name, else the agent's)
 * @property {string|null} [userMessage] plain text posted as the user's first message
 */

/**
 * Launches AI chats (service `ow_ai.chat_launcher`): reopens an empty chat
 * of the same kind (interface, record, agent), else creates one
 * (`ow.ai.agent.action_launch_chat`), then
 * opens it (a chat window, or the Discuss app when it is open) and posts the
 * user's message, if any.
 */
export class OwAiChatLauncher {
    /**
     * @param {import("@web/env").OdooEnv} env
     * @param {import("services").ServiceFactories} services
     */
    constructor(env, services) {
        this.env = env;
        this.orm = services.orm;
        this.store = services["mail.store"];
        this.notification = services.notification;
        /** Launches in progress (without a message), by kind: a double click opens one chat. */
        this.pendingLaunches = new Map();
        /**
         * The agent each kind of chat (`[interfaceKey, recordModel]`, what the
         * server resolves the composer and agent from) got at its last launch in
         * this tab: an empty chat is only reused when it has that agent.
         */
        this.agentIdByKind = new Map();
        /** Chats opened as launched by the server (`ow_ai.open_chat`, e.g. a test chat): never reused. */
        this.nonReusableChannelIds = new Set();
    }

    /**
     * @param {OwAiLaunchOptions} [options]
     * @returns {Promise<import("models").DiscussChannel|undefined>} the opened chat
     */
    launchChat({
        interfaceKey = "systray",
        recordModel = null,
        recordId = null,
        channelTitle = null,
        userMessage = null,
    } = {}) {
        const options = { interfaceKey, recordModel, recordId, channelTitle, userMessage };
        if (userMessage) {
            return this._launchChat(options);
        }
        const key = JSON.stringify([interfaceKey, recordModel, recordId]);
        if (!this.pendingLaunches.has(key)) {
            const launch = this._launchChat(options);
            this.pendingLaunches.set(key, launch);
            launch.finally(() => this.pendingLaunches.delete(key));
        }
        return this.pendingLaunches.get(key);
    }

    /**
     * Open a chat launched by the server: the payload of
     * `ow.ai.agent.action_launch_chat` (`{channel_id, session_id, store_data,
     * prompt_buttons, subtitle}`), e.g. the `ow_ai.open_chat` client action's params.
     *
     * @returns {Promise<import("models").DiscussChannel|undefined>}
     */
    async openLaunchedChat(result) {
        try {
            const channel = this._insertLaunchedChat(result);
            if (!channel) {
                throw new Error(_t("The AI chat could not be found."));
            }
            this.nonReusableChannelIds.add(channel.id);
            this._openChat(channel);
            return channel;
        } catch (error) {
            this._notifyError(error);
        }
    }

    /** @param {OwAiLaunchOptions} options */
    async _launchChat({ interfaceKey, recordModel, recordId, channelTitle, userMessage }) {
        try {
            let channel = this._findReusableChat({ interfaceKey, recordModel, recordId });
            if (!channel) {
                const result = await this.orm.call("ow.ai.agent", "action_launch_chat", [[]], {
                    interface_key: interfaceKey,
                    res_model: recordModel,
                    res_id: recordId,
                    channel_title: channelTitle,
                });
                channel = this._insertLaunchedChat(result);
                if (!channel) {
                    throw new Error(_t("The AI chat could not be found."));
                }
                const agentId = channel.owAiSession?.agent_id?.id;
                if (agentId) {
                    this.agentIdByKind.set(this._kindKey(interfaceKey, recordModel), agentId);
                }
            }
            this._openChat(channel);
            if (userMessage?.trim()) {
                await channel.thread.post(prettifyMessageText(userMessage));
            }
            return channel;
        } catch (error) {
            this._notifyError(error);
        }
    }

    _kindKey(interfaceKey, recordModel) {
        return JSON.stringify([interfaceKey, recordModel || null]);
    }

    /**
     * An AI chat of the same kind that nobody wrote in yet: same interface
     * (composer), same record, the agent this kind of chat got at its last
     * launch in this tab (none yet: no reuse), messages loaded and none.
     *
     * @returns {import("models").DiscussChannel|undefined}
     */
    _findReusableChat({ interfaceKey, recordModel, recordId }) {
        const agentId = this.agentIdByKind.get(this._kindKey(interfaceKey, recordModel));
        if (!agentId) {
            return undefined;
        }
        let found;
        for (const channel of this.store["discuss.channel"].records.values()) {
            const session = channel.isOwAiChat && channel.self_member_id && channel.owAiSession;
            if (
                !session ||
                this.nonReusableChannelIds.has(channel.id) ||
                session.agent_id?.id !== agentId ||
                session.composer_id?.interface_key !== interfaceKey ||
                (session.res_model || null) !== (recordModel || null) ||
                (session.res_id || null) !== (recordId || null) ||
                session.loop_state !== "ready" ||
                session.isSubmitting ||
                !channel.thread.isLoaded ||
                !channel.thread.isEmpty
            ) {
                continue;
            }
            if (!found || channel.id > found.id) {
                found = channel;
            }
        }
        return found;
    }

    /** @returns {import("models").DiscussChannel|undefined} */
    _insertLaunchedChat({ channel_id, session_id, store_data, prompt_buttons, subtitle } = {}) {
        if (store_data) {
            this.store.insert(store_data);
        }
        const channel = this.store["discuss.channel"].get(channel_id);
        const session = this.store["ow.ai.session"].get(session_id);
        if (session) {
            session.promptButtons = prompt_buttons ?? [];
        }
        const agent = channel?.ow_ai_agent_id;
        if (agent && !agent.subtitle && subtitle) {
            agent.subtitle = subtitle;
        }
        return channel;
    }

    /** In the Discuss app when it is open, else in a chat window (even a compact chat hub's). */
    _openChat(channel) {
        channel.open({ focus: true, bypassCompact: true });
    }

    _notifyError(error) {
        this.notification.add(
            error?.data?.message || error?.message || _t("The AI chat could not be opened."),
            { type: "danger" }
        );
    }
}

export const owAiChatLauncherService = {
    dependencies: ["orm", "mail.store", "notification"],
    /**
     * @param {import("@web/env").OdooEnv} env
     * @param {import("services").ServiceFactories} services
     */
    start(env, services) {
        return new OwAiChatLauncher(env, services);
    },
};

registry.category("services").add("ow_ai.chat_launcher", owAiChatLauncherService);
