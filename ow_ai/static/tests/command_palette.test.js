import {
    click,
    contains,
    insertText,
    onRpcBefore,
    start,
    startServer,
} from "@mail/../tests/mail_test_helpers";
import { describe, expect, press, test } from "@odoo/hoot";
import { animationFrame } from "@odoo/hoot-mock";
import { onRpc, serverState } from "@web/../tests/web_test_helpers";

import { createOwAiAgent, defineOwAiModels, removeOwAiAccess } from "./ow_ai_test_helpers";

describe.current.tags("desktop");
defineOwAiModels();

const SEARCH = ".o_command_palette_search input";

async function openPalette() {
    await press(["control", "k"]);
    await animationFrame();
    await contains(SEARCH);
}

test("'Ask AI: <query>' launches a chat and asks the query", async () => {
    const pyEnv = await startServer();
    createOwAiAgent(pyEnv, { name: "Ava" });
    onRpc("ow.ai.agent", "action_launch_chat", ({ kwargs }) => {
        expect.step(`action_launch_chat ${kwargs.interface_key}`);
    });
    let advanceParams;
    onRpcBefore("/ow_ai/session/advance", (params) => {
        advanceParams = params;
        expect.step("advance");
    });
    await start();
    await openPalette();
    await insertText(SEARCH, "sales this month");
    await contains(".o_command_name:text('Ask AI: sales this month')");
    await click(".o_command:has(.o_command_name:text('Ask AI: sales this month'))");
    await expect.waitForSteps(["action_launch_chat systray", "advance"]);
    await contains(".o_command_palette", { count: 0 });
    await contains(".o-mail-ChatWindow-header:text('Ava')");
    await contains(".o-mail-ChatWindow .o-mail-Message-body:text('sales this month')");
    const [message] = pyEnv["mail.message"].search_read([
        ["model", "=", "discuss.channel"],
        ["author_id", "=", serverState.partnerId],
    ]);
    expect(advanceParams.message_id).toBe(message.id);
});

test("the palette has an 'Open AI chat' command", async () => {
    const pyEnv = await startServer();
    createOwAiAgent(pyEnv, { name: "Ava" });
    onRpc("ow.ai.agent", "action_launch_chat", () => expect.step("action_launch_chat"));
    await start();
    await openPalette();
    await insertText(SEARCH, "Open AI chat");
    await click(".o_command:has(.o_command_name:text('Open AI chat'))");
    await expect.waitForSteps(["action_launch_chat"]);
    await contains(".o-mail-ChatWindow-header:text('Ava')");
});

test("no AI commands without access to the assistant", async () => {
    const pyEnv = await startServer();
    removeOwAiAccess(pyEnv);
    await start();
    await openPalette();
    await insertText(SEARCH, "sales this month");
    await contains(".o_command_palette_listbox_empty");
    await contains(".o_command_name:text('Ask AI: sales this month')", { count: 0 });
    await insertText(SEARCH, "Open AI chat", { replace: true });
    await contains(".o_command_palette_listbox_empty");
    await contains(".o_command_name:text('Open AI chat')", { count: 0 });
});
