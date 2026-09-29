import { ComposerAction, registerComposerAction } from "@mail/core/common/composer_actions";
import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";

/** The only composer actions of an AI chat: send, attach files and its settings. */
const OW_AI_COMPOSER_ACTIONS = new Set([
    "send-message",
    "upload-files",
    "ow_ai_auto_approve",
    "ow_ai_web_search",
    "ow_ai_show_steps",
]);

/** @param {import("models").Composer} composer */
function getOwAiSession(composer) {
    const thread = composer?.targetThread;
    return thread?.isOwAiChat && !composer.message ? thread.owAiSession : undefined;
}

/**
 * A chat setting in the composer's "More Actions" menu (Odoo 19 renders a
 * composer's actions without groups: the settings come after mail's actions,
 * by sequence). Its icon is an on/off switch (`ow_ai.ComposerSettingIcon`);
 * the menu stays open (`keepDropdownOpen`, see `composer_actions_patch.xml`).
 *
 * @param {string} id
 * @param {Object} param1
 * @param {string} param1.key `auto_confirm`, `show_agent_steps` or `web_search`
 * @param {string} param1.icon
 * @param {string} param1.name
 * @param {number} param1.sequence
 * @param {Function} [param1.extraCondition] `({ composer, store }) => boolean`: the setting
 *   is only shown when this holds too
 */
function registerOwAiSettingToggle(id, { key, icon, name, sequence, extraCondition }) {
    registerComposerAction(id, {
        condition: ({ composer, store }) =>
            Boolean(getOwAiSession(composer)) &&
            (!extraCondition || Boolean(extraCondition({ composer, store }))),
        icon: ({ composer }) => ({
            template: "ow_ai.ComposerSettingIcon",
            params: { checked: Boolean(getOwAiSession(composer)?.config?.[key]), icon },
        }),
        keepDropdownOpen: true,
        name,
        onSelected: async ({ composer, store }) => {
            const session = getOwAiSession(composer);
            try {
                await session.updateConfig({ [key]: !session.config?.[key] });
            } catch (error) {
                store.env.services.notification.add(
                    error?.data?.message || _t("The chat settings could not be saved."),
                    { type: "danger" }
                );
            }
        },
        sequence,
    });
}

registerOwAiSettingToggle("ow_ai_auto_approve", {
    key: "auto_confirm",
    icon: "fa fa-check-circle",
    name: _t("Auto-approve actions"),
    sequence: 50,
});
registerOwAiSettingToggle("ow_ai_web_search", {
    key: "web_search",
    icon: "fa fa-search",
    name: _t("Web search"),
    sequence: 51,
    // only when a search service is configured (`res.users._init_store_data`)
    extraCondition: ({ store }) => store.has_ow_ai_web_search,
});
registerOwAiSettingToggle("ow_ai_show_steps", {
    key: "show_agent_steps",
    icon: "fa fa-list-ul",
    name: _t("Show steps"),
    sequence: 52,
});

patch(ComposerAction.prototype, {
    /** In an AI chat: no emoji, GIF, voice, poll, canned response… */
    _condition({ composer }) {
        if (
            composer?.targetThread?.isOwAiChat &&
            !this.definition.isMoreAction &&
            !OW_AI_COMPOSER_ACTIONS.has(this.id)
        ) {
            return false;
        }
        return super._condition(...arguments);
    },
});
