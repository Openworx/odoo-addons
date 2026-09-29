import { mailDataHelpers } from "@mail/../tests/mock_server/mail_mock_server";

import { fields, makeKwArgs, models } from "@web/../tests/web_test_helpers";

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
    // `type: "generic"` is required on this Odoo 19: a Generic field left without an
    // explicit `type` has `field.type === undefined`, and `Model._read_format`'s
    // `isM2OField(field.type)` then reads `.type` off that `undefined`, crashing any
    // `read`/`search_read` that (like a bare `search_read([])`) falls back to all fields.
    loop_state = fields.Generic({ type: "generic", default: "ready" });
    resume_token = fields.Generic({ type: "generic", default: false });
    auto_confirm = fields.Boolean({ default: false });
    show_agent_steps = fields.Boolean({ default: false });
    web_search = fields.Boolean({ default: false });
    /** `userInputRequest` payload while waiting for the user, as the server builds it */
    user_input_request = fields.Generic({ type: "generic", default: false });
    /** `clientToolRequest` payload while waiting for the browser, as the server builds it */
    client_tool_request = fields.Generic({ type: "generic", default: false });
    tool_status = fields.Generic({ type: "generic", default: false });

    _store_session_fields() {
        const { Store } = mailDataHelpers;
        return [
            // the server's `Store.One('channel_id', [])`: the channel's id only
            Store.one("channel_id", makeKwArgs({ fields: ["id"] })),
            Store.one("agent_id", makeKwArgs({ fields: ["name", "partner_id"] })),
            "res_model",
            "res_id",
            Store.one("composer_id", makeKwArgs({ fields: ["interface_key"] })),
            Store.attr("config", (session) => ({
                auto_confirm: Boolean(session.auto_confirm),
                show_agent_steps: Boolean(session.show_agent_steps),
                web_search: Boolean(session.web_search),
            })),
            "loop_state",
            Store.attr(
                "resume_token",
                makeKwArgs({
                    predicate: (session) => INTERACTION_STATES.includes(session.loop_state),
                })
            ),
            Store.attr("userInputRequest", (session) => session.user_input_request || false),
            Store.attr("clientToolRequest", (session) => session.client_tool_request || false),
            Store.attr("toolStatus", (session) => session.tool_status || false),
        ];
    }
}
