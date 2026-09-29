import {
    click,
    contains,
    focus,
    insertText,
    onRpcBefore,
    setupChatHub,
    start,
    startServer,
} from "@mail/../tests/mail_test_helpers";
import { describe, expect, getFixture, press, queryAllTexts, test } from "@odoo/hoot";
import { serverState } from "@web/../tests/web_test_helpers";

import { OW_AI_CLIENT_IDENTIFIER } from "@ow_ai/core/common/ow_ai_client_identifier";

import {
    createChatMessage,
    createOwAiChat,
    defineOwAiModels,
    inputCardBody,
    updateOwAiSession,
} from "./ow_ai_test_helpers";

describe.current.tags("desktop");
defineOwAiModels();

const CARD = ".o-mail-ChatWindow .o_ow_ai_user_input_request";

/**
 * Open an AI chat whose session waits on `request`: the user's message, the
 * history copy of the card, and the session's Store request.
 */
async function startWithCard(request, { loopState, onResume } = {}) {
    const pyEnv = await startServer();
    const chat = createOwAiChat(pyEnv, { agentName: "Ava" });
    createChatMessage(pyEnv, chat.channelId, {
        author_id: serverState.partnerId,
        body: "<p>Do it</p>",
    });
    createChatMessage(pyEnv, chat.channelId, {
        author_id: chat.agentPartnerId,
        body: inputCardBody("<p>Create <b>Acme</b>?</p>"),
    });
    pyEnv["ow.ai.session"].write([chat.sessionId], {
        loop_state:
            loopState ??
            (request.type === "confirmation" ? "waiting_confirmation" : "waiting_answer"),
        resume_token: "token-1",
        user_input_request: {
            body: ["markup", "<p>Create <b>Acme</b>?</p>"],
            resumeToken: "token-1",
            ...request,
        },
    });
    const resumeCalls = [];
    onRpcBefore("/ow_ai/session/resume", (params) => {
        resumeCalls.push(params);
        expect.step("resume");
        return onResume?.(params);
    });
    setupChatHub({ opened: [chat.channelId] });
    await start();
    await contains(CARD);
    return { ...chat, pyEnv, resumeCalls };
}

const CONFIRMATION = {
    type: "confirmation",
    choices: ["confirm_once", "auto_confirm", "decline"],
    labels: {
        confirm_once: "Yes, do it",
        auto_confirm: "Yes, always approve in this chat",
        decline: "No, I want something else",
    },
};

const QUESTION = {
    type: "question",
    choices: ["A", "B", "C"],
    multiSelect: false,
    allowFreeText: false,
};

test("a confirmation card shows its body and three choices, and resumes with the one clicked", async () => {
    const { channelId, resumeCalls, sessionId } = await startWithCard(CONFIRMATION);
    // the body is HTML (Store markup), not escaped text
    await contains(`${CARD} .o_ow_ai_user_input_request_body b:text('Acme')`);
    expect(queryAllTexts(`${CARD} button`)).toEqual([
        "Yes, do it",
        "Yes, always approve in this chat",
        "No, I want something else",
    ]);
    // the history copy of the card is left out while the card is live
    await contains(".o-mail-ChatWindow .o-mail-Message", { count: 1 });
    await click(`${CARD} button:text('Yes, do it')`);
    await expect.waitForSteps(["resume"]);
    expect(resumeCalls[0]).toEqual({
        channel_id: channelId,
        session_id: sessionId,
        resume_token: "token-1",
        response: { kind: "confirmation", value: "confirm_once" },
        // the answering tab runs the rest of the turn's client tools
        client_identifier: OW_AI_CLIENT_IDENTIFIER,
    });
    // the server drops the request: the card goes, its history copy comes back
    await contains(CARD, { count: 0 });
    await contains(".o-mail-ChatWindow .o-mail-Message .o_ow_ai_input_card b:text('Acme')");
});

test("a confirmation card falls back to the default labels, spins and disables while sending", async () => {
    const pending = Promise.withResolvers();
    const { resumeCalls } = await startWithCard(
        { ...CONFIRMATION, labels: {} },
        { onResume: () => pending.promise }
    );
    expect(queryAllTexts(`${CARD} button`)).toEqual([
        "Yes, do it",
        "Yes, always approve in this chat",
        "No",
    ]);
    await click(`${CARD} button[name='decline']`);
    await expect.waitForSteps(["resume"]);
    expect(resumeCalls[0].response).toEqual({ kind: "confirmation", value: "decline" });
    await contains(`${CARD} .fa-spin`);
    await contains(`${CARD} button:disabled`, { count: 3 });
    pending.resolve();
    await contains(CARD, { count: 0 });
});

test("a single-choice question sends the chosen option once one is picked", async () => {
    const { resumeCalls } = await startWithCard(QUESTION);
    await contains(`${CARD} input[type='radio']`, { count: 3 });
    await contains(`${CARD} .o_ow_ai_skip`);
    await contains(`${CARD} .o_ow_ai_send:disabled`);
    await click(`${CARD} .o_ow_ai_option:has(:text('A'))`);
    await click(`${CARD} .o_ow_ai_option:has(:text('B'))`);
    await contains(`${CARD} input:checked`, { count: 1 });
    await contains(`${CARD} .o_ow_ai_send:enabled`);
    await click(`${CARD} .o_ow_ai_send`);
    await expect.waitForSteps(["resume"]);
    expect(resumeCalls[0].response).toEqual({
        kind: "question",
        value: { choices: ["B"], text: "" },
    });
});

test("a multiple-choice question sends every checked option", async () => {
    const { resumeCalls } = await startWithCard({ ...QUESTION, multiSelect: true });
    await contains(`${CARD} input[type='checkbox']`, { count: 3 });
    await click(`${CARD} .o_ow_ai_option:has(:text('C'))`);
    await click(`${CARD} .o_ow_ai_option:has(:text('A'))`);
    await contains(`${CARD} input:checked`, { count: 2 });
    await click(`${CARD} .o_ow_ai_send`);
    await expect.waitForSteps(["resume"]);
    expect(resumeCalls[0].response).toEqual({
        kind: "question",
        value: { choices: ["A", "C"], text: "" },
    });
});

test("a question allowing free text can be answered with text only", async () => {
    const { resumeCalls } = await startWithCard({ ...QUESTION, allowFreeText: true });
    await contains(`${CARD} .o_ow_ai_send:disabled`);
    await insertText(`${CARD} .o_ow_ai_free_text`, "Something else");
    await contains(`${CARD} .o_ow_ai_send:enabled`);
    await click(`${CARD} .o_ow_ai_send`);
    await expect.waitForSteps(["resume"]);
    expect(resumeCalls[0].response).toEqual({
        kind: "question",
        value: { choices: [], text: "Something else" },
    });
});

test("a question without free text has no text input", async () => {
    await startWithCard(QUESTION);
    await contains(`${CARD} .o_ow_ai_free_text`, { count: 0 });
});

test("skipping a question resumes with `skip`", async () => {
    const { resumeCalls } = await startWithCard(QUESTION);
    await click(`${CARD} .o_ow_ai_skip`);
    await expect.waitForSteps(["resume"]);
    expect(resumeCalls[0].response).toEqual({ kind: "skip" });
});

test("a question is answered with the keyboard: a digit picks, Enter sends", async () => {
    const { resumeCalls } = await startWithCard(QUESTION);
    await focus(CARD);
    await press("1");
    await contains(`${CARD} .o_ow_ai_option_selected:has(:text('A'))`);
    await press("Enter");
    await expect.waitForSteps(["resume"]);
    expect(resumeCalls[0].response).toEqual({
        kind: "question",
        value: { choices: ["A"], text: "" },
    });
});

test("Enter on the focused Skip button skips (it does not send)", async () => {
    const { resumeCalls } = await startWithCard(QUESTION);
    await click(`${CARD} .o_ow_ai_option:has(:text('A'))`);
    await focus(`${CARD} .o_ow_ai_skip`);
    await press("Enter");
    await expect.waitForSteps(["resume"]);
    expect(resumeCalls[0].response).toEqual({ kind: "skip" });
});

test("Escape skips a question", async () => {
    const { resumeCalls } = await startWithCard(QUESTION);
    await focus(CARD);
    await press("Escape");
    await expect.waitForSteps(["resume"]);
    expect(resumeCalls[0].response).toEqual({ kind: "skip" });
});

test("a failed answer is reported and the card stays", async () => {
    await startWithCard(QUESTION, {
        onResume: () => {
            throw new Error("The session moved on");
        },
    });
    await click(`${CARD} .o_ow_ai_option:has(:text('A'))`);
    await click(`${CARD} .o_ow_ai_send`);
    await expect.waitForSteps(["resume"]);
    await contains(".o_notification:has(.bg-danger):text('The session moved on')");
    await contains(`${CARD} .o_ow_ai_send:enabled`);
});

test("a question card does not take the focus from outside the chat", async () => {
    const pyEnv = await startServer();
    const { channelId, sessionId } = createOwAiChat(pyEnv);
    createChatMessage(pyEnv, channelId, { author_id: serverState.partnerId, body: "Pick one" });
    setupChatHub({ opened: [channelId] });
    await start();
    await contains(".o-mail-ChatWindow .o-mail-Message");
    // e.g. a button of the view behind the chat window
    const outside = document.createElement("button");
    outside.textContent = "Outside";
    getFixture().append(outside);
    outside.focus();
    updateOwAiSession(pyEnv, sessionId, {
        loop_state: "waiting_answer",
        resume_token: "token-1",
        user_input_request: {
            type: "question",
            body: "Which?",
            choices: ["A"],
            resumeToken: "token-1",
        },
    });
    await contains(CARD);
    expect(document.activeElement).toBe(outside);
});

test("a question card takes the focus when nothing else has it", async () => {
    const pyEnv = await startServer();
    const { channelId, sessionId } = createOwAiChat(pyEnv);
    createChatMessage(pyEnv, channelId, { author_id: serverState.partnerId, body: "Pick one" });
    setupChatHub({ opened: [channelId] });
    await start();
    await contains(".o-mail-ChatWindow .o-mail-Message");
    document.activeElement?.blur();
    updateOwAiSession(pyEnv, sessionId, {
        loop_state: "waiting_answer",
        resume_token: "token-1",
        user_input_request: {
            type: "question",
            body: "Which?",
            choices: ["A"],
            resumeToken: "token-1",
        },
    });
    await contains(`${CARD}:focus`);
});

test("no card once the session stopped waiting for the user", async () => {
    const pyEnv = await startServer();
    const { channelId, sessionId } = createOwAiChat(pyEnv);
    createChatMessage(pyEnv, channelId, { author_id: serverState.partnerId, body: "Hi" });
    pyEnv["ow.ai.session"].write([sessionId], {
        loop_state: "waiting_model",
        user_input_request: { type: "question", body: "Which?", choices: ["A"] },
    });
    setupChatHub({ opened: [channelId] });
    await start();
    await contains(".o-mail-ChatWindow .o-mail-Message", { count: 1 });
    await contains(CARD, { count: 0 });
});
