import { onRpcBefore, start, startServer } from "@mail/../tests/mail_test_helpers";
import { Store } from "@mail/core/common/store_service";
import { advanceTime, expect, test, waitUntil } from "@odoo/hoot";
import { markup } from "@odoo/owl";
import { Command, getService, serverState } from "@web/../tests/web_test_helpers";

import { createOwAiChat, defineOwAiModels, updateOwAiSession } from "./ow_ai_test_helpers";

const Markup = markup().constructor;

defineOwAiModels();

/** Load `channelId` in the store the way the web client does (`/mail/store` fetch). */
async function fetchChannel(channelId) {
    const store = getService("mail.store");
    const channel = await store["discuss.channel"].getOrFetch(channelId);
    return { channel, store };
}

function getAgentMember(channel) {
    return channel.channel_member_ids.find((member) => member.isOwAiAgent);
}

test("an AI chat channel carries its agent and its session", async () => {
    const pyEnv = await startServer();
    const { agentPartnerId, channelId, sessionId } = createOwAiChat(pyEnv, {
        agentName: "Ava",
    });
    const composerId = pyEnv["ow.ai.composer"].create({ interface_key: "systray" });
    pyEnv["ow.ai.session"].write([sessionId], { composer_id: composerId });
    const otherPartnerId = pyEnv["res.partner"].create({ name: "Demo" });
    const chatId = pyEnv["discuss.channel"].create({
        channel_type: "chat",
        channel_member_ids: [
            Command.create({ partner_id: serverState.partnerId }),
            Command.create({ partner_id: otherPartnerId }),
        ],
    });
    await start();
    const { channel, store } = await fetchChannel(channelId);
    expect(channel.isOwAiChat).toBe(true);
    expect(channel.isChatChannel).toBe(true);
    expect(channel.displayName).toBe("Ava");
    expect(channel.ow_ai_agent_id.name).toBe("Ava");
    expect(channel.ow_ai_agent_id.subtitle).toBe("Your assistant");
    expect(channel.ow_ai_agent_id.partner_id.id).toBe(agentPartnerId);
    const session = store["ow.ai.session"].get(sessionId);
    expect(channel.owAiSession.eq(session)).toBe(true);
    expect(session.channel_id.eq(channel)).toBe(true);
    expect(session.agent_id.eq(channel.ow_ai_agent_id)).toBe(true);
    expect(session.composer_id.interface_key).toBe("systray");
    expect(session.config).toEqual({ auto_confirm: false, show_agent_steps: false });
    expect(session.loop_state).toBe("ready");
    expect(session.isGenerating).toBe(false);
    expect(session.isWaitingForUser).toBe(false);
    expect(channel.isAiGenerating).toBe(false);
    expect(channel.isAiWaitingForUser).toBe(false);
    expect(getAgentMember(channel).partner_id.id).toBe(agentPartnerId);
    expect(store["res.partner"].get(agentPartnerId).isBot).toBe(true);
    expect(store["res.partner"].get(serverState.partnerId).isBot).toBe(false);
    const { channel: chat } = await fetchChannel(chatId);
    expect(chat.isOwAiChat).toBe(false);
    expect(chat.owAiSession).toBe(undefined);
    expect(chat.isAiGenerating).toBe(false);
    expect(chat.channel_member_ids.some((member) => member.isOwAiAgent)).toBe(false);
});

test("the agent member types while the session generates, without typing timeout", async () => {
    const pyEnv = await startServer();
    const { channelId, sessionId } = createOwAiChat(pyEnv);
    await start();
    const { channel } = await fetchChannel(channelId);
    const agentMember = getAgentMember(channel);
    expect(agentMember.isTyping).toBe(false);
    updateOwAiSession(pyEnv, sessionId, { loop_state: "waiting_model" });
    await waitUntil(() => channel.isAiGenerating);
    expect(channel.owAiSession.isGenerating).toBe(true);
    expect(agentMember.isTyping).toBe(true);
    expect(channel.otherTypingMembers.map((member) => member.id)).toEqual([agentMember.id]);
    await advanceTime(Store.OTHER_LONG_TYPING + 1000);
    expect(agentMember.isTyping).toBe(true);
    updateOwAiSession(pyEnv, sessionId, { loop_state: "waiting_client_result" });
    await advanceTime(Store.OTHER_LONG_TYPING + 1000);
    expect(channel.isAiGenerating).toBe(true);
    expect(agentMember.isTyping).toBe(true);
    updateOwAiSession(pyEnv, sessionId, { loop_state: "ready" });
    await waitUntil(() => !channel.isAiGenerating);
    expect(agentMember.isTyping).toBe(false);
    expect(channel.otherTypingMembers.length).toBe(0);
});

test("a session with a user input request waits for the user", async () => {
    const pyEnv = await startServer();
    const { channelId, sessionId } = createOwAiChat(pyEnv);
    await start();
    const { channel, store } = await fetchChannel(channelId);
    updateOwAiSession(pyEnv, sessionId, {
        loop_state: "waiting_confirmation",
        resume_token: "token-1",
        user_input_request: {
            type: "confirmation",
            body: ["markup", "<p>Create <b>Acme</b>?</p>"],
            choices: ["confirm_once", "auto_confirm", "decline"],
            labels: { confirm_once: "Yes", auto_confirm: "Always", decline: "No" },
            multiSelect: false,
            allowFreeText: false,
            resumeToken: "token-1",
        },
    });
    await waitUntil(() => channel.isAiWaitingForUser);
    const session = channel.owAiSession;
    expect(session.isWaitingForUser).toBe(true);
    expect(session.isGenerating).toBe(false);
    expect(channel.isAiGenerating).toBe(false);
    expect(session.resume_token).toBe("token-1");
    expect(session.pendingResumeToken).toBe("token-1");
    const request = session.userInputRequest;
    expect(request.id).toBe(sessionId);
    expect(request.session.eq(session)).toBe(true);
    expect(request.type).toBe("confirmation");
    expect(request.body).toBeInstanceOf(Markup);
    expect(request.body.toString()).toBe("<p>Create <b>Acme</b>?</p>");
    expect(request.choices).toEqual(["confirm_once", "auto_confirm", "decline"]);
    expect(request.labels.confirm_once).toBe("Yes");
    expect(request.multiSelect).toBe(false);
    expect(request.allowFreeText).toBe(false);
    expect(request.resumeToken).toBe("token-1");
    // leaving the interaction drops the request and its (now stale) token
    updateOwAiSession(pyEnv, sessionId, {
        loop_state: "ready",
        resume_token: false,
        user_input_request: false,
    });
    await waitUntil(() => !channel.isAiWaitingForUser);
    expect(session.userInputRequest).toBe(undefined);
    expect(session.pendingResumeToken).toBe(undefined);
    expect(store["ow.ai.user.input.request"].records.size).toBe(0);
});

test("deleting an AI chat deletes it on the server and in the store", async () => {
    const pyEnv = await startServer();
    const { channelId, sessionId } = createOwAiChat(pyEnv);
    onRpcBefore("/ow_ai/session/delete_chat", (params) =>
        expect.step(`delete_chat ${JSON.stringify(params)}`)
    );
    await start();
    const { channel, store } = await fetchChannel(channelId);
    await channel.deleteOwAiChat();
    expect.verifySteps([`delete_chat {"channel_id":${channelId}}`]);
    expect(channel.exists()).toBe(false);
    expect(pyEnv["discuss.channel"].search([["id", "=", channelId]])).toEqual([]);
    await waitUntil(() => !store["discuss.channel"].get(channelId));
    expect(store["ow.ai.session"].get(sessionId)?.channel_id).toBe(undefined);
});
