import {
    click,
    contains,
    hover,
    onRpcBefore,
    setupChatHub,
    start,
    startServer,
} from "@mail/../tests/mail_test_helpers";
import { describe, expect, queryAll, test } from "@odoo/hoot";
import { getService, patchWithCleanup, serverState } from "@web/../tests/web_test_helpers";

import { createChatMessage, createOwAiChat, defineOwAiModels } from "./ow_ai_test_helpers";

describe.current.tags("desktop");
defineOwAiModels();

const WINDOW = ".o-mail-ChatWindow";
const ANSWER = `${WINDOW} .o-mail-Message:has(:text('Acme is in Utrecht.'))`;
const USER_MESSAGE = `${WINDOW} .o-mail-Message:has(:text('Where is Acme?'))`;

/** An AI chat with a question of the user and the agent's answer. */
async function startWithAnswer({ recordBound = false } = {}) {
    const pyEnv = await startServer();
    const chat = createOwAiChat(pyEnv, { agentName: "Ava" });
    if (recordBound) {
        const partnerId = pyEnv["res.partner"].create({ name: "Acme" });
        pyEnv["ow.ai.session"].write([chat.sessionId], {
            res_model: "res.partner",
            res_id: partnerId,
        });
        chat.partnerId = partnerId;
    }
    createChatMessage(pyEnv, chat.channelId, {
        author_id: serverState.partnerId,
        body: "<p>Where is Acme?</p>",
    });
    createChatMessage(pyEnv, chat.channelId, {
        author_id: chat.agentPartnerId,
        body: "<p>Acme is in Utrecht.</p>",
    });
    setupChatHub({ opened: [chat.channelId] });
    await start();
    await contains(ANSWER);
    return { ...chat, pyEnv };
}

/** Names of a message's actions: quick buttons, then the "Expand" menu. */
async function messageActionNames(selector) {
    await hover(selector);
    await contains(`${selector} .o-mail-Message-actions button[name='copy-message']`);
    const names = queryAll(`${selector} .o-mail-Message-actions button[name]`)
        .map((el) => el.getAttribute("name"))
        .filter((name) => !name.startsWith("more-action"));
    if (queryAll(`${selector} [title='Expand']`).length) {
        await click(`${selector} [title='Expand']`);
        await contains(".o-mail-Message-moreMenu");
        names.push(
            ...queryAll(".o-mail-Message-moreMenu .o-dropdown-item[name]").map((el) =>
                el.getAttribute("name")
            )
        );
    }
    return names;
}

test("an answer in a record chat can be copied, sent as a message or logged as a note", async () => {
    await startWithAnswer({ recordBound: true });
    expect(await messageActionNames(ANSWER)).toEqual([
        "copy-message",
        "ow_ai_send_as_message",
        "ow_ai_log_note",
    ]);
});

test("an answer in a chat without record can only be copied", async () => {
    await startWithAnswer();
    expect(await messageActionNames(ANSWER)).toEqual(["copy-message"]);
});

test("the user's own messages can only be copied (no edit, delete, reply, reaction)", async () => {
    await startWithAnswer({ recordBound: true });
    // on the right of the chat
    await contains(`${USER_MESSAGE}.o_ow_ai_user_message`);
    expect(await messageActionNames(USER_MESSAGE)).toEqual(["copy-message"]);
});

test("'Log as note' opens the full composer on the chat's record with the answer", async () => {
    const { partnerId } = await startWithAnswer({ recordBound: true });
    const actions = [];
    patchWithCleanup(getService("action"), {
        doAction(action) {
            actions.push(action);
            expect.step("doAction");
        },
    });
    await hover(ANSWER);
    await click(`${ANSWER} [title='Expand']`);
    await click(".o-mail-Message-moreMenu .o-dropdown-item[name='ow_ai_log_note']");
    await expect.waitForSteps(["doAction"]);
    const [action] = actions;
    expect(action.res_model).toBe("mail.compose.message");
    expect(action.target).toBe("new");
    expect(action.context).toMatchObject({
        default_model: "res.partner",
        default_res_ids: [partnerId],
        default_subtype_xmlid: "mail.mt_note",
        default_composition_mode: "comment",
    });
    expect(String(action.context.default_body)).toInclude("Acme is in Utrecht.");
    await hover(ANSWER);
    await click(`${ANSWER} [title='Expand']`);
    await click(".o-mail-Message-moreMenu .o-dropdown-item[name='ow_ai_send_as_message']");
    await expect.waitForSteps(["doAction"]);
    expect(actions[1].context.default_subtype_xmlid).toBe("mail.mt_comment");
});

test("the chat window of an AI chat offers 'Delete Chat' but no invitation, members or call", async () => {
    await startWithAnswer();
    await contains(`${WINDOW}-header [title='Fold']`);
    await contains(`${WINDOW}-header [title*='Close Chat Window']`);
    await contains(`${WINDOW}-header [title='Start Call']`, { count: 0 });
    await click(`${WINDOW} [title='Open Actions Menu']`);
    await contains(".o-dropdown-item:text('Delete Chat')");
    await contains(".o-dropdown-item:text('Search Messages')");
    for (const name of ["Invite People", "Members", "Pinned Messages", "Leave Conversation"]) {
        await contains(`.o-dropdown-item:text('${name}')`, { count: 0 });
    }
});

test("'Delete Chat' asks for confirmation, then deletes the chat", async () => {
    const { channelId } = await startWithAnswer();
    onRpcBefore("/ow_ai/session/delete_chat", (params) => {
        expect.step(`delete_chat ${params.channel_id}`);
    });
    await click(`${WINDOW} [title='Open Actions Menu']`);
    await click(".o-dropdown-item:text('Delete Chat')");
    await contains(".modal-body:contains('This cannot be undone.')");
    await click(".modal-footer button:text('Delete')");
    await expect.waitForSteps([`delete_chat ${channelId}`]);
    await contains(WINDOW, { count: 0 });
});
