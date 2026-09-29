import {
    click,
    contains,
    onRpcBefore,
    openDiscuss,
    setupChatHub,
    start,
    startServer,
} from "@mail/../tests/mail_test_helpers";
import { describe, expect, test } from "@odoo/hoot";
import { getService, serverState } from "@web/../tests/web_test_helpers";

import { createChatMessage, createOwAiChat, defineOwAiModels } from "./ow_ai_test_helpers";

describe.current.tags("desktop");
defineOwAiModels();

const WINDOW = ".o-mail-ChatWindow";

test("an empty AI chat introduces the agent and offers the launcher's prompt buttons", async () => {
    const pyEnv = await startServer();
    const { channelId, sessionId } = createOwAiChat(pyEnv, {
        agentName: "Ava",
        subtitle: "Ask me about your data",
    });
    onRpcBefore("/ow_ai/session/advance", () => expect.step("advance"));
    await start();
    // as the launcher does with the `action_launch_chat` payload
    const store = getService("mail.store");
    const channel = await store["discuss.channel"].getOrFetch(channelId);
    store["ow.ai.session"].get(sessionId).promptButtons = [
        { name: "Top customers", prompt: "Who are my top 5 customers?" },
    ];
    channel.openChatWindow();
    await contains(`${WINDOW} .o_ow_ai_chat_intro_name:text('Ava')`);
    await contains(`${WINDOW} .o_ow_ai_chat_intro_subtitle:text('Ask me about your data')`);
    await click(`${WINDOW} .o_ow_ai_prompt_button:text('Top customers')`);
    await expect.waitForSteps(["advance"]);
    await contains(`${WINDOW} .o-mail-Message:has(:text('Who are my top 5 customers?'))`);
    // not empty anymore: no prompt buttons
    await contains(`${WINDOW} .o_ow_ai_prompt_button`, { count: 0 });
    await contains(`${WINDOW} .o_ow_ai_chat_intro_name:text('Ava')`);
});

test("the user's messages are on the right in the Discuss app too", async () => {
    const pyEnv = await startServer();
    const { agentPartnerId, channelId } = createOwAiChat(pyEnv);
    createChatMessage(pyEnv, channelId, { author_id: serverState.partnerId, body: "Hello" });
    createChatMessage(pyEnv, channelId, { author_id: agentPartnerId, body: "Hi there" });
    await start();
    await openDiscuss(channelId);
    await contains(".o-mail-Message.o_ow_ai_user_message:has(:text('Hello'))");
    await contains(".o-mail-Message.o_ow_ai_agent_answer:has(:text('Hi there'))");
    await contains(".o-mail-Message.o_ow_ai_user_message:has(:text('Hi there'))", { count: 0 });
});

test("odd code classes and languages are left plain", async () => {
    const pyEnv = await startServer();
    const { agentPartnerId, channelId } = createOwAiChat(pyEnv);
    createChatMessage(pyEnv, channelId, { author_id: serverState.partnerId, body: "Code?" });
    // raw classes kept from the model's HTML: not a `language-*` class, or an
    // inherited object member as language
    createChatMessage(pyEnv, channelId, {
        author_id: agentPartnerId,
        body: [
            `<pre><code class="x-language-python">one</code></pre>`,
            `<pre><code class="language-constructor">two</code></pre>`,
            `<pre><code class="language-__proto__">three</code></pre>`,
            `<pre><code class="language-toString">four</code></pre>`,
            `<pre><code class="language-">five</code></pre>`,
            `<pre><code class="language-sql">SELECT 1</code></pre>`,
        ].join(""),
    });
    setupChatHub({ opened: [channelId] });
    await start();
    await contains(`${WINDOW} .o_ow_ai_agent_answer pre:not([data-embedded])`, { count: 5 });
    await contains(`${WINDOW} .o_ow_ai_agent_answer pre:not([data-embedded]):text('three')`);
    await contains(
        `${WINDOW} .o_ow_ai_agent_answer pre[data-embedded][data-language-id='sql']:text('SELECT 1')`
    );
});

test("code blocks of an answer are handed to the syntax highlighting", async () => {
    const pyEnv = await startServer();
    const { agentPartnerId, channelId } = createOwAiChat(pyEnv);
    createChatMessage(pyEnv, channelId, { author_id: serverState.partnerId, body: "Code?" });
    createChatMessage(pyEnv, channelId, {
        author_id: agentPartnerId,
        body: `<p>Here:</p><pre class="o_ow_ai_pre"><code class="python language-python">print("hi")</code></pre><pre class="o_ow_ai_pre"><code class="language-brainfuck">+++</code></pre><pre class="o_ow_ai_pre"><code>plain</code></pre>`,
    });
    setupChatHub({ opened: [channelId] });
    await start();
    await contains(
        `${WINDOW} .o_ow_ai_agent_answer pre[data-embedded='readonlySyntaxHighlighting'][data-language-id='python']`
    );
    // highlighted by Prism (loaded from `html_editor.assets_prism`)
    await contains(
        `${WINDOW} .o_ow_ai_agent_answer pre[data-embedded-mounted='1'] .token.string:text('"hi"')`
    );
    // unknown or missing languages stay plain
    await contains(`${WINDOW} .o_ow_ai_agent_answer pre[data-embedded]`, { count: 1 });
    await contains(`${WINDOW} .o_ow_ai_agent_answer pre:not([data-embedded]):text('+++')`);
});
