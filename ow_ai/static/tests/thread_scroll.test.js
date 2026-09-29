import { contains, setupChatHub, start, startServer } from "@mail/../tests/mail_test_helpers";
import { animationFrame, describe, test, waitUntil } from "@odoo/hoot";
import { queryOne } from "@odoo/hoot-dom";
import { serverState } from "@web/../tests/web_test_helpers";

import {
    agentStepBody,
    createChatMessage,
    createOwAiChat,
    defineOwAiModels,
    getChannelThread,
    postChatMessage,
    toolSummaryBody,
    updateOwAiSession,
} from "./ow_ai_test_helpers";

describe.current.tags("desktop");
defineOwAiModels();

const THREAD = ".o-mail-ChatWindow .o-mail-Thread";

/** A chat long enough to scroll, open at its bottom, the assistant answering. */
async function startScrolledChat({ showSteps = true, loopState = "waiting_model" } = {}) {
    const pyEnv = await startServer();
    const chat = createOwAiChat(pyEnv, { agentName: "Ava" });
    for (let i = 0; i < 12; i++) {
        createChatMessage(pyEnv, chat.channelId, {
            author_id: i % 2 ? chat.agentPartnerId : serverState.partnerId,
            body: `<p>Message ${i}</p><p>with a second line</p>`,
        });
    }
    pyEnv["ow.ai.session"].write([chat.sessionId], {
        loop_state: loopState,
        show_agent_steps: showSteps,
    });
    setupChatHub({ opened: [chat.channelId] });
    await start();
    await contains(`${THREAD} .o-mail-Message:has(:text('Message 11'))`);
    // the user reads the end of the chat (it opens at the first unread message),
    // and the thread knows it (`Thread.saveScroll`, on the scroll event); mail
    // may still apply its opening scroll (on its first resize observation) and
    // move the chat back: scroll again until the thread is at its bottom
    const thread = getChannelThread(chat.channelId);
    await waitUntil(() => {
        if (thread.scrollTop !== "bottom") {
            const threadEl = queryOne(THREAD);
            threadEl.scrollTop = threadEl.scrollHeight;
        }
        return thread.scrollTop === "bottom";
    });
    await contains(THREAD, { scroll: "bottom" });
    return { ...chat, pyEnv, thread };
}

test("the chat follows an agent step folded into 'Steps taken', then the answer", async () => {
    const { agentPartnerId, channelId, pyEnv, sessionId } = await startScrolledChat();
    postChatMessage(pyEnv, channelId, {
        author_id: agentPartnerId,
        body: agentStepBody("<p>Looking it up</p>"),
        subtype_xmlid: "mail.mt_note",
    });
    postChatMessage(pyEnv, channelId, {
        author_id: agentPartnerId,
        body: toolSummaryBody("Searched contacts: 3 of 3"),
        message_type: "notification",
        subtype_xmlid: "mail.mt_note",
    });
    await contains(`${THREAD} .o_ow_ai_steps .o_ow_ai_steps_count:text('2')`);
    await contains(THREAD, { scroll: "bottom" });
    postChatMessage(pyEnv, channelId, {
        author_id: agentPartnerId,
        body: "<p>Acme is your top customer.</p>",
    });
    updateOwAiSession(pyEnv, sessionId, { loop_state: "ready" });
    await contains(`${THREAD} .o-mail-Message:has(:text('Acme is your top customer.'))`);
    await contains(THREAD, { scroll: "bottom" });
});

test("the chat follows hidden steps, then the answer", async () => {
    const { agentPartnerId, channelId, pyEnv, sessionId, thread } = await startScrolledChat({
        showSteps: false,
    });
    postChatMessage(pyEnv, channelId, {
        author_id: agentPartnerId,
        body: agentStepBody("<p>Looking it up</p>"),
        subtype_xmlid: "mail.mt_note",
    });
    // nothing to see: wait until the bus delivered it
    await waitUntil(() => thread.messages.some((message) => message.owAiIsAgentStep));
    await animationFrame();
    await contains(`${THREAD} .o_ow_ai_steps`, { count: 0 });
    postChatMessage(pyEnv, channelId, {
        author_id: agentPartnerId,
        body: "<p>Acme is your top customer.</p>",
    });
    updateOwAiSession(pyEnv, sessionId, { loop_state: "ready" });
    await contains(`${THREAD} .o-mail-Message:has(:text('Acme is your top customer.'))`);
    await contains(THREAD, { scroll: "bottom" });
});

test("the chat stays at its bottom when the assistant's status line comes and goes", async () => {
    const { pyEnv, sessionId } = await startScrolledChat({ loopState: "ready" });
    await contains(`${THREAD} .o_ow_ai_status`, { count: 0 });
    // the user's message was posted: the assistant starts answering
    updateOwAiSession(pyEnv, sessionId, { loop_state: "waiting_model" });
    await contains(`${THREAD} .o_ow_ai_status:text('Thinking…')`);
    await contains(THREAD, { scroll: "bottom" });
    updateOwAiSession(pyEnv, sessionId, {
        tool_status: "Searching contacts with a long name to make the status line wrap…",
    });
    await contains(`${THREAD} .o_ow_ai_status:contains('Searching contacts')`);
    await contains(THREAD, { scroll: "bottom" });
});
