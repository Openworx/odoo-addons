import { ResPartner } from "@mail/core/common/res_partner_model";
import { fields } from "@mail/model/export";
import { patch } from "@web/core/utils/patch";

/**
 * Odoo 19 tells a bot by its IM status (OdooBot's is `bot`, set by
 * `res.partner._compute_im_status`): a bot badge on its avatar
 * (`mail.ImStatus`), and online in member lists (`store.onlineMemberStatuses`).
 */
const BOT_IM_STATUS = "bot";

/** @type {import("models").ResPartner} */
const resPartnerPatch = {
    setup() {
        super.setup(...arguments);
        this.ow_ai_agent_ids = fields.Many("ow.ai.agent", { inverse: "partner_id" });
        /**
         * An agent's partner is a bot, like OdooBot. The server gives it the
         * status of a partner without user (`im_partner`): whenever it is an
         * agent's partner without the bot status (agent linked, status
         * updated by the server or the bus), it gets it back.
         */
        this.owAiNeedsBotStatus = fields.Attr(false, {
            compute() {
                return this.ow_ai_agent_ids.length > 0 && this.im_status !== BOT_IM_STATUS;
            },
            onUpdate() {
                if (this.owAiNeedsBotStatus) {
                    this.im_status = BOT_IM_STATUS;
                }
            },
            eager: true,
        });
    },
};

patch(ResPartner.prototype, resPartnerPatch);
