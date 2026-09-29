import { registry } from "@web/core/registry";

/**
 * Client action `ow_ai.open_chat` (e.g. `ow.ai.agent.action_test_chat`):
 * `params` is an `ow.ai.agent.action_launch_chat` payload; opens that chat
 * and leaves the current view as it is.
 */
async function openChatAction(env, action) {
    await env.services["ow_ai.chat_launcher"].openLaunchedChat(action.params ?? {});
}

registry.category("actions").add("ow_ai.open_chat", openChatAction);
