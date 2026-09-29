import { ChannelMember } from "@mail/discuss/core/common/channel_member_model";
import { fields } from "@mail/model/export";
import { browser } from "@web/core/browser/browser";
import { patch } from "@web/core/utils/patch";

/**
 * The agent member of an AI chat "is typing" for as long as the session
 * generates. Mail stops a typing member after `Store.OTHER_LONG_TYPING`
 * (`registerTypingTimeout`); the agent member's typing follows the session
 * instead, however long the turn takes.
 *
 * @type {import("models").ChannelMember}
 */
const channelMemberPatch = {
    setup() {
        super.setup(...arguments);
        this.owAiAgentGenerating = fields.Attr(false, {
            compute() {
                return this.isOwAiAgent && Boolean(this.channel_id?.isAiGenerating);
            },
            onUpdate() {
                if (!this.isOwAiAgent) {
                    return;
                }
                if (this.owAiAgentGenerating) {
                    // a typing notification may have started the timeout before the turn
                    browser.clearTimeout(this.typingTimeoutId);
                }
                this.isTyping = this.owAiAgentGenerating;
            },
            eager: true,
        });
    },
    get isOwAiAgent() {
        return Boolean(
            this.partner_id && this.channel_id?.ow_ai_agent_id?.partner_id?.eq(this.partner_id)
        );
    },
    /** @override */
    registerTypingTimeout() {
        if (this.isOwAiAgent && this.channel_id.isAiGenerating) {
            return;
        }
        return super.registerTypingTimeout(...arguments);
    },
};

patch(ChannelMember.prototype, channelMemberPatch);
