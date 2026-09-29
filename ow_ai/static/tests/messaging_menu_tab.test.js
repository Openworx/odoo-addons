import {
    click,
    contains,
    openDiscuss,
    openMessagingMenu,
    start,
    startServer,
} from "@mail/../tests/mail_test_helpers";
import { describe, expect, test } from "@odoo/hoot";
import { onRpc, serverState } from "@web/../tests/web_test_helpers";

import {
    createChatMessage,
    createOwAiAgent,
    createOwAiChat,
    defineOwAiModels,
    removeOwAiAccess,
} from "./ow_ai_test_helpers";

describe.current.tags("desktop");
defineOwAiModels();

const AI_TAB = ".o-mail-MessagingMenuInDropdown .o-mail-MessagingMenu-tab[data-id='ow-ai-chat']";

/** Two AI chats of the current user: "Unread chat" has an unread answer. */
function createChats(pyEnv) {
    const unread = createOwAiChat(pyEnv, { agentName: "Ava" });
    pyEnv["discuss.channel"].write([unread.channelId], { name: "Unread chat" });
    createChatMessage(pyEnv, unread.channelId, {
        author_id: unread.agentPartnerId,
        body: "<p>Here are the sales.</p>",
    });
    const read = createOwAiChat(pyEnv, { agentName: "Max" });
    pyEnv["discuss.channel"].write([read.channelId], { name: "Read chat" });
    const messageId = createChatMessage(pyEnv, read.channelId, {
        author_id: serverState.partnerId,
        body: "<p>Thanks</p>",
    });
    const [memberId] = pyEnv["discuss.channel.member"].search([
        ["channel_id", "=", read.channelId],
        ["partner_id", "=", serverState.partnerId],
    ]);
    pyEnv["discuss.channel.member"].write([memberId], { new_message_separator: messageId + 1 });
    return { unread, read };
}

test("AI chats are listed under the AI tab, with the unread ones counted", async () => {
    const pyEnv = await startServer();
    createChats(pyEnv);
    await start();
    await openMessagingMenu();
    await contains(`${AI_TAB}[aria-label='OW AI'] .oi[data-icon='wand_stars']`);
    await contains(`${AI_TAB} .o-mail-MessagingMenu-tabCounter:text('1')`);
    // not in the Chats tab
    await click(".o-mail-MessagingMenuInDropdown .o-mail-MessagingMenu-tab[data-id='chat']");
    await contains(
        ".o-mail-MessagingMenuInDropdown .o-mail-MessagingMenu-tab.active[data-id='chat']"
    );
    await contains(".o-mail-NotificationItem:text('Unread chat')", { count: 0 });
    await click(AI_TAB);
    await contains(".o-mail-NotificationItem", { count: 2 });
    await contains(".o-mail-NotificationItem:has(:text('Unread chat'))");
    await contains(".o-mail-NotificationItem:has(:text('Read chat'))");
    await click(".o-mail-MessagingMenu-filter:text('Unread')");
    await contains(".o-mail-NotificationItem", { count: 1 });
    await contains(".o-mail-NotificationItem:has(:text('Unread chat'))");
    // an AI chat opens in a chat window
    await click(".o-mail-NotificationItem:has(:text('Unread chat'))");
    await contains(".o-mail-ChatWindow-header:text('Unread chat')");
});

test("the AI tab's 'New chat' button launches a chat", async () => {
    const pyEnv = await startServer();
    createOwAiAgent(pyEnv, { name: "Ava" });
    onRpc("ow.ai.agent", "action_launch_chat", ({ kwargs }) => {
        expect.step(`action_launch_chat ${kwargs.interface_key}`);
    });
    await start();
    await openMessagingMenu("discuss.tab_ow-ai-chat");
    await contains(".o-mail-MessagingMenuEmpty :text('No AI chat yet')");
    await click(".o-mail-MessagingMenu button:has(.oi[data-icon='add']):text('New chat')");
    await expect.waitForSteps(["action_launch_chat systray"]);
    await contains(".o-mail-ChatWindow-header:text('Ava')");
});

test("the Discuss app has the AI tab too", async () => {
    const pyEnv = await startServer();
    createChats(pyEnv);
    await start();
    await openDiscuss("discuss.tab_ow-ai-chat");
    await contains(".o-mail-Discuss .o-mail-MessagingMenu-tab.active[data-id='ow-ai-chat']");
    await contains(".o-mail-Discuss .o-mail-NotificationItem", { count: 2 });
});

test("no AI tab without access to the assistant", async () => {
    const pyEnv = await startServer();
    removeOwAiAccess(pyEnv);
    await start();
    await openMessagingMenu();
    await contains(".o-mail-MessagingMenuInDropdown .o-mail-MessagingMenu-tab[data-id='chat']");
    await contains(AI_TAB, { count: 0 });
});
