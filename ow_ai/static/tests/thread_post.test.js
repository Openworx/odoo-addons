import {
    contains,
    insertText,
    onRpcBefore,
    setupChatHub,
    start,
    startServer,
    triggerHotkey,
} from "@mail/../tests/mail_test_helpers";
import { describe, expect, test, waitUntil } from "@odoo/hoot";
import { markup } from "@odoo/owl";
import { getService, serverState } from "@web/../tests/web_test_helpers";

import { OW_AI_CLIENT_IDENTIFIER } from "@ow_ai/core/common/ow_ai_client_identifier";

import {
    createOwAiChat,
    defineOwAiModels,
    getChannelThread,
    updateOwAiSession,
} from "./ow_ai_test_helpers";

describe.current.tags("desktop");
defineOwAiModels();

const COMPOSER = ".o-mail-ChatWindow .o-mail-Composer-input";

function postedMessageIds(pyEnv, channelId) {
    return pyEnv["mail.message"]
        .search_read([
            ["model", "=", "discuss.channel"],
            ["res_id", "=", channelId],
            ["author_id", "=", serverState.partnerId],
        ])
        .map((message) => message.id);
}

test("posting in an AI chat advances the session with the posted message", async () => {
    const pyEnv = await startServer();
    const { channelId, sessionId } = createOwAiChat(pyEnv, { agentName: "Ava" });
    const advanceDone = Promise.withResolvers();
    let advanceParams;
    onRpcBefore("/ow_ai/session/advance", async (params) => {
        advanceParams = params;
        expect.step("advance");
        await advanceDone.promise;
    });
    setupChatHub({ opened: [channelId] });
    await start();
    await insertText(COMPOSER, "hello");
    await triggerHotkey("Enter");
    await expect.waitForSteps(["advance"]);
    const store = getService("mail.store");
    const session = store["ow.ai.session"].get(sessionId);
    const channel = getChannelThread(channelId);
    // the advance RPC is still pending
    expect(session.isSubmitting).toBe(true);
    expect(session.isGenerating).toBe(true);
    expect(channel.isAiGenerating).toBe(true);
    const messageIds = postedMessageIds(pyEnv, channelId);
    expect(messageIds.length).toBe(1);
    expect(advanceParams.channel_id).toBe(channelId);
    expect(advanceParams.message_id).toBe(messageIds[0]);
    expect(advanceParams.session_config).toEqual({
        auto_confirm: false,
        show_agent_steps: false,
        web_search: false,
    });
    expect(advanceParams.current_view_info).toBe(null);
    // the tab that runs the assistant's client tools
    expect(advanceParams.client_identifier).toBe(OW_AI_CLIENT_IDENTIFIER);
    advanceDone.resolve();
    await waitUntil(() => !session.isSubmitting);
    expect(session.loop_state).toBe("waiting_model");
    expect(channel.isAiGenerating).toBe(true);
    // the agent member types while the assistant answers (shown by the chat window header)
    expect(channel.hasOtherMembersTyping).toBe(true);
    expect(channel.otherTypingMembers.map((member) => member.name)).toEqual(["Ava"]);
});

test("posting while the assistant is answering is refused", async () => {
    const pyEnv = await startServer();
    const { channelId, sessionId } = createOwAiChat(pyEnv);
    pyEnv["ow.ai.session"].write([sessionId], { loop_state: "waiting_model" });
    onRpcBefore("/ow_ai/session/advance", () => expect.step("advance"));
    setupChatHub({ opened: [channelId] });
    await start();
    // the composer is disabled meanwhile...
    await contains(`${COMPOSER}:disabled`);
    // ...a post can only come from a race
    const channel = getChannelThread(channelId);
    await channel.post(markup`hello again`);
    await contains(
        ".o_notification:has(.bg-warning):text('The assistant is still answering. Please wait.')"
    );
    expect(postedMessageIds(pyEnv, channelId)).toEqual([]);
    expect.verifySteps([]);
});

test("posting while a card waits for the user still advances (the server skips the card)", async () => {
    const pyEnv = await startServer();
    const { channelId, sessionId } = createOwAiChat(pyEnv);
    onRpcBefore("/ow_ai/session/advance", () => expect.step("advance"));
    setupChatHub({ opened: [channelId] });
    await start();
    await contains(COMPOSER);
    updateOwAiSession(pyEnv, sessionId, {
        loop_state: "waiting_answer",
        resume_token: "token-1",
        user_input_request: { type: "question", body: "Which one?", resumeToken: "token-1" },
    });
    const channel = getChannelThread(channelId);
    await waitUntil(() => channel.isAiWaitingForUser);
    await insertText(COMPOSER, "never mind");
    await triggerHotkey("Enter");
    await expect.waitForSteps(["advance"]);
    // the server drops the card and publishes the new state
    await waitUntil(() => !channel.owAiSession.userInputRequest);
    expect(channel.owAiSession.loop_state).toBe("waiting_model");
    expect(channel.isAiWaitingForUser).toBe(false);
    expect(channel.isAiGenerating).toBe(true);
});

test("posting while a client tool call is pending still advances (the server aborts the call)", async () => {
    const pyEnv = await startServer();
    const { channelId, sessionId } = createOwAiChat(pyEnv);
    // e.g. this tab missed the blocking `ow_ai.session/client_tools` notification
    pyEnv["ow.ai.session"].write([sessionId], {
        loop_state: "waiting_client_result",
        resume_token: "token-1",
        client_tool_request: { name: "reload", params: {}, resumeToken: "token-1" },
    });
    onRpcBefore("/ow_ai/session/advance", () => expect.step("advance"));
    setupChatHub({ opened: [channelId] });
    await start();
    await contains(COMPOSER);
    const channel = getChannelThread(channelId);
    expect(channel.isAiGenerating).toBe(true);
    await insertText(COMPOSER, "are you stuck?");
    await triggerHotkey("Enter");
    await expect.waitForSteps(["advance"]);
    expect(postedMessageIds(pyEnv, channelId).length).toBe(1);
    await waitUntil(() => !channel.owAiSession.clientToolRequest);
    await waitUntil(() => !channel.owAiSession.isSubmitting);
    expect(channel.owAiSession.loop_state).toBe("waiting_model");
});

test("an advance error is shown as a danger notification", async () => {
    const pyEnv = await startServer();
    const { channelId, sessionId } = createOwAiChat(pyEnv);
    onRpcBefore("/ow_ai/session/advance", () => {
        throw new Error("The AI is unavailable");
    });
    setupChatHub({ opened: [channelId] });
    await start();
    await insertText(COMPOSER, "hello");
    await triggerHotkey("Enter");
    await contains(".o_notification:has(.bg-danger):text('The AI is unavailable')");
    const session = getService("mail.store")["ow.ai.session"].get(sessionId);
    await waitUntil(() => !session.isSubmitting);
    expect(session.isGenerating).toBe(false);
});
