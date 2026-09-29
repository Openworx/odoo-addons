import { registry } from "@web/core/registry";

import { OW_AI_CLIENT_IDENTIFIER } from "@ow_ai/core/common/ow_ai_client_identifier";
import { runOwAiClientTool } from "@ow_ai/core/common/ow_ai_client_tool_registry";

export const OW_AI_CLIENT_TOOLS_NOTIFICATION = "ow_ai.session/client_tools";

/**
 * Runs the client tools the server sends on the `ow_ai.session/client_tools`
 * bus notification: `{ session_id, client_tools: [{name, params}], resume_token?,
 * blocking?, client_identifier? }`. A blocking request (`resume_token`,
 * `blocking` not `false`) resumes its session with the results; a
 * non-blocking one is fire-and-forget. Only the tab that sent the turn's
 * message acts (`client_identifier`; any tab when the server names none), and
 * only for sessions known to its store.
 */
export class OwAiClientTools {
    /**
     * @param {import("@web/env").OdooEnv} env
     * @param {import("services").ServiceFactories} services
     */
    constructor(env, services) {
        this.env = env;
        this.busService = services.bus_service;
        this.store = services["mail.store"];
    }

    setup() {
        this.busService.subscribe(OW_AI_CLIENT_TOOLS_NOTIFICATION, (payload) =>
            this.handleNotification(payload)
        );
    }

    /** @param {{session_id: number, client_tools: Object[], resume_token?: string, blocking?: boolean, client_identifier?: string|false}} payload */
    async handleNotification({
        session_id,
        client_tools = [],
        resume_token,
        blocking,
        client_identifier,
    }) {
        if (client_identifier && client_identifier !== OW_AI_CLIENT_IDENTIFIER) {
            return;
        }
        const session = this.store["ow.ai.session"].get(session_id);
        if (!session) {
            return;
        }
        if (blocking ?? Boolean(resume_token)) {
            await session.processPendingClientTool({
                tools: client_tools,
                resumeToken: resume_token,
            });
            return;
        }
        for (const tool of client_tools) {
            try {
                await runOwAiClientTool(this.env, tool);
            } catch (error) {
                console.warn(`AI client tool "${tool.name}" failed: ${error?.message || error}`);
            }
        }
    }
}

export const owAiClientToolsService = {
    dependencies: ["bus_service", "mail.store"],
    /**
     * @param {import("@web/env").OdooEnv} env
     * @param {import("services").ServiceFactories} services
     */
    start(env, services) {
        const clientTools = new OwAiClientTools(env, services);
        clientTools.setup();
        return clientTools;
    },
};

registry.category("services").add("ow_ai.client_tools", owAiClientToolsService);
