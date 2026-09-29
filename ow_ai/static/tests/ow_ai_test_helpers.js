import { mailModels, registryNamesToCloneWithCleanup } from "@mail/../tests/mail_test_helpers";

import {
    Command,
    defineModels,
    defineParams,
    getService,
    makeKwArgs,
    serverState,
} from "@web/../tests/web_test_helpers";

import { DiscussChannel } from "./mock_server/models/discuss_channel";
import { OwAiAgent } from "./mock_server/models/ow_ai_agent";
import { OwAiComposer } from "./mock_server/models/ow_ai_composer";
import { OwAiSession } from "./mock_server/models/ow_ai_session";
import { ResUsers } from "./mock_server/models/res_users";
import { publishOwAiSession } from "./mock_server/ow_ai_mock_server";

export { publishOwAiSession };

// tests replace client tools with spies: give each test its own copy of the registry
registryNamesToCloneWithCleanup.push("ow_ai.client_tools");

export const owAiModels = {
    ...mailModels,
    DiscussChannel,
    OwAiAgent,
    OwAiComposer,
    OwAiSession,
    ResUsers,
};

/** The mail mock models plus the `ow_ai` ones (to call at the top of a test file). */
export function defineOwAiModels() {
    defineParams({ suite: "mail" }, "replace");
    return defineModels(owAiModels);
}

/**
 * Create an agent (with its partner) and an `ow_ai_chat` channel with its root
 * session for the current user, like `ow.ai.agent.action_launch_chat`.
 *
 * @param {import("@web/../tests/web_test_helpers").MockServerEnvironment} pyEnv
 * @param {{ agentName?: string, subtitle?: string }} [options]
 */
export function createOwAiChat(pyEnv, { agentName = "Ava", subtitle = "Your assistant" } = {}) {
    const agentPartnerId = pyEnv["res.partner"].create({ name: agentName });
    const agentId = pyEnv["ow.ai.agent"].create({
        name: agentName,
        subtitle,
        partner_id: agentPartnerId,
    });
    const { channelId, sessionId } = pyEnv["ow.ai.agent"]._create_chat_channel(
        agentId,
        serverState.partnerId
    );
    return { agentId, agentPartnerId, channelId, sessionId };
}

/**
 * Create an agent (with its partner) and the composers `action_launch_chat`
 * resolves it from (`systray` and `record_chat`), with their prompt buttons.
 *
 * @param {import("@web/../tests/web_test_helpers").MockServerEnvironment} pyEnv
 * @param {{ name?: string, subtitle?: string, promptButtons?: {name: string, prompt: string}[] }} [options]
 */
export function createOwAiAgent(
    pyEnv,
    { name = "Ava", subtitle = "Your assistant", promptButtons = [] } = {}
) {
    const partnerId = pyEnv["res.partner"].create({ name });
    const agentId = pyEnv["ow.ai.agent"].create({ name, subtitle, partner_id: partnerId });
    for (const interfaceKey of ["systray", "record_chat"]) {
        pyEnv["ow.ai.composer"].create({
            interface_key: interfaceKey,
            agent_id: agentId,
            prompt_buttons: promptButtons,
        });
    }
    return { agentId, partnerId };
}

/**
 * The `Thread` of the channel in the store (after `start()`), if loaded.
 *
 * @param {number} channelId
 * @returns {import("models").Thread|undefined}
 */
export function getChannelThread(channelId) {
    return getService("mail.store").Thread.get({ model: "discuss.channel", id: channelId });
}

/**
 * Load the channel in the store the way the web client does (`/mail/data`
 * `discuss.channel` fetch), after `start()`, and return its `Thread`.
 *
 * @param {number} channelId
 * @returns {Promise<import("models").Thread>}
 */
export async function fetchChannelThread(channelId) {
    await getService("mail.store").fetchChannel(channelId);
    return getChannelThread(channelId);
}

/**
 * Configure web search on the mock server (a SearXNG URL is set), before
 * `startServer()`: the store init publishes `has_ow_ai_web_search`; new
 * chats still start with their "Web search" switch off (the default setting).
 */
export function configureOwAiWebSearch() {
    serverState.owAiWebSearch = true;
}

/** Remove the current user's access to the assistant (`ow_ai.group_ai_user`). */
export function removeOwAiAccess(pyEnv) {
    pyEnv["res.users"].write([serverState.userId], { group_ids: [Command.clear()] });
}

/**
 * Write `vals` on the mock session and push its Store payload on the bus,
 * like the server does after each state change.
 *
 * @param {import("@web/../tests/web_test_helpers").MockServerEnvironment} pyEnv
 * @param {number} sessionId
 * @param {Object} vals
 */
export function updateOwAiSession(pyEnv, sessionId, vals) {
    pyEnv["ow.ai.session"].write([sessionId], vals);
    publishOwAiSession(pyEnv, sessionId);
}

/** Body of an agent step (`html_output.agent_step_markup`). */
export function agentStepBody(html, eventId = 1) {
    return `<div class="o_ow_ai_agent_step" data-id="${eventId}">${html}</div>`;
}

/** Body of a tool summary (`html_output.tool_summary_markup`). */
export function toolSummaryBody(text, { callId = "call_1", eventId = 1, icon = "fa-search" } = {}) {
    return `<div class="o_ow_ai_tool_summary" data-id="${callId}" data-oe-id="${eventId}"><i class="fa ${icon}" aria-hidden="true"></i>${text}</div>`;
}

/** Body of the history copy of a card (`html_output.input_card_markup`). */
export function inputCardBody(html) {
    return `<div class="o_ow_ai_input_card">${html}</div>`;
}

/**
 * Create a message of the chat in the mock database, before `start()`.
 * Agent steps and tool summaries are notes (`mail.mt_note`); tool
 * summaries and chat notes are notifications.
 *
 * @param {import("@web/../tests/web_test_helpers").MockServerEnvironment} pyEnv
 * @param {number} channelId
 * @param {{author_id: number, body: string, message_type?: string, subtype_xmlid?: string}} vals
 */
export function createChatMessage(
    pyEnv,
    channelId,
    { author_id, body, message_type = "comment", subtype_xmlid = "mail.mt_comment" }
) {
    const [subtypeId] = pyEnv["mail.message.subtype"].search([
        ["subtype_xmlid", "=", subtype_xmlid],
    ]);
    return pyEnv["mail.message"].create({
        author_id,
        body,
        message_type,
        model: "discuss.channel",
        res_id: channelId,
        subtype_id: subtypeId,
    });
}

/**
 * Post a message in the chat as `author_id` and push it on the bus, like the
 * server does while the assistant answers (after `start()`).
 *
 * @param {import("@web/../tests/web_test_helpers").MockServerEnvironment} pyEnv
 * @param {number} channelId
 * @param {{author_id: number, body: string, message_type?: string, subtype_xmlid?: string}} vals
 */
export function postChatMessage(
    pyEnv,
    channelId,
    { author_id, body, message_type = "comment", subtype_xmlid = "mail.mt_comment" }
) {
    return pyEnv["discuss.channel"].message_post(
        channelId,
        makeKwArgs({ author_id, body, message_type, subtype_xmlid })
    );
}
