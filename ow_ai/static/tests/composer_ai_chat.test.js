import {
    click,
    contains,
    insertText,
    onRpcBefore,
    openDiscuss,
    setupChatHub,
    start,
    startServer,
} from "@mail/../tests/mail_test_helpers";
import { animationFrame, describe, expect, test } from "@odoo/hoot";
import { Command, onRpc, serverState } from "@web/../tests/web_test_helpers";

import { OW_AI_ACCEPTED_FILE_TYPES } from "@ow_ai/discuss/composer_patch";

import { createOwAiChat, defineOwAiModels, updateOwAiSession } from "./ow_ai_test_helpers";

describe.current.tags("desktop");
defineOwAiModels();

const WINDOW = ".o-mail-ChatWindow";

test("the composer is disabled while the assistant answers", async () => {
    const pyEnv = await startServer();
    const { channelId, sessionId } = createOwAiChat(pyEnv);
    pyEnv["ow.ai.session"].write([sessionId], { loop_state: "waiting_model" });
    setupChatHub({ opened: [channelId] });
    await start();
    await contains(
        `${WINDOW} .o-mail-Composer-input:disabled[placeholder='The assistant is answering…']`
    );
    await contains(`${WINDOW} .o-mail-Composer button[title='More Actions']:disabled`);
    updateOwAiSession(pyEnv, sessionId, { loop_state: "ready" });
    await contains(`${WINDOW} .o-mail-Composer-input:enabled`);
    await contains(
        `${WINDOW} .o-mail-Composer-input:not([placeholder='The assistant is answering…'])`
    );
});

test("the composer of the Discuss app is disabled too", async () => {
    const pyEnv = await startServer();
    const { channelId, sessionId } = createOwAiChat(pyEnv);
    pyEnv["ow.ai.session"].write([sessionId], { loop_state: "waiting_model" });
    await start();
    await openDiscuss(channelId);
    await contains(".o-mail-DiscussContent .o-mail-Composer-input:disabled");
    updateOwAiSession(pyEnv, sessionId, { loop_state: "ready" });
    await contains(".o-mail-DiscussContent .o-mail-Composer-input:enabled");
});

test("the composer stays enabled while a card waits for the user", async () => {
    const pyEnv = await startServer();
    const { channelId, sessionId } = createOwAiChat(pyEnv);
    pyEnv["ow.ai.session"].write([sessionId], {
        loop_state: "waiting_answer",
        resume_token: "token-1",
        user_input_request: { type: "question", body: "Which?", choices: ["A"] },
    });
    setupChatHub({ opened: [channelId] });
    await start();
    await contains(`${WINDOW} .o-mail-Composer-input:enabled`);
});

test("the chat settings toggles call /ow_ai/session/config", async () => {
    const pyEnv = await startServer();
    const { channelId, sessionId } = createOwAiChat(pyEnv);
    const configCalls = [];
    onRpcBefore("/ow_ai/session/config", (params) => {
        configCalls.push(params);
        expect.step("config");
    });
    setupChatHub({ opened: [channelId] });
    await start();
    await click(`${WINDOW} .o-mail-Composer button[title='More Actions']`);
    await contains(".o-dropdown-item[name='ow_ai_auto_approve']:contains('Auto-approve actions')");
    await contains(".o-dropdown-item[name='ow_ai_show_steps']:contains('Show steps')");
    await contains(".o-dropdown-item[name='ow_ai_auto_approve'] input:not(:checked)");
    await contains(".o-dropdown-item[name='ow_ai_show_steps'] input:not(:checked)");
    await click(".o-dropdown-item[name='ow_ai_auto_approve']");
    await expect.waitForSteps(["config"]);
    expect(configCalls[0]).toEqual({ channel_id: channelId, config: { auto_confirm: true } });
    // the menu stays open and shows the new state
    await contains(".o-dropdown-item[name='ow_ai_auto_approve'] input:checked");
    await click(".o-dropdown-item[name='ow_ai_show_steps']");
    await expect.waitForSteps(["config"]);
    expect(configCalls[1]).toEqual({ channel_id: channelId, config: { show_agent_steps: true } });
    await contains(".o-dropdown-item[name='ow_ai_show_steps'] input:checked");
    const [session] = pyEnv["ow.ai.session"].browse(sessionId);
    expect([session.auto_confirm, session.show_agent_steps]).toEqual([true, true]);
});

test("an AI chat composer has no emoji, GIF, voice or mention actions and restricts uploads", async () => {
    const pyEnv = await startServer();
    const { channelId } = createOwAiChat(pyEnv);
    setupChatHub({ opened: [channelId] });
    await start();
    await contains(`${WINDOW} .o-mail-Composer-input`);
    await contains(`${WINDOW} .o-mail-Composer button[name='add-emoji']`, { count: 0 });
    await contains(`${WINDOW} .o-mail-Composer button[name='send-message']`);
    await click(`${WINDOW} .o-mail-Composer button[title='More Actions']`);
    await contains(".o-dropdown-item[name='upload-files']");
    for (const name of ["add-gif", "voice-start", "add-canned-response", "create-poll"]) {
        await contains(`.o-dropdown-item[name='${name}']`, { count: 0 });
    }
    expect(OW_AI_ACCEPTED_FILE_TYPES).toInclude("application/pdf");
    await contains(
        `${WINDOW} .o-mail-Composer input[type='file'][accept='${OW_AI_ACCEPTED_FILE_TYPES}']`
    );
});

test("typing @ in an AI chat suggests nobody", async () => {
    const pyEnv = await startServer();
    pyEnv["res.partner"].create({ name: "Mitchell Admin 2" });
    const { channelId } = createOwAiChat(pyEnv);
    onRpc("res.partner", "get_mention_suggestions_from_channel", () => {
        expect.step("mention suggestions");
    });
    setupChatHub({ opened: [channelId] });
    await start();
    await insertText(`${WINDOW} .o-mail-Composer-input`, "@Mit");
    await animationFrame();
    await contains(".o-mail-Composer-suggestionList .o-mail-NavigableList-item", { count: 0 });
    expect.verifySteps([]);
});

test("other chats keep their composer actions", async () => {
    const pyEnv = await startServer();
    const partnerId = pyEnv["res.partner"].create({ name: "Demo" });
    pyEnv["res.users"].create({ partner_id: partnerId });
    const channelId = pyEnv["discuss.channel"].create({
        channel_type: "chat",
        channel_member_ids: [
            Command.create({ partner_id: serverState.partnerId }),
            Command.create({ partner_id: partnerId }),
        ],
    });
    setupChatHub({ opened: [channelId] });
    await start();
    await contains(`${WINDOW} .o-mail-Composer button[name='add-emoji']`);
    await contains(`${WINDOW} .o-mail-Composer input[type='file'][accept='*']`);
});
