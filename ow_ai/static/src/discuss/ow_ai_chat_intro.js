import { Component, markup, t, useProps } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

/**
 * The start of an AI chat (mail's thread "start message" slot): the agent's
 * avatar, name and subtitle, plus, while the chat is empty, the prompt
 * buttons of the composer it was launched from (`session.promptButtons`,
 * set by the launcher). A prompt button posts its prompt as the user's message.
 */
export class OwAiChatIntro extends Component {
    static template = "ow_ai.ChatIntro";

    setup() {
        super.setup();
        this.store = useService("mail.store");
        this.props = useProps({ thread: t.instanceOf(this.store["mail.thread"]) });
    }

    get channel() {
        return this.props.thread.channel;
    }

    get agent() {
        return this.channel?.ow_ai_agent_id ?? this.channel?.owAiSession?.agent_id;
    }

    get avatarUrl() {
        return this.agent?.partner_id?.avatarUrl ?? this.channel?.avatarUrl;
    }

    get name() {
        return this.agent?.name || this.channel?.displayName;
    }

    get promptButtons() {
        const session = this.channel?.owAiSession;
        if (!this.props.thread.isEmpty || !session || session.isComposerLocked) {
            return [];
        }
        return (session.promptButtons ?? []).filter((button) => button?.prompt);
    }

    onClickPrompt(button) {
        // `markup` escapes the interpolated prompt: it is posted as plain text
        return this.props.thread.post(markup`${button.prompt}`);
    }
}
