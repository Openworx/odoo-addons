import { Message } from "@mail/core/common/message";
import { patch } from "@web/core/utils/patch";

/**
 * Languages of Community's Prism build (`web/static/lib/prismjs`, loaded
 * lazily by html_editor's read-only syntax highlighting), by the names a
 * Markdown fence may use. A `Map`: a fence named `constructor` must not find
 * an inherited object member (Prism has no grammar for it and would throw).
 */
const PRISM_LANGUAGES = new Map([
    ["css", "css"],
    ["diff", "diff"],
    ["html", "markup"],
    ["java", "java"],
    ["javascript", "javascript"],
    ["js", "javascript"],
    ["json", "json"],
    ["markdown", "markdown"],
    ["md", "markdown"],
    ["markup", "markup"],
    ["py", "python"],
    ["python", "python"],
    ["sass", "sass"],
    ["scss", "scss"],
    ["sql", "sql"],
    ["ts", "typescript"],
    ["typescript", "typescript"],
    ["xml", "markup"],
]);
const LANGUAGE_CLASS_PREFIX = "language-";

/**
 * Hand the fenced code blocks of an answer (`<pre><code class="language-x">`)
 * to mail's read-only syntax highlighting (`renderEmbeddedCodeBlocks`, which
 * mounts `readonlySyntaxHighlighting` on `pre[data-embedded]`). Only a class
 * token starting with `language-` naming a language of the Prism build
 * counts: the body is the model's HTML, its classes can be anything.
 *
 * @param {HTMLElement} bodyEl
 */
function markCodeBlocksForHighlighting(bodyEl) {
    for (const code of bodyEl.querySelectorAll("pre > code")) {
        const pre = code.parentElement;
        if (pre.dataset.embedded) {
            continue;
        }
        const languageId = [...code.classList]
            .filter((cls) => cls.startsWith(LANGUAGE_CLASS_PREFIX))
            .map((cls) =>
                PRISM_LANGUAGES.get(cls.slice(LANGUAGE_CLASS_PREFIX.length).toLowerCase())
            )
            .find(Boolean);
        if (languageId) {
            pre.dataset.embedded = "readonlySyntaxHighlighting";
            pre.dataset.languageId = languageId;
        }
    }
}

/** @type {import("@mail/core/common/message").Message} */
const messagePatch = {
    get isOwAiChatMessage() {
        return Boolean(this.props.thread?.channel?.isOwAiChat);
    },

    get attClass() {
        const attClass = super.attClass;
        if (!this.isOwAiChatMessage) {
            return attClass;
        }
        return {
            ...attClass,
            o_ow_ai_agent_answer: this.message.owAiIsAnswer,
            o_ow_ai_input_card_message: this.message.owAiIsUserInputCard,
            o_ow_ai_user_message: this.message.isSelfAuthored,
        };
    },

    /** The user's own messages sit on the right of an AI chat, in Discuss too. */
    get isAlignedRight() {
        if (this.isOwAiChatMessage && this.message.isSelfAuthored && !this.env.messageCard) {
            return true;
        }
        return super.isAlignedRight;
    },

    /** @override */
    prepareMessageBody(bodyEl) {
        if (bodyEl && this.isOwAiChatMessage && this.message.owAiIsAnswer) {
            markCodeBlocksForHighlighting(bodyEl);
        }
        return super.prepareMessageBody(...arguments);
    },
};

patch(Message.prototype, messagePatch);
