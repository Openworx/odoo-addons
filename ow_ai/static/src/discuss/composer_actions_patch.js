import {
    ComposerAction,
    describeComposerActionGroup,
    registerComposerAction,
} from "@mail/core/common/composer_actions";
import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";

import { OwAiToggleIndicator } from "@ow_ai/discuss/ow_ai_toggle_indicator";

/** The only composer actions of an AI chat: send, attach files and its settings. */
const OW_AI_COMPOSER_ACTIONS = new Set([
    "send-message",
    "upload-files",
    "ow_ai_auto_approve",
    "ow_ai_show_steps",
]);
/** Action group of the chat settings (in the composer's "More Actions" menu). */
export const OW_AI_SETTINGS_GROUP = 50;

/** @param {import("models").Composer} composer */
function getOwAiSession(composer) {
    const channel = composer?.targetThread?.channel;
    return channel?.isOwAiChat && !composer.message ? channel.owAiSession : undefined;
}

/**
 * @param {string} key `auto_confirm` or `show_agent_steps`
 * @param {string} icon
 * @param {string} name
 * @param {number} sequence
 */
function registerOwAiSettingToggle(id, { key, icon, name, sequence }) {
    registerComposerAction(id, {
        closingModeAsDropdown: "none",
        condition: ({ composer }) => Boolean(getOwAiSession(composer)),
        extraContentComponent: OwAiToggleIndicator,
        extraContentComponentProps: ({ composer }) => ({
            checked: Boolean(getOwAiSession(composer)?.config?.[key]),
        }),
        icon,
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
        sequenceGroup: OW_AI_SETTINGS_GROUP,
    });
}

describeComposerActionGroup(OW_AI_SETTINGS_GROUP, { name: _t("AI chat settings") });
registerOwAiSettingToggle("ow_ai_auto_approve", {
    key: "auto_confirm",
    icon: "check_circle",
    name: _t("Auto-approve actions"),
    sequence: 10,
});
registerOwAiSettingToggle("ow_ai_show_steps", {
    key: "show_agent_steps",
    icon: "checklist",
    name: _t("Show steps"),
    sequence: 20,
});

patch(ComposerAction.prototype, {
    /** In an AI chat: no emoji, GIF, voice, poll, canned response… */
    _condition({ composer }) {
        if (
            composer?.targetThread?.channel?.isOwAiChat &&
            !this.definition.isMoreAction &&
            !OW_AI_COMPOSER_ACTIONS.has(this.id)
        ) {
            return false;
        }
        return super._condition(...arguments);
    },
});
