import {
    click,
    contains,
    setupChatHub,
    start,
    startServer,
} from "@mail/../tests/mail_test_helpers";
import { describe, expect, test } from "@odoo/hoot";
import { getService, serverState } from "@web/../tests/web_test_helpers";

import { createOwAiChat, defineOwAiModels } from "./ow_ai_test_helpers";

describe.current.tags("desktop");
defineOwAiModels();

test("messages of an AI chat are classified by author, type and subtype", async () => {
    const pyEnv = await startServer();
    const { agentPartnerId, channelId, sessionId } = createOwAiChat(pyEnv, { agentName: "Ava" });
    pyEnv["ow.ai.session"].write([sessionId], { show_agent_steps: true });
    const [noteSubtypeId] = pyEnv["mail.message.subtype"].search([
        ["subtype_xmlid", "=", "mail.mt_note"],
    ]);
    const [commentSubtypeId] = pyEnv["mail.message.subtype"].search([
        ["subtype_xmlid", "=", "mail.mt_comment"],
    ]);
    const post = (vals) =>
        pyEnv["mail.message"].create({ model: "discuss.channel", res_id: channelId, ...vals });
    const userMessageId = post({
        author_id: serverState.partnerId,
        body: "<p>Find Acme</p>",
        message_type: "comment",
        subtype_id: commentSubtypeId,
    });
    const stepId = post({
        author_id: agentPartnerId,
        body: '<div class="o_ow_ai_agent_step" data-id="7"><p>Looking it up</p></div>',
        message_type: "comment",
        subtype_id: noteSubtypeId,
    });
    const toolSummaryId = post({
        author_id: agentPartnerId,
        body: '<div class="o_ow_ai_tool_summary" data-id="call_1" data-oe-id="7"><i class="fa fa-search" aria-hidden="true"></i>Searching contacts</div>',
        message_type: "notification",
        subtype_id: noteSubtypeId,
    });
    const cardId = post({
        author_id: agentPartnerId,
        body: '<div class="o_ow_ai_input_card"><p>Create Acme?</p></div>',
        message_type: "comment",
        subtype_id: commentSubtypeId,
    });
    const noteId = post({
        author_id: agentPartnerId,
        body: '<div class="o_mail_notification" data-oe-type="ow_ai_note">Auto-approval enabled for this chat</div>',
        message_type: "notification",
        subtype_id: commentSubtypeId,
    });
    const answerId = post({
        author_id: agentPartnerId,
        body: "<p>Acme is in Utrecht.</p>",
        message_type: "comment",
        subtype_id: commentSubtypeId,
    });
    setupChatHub({ opened: [channelId] });
    await start();
    // user message, card, answer; the step and the tool summary are in the "Steps taken" block
    await contains(".o-mail-ChatWindow .o-mail-Message", { count: 3 });
    await contains(".o-mail-ChatWindow .o_ow_ai_steps .o_ow_ai_steps_count:text('2')");
    // folded: the turn is over
    await click(".o-mail-ChatWindow .o_ow_ai_steps_header");
    await contains(".o-mail-ChatWindow .o_ow_ai_steps_list:contains('Searching contacts')");
    // chat notes stay visible (mail's `notificationHidden` stays false)
    await contains(".o-mail-ChatWindow .o-mail-NotificationMessage", { count: 1 });
    await contains(".o-mail-NotificationMessage:contains('Auto-approval enabled for this chat')");
    const messages = getService("mail.store")["mail.message"];
    const flags = (id) => {
        const message = messages.get(id);
        return {
            agent: message.owAiAgentAuthored,
            step: message.owAiIsAgentStep,
            tool: message.owAiIsToolSummary,
            answer: message.owAiIsAnswer,
            card: message.owAiIsUserInputCard,
        };
    };
    const none = { agent: false, step: false, tool: false, answer: false, card: false };
    expect(flags(userMessageId)).toEqual(none);
    expect(flags(stepId)).toEqual({ ...none, agent: true, step: true });
    expect(flags(toolSummaryId)).toEqual({ ...none, agent: true, tool: true });
    expect(messages.get(toolSummaryId).owAiBody.toolSummary).toEqual({
        text: "Searching contacts",
        icon: "fa-search",
        callId: "call_1",
        eventId: 7,
    });
    expect(flags(cardId)).toEqual({ ...none, agent: true, card: true });
    expect(flags(noteId)).toEqual({ ...none, agent: true });
    expect(messages.get(noteId).notificationType).toBe("ow_ai_note");
    expect(flags(answerId)).toEqual({ ...none, agent: true, answer: true });
});
