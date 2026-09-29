import { contains, onRpcBefore, start, startServer } from "@mail/../tests/mail_test_helpers";
import { expect, test, waitUntil } from "@odoo/hoot";
import { getService, patchWithCleanup } from "@web/../tests/web_test_helpers";
import { registry } from "@web/core/registry";

import { OW_AI_CLIENT_IDENTIFIER } from "@ow_ai/core/common/ow_ai_client_identifier";
import { runOwAiClientTool } from "@ow_ai/core/common/ow_ai_client_tool_registry";

import { createOwAiChat, defineOwAiModels, updateOwAiSession } from "./ow_ai_test_helpers";

defineOwAiModels();

const NOTIFICATION_TYPE = "ow_ai.session/client_tools";

/** Send the notification on the chat's bus channel, like `tool_batch._pause_for_client`. */
function sendClientTools(pyEnv, channelId, payload) {
    const [channel] = pyEnv["discuss.channel"].browse(channelId);
    pyEnv["bus.bus"]._sendone(channel, NOTIFICATION_TYPE, payload);
}

/** Open the AI chat in the store, as the chat window would. */
async function startWithOwAiChat() {
    const pyEnv = await startServer();
    const chat = createOwAiChat(pyEnv);
    await start();
    await getService("mail.store")["discuss.channel"].getOrFetch(chat.channelId);
    return { pyEnv, ...chat };
}

function spyOnResume() {
    const calls = [];
    onRpcBefore("/ow_ai/session/resume", (params) => {
        calls.push(params);
        expect.step("resume");
    });
    return calls;
}

test("a non-blocking client tool notification runs the registry entry", async () => {
    registry
        .category("ow_ai.client_tools")
        .add("reload", (env, params) => expect.step(`reload ${JSON.stringify(params)}`), {
            force: true,
        });
    const resumeCalls = spyOnResume();
    const { channelId, pyEnv, sessionId } = await startWithOwAiChat();
    sendClientTools(pyEnv, channelId, {
        session_id: sessionId,
        client_tools: [{ name: "reload", params: {} }],
        blocking: false,
    });
    await expect.waitForSteps(["reload {}"]);
    expect(resumeCalls).toEqual([]);
});

test("a blocking client tool request resumes the session with the results, once", async () => {
    const clientTools = registry.category("ow_ai.client_tools");
    clientTools.add(
        "reload",
        () => {
            expect.step("reload");
            return "Reloaded.";
        },
        { force: true }
    );
    clientTools.add("sentinel", () => expect.step("sentinel"));
    const resumeCalls = spyOnResume();
    const { channelId, pyEnv, sessionId } = await startWithOwAiChat();
    // like `tool_batch._pause_for_client`: Store push, then the bus notification
    updateOwAiSession(pyEnv, sessionId, {
        loop_state: "waiting_client_result",
        resume_token: "token-1",
        client_tool_request: { name: "reload", params: {}, resumeToken: "token-1" },
    });
    const request = {
        session_id: sessionId,
        client_tools: [{ name: "reload", params: {} }],
        resume_token: "token-1",
    };
    sendClientTools(pyEnv, channelId, request);
    await expect.waitForSteps(["reload", "resume"]);
    expect(resumeCalls).toEqual([
        {
            channel_id: channelId,
            session_id: sessionId,
            resume_token: "token-1",
            response: { kind: "client_result", value: ["Reloaded."] },
            // the tab that ran the tools: the rest of the turn's client tools target it
            client_identifier: OW_AI_CLIENT_IDENTIFIER,
        },
    ]);
    // the same request (same token) never runs again: neither delivered twice...
    sendClientTools(pyEnv, channelId, request);
    // ...nor run from the Store request
    const session = getService("mail.store")["ow.ai.session"].get(sessionId);
    await session.processPendingClientTool({
        tools: [{ name: "reload", params: {} }],
        resumeToken: "token-1",
    });
    // a later request proves the duplicate notification was handled (neither ran nor resumed)
    sendClientTools(pyEnv, channelId, {
        session_id: sessionId,
        client_tools: [{ name: "sentinel", params: {} }],
        resume_token: "token-sentinel",
    });
    await expect.waitForSteps(["sentinel", "resume"]);
    expect(resumeCalls.map((call) => call.resume_token)).toEqual(["token-1", "token-sentinel"]);
});

test("a blocking client tool runs again when sending its result failed", async () => {
    registry.category("ow_ai.client_tools").add("reload", () => expect.step("reload"), {
        force: true,
    });
    let failResume = true;
    onRpcBefore("/ow_ai/session/resume", () => {
        expect.step("resume");
        if (failResume) {
            failResume = false;
            throw new Error("The server is unreachable");
        }
    });
    const { channelId, pyEnv, sessionId } = await startWithOwAiChat();
    updateOwAiSession(pyEnv, sessionId, {
        loop_state: "waiting_client_result",
        resume_token: "token-5",
    });
    const request = {
        session_id: sessionId,
        client_tools: [{ name: "reload", params: {} }],
        resume_token: "token-5",
    };
    sendClientTools(pyEnv, channelId, request);
    await expect.waitForSteps(["reload", "resume"]);
    await contains(".o_notification:has(.bg-danger):text('The server is unreachable')");
    const session = getService("mail.store")["ow.ai.session"].get(sessionId);
    expect(session.lastClientToolToken).toBe(undefined);
    sendClientTools(pyEnv, channelId, request);
    await expect.waitForSteps(["reload", "resume"]);
    await waitUntil(() => session.lastClientToolToken === "token-5");
    expect(session.loop_state).toBe("waiting_model");
});

test("a failing blocking client tool resumes the session with the error", async () => {
    registry.category("ow_ai.client_tools").add(
        "reload",
        () => {
            throw new Error("No view to reload");
        },
        { force: true }
    );
    const resumeCalls = spyOnResume();
    const { channelId, pyEnv, sessionId } = await startWithOwAiChat();
    updateOwAiSession(pyEnv, sessionId, {
        loop_state: "waiting_client_result",
        resume_token: "token-2",
    });
    sendClientTools(pyEnv, channelId, {
        session_id: sessionId,
        client_tools: [{ name: "reload", params: {} }],
        resume_token: "token-2",
    });
    await expect.waitForSteps(["resume"]);
    expect(resumeCalls[0].response).toEqual({ kind: "client_error", value: "No view to reload" });
});

test("the pending client tool request of the store can be processed", async () => {
    registry.category("ow_ai.client_tools").add(
        "reload",
        (env, params) => {
            expect.step(`reload ${JSON.stringify(params)}`);
        },
        { force: true }
    );
    const resumeCalls = spyOnResume();
    const { pyEnv, sessionId } = await startWithOwAiChat();
    updateOwAiSession(pyEnv, sessionId, {
        loop_state: "waiting_client_result",
        resume_token: "token-3",
        client_tool_request: { name: "reload", params: { soft: true }, resumeToken: "token-3" },
    });
    const session = getService("mail.store")["ow.ai.session"].get(sessionId);
    await waitUntil(() => session.clientToolRequest);
    // a Store update alone never runs a tool
    expect.verifySteps([]);
    await session.processPendingClientTool();
    expect.verifySteps(['reload {"soft":true}', "resume"]);
    expect(resumeCalls[0].response).toEqual({ kind: "client_result", value: ["Done."] });
});

test("client tool notifications of an unknown session are ignored", async () => {
    registry.category("ow_ai.client_tools").add("reload", () => expect.step("reload"), {
        force: true,
    });
    const resumeCalls = spyOnResume();
    const { channelId, pyEnv, sessionId } = await startWithOwAiChat();
    const otherSessionId = pyEnv["ow.ai.session"].create({ loop_state: "ready" });
    sendClientTools(pyEnv, channelId, {
        session_id: otherSessionId,
        client_tools: [{ name: "reload", params: {} }],
        resume_token: "token-4",
    });
    // a later notification of the known session proves the first one was handled (ignored)
    sendClientTools(pyEnv, channelId, {
        session_id: sessionId,
        client_tools: [{ name: "reload", params: {} }],
        blocking: false,
    });
    await expect.waitForSteps(["reload"]);
    expect(resumeCalls).toEqual([]);
});

test("only the tab that sent the message runs its client tools", async () => {
    registry
        .category("ow_ai.client_tools")
        .add("reload", (env, params) => expect.step(`reload ${params.tab}`), { force: true });
    const resumeCalls = spyOnResume();
    const { channelId, pyEnv, sessionId } = await startWithOwAiChat();
    // another tab's request: not run here, neither blocking...
    updateOwAiSession(pyEnv, sessionId, {
        loop_state: "waiting_client_result",
        resume_token: "token-6",
    });
    sendClientTools(pyEnv, channelId, {
        session_id: sessionId,
        client_tools: [{ name: "reload", params: { tab: "other" } }],
        resume_token: "token-6",
        client_identifier: "another-tab",
    });
    // ...nor non-blocking
    sendClientTools(pyEnv, channelId, {
        session_id: sessionId,
        client_tools: [{ name: "reload", params: { tab: "other" } }],
        blocking: false,
        client_identifier: "another-tab",
    });
    // this tab's request (it sent `client_identifier` with the advance) runs
    sendClientTools(pyEnv, channelId, {
        session_id: sessionId,
        client_tools: [{ name: "reload", params: { tab: "this" } }],
        resume_token: "token-6",
        client_identifier: OW_AI_CLIENT_IDENTIFIER,
    });
    await expect.waitForSteps(["reload this", "resume"]);
    expect(resumeCalls.map((call) => call.resume_token)).toEqual(["token-6"]);
});

test("the default client tools use the action service", async () => {
    const env = await start();
    let currentAction = { type: "ir.actions.client", tag: "mail.action_discuss" };
    patchWithCleanup(env.services.action, {
        async doAction(action, options) {
            expect.step(`doAction ${JSON.stringify(action)} ${JSON.stringify(options ?? {})}`);
        },
        // a promise, like Odoo 20's action service
        get currentAction() {
            return Promise.resolve(currentAction);
        },
    });
    // a client action (e.g. Discuss) is not reloaded
    expect(await runOwAiClientTool(env, { name: "reload", params: {} })).toBe("No view to reload.");
    expect.verifySteps([]);
    currentAction = { type: "ir.actions.act_window", res_model: "res.partner" };
    expect(await runOwAiClientTool(env, { name: "reload", params: {} })).toBe(
        "The current view was reloaded."
    );
    await runOwAiClientTool(env, {
        name: "do_action",
        params: { action: 42, context: { default_name: "Acme" } },
    });
    await runOwAiClientTool(env, {
        name: "do_action",
        params: { action: "base.action_partner_form" },
    });
    expect.verifySteps([
        'doAction "soft_reload" {}',
        'doAction 42 {"additionalContext":{"default_name":"Acme"}}',
        'doAction "base.action_partner_form" {"additionalContext":{}}',
    ]);
    await expect(
        runOwAiClientTool(env, {
            name: "do_action",
            params: { action: { type: "ir.actions.act_window", res_model: "res.partner" } },
        })
    ).rejects.toThrow(/action id or XML id/);
    await expect(runOwAiClientTool(env, { name: "unknown_tool", params: {} })).rejects.toThrow(
        /Unknown client tool/
    );
    expect.verifySteps([]);
});
