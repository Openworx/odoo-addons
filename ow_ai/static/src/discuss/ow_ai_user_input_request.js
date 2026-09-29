import { Component, onMounted, proxy, signal, t, useProps } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { useService } from "@web/core/utils/hooks";

/** Confirmation choices, in display order, with their default label and button style. */
const CONFIRMATION_BUTTONS = [
    { value: "confirm_once", label: _t("Yes, do it"), className: "btn-primary" },
    {
        value: "auto_confirm",
        label: _t("Yes, always approve in this chat"),
        className: "btn-secondary",
    },
    { value: "decline", label: _t("No"), className: "btn-link" },
];

/** Digits "1".."4" pick the matching option of a question card. */
const MAX_KEYBOARD_OPTIONS = 4;

/**
 * The live card of a session waiting for the user (`session.userInputRequest`):
 * a confirmation (three buttons) or a question (options, optional free text,
 * Send/Skip). The answer resumes the session (`thread.requestOwAiResume`);
 * the server's Store push then drops the request, which unmounts the card.
 */
export class OwAiUserInputRequest extends Component {
    static template = "ow_ai.UserInputRequest";

    setup() {
        super.setup();
        this.store = useService("mail.store");
        this.notification = useService("notification");
        this.props = useProps({
            request: t.instanceOf(this.store["ow.ai.user.input.request"]),
            thread: t.instanceOf(this.store["mail.thread"]),
        });
        this.rootRef = signal.ref(HTMLDivElement);
        this.state = proxy({ selected: [], text: "", isSubmitting: false });
        onMounted(() => {
            // keyboard answers (never for a confirmation: a stray Enter must not
            // approve a change)
            if (this.isQuestion && this.mayTakeFocus()) {
                this.rootRef()?.focus({ preventScroll: true });
            }
        });
    }

    /**
     * Only when nothing has the focus, or something of this chat (its chat
     * window, its Discuss thread) that is not a text field being typed in:
     * never from the view behind the chat.
     */
    mayTakeFocus() {
        const active = document.activeElement;
        if (!active || active === document.body) {
            return true;
        }
        const root = this.rootRef();
        const chat = root?.closest(".o-mail-ChatWindow, .o-mail-DiscussContent, .o-mail-Thread");
        const isTyping =
            ["INPUT", "TEXTAREA", "SELECT"].includes(active.tagName) || active.isContentEditable;
        return Boolean(chat?.contains(active)) && !isTyping;
    }

    get request() {
        return this.props.request;
    }

    get isQuestion() {
        return this.request.type === "question";
    }

    get confirmationButtons() {
        const offered = this.request.choices?.length ? this.request.choices : null;
        return CONFIRMATION_BUTTONS.filter(({ value }) => !offered || offered.includes(value)).map(
            (button) => ({ ...button, label: this.request.labels?.[button.value] || button.label })
        );
    }

    get options() {
        return this.request.choices ?? [];
    }

    get canSend() {
        return (
            this.state.selected.length > 0 ||
            Boolean(this.request.allowFreeText && this.state.text.trim())
        );
    }

    isSelected(option) {
        return this.state.selected.includes(option);
    }

    toggleOption(option) {
        if (this.state.isSubmitting) {
            return;
        }
        if (!this.request.multiSelect) {
            this.state.selected = [option];
        } else if (this.isSelected(option)) {
            this.state.selected = this.state.selected.filter((selected) => selected !== option);
        } else {
            this.state.selected = [...this.state.selected, option];
        }
    }

    onConfirm(value) {
        return this.submit({ kind: "confirmation", value });
    }

    onSend() {
        if (!this.canSend) {
            return;
        }
        const selected = new Set(this.state.selected);
        return this.submit({
            kind: "question",
            value: {
                // in the offered order
                choices: this.options.filter((option) => selected.has(option)),
                text: this.request.allowFreeText ? this.state.text.trim() : "",
            },
        });
    }

    onSkip() {
        return this.submit({ kind: "skip" });
    }

    /** @param {KeyboardEvent} ev */
    onKeydown(ev) {
        if (!this.isQuestion || ev.altKey || ev.ctrlKey || ev.metaKey) {
            return;
        }
        const inFreeText = ev.target.classList?.contains("o_ow_ai_free_text");
        if (ev.key === "Enter" && ev.target.closest?.("button")) {
            return; // a focused button (Skip, Send) acts itself
        }
        if (ev.key === "Enter") {
            ev.preventDefault();
            ev.stopPropagation();
            this.onSend();
        } else if (ev.key === "Escape") {
            ev.preventDefault();
            ev.stopPropagation();
            this.onSkip();
        } else if (!inFreeText && /^[1-9]$/.test(ev.key)) {
            const index = Number(ev.key) - 1;
            if (index < Math.min(this.options.length, MAX_KEYBOARD_OPTIONS)) {
                ev.preventDefault();
                this.toggleOption(this.options[index]);
            }
        }
    }

    /** @param {{kind: string, value?: any}} response */
    async submit(response) {
        if (this.state.isSubmitting) {
            return;
        }
        this.state.isSubmitting = true;
        try {
            const acknowledgement = await this.props.thread.requestOwAiResume(response, {
                session: this.request.session,
                resumeToken: this.request.resumeToken,
            });
            if (acknowledgement && !acknowledgement.interactionConsumed) {
                this.notification.add(_t("This request is no longer waiting for an answer."), {
                    type: "warning",
                });
            }
        } catch (error) {
            this.notification.add(
                error?.data?.message || error?.message || _t("Your answer could not be sent."),
                { type: "danger" }
            );
        } finally {
            this.state.isSubmitting = false;
        }
    }
}
