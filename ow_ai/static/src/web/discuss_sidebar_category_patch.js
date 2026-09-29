import { DiscussSidebarCategory } from "@mail/discuss/core/public_web/discuss_sidebar_categories";
import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";

/**
 * "New chat" on the "AI" category of the Discuss sidebar (the launcher only
 * exists in the web client).
 *
 * @type {import("@mail/discuss/core/public_web/discuss_sidebar_categories").DiscussSidebarCategory}
 */
const discussSidebarCategoryPatch = {
    get actions() {
        const actions = super.actions;
        if (this.store.has_access_ow_ai && this.category.eq(this.store.discuss.owAiCategory)) {
            actions.push({
                // quoted: an unquoted `class` key stops Odoo's translation
                // export (babel JS lexer) from seeing the _t() below
                "class": "o_ow_ai_new_chat",
                icon: "fa fa-plus",
                label: _t("New AI chat"),
                onSelect: () => this.env.services["ow_ai.chat_launcher"].launchChat(),
            });
        }
        return actions;
    },
};

patch(DiscussSidebarCategory.prototype, discussSidebarCategoryPatch);
