import { fields, models } from "@web/../tests/web_test_helpers";

const INTERACTION_STATES = ["waiting_confirmation", "waiting_answer", "waiting_client_result"];

/**
 * Mock of `ow.ai.session`. `_store_session_fields` mirrors the Python
 * `ow.ai.session._store_session_fields` payload (models/ow_ai_session_post.py);
 * the request payloads are stored as plain JSON on the mock record.
 */
export class OwAiSession extends models.ServerModel {
    _name = "ow.ai.session";

    agent_id = fields.Many2one({ relation: "ow.ai.agent" });
    composer_id = fields.Many2one({ relation: "ow.ai.composer" });
    channel_id = fields.Many2one({ relation: "discuss.channel" });
    res_model = fields.Char();
    res_id = fields.Integer();
    loop_state = fields.Generic({ default: "ready" });
    resume_token = fields.Generic({ default: false });
    auto_confirm = fields.Boolean({ default: false });
    show_agent_steps = fields.Boolean({ default: false });
    /** `userInputRequest` payload while waiting for the user, as the server builds it */
    user_input_request = fields.Generic({ default: false });
    /** `clientToolRequest` payload while waiting for the browser, as the server builds it */
    client_tool_request = fields.Generic({ default: false });
    tool_status = fields.Generic({ default: false });

    _store_session_fields(res) {
        res.one("channel_id", []);
        res.one("agent_id", ["name", "partner_id"]);
        res.attr("res_model");
        res.attr("res_id");
        res.one("composer_id", ["interface_key"]);
        res.attr("config", (session) => ({
            auto_confirm: Boolean(session.auto_confirm),
            show_agent_steps: Boolean(session.show_agent_steps),
        }));
        res.attr("loop_state");
        res.attr("resume_token", undefined, {
            predicate: (session) => INTERACTION_STATES.includes(session.loop_state),
        });
        res.attr("userInputRequest", (session) => session.user_input_request || false);
        res.attr("clientToolRequest", (session) => session.client_tool_request || false);
        res.attr("toolStatus", (session) => session.tool_status || false);
    }
}
