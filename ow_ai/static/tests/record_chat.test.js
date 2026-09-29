import {
    click,
    contains,
    insertText,
    openFormView,
    openListView,
    start,
    startServer,
} from "@mail/../tests/mail_test_helpers";
import { describe, expect, test } from "@odoo/hoot";
import { onRpc, toggleActionMenu, toggleMenuItem } from "@web/../tests/web_test_helpers";

import { createOwAiAgent, defineOwAiModels, removeOwAiAccess } from "./ow_ai_test_helpers";

describe.current.tags("desktop");
defineOwAiModels();

const ASK_AI = ".o_menu_systray .o_ow_ai_systray_ask_ai";

function spyOnLaunch() {
    const calls = [];
    onRpc("ow.ai.agent", "action_launch_chat", ({ kwargs }) => {
        calls.push(kwargs);
        expect.step("action_launch_chat");
    });
    return calls;
}

test("'Ask AI' on a form launches a chat about its record", async () => {
    const pyEnv = await startServer();
    createOwAiAgent(pyEnv, { name: "Ava" });
    const partnerId = pyEnv["res.partner"].create({ name: "Acme" });
    const calls = spyOnLaunch();
    onRpc("res.partner", "web_save", () => expect.step("web_save"));
    await start();
    await openFormView("res.partner", partnerId);
    await click(ASK_AI);
    await expect.waitForSteps(["action_launch_chat"]);
    expect(calls[0]).toMatchObject({
        interface_key: "record_chat",
        res_model: "res.partner",
        res_id: partnerId,
        channel_title: "Acme",
    });
    await contains(".o-mail-ChatWindow-header:text('Acme')");
    const [session] = pyEnv["ow.ai.session"].search_read([]);
    expect(session.res_model).toBe("res.partner");
    expect(session.res_id).toBe(partnerId);
});

test("a dirty form is saved before its record chat is launched", async () => {
    const pyEnv = await startServer();
    createOwAiAgent(pyEnv);
    const partnerId = pyEnv["res.partner"].create({ name: "Acme" });
    const calls = spyOnLaunch();
    onRpc("res.partner", "web_save", () => expect.step("web_save"));
    await start();
    await openFormView("res.partner", partnerId);
    await insertText(".o_field_widget[name='name'] input", "Acme Corp", { replace: true });
    await click(ASK_AI);
    await expect.waitForSteps(["web_save", "action_launch_chat"]);
    expect(calls[0]).toMatchObject({ res_id: partnerId, channel_title: "Acme Corp" });
    expect(pyEnv["res.partner"].browse(partnerId)[0].name).toBe("Acme Corp");
});

test("a new record is created before its record chat is launched", async () => {
    const pyEnv = await startServer();
    createOwAiAgent(pyEnv);
    const calls = spyOnLaunch();
    onRpc("res.partner", "web_save", () => expect.step("web_save"));
    await start();
    await openFormView("res.partner");
    await insertText(".o_field_widget[name='name'] input", "Newco");
    await click(ASK_AI);
    await expect.waitForSteps(["web_save", "action_launch_chat"]);
    const [partnerId] = pyEnv["res.partner"].search([["name", "=", "Newco"]]);
    expect(calls[0]).toMatchObject({
        res_model: "res.partner",
        res_id: partnerId,
        channel_title: "Newco",
    });
});

test("'Ask AI' outside a form launches a plain chat", async () => {
    const pyEnv = await startServer();
    createOwAiAgent(pyEnv);
    pyEnv["res.partner"].create({ name: "Acme" });
    const calls = spyOnLaunch();
    await start();
    await openListView("res.partner", { arch: "<list><field name='name'/></list>" });
    await click(ASK_AI);
    await expect.waitForSteps(["action_launch_chat"]);
    expect(calls[0]).toMatchObject({ interface_key: "systray", res_model: null, res_id: null });
});

test("the form's action menu offers 'Ask AI about this record'", async () => {
    const pyEnv = await startServer();
    createOwAiAgent(pyEnv);
    const partnerId = pyEnv["res.partner"].create({ name: "Acme" });
    const calls = spyOnLaunch();
    await start();
    await openFormView("res.partner", partnerId);
    await toggleActionMenu();
    await contains(".o_menu_item:text('Ask AI about this record') .oi[data-icon='wand_stars']");
    await toggleMenuItem("Ask AI about this record");
    await expect.waitForSteps(["action_launch_chat"]);
    expect(calls[0]).toMatchObject({
        interface_key: "record_chat",
        res_model: "res.partner",
        res_id: partnerId,
    });
    await contains(".o-mail-ChatWindow-header:text('Acme')");
});

test("no 'Ask AI about this record' without access to the assistant", async () => {
    const pyEnv = await startServer();
    removeOwAiAccess(pyEnv);
    const partnerId = pyEnv["res.partner"].create({ name: "Acme" });
    await start();
    await openFormView("res.partner", partnerId);
    await toggleActionMenu();
    await contains(".o_menu_item:text('Duplicate')");
    await contains(".o_menu_item:text('Ask AI about this record')", { count: 0 });
});
