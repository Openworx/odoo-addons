import {
    click,
    contains,
    openDiscuss,
    patchUiSize,
    SIZES,
    start,
    startServer,
} from "@mail/../tests/mail_test_helpers";
import { describe, expect, test } from "@odoo/hoot";
import { Command, getService, onRpc, serverState } from "@web/../tests/web_test_helpers";

import {
    createChatMessage,
    createOwAiAgent,
    createOwAiChat,
    defineOwAiModels,
    getChannelThread,
    removeOwAiAccess,
} from "./ow_ai_test_helpers";

describe.current.tags("desktop");
defineOwAiModels();

const MESSAGING_MENU = ".o-mail-MessagingMenu";
/** The "AI" filter of the messaging menu's header (desktop). */
const AI_TAB = `${MESSAGING_MENU} .o-mail-MessagingMenu-headerFilter.o_ow_ai_messaging_menu_tab`;
const ITEM = `${MESSAGING_MENU} .o-mail-NotificationItem`;

/** A messaging menu item by its exact name (`:contains` ignores the case: "Unread chat"). */
function item(name, root = MESSAGING_MENU) {
    return `${root} .o-mail-NotificationItem:has(.o-mail-NotificationItem-name:text('${name}'))`;
}

/** A Discuss sidebar channel after the category header `category`, by its exact name. */
function sidebarChannel(category, name) {
    return `${category} ~ .o-mail-DiscussSidebarChannel-container:has(.o-mail-DiscussSidebarChannel-itemName:text('${name}'))`;
}

/**
 * Two AI chats of the current user ("Unread chat" has an unread answer), a
 * direct chat with Demo and a channel.
 */
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
    pyEnv["discuss.channel.member"].write([memberId], {
        new_message_separator: messageId + 1,
        seen_message_id: messageId,
    });
    const demoPartnerId = pyEnv["res.partner"].create({ name: "Demo" });
    pyEnv["res.users"].create({ partner_id: demoPartnerId });
    const chatId = pyEnv["discuss.channel"].create({
        channel_type: "chat",
        channel_member_ids: [
            Command.create({ partner_id: serverState.partnerId }),
            Command.create({ partner_id: demoPartnerId }),
        ],
    });
    const channelId = pyEnv["discuss.channel"].create({ name: "General" });
    return { unread, read, chatId, channelId };
}

test("AI chats are listed under the AI tab, with the unread ones counted", async () => {
    const pyEnv = await startServer();
    createChats(pyEnv);
    await start();
    await click(".o_menu_systray i[aria-label='Messages']");
    await contains(`${AI_TAB}:contains('OW AI')`);
    await contains(`${AI_TAB} .o_ow_ai_messaging_menu_tab_counter:text('1')`);
    // the other tabs keep their chats, without the AI chats
    await click(`${MESSAGING_MENU} .o-mail-MessagingMenu-headerFilter:text('Chats')`);
    await contains(`${MESSAGING_MENU} .o-mail-MessagingMenu-headerFilter.o-active:text('Chats')`);
    await contains(item("Demo"));
    await contains(item("Unread chat"), { count: 0 });
    await contains(item("Read chat"), { count: 0 });
    await click(`${MESSAGING_MENU} .o-mail-MessagingMenu-headerFilter:text('Channels')`);
    await contains(item("General"));
    await contains(item("Unread chat"), { count: 0 });
    // the AI tab lists the AI chats only
    await click(AI_TAB);
    await contains(`${AI_TAB}.o-active`);
    await contains(ITEM, { count: 2 });
    await contains(item("Unread chat"));
    await contains(item("Read chat"));
    // an AI chat opens in a chat window
    await click(item("Unread chat"));
    await contains(".o-mail-ChatWindow-header:contains('Unread chat')");
});

test("the AI tab's 'New chat' button launches a chat", async () => {
    const pyEnv = await startServer();
    createOwAiAgent(pyEnv, { name: "Ava" });
    onRpc("ow.ai.agent", "action_launch_chat", ({ kwargs }) => {
        expect.step(`action_launch_chat ${kwargs.interface_key}`);
    });
    await start();
    await click(".o_menu_systray i[aria-label='Messages']");
    await click(AI_TAB);
    await contains(`${MESSAGING_MENU} .o_ow_ai_messaging_menu_empty:contains('No AI chat yet')`);
    await contains(`${MESSAGING_MENU} :contains('No conversation yet')`, { count: 0 });
    // "New chat" replaces "New Message" in the header while the AI tab is active
    await contains(`${MESSAGING_MENU} button:text('New Message')`, { count: 0 });
    await contains(`${MESSAGING_MENU} .o-mail-MessagingMenu-header .o_ow_ai_new_chat`);
    await click(`${MESSAGING_MENU} .o_ow_ai_messaging_menu_empty button:text('New chat')`);
    await expect.waitForSteps(["action_launch_chat systray"]);
    await contains(".o-mail-ChatWindow-header:contains('Ava')");
});

test("the Discuss app lists the AI chats in their own 'AI' category", async () => {
    const pyEnv = await startServer();
    const { chatId, read, unread } = createChats(pyEnv);
    createOwAiAgent(pyEnv, { name: "Ava" });
    onRpc("ow.ai.agent", "action_launch_chat", () => expect.step("action_launch_chat"));
    await start();
    await openDiscuss();
    const CATEGORY = ".o-mail-DiscussSidebarCategory-owAiChat";
    await contains(`${CATEGORY}:contains('OW AI')`);
    // the "AI" category comes after "Direct messages" (sequence 35 > 30): the
    // AI chats are after its header, the direct chat before it
    for (const name of ["Unread chat", "Read chat"]) {
        await contains(sidebarChannel(CATEGORY, name));
    }
    await contains(sidebarChannel(".o-mail-DiscussSidebarCategory-chat", "Demo"));
    await contains(sidebarChannel(CATEGORY, "Demo"), { count: 0 });
    await contains(`.o-mail-DiscussSidebarCategory-chat ~ ${CATEGORY}`);
    const store = getService("mail.store");
    for (const { channelId } of [unread, read]) {
        const thread = getChannelThread(channelId);
        expect(thread.discussAppCategory.id).toBe("ow_ai.category_chats");
        expect(thread.in(store.discuss.owAiChats)).toBe(true);
    }
    const chat = getChannelThread(chatId);
    expect(chat.discussAppCategory.id).toBe("chats");
    expect(chat.in(store.discuss.owAiChats)).toBe(false);
    // the category's "New AI chat" action
    await click(`${CATEGORY} button[title='New AI chat']`);
    await expect.waitForSteps(["action_launch_chat"]);
});

test("mobile: the AI tab is one of the messaging menu's tabs", async () => {
    const pyEnv = await startServer();
    createChats(pyEnv);
    patchUiSize({ size: SIZES.SM });
    await start();
    await click(".o_menu_systray i[aria-label='Messages']");
    await contains(
        ".o-mail-MessagingMenu-tab[aria-label='OW AI'] .o-mail-MessagingMenu-tabCounter:text('1')"
    );
    await click(".o-mail-MessagingMenu-tab[aria-label='OW AI']");
    await contains(".o-mail-MessagingMenu-tab.active[aria-label='OW AI'] i.fa-magic");
    await contains(".o-mail-NotificationItem", { count: 2 });
    await contains(item("Unread chat", ".o-mail-MessagingMenu"));
    await contains(item("Read chat", ".o-mail-MessagingMenu"));
});

test("no AI tab without access to the assistant", async () => {
    const pyEnv = await startServer();
    removeOwAiAccess(pyEnv);
    await start();
    await click(".o_menu_systray i[aria-label='Messages']");
    await contains(`${MESSAGING_MENU} .o-mail-MessagingMenu-headerFilter:text('Chats')`);
    await contains(AI_TAB, { count: 0 });
});

test("mobile: no AI tab without access to the assistant", async () => {
    const pyEnv = await startServer();
    removeOwAiAccess(pyEnv);
    patchUiSize({ size: SIZES.SM });
    await start();
    await click(".o_menu_systray i[aria-label='Messages']");
    await contains(".o-mail-MessagingMenu-tab[aria-label='Chats']");
    await contains(".o-mail-MessagingMenu-tab[aria-label='OW AI']", { count: 0 });
});
