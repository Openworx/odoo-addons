import { fields } from "@mail/model/export";
import { DiscussApp } from "@mail/core/public_web/discuss_app_model";
import { DiscussAppCategory } from "@mail/discuss/core/public_web/discuss_app_category_model";
import { compareDatetime } from "@mail/utils/common/misc";
import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";

/**
 * The user's AI chats (`owAiChats`) and their "AI" category of the Discuss
 * sidebar, shown once there is one.
 *
 * @type {import("models").DiscussApp}
 */
const discussAppPatch = {
    setup(env) {
        super.setup(...arguments);
        this.owAiCategory = fields.One("DiscussAppCategory", {
            compute() {
                return {
                    extraClass: "o-mail-DiscussSidebarCategory-owAiChat",
                    hideWhenEmpty: true,
                    icon: "fa fa-magic",
                    id: "ow_ai.category_chats",
                    name: _t("OW AI"),
                    sequence: 35, // after "Direct messages" (30)
                };
            },
            eager: true,
        });
        this.owAiChats = fields.Many("Thread", { inverse: "appAsOwAiChats" });
    },
};

patch(DiscussApp.prototype, discussAppPatch);

/** @type {import("models").DiscussAppCategory} */
const discussAppCategoryPatch = {
    /** AI chats are sorted like direct chats: the latest activity first. */
    sortThreads(t1, t2) {
        if (this.eq(this.app?.owAiCategory)) {
            return compareDatetime(t2.lastInterestDt, t1.lastInterestDt) || t2.id - t1.id;
        }
        return super.sortThreads(t1, t2);
    },
};

patch(DiscussAppCategory.prototype, discussAppCategoryPatch);
