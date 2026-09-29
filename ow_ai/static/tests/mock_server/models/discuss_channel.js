import { mailModels } from "@mail/../tests/mail_test_helpers";

import { fields } from "@web/../tests/web_test_helpers";

const isOwAiAgentChannel = (channel) => channel.channel_type === "ow_ai_chat";

/** Mirrors `discuss.channel._store_ow_ai_fields` (models/discuss_channel.py). */
export class DiscussChannel extends mailModels.DiscussChannel {
    ow_ai_agent_id = fields.Many2one({ relation: "ow.ai.agent" });
    ow_ai_session_ids = fields.One2many({
        relation: "ow.ai.session",
        relation_field: "channel_id",
    });

    _store_channel_fields(res) {
        super._store_channel_fields(res);
        res.one("ow_ai_agent_id", ["name", "subtitle", "partner_id"], {
            predicate: isOwAiAgentChannel,
            sudo: true,
        });
        res.many("ow_ai_session_ids", "_store_session_fields", {
            predicate: isOwAiAgentChannel,
            sudo: true,
        });
    }

    _types_allowing_seen_infos() {
        return [...super._types_allowing_seen_infos(), "ow_ai_chat"];
    }
}
