import {
    getPreValue,
    highlightPre,
} from "@html_editor/others/embedded_components/core/syntax_highlighting/syntax_highlighting_utils";
import { Message } from "@mail/core/common/message";
import { loadBundle } from "@web/core/assets";
import { cookie } from "@web/core/browser/cookie";
import { patch } from "@web/core/utils/patch";

/**
 * Languages of Community's Prism build (`web/static/lib/prismjs`, loaded
 * lazily from html_editor's `html_editor.assets_prism` bundle), by the names a
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

/** The `<pre>` elements already handled: `prepareMessageBody` may see a body twice. */
const handledCodeBlocks = new WeakSet();

/**
 * Highlight the fenced code blocks of an answer (`<pre><code class="language-x">`)
 * with Prism, like html_editor's read-only syntax highlighting does (Odoo 19's
 * mail mounts no embedded components in message bodies): the block's text is
 * replaced by Prism's tokens, the `<pre>` gets `data-language-id`. Only a
 * class token starting with `language-` naming a language of the Prism build
 * counts: the body is the model's HTML, its classes can be anything.
 *
 * @param {HTMLElement} bodyEl
 */
function highlightCodeBlocks(bodyEl) {
    const blocks = [];
    for (const code of bodyEl.querySelectorAll("pre > code")) {
        const pre = code.parentElement;
        if (handledCodeBlocks.has(pre)) {
            continue;
        }
        handledCodeBlocks.add(pre);
        const languageId = [...code.classList]
            .filter((cls) => cls.startsWith(LANGUAGE_CLASS_PREFIX))
            .map((cls) =>
                PRISM_LANGUAGES.get(cls.slice(LANGUAGE_CLASS_PREFIX.length).toLowerCase())
            )
            .find(Boolean);
        if (languageId) {
            pre.dataset.languageId = languageId;
            blocks.push(pre);
        }
    }
    if (!blocks.length) {
        return;
    }
    const bundle = `html_editor.assets_prism${
        cookie.get("color_scheme") === "dark" ? "_dark" : ""
    }`;
    loadBundle(bundle, { targetDoc: bodyEl.ownerDocument }).then(
        () => {
            // without Prism, `highlightPre` would set the decoded code text
            // (the model's output) as HTML: leave the code plain instead
            if (!window.Prism) {
                return;
            }
            for (const pre of blocks) {
                // a body rendered again in the meantime is handled on its own
                if (pre.isConnected) {
                    highlightPre(pre, getPreValue(pre), pre.dataset.languageId);
                }
            }
        },
        () => {} // Prism could not be loaded: the code stays plain
    );
}

/** @type {import("@mail/core/common/message").Message} */
const messagePatch = {
    get isOwAiChatMessage() {
        return Boolean(this.props.thread?.isOwAiChat);
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
        const result = super.prepareMessageBody(...arguments);
        if (bodyEl && this.isOwAiChatMessage && this.message.owAiIsAnswer) {
            highlightCodeBlocks(bodyEl);
        }
        return result;
    },
};

patch(Message.prototype, messagePatch);
