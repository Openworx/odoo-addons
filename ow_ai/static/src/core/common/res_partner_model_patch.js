import { ResPartner } from "@mail/core/common/res_partner_model";
import { fields } from "@mail/model/export";
import { patch } from "@web/core/utils/patch";

/** @type {import("models").ResPartner} */
const resPartnerPatch = {
    setup() {
        super.setup(...arguments);
        this.ow_ai_agent_ids = fields.Many("ow.ai.agent", { inverse: "partner_id" });
    },
    /** An agent's partner is a bot, like OdooBot (bot badge on its avatar, always online). */
    get isBot() {
        return super.isBot || this.ow_ai_agent_ids.length > 0;
    },
};

patch(ResPartner.prototype, resPartnerPatch);
