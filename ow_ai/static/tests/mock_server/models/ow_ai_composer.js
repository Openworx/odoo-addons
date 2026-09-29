import { fields, models } from "@web/../tests/web_test_helpers";

/**
 * Mock of `ow.ai.composer`: the client only reads `interface_key`; the
 * prompt buttons (`prompt_button_ids`) are stored as `[{name, prompt}]`.
 */
export class OwAiComposer extends models.ServerModel {
    _name = "ow.ai.composer";

    interface_key = fields.Char();
    agent_id = fields.Many2one({ relation: "ow.ai.agent" });
    // `type: "generic"`: see the comment on `ow_ai_session.js`'s Generic fields.
    prompt_buttons = fields.Generic({ type: "generic", default: [] });
}
