import { registry } from "@web/core/registry";

/**
 * Browser-side tools the assistant can invoke (`ow_ai.session/client_tools`).
 * An entry is `async (env, params) => result`: the result (a string or JSON
 * value) is sent back to the model for a blocking request; throwing reports a
 * `client_error`.
 */
export const owAiClientToolRegistry = registry.category("ow_ai.client_tools");

/**
 * @param {import("@web/env").OdooEnv} env
 * @param {{ name: string, params?: Object, args?: Object }} tool the server sends
 *  `params` (`args` is accepted as an alias)
 */
export async function runOwAiClientTool(env, { name, params, args }) {
    const tool = owAiClientToolRegistry.get(name, null);
    if (!tool) {
        throw new Error(`Unknown client tool: ${name}`);
    }
    return tool(env, params ?? args ?? {});
}

function getActionService(env) {
    const action = env.services.action;
    if (!action) {
        throw new Error("No view is open in this window.");
    }
    return action;
}

/**
 * Soft-refresh the current view (web's `soft_reload` client action). Only a
 * window action is reloaded: a client action (Discuss itself, for instance)
 * would be remounted for nothing.
 */
owAiClientToolRegistry.add("reload", async (env) => {
    const action = getActionService(env);
    // a promise: the action of a virtual controller (e.g. a breadcrumb) may still have to load
    const currentAction = await action.currentAction;
    if (currentAction?.type !== "ir.actions.act_window") {
        return "No view to reload.";
    }
    await action.doAction("soft_reload");
    return "The current view was reloaded.";
});

const XML_ID_REGEX = /^\w+\.\w+$/;

/**
 * Open an action given by its database id or XML id; a raw action dictionary
 * is refused (it would let the model build arbitrary actions).
 */
owAiClientToolRegistry.add("do_action", async (env, { action, context } = {}) => {
    const isId = Number.isInteger(action) && action > 0;
    const isXmlId = typeof action === "string" && XML_ID_REGEX.test(action);
    if (!isId && !isXmlId) {
        throw new Error("do_action only accepts an action id or XML id.");
    }
    const additionalContext =
        context && typeof context === "object" && !Array.isArray(context) ? context : {};
    await getActionService(env).doAction(action, { additionalContext });
    return "The action was opened.";
});
