import { Component, t, useProps } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

/**
 * The assistant's status at the bottom of an AI chat, in the Discuss app and
 * in chat windows alike: the running tool's `toolStatus` or "Thinking…" while
 * it answers; "Waiting for this page…" with a retry while a client tool
 * waits on the browser (e.g. this tab missed the request).
 */
export class OwAiThreadStatus extends Component {
    static template = "ow_ai.ThreadStatus";

    setup() {
        super.setup();
        this.store = useService("mail.store");
        this.props = useProps({ channel: t.instanceOf(this.store["discuss.channel"]) });
    }

    get session() {
        return this.props.channel.owAiSession;
    }

    get isWaitingForPage() {
        return this.session?.loop_state === "waiting_client_result" && !this.session.isSubmitting;
    }

    get canRetry() {
        return Boolean(
            this.session?.clientToolRequest?.resumeToken && !this.session.runningClientToolToken
        );
    }

    onClickRetry() {
        this.session?.processPendingClientTool();
    }
}
