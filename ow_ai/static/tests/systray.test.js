import {
    click,
    contains,
    setupChatHub,
    start,
    startServer,
} from "@mail/../tests/mail_test_helpers";
import { animationFrame, describe, expect, test, waitUntil } from "@odoo/hoot";
import { getService, makeKwArgs, onRpc } from "@web/../tests/web_test_helpers";

import {
    createOwAiAgent,
    createOwAiChat,
    defineOwAiModels,
    getChannelThread,
    removeOwAiAccess,
} from "./ow_ai_test_helpers";

describe.current.tags("desktop");
defineOwAiModels();

const ASK_AI = ".o_menu_systray .o_ow_ai_systray_ask_ai";

/** Record the `action_launch_chat` calls (their kwargs) as `action_launch_chat` steps. */
function spyOnLaunch() {
    const calls = [];
    onRpc("ow.ai.agent", "action_launch_chat", ({ args, kwargs }) => {
        calls.push({ args, kwargs });
        expect.step("action_launch_chat");
    });
    return calls;
}

test("'Ask AI' launches a chat and opens its chat window", async () => {
    const pyEnv = await startServer();
    createOwAiAgent(pyEnv, {
        name: "Ava",
        promptButtons: [{ name: "What can you do?", prompt: "What can you help me with?" }],
    });
    const calls = spyOnLaunch();
    await start();
    await contains(`${ASK_AI}[title='Ask AI']`);
    await click(ASK_AI);
    await expect.waitForSteps(["action_launch_chat"]);
    expect(calls[0].args[0]).toEqual([]);
    expect(calls[0].kwargs).toMatchObject({
        interface_key: "systray",
        res_model: null,
        res_id: null,
        channel_title: null,
    });
    await contains(".o-mail-ChatWindow-header:text('Ava')");
    // the composer's prompt buttons, attached by the launcher (never published)
    await contains(".o-mail-ChatWindow .o_ow_ai_prompt_button:text('What can you do?')");
    const [channel] = pyEnv["discuss.channel"].search_read([["channel_type", "=", "ow_ai_chat"]]);
    const session = getChannelThread(channel.id).owAiSession;
    expect(session.promptButtons).toEqual([
        { name: "What can you do?", prompt: "What can you help me with?" },
    ]);
});

test("'Ask AI' reopens an empty chat instead of creating another one", async () => {
    const pyEnv = await startServer();
    createOwAiAgent(pyEnv, { name: "Ava" });
    spyOnLaunch();
    await start();
    await click(ASK_AI);
    await expect.waitForSteps(["action_launch_chat"]);
    await contains(".o-mail-ChatWindow-header:text('Ava')");
    const [channelId] = pyEnv["discuss.channel"].search([["channel_type", "=", "ow_ai_chat"]]);
    const channel = getChannelThread(channelId);
    await waitUntil(() => channel.isLoaded);
    await click(".o-mail-ChatWindow-header [title*='Close Chat Window']");
    await contains(".o-mail-ChatWindow", { count: 0 });
    await click(ASK_AI);
    await contains(".o-mail-ChatWindow-header:text('Ava')");
    expect.verifySteps([]);
    expect(pyEnv["discuss.channel"].search([["channel_type", "=", "ow_ai_chat"]])).toEqual([
        channelId,
    ]);
});

test("a failed launch shows a notification", async () => {
    const pyEnv = await startServer();
    createOwAiAgent(pyEnv);
    onRpc("ow.ai.agent", "action_launch_chat", () => {
        throw new Error("The assistant is not configured.");
    });
    await start();
    await click(ASK_AI);
    await contains(".o_notification:has(.bg-danger):text('The assistant is not configured.')");
    await contains(".o-mail-ChatWindow", { count: 0 });
});

test("the button is absent without access to the assistant", async () => {
    const pyEnv = await startServer();
    removeOwAiAccess(pyEnv);
    onRpc("res.users", "has_group", ({ args }) => {
        if (args[1] === "ow_ai.group_ai_user") {
            expect.step("has_group ow_ai.group_ai_user");
        }
    });
    await start();
    await contains(".o_menu_systray i[aria-label='Messages']");
    // the button decides once the group check answered
    await expect.waitForSteps(["has_group ow_ai.group_ai_user"]);
    await animationFrame();
    await contains(ASK_AI, { count: 0 });
});

test("'Ask AI' does not reuse the empty test chat of another agent", async () => {
    const pyEnv = await startServer();
    createOwAiAgent(pyEnv, { name: "Ava" }); // the agent of the systray composer
    const maxPartnerId = pyEnv["res.partner"].create({ name: "Max" });
    const maxId = pyEnv["ow.ai.agent"].create({ name: "Max", partner_id: maxPartnerId });
    // `ow.ai.agent.action_test_chat` on Max: the systray composer, Max's session
    const params = pyEnv["ow.ai.agent"].action_launch_chat(
        [maxId],
        makeKwArgs({ interface_key: "systray" })
    );
    const calls = spyOnLaunch();
    await start();
    await getService("action").doAction({
        type: "ir.actions.client",
        tag: "ow_ai.open_chat",
        params,
    });
    await contains(".o-mail-ChatWindow-header:text('Max')");
    const testChat = getChannelThread(params.channel_id);
    await waitUntil(() => testChat.isLoaded && testChat.isEmpty);
    await click(ASK_AI);
    await expect.waitForSteps(["action_launch_chat"]);
    expect(calls[0].kwargs).toMatchObject({ interface_key: "systray" });
    await contains(".o-mail-ChatWindow-header:text('Ava')");
    const [newChatId] = pyEnv["discuss.channel"].search([
        ["channel_type", "=", "ow_ai_chat"],
        ["id", "!=", params.channel_id],
    ]);
    expect(getChannelThread(newChatId).owAiSession.agent_id.name).toBe("Ava");
});

test("'Ask AI' does not reuse an empty chat before a launch in this tab", async () => {
    const pyEnv = await startServer();
    createOwAiAgent(pyEnv, { name: "Ava" });
    // an empty chat without composer (e.g. from another interface), loaded in its chat window
    const other = createOwAiChat(pyEnv, { agentName: "Ava" });
    setupChatHub({ opened: [other.channelId] });
    spyOnLaunch();
    await start();
    // the chat hub loads the chat window's channel after `start()`
    await waitUntil(() => getChannelThread(other.channelId)?.isLoaded);
    await click(ASK_AI);
    await expect.waitForSteps(["action_launch_chat"]);
    expect(pyEnv["discuss.channel"].search([["channel_type", "=", "ow_ai_chat"]]).length).toBe(2);
});

test("the 'ow_ai.open_chat' client action opens the launched chat", async () => {
    const pyEnv = await startServer();
    createOwAiAgent(pyEnv, {
        name: "Ava",
        promptButtons: [{ name: "Hello", prompt: "Say hello" }],
    });
    // like `ow.ai.agent.action_test_chat`
    const params = pyEnv["ow.ai.agent"].action_launch_chat(
        [],
        makeKwArgs({ interface_key: "systray" })
    );
    await start();
    await getService("action").doAction({
        type: "ir.actions.client",
        tag: "ow_ai.open_chat",
        params,
    });
    await contains(".o-mail-ChatWindow-header:text('Ava')");
    await contains(".o-mail-ChatWindow .o_ow_ai_prompt_button:text('Hello')");
});
