import { mailModels } from "@mail/../tests/mail_test_helpers";
import { mailDataHelpers } from "@mail/../tests/mock_server/mail_mock_server";

import { fields, getKwArgs, makeKwArgs } from "@web/../tests/web_test_helpers";

const isOwAiAgentChannel = (channel) => channel.channel_type === "ow_ai_chat";
/** No session yet: `ow_ai_session_ids` is left out (never an empty list). */
const hasOwAiSession = (channel) =>
    isOwAiAgentChannel(channel) && channel.ow_ai_session_ids?.length > 0;

export class DiscussChannel extends mailModels.DiscussChannel {
    ow_ai_agent_id = fields.Many2one({ relation: "ow.ai.agent" });
    ow_ai_session_ids = fields.One2many({
        relation: "ow.ai.session",
        relation_field: "channel_id",
    });

    /**
     * The default channel payload (no `fields`) also holds the AI fields, like
     * the server's `_to_store_defaults` (models/discuss_channel.py).
     *
     * @override
     * @type {typeof mailModels.DiscussChannel["prototype"]["_to_store"]}
     */
    _to_store(store, fields) {
        const kwargs = getKwArgs(arguments, "store", "fields");
        super._to_store(...arguments);
        if (!kwargs.fields?.length) {
            kwargs.store._add_record_fields(this, this._store_ow_ai_fields());
        }
    }

    /** Mirrors `discuss.channel._store_ow_ai_fields` (models/discuss_channel.py). */
    _store_ow_ai_fields() {
        const { Store } = mailDataHelpers;
        return [
            Store.one(
                "ow_ai_agent_id",
                makeKwArgs({
                    fields: ["name", "subtitle", "partner_id"],
                    predicate: isOwAiAgentChannel,
                })
            ),
            Store.many(
                "ow_ai_session_ids",
                makeKwArgs({
                    fields: this.env["ow.ai.session"]._store_session_fields(),
                    predicate: hasOwAiSession,
                })
            ),
        ];
    }

    _types_allowing_seen_infos() {
        return [...super._types_allowing_seen_infos(), "ow_ai_chat"];
    }
}
