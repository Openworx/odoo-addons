import { parseRequestParams, registerRoute } from "@mail/../tests/mock_server/mail_mock_server";
import { Store } from "@mail/../tests/mock_server/store";

import { MockServerError } from "@web/../tests/web_test_helpers";

/**
 * Mock routes of `controllers/session.py`. Tests observe or delay them with
 * `onRpcBefore(route, callback)` from the mail test helpers.
 *
 * @template [T={}]
 * @typedef {import("@web/../tests/web_test_helpers").RouteCallback<T>} RouteCallback
 */

const INTERACTION_STATES = ["waiting_confirmation", "waiting_answer", "waiting_client_result"];

/**
 * Push the session's Store payload on its channel, like `ow.ai.session._publish_state`.
 *
 * @param {import("@web/../tests/web_test_helpers").MockServerEnvironment} env
 * @param {number} sessionId
 */
export function publishOwAiSession(env, sessionId) {
    const [session] = env["ow.ai.session"].browse(sessionId);
    const [channel] = env["discuss.channel"].browse(session.channel_id);
    env["bus.bus"]._sendone(
        channel,
        "mail.record/insert",
        new Store().add(env["ow.ai.session"].browse(sessionId), "_store_session_fields").as_dict()
    );
}

function getRootSession(env, channelId) {
    const [session] = env["ow.ai.session"]
        .search_read([["channel_id", "=", channelId]])
        .sort((a, b) => b.id - a.id);
    if (!session) {
        throw new MockServerError(`No AI session on channel ${channelId}`);
    }
    return session;
}

registerRoute("/ow_ai/session/advance", ow_ai_session_advance);
/** @type {RouteCallback} */
async function ow_ai_session_advance(request) {
    const { channel_id } = await parseRequestParams(request);
    const session = getRootSession(this.env, channel_id);
    if (session.loop_state !== "ready" && !INTERACTION_STATES.includes(session.loop_state)) {
        throw new MockServerError("The assistant is still answering. Please wait.");
    }
    this.env["ow.ai.session"].write([session.id], {
        loop_state: "waiting_model",
        resume_token: false,
        user_input_request: false,
        client_tool_request: false,
    });
    publishOwAiSession(this.env, session.id);
    return { loop_state: "waiting_model", session_id: session.id };
}

registerRoute("/ow_ai/session/resume", ow_ai_session_resume);
/** @type {RouteCallback} */
async function ow_ai_session_resume(request) {
    const { channel_id, session_id, resume_token } = await parseRequestParams(request);
    const session = getRootSession(this.env, channel_id);
    if (
        session.id !== session_id ||
        !INTERACTION_STATES.includes(session.loop_state) ||
        session.resume_token !== resume_token
    ) {
        return { interactionConsumed: false, loop_state: session.loop_state };
    }
    this.env["ow.ai.session"].write([session.id], {
        loop_state: "waiting_model",
        resume_token: false,
        user_input_request: false,
        client_tool_request: false,
    });
    publishOwAiSession(this.env, session.id);
    return { interactionConsumed: true, loop_state: "waiting_model" };
}

registerRoute("/ow_ai/session/config", ow_ai_session_config);
/** @type {RouteCallback} */
async function ow_ai_session_config(request) {
    const { channel_id, config } = await parseRequestParams(request);
    const session = getRootSession(this.env, channel_id);
    const vals = {};
    for (const key of ["auto_confirm", "show_agent_steps"]) {
        if (key in (config ?? {})) {
            vals[key] = Boolean(config[key]);
        }
    }
    this.env["ow.ai.session"].write([session.id], vals);
    publishOwAiSession(this.env, session.id);
    const [updated] = this.env["ow.ai.session"].browse(session.id);
    return { auto_confirm: updated.auto_confirm, show_agent_steps: updated.show_agent_steps };
}

registerRoute("/ow_ai/session/delete_chat", ow_ai_session_delete_chat);
/** @type {RouteCallback} */
async function ow_ai_session_delete_chat(request) {
    const { channel_id } = await parseRequestParams(request);
    // the mock `discuss.channel.unlink` sends `discuss.channel/delete`, like the server
    this.env["discuss.channel"].unlink([channel_id]);
    return true;
}
