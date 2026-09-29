import { Thread } from "@mail/core/common/thread_model";
import { fields } from "@mail/model/export";
import { patch } from "@web/core/utils/patch";

/** @type {import("models").Thread} */
const threadPatch = {
    setup() {
        super.setup(...arguments);
        this.appAsOwAiChats = fields.One("DiscussApp", {
            compute() {
                return this.isOwAiChat ? this.store.discuss : null;
            },
        });
    },
    /** AI chats have their own "AI" category in the Discuss sidebar. */
    _computeDiscussAppCategory() {
        if (!this.isOwAiChat) {
            return super._computeDiscussAppCategory();
        }
        return this.appAsOwAiChats?.owAiCategory;
    },
};

patch(Thread.prototype, threadPatch);
