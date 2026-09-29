import {
    click,
    contains,
    onRpcBefore,
    setupChatHub,
    start,
    startServer,
} from "@mail/../tests/mail_test_helpers";
import { describe, expect, test } from "@odoo/hoot";
import { onRpc, serverState } from "@web/../tests/web_test_helpers";

import {
    agentStepBody,
    createChatMessage,
    createOwAiChat,
    defineOwAiModels,
    postChatMessage,
    toolSummaryBody,
    updateOwAiSession,
} from "./ow_ai_test_helpers";

describe.current.tags("desktop");
defineOwAiModels();

const WINDOW = ".o-mail-ChatWindow";
const STEPS = `${WINDOW} .o_ow_ai_steps`;

/** A turn in progress: the user's message, two agent steps around a tool call. */
async function startWithSteps({ sessionValues = {} } = {}) {
    const pyEnv = await startServer();
    const chat = createOwAiChat(pyEnv, { agentName: "Ava" });
    const { agentPartnerId, channelId, sessionId } = chat;
    createChatMessage(pyEnv, channelId, {
        author_id: serverState.partnerId,
        body: "<p>Top customers?</p>",
    });
    createChatMessage(pyEnv, channelId, {
        author_id: agentPartnerId,
        body: agentStepBody("<p>Looking up the customers</p>", 7),
        subtype_xmlid: "mail.mt_note",
    });
    createChatMessage(pyEnv, channelId, {
        author_id: agentPartnerId,
        body: toolSummaryBody("Searched contacts: 3 of 3", { callId: "call_1", eventId: 7 }),
        message_type: "notification",
        subtype_xmlid: "mail.mt_note",
    });
    createChatMessage(pyEnv, channelId, {
        author_id: agentPartnerId,
        body: agentStepBody("<p>Now summing the sales</p>", 8),
        subtype_xmlid: "mail.mt_note",
    });
    pyEnv["ow.ai.session"].write([sessionId], {
        loop_state: "waiting_model",
        show_agent_steps: true,
        ...sessionValues,
    });
    setupChatHub({ opened: [channelId] });
    await start();
    await contains(`${WINDOW} .o-mail-Message:has(:text('Top customers?'))`);
    return { ...chat, pyEnv };
}

test("consecutive agent steps and tool summaries fold into one open block while the assistant works", async () => {
    await startWithSteps();
    await contains(STEPS, { count: 1 });
    await contains(`${STEPS} .o_ow_ai_steps_header:contains('Steps taken')`);
    await contains(`${STEPS} .o_ow_ai_steps_count:text('3')`);
    await contains(`${STEPS} .o_ow_ai_steps_list`);
    await contains(`${STEPS} .o_ow_ai_step`, { count: 2 });
    await contains(`${STEPS} .o_ow_ai_step:text('Looking up the customers')`);
    await contains(`${STEPS} .o_ow_ai_tool_summary_row:text('Searched contacts: 3 of 3')`);
    // the server's icon class (Font Awesome) is carried straight through
    await contains(`${STEPS} .o_ow_ai_tool_summary_row i.fa.fa-search`);
    // rendered only in the block: not as messages or notifications
    await contains(`${WINDOW} .o-mail-Message`, { count: 1 });
    await contains(`${WINDOW} .o-mail-NotificationMessage`, { count: 0 });
    // the assistant's status while it answers
    await contains(`${WINDOW} .o_ow_ai_status:text('Thinking…')`);
});

test("the steps block folds when the answer arrives, and unfolds on click", async () => {
    const { agentPartnerId, channelId, pyEnv, sessionId } = await startWithSteps();
    await contains(`${STEPS} .o_ow_ai_steps_list`);
    postChatMessage(pyEnv, channelId, {
        author_id: agentPartnerId,
        body: "<p>Acme is your top customer.</p>",
    });
    updateOwAiSession(pyEnv, sessionId, { loop_state: "ready" });
    await contains(`${WINDOW} .o-mail-Message:has(:text('Acme is your top customer.'))`);
    await contains(`${STEPS} .o_ow_ai_steps_list`, { count: 0 });
    await contains(`${WINDOW} .o_ow_ai_status`, { count: 0 });
    // the answer is marked, and shows its author after the block
    await contains(
        `${WINDOW} .o-mail-Message.o_ow_ai_agent_answer:has(.o-mail-Message-author:text('Ava'))`
    );
    await click(`${STEPS} .o_ow_ai_steps_header`);
    await contains(`${STEPS} .o_ow_ai_steps_list .o_ow_ai_step`, { count: 2 });
});

test("steps are hidden when the chat does not show them", async () => {
    await startWithSteps({ sessionValues: { show_agent_steps: false } });
    await contains(`${WINDOW} .o_ow_ai_status:text('Thinking…')`);
    await contains(STEPS, { count: 0 });
    await contains(`${WINDOW} .o-mail-Message`, { count: 1 });
    await contains(`${WINDOW} .o-mail-NotificationMessage`, { count: 0 });
});

test("the status line shows the running tool's status", async () => {
    await startWithSteps({ sessionValues: { tool_status: "Summing the sales…" } });
    await contains(`${WINDOW} .o_ow_ai_status .o_ow_ai_shimmer:text('Summing the sales…')`);
});

test("a tool summary unfolds the call's arguments", async () => {
    onRpc("ow.ai.session.event", "get_tool_params", ({ args }) => {
        expect.step(`get_tool_params ${JSON.stringify(args)}`);
        return { model_name: "res.partner", limit: 3 };
    });
    await startWithSteps();
    await click(`${STEPS} .o_ow_ai_tool_summary_toggle`);
    await expect.waitForSteps([`get_tool_params [[7],"call_1"]`]);
    await contains(`${STEPS} .o_ow_ai_tool_params pre:contains('"model_name": "res.partner"')`);
    // fetched once
    await click(`${STEPS} .o_ow_ai_tool_summary_toggle`);
    await contains(`${STEPS} .o_ow_ai_tool_params`, { count: 0 });
    await click(`${STEPS} .o_ow_ai_tool_summary_toggle`);
    await contains(`${STEPS} .o_ow_ai_tool_params pre`);
    expect.verifySteps([]);
});

test("waiting for a client tool offers to run it again in this tab", async () => {
    const pyEnv = await startServer();
    const { channelId, sessionId } = createOwAiChat(pyEnv);
    createChatMessage(pyEnv, channelId, { author_id: serverState.partnerId, body: "Reload" });
    pyEnv["ow.ai.session"].write([sessionId], {
        loop_state: "waiting_client_result",
        resume_token: "token-1",
        client_tool_request: { name: "reload", params: {}, resumeToken: "token-1" },
    });
    onRpcBefore("/ow_ai/session/resume", (params) => {
        expect.step(`resume ${params.response.kind}`);
    });
    setupChatHub({ opened: [channelId] });
    await start();
    await contains(`${WINDOW} .o_ow_ai_status:contains('Waiting for this page…')`);
    // the composer stays usable: posting aborts the pending call
    await contains(`${WINDOW} .o-mail-Composer-input:enabled`);
    await click(`${WINDOW} .o_ow_ai_status_retry`);
    // no view to reload in the test's web client: the tool reports it, then resumes
    await expect.waitForSteps(["resume client_result"]);
    await contains(`${WINDOW} .o_ow_ai_status:text('Thinking…')`);
});
