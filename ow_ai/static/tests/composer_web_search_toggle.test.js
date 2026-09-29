import {
    click,
    contains,
    onRpcBefore,
    setupChatHub,
    start,
    startServer,
} from "@mail/../tests/mail_test_helpers";
import { describe, expect, queryAll, test } from "@odoo/hoot";
import { getService } from "@web/../tests/web_test_helpers";

import { configureOwAiWebSearch, createOwAiChat, defineOwAiModels } from "./ow_ai_test_helpers";

describe.current.tags("desktop");
defineOwAiModels();

const WINDOW = ".o-mail-ChatWindow";
const MORE_ACTIONS = `${WINDOW} .o-mail-Composer button[title='More Actions']`;
const WEB_SEARCH = ".o-dropdown-item[name='ow_ai_web_search']";

test("the Web search switch of a chat, shown only when web search is configured", async () => {
    configureOwAiWebSearch();
    const pyEnv = await startServer();
    const { channelId, sessionId } = createOwAiChat(pyEnv);
    const configCalls = [];
    onRpcBefore("/ow_ai/session/config", (params) => {
        configCalls.push(params);
        expect.step("config");
    });
    setupChatHub({ opened: [channelId] });
    await start();
    await click(MORE_ACTIONS);
    await contains(`${WEB_SEARCH}:contains('Web search') i.fa-search`);
    const names = queryAll(".o-dropdown-item[name]").map((el) => el.getAttribute("name"));
    const index = names.indexOf("ow_ai_auto_approve");
    expect(names.slice(index, index + 3)).toEqual([
        "ow_ai_auto_approve",
        "ow_ai_web_search",
        "ow_ai_show_steps",
    ]);
    // a new chat starts with the switch off (`ow_ai.web_search_default` is off)
    await contains(`${WEB_SEARCH} input:not(:checked)`);
    await click(WEB_SEARCH);
    await expect.waitForSteps(["config"]);
    expect(configCalls[0]).toEqual({ channel_id: channelId, config: { web_search: true } });
    // the menu stays open and shows the new state
    await contains(`${WEB_SEARCH} input:checked`);
    const [session] = pyEnv["ow.ai.session"].browse(sessionId);
    expect(session.web_search).toBe(true);
    // without web search (no SearXNG URL) the switch is gone
    getService("mail.store").has_ow_ai_web_search = false;
    await click(MORE_ACTIONS);
    await contains(".o-dropdown-item[name='ow_ai_show_steps']", { count: 0 });
    await click(MORE_ACTIONS);
    await contains(".o-dropdown-item[name='ow_ai_show_steps']");
    await contains(WEB_SEARCH, { count: 0 });
});
