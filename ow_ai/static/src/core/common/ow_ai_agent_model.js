import { fields, Record } from "@mail/model/export";

/** An `ow.ai.agent`, as published on its chat channel (`ow_ai_agent_id`). */
export class OwAiAgent extends Record {
    static _name = "ow.ai.agent";
    static id = "id";

    /** @type {number} */
    id;
    /** @type {string} */
    name;
    /** @type {string|false} */
    subtitle;
    partner_id = fields.One("res.partner");
}

OwAiAgent.register();
