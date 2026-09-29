/**
 * Shared selectors and steps of the AI chat tours. The tours run against the
 * scripted `FakeTransport` of `ow_ai/tests/test_tours.py`: they are not meant
 * to be started from the interface.
 */

export const CHAT_WINDOW = ".o-mail-ChatWindow";
export const ASK_AI_BUTTON = ".o_menu_systray .o_ow_ai_systray_ask_ai";
export const COMPOSER_INPUT = `${CHAT_WINDOW} .o-mail-Composer-input`;
/** The composer once it rendered some text to send (its Send button is enabled). */
const COMPOSER_WITH_TEXT = `${CHAT_WINDOW} .o-mail-Composer:has([aria-label='Send']:enabled)`;

/**
 * Selector of the agent's answer containing `text` in the chat window.
 *
 * @param {string} text
 */
export function answer(text) {
    return `${CHAT_WINDOW} .o-mail-Message.o_ow_ai_agent_answer:contains("${text}")`;
}

/** Click the systray "Ask AI" button and wait for the chat window it opens. */
export function openChatFromSystray() {
    return [
        {
            content: "Open an AI chat from the systray",
            trigger: ASK_AI_BUTTON,
            run: "click",
        },
        {
            content: "The chat opens in a chat window, ready for a message",
            trigger: `${COMPOSER_INPUT}:enabled`,
        },
    ];
}

/**
 * Type `text` in the chat window's composer and send it with Enter.
 *
 * @param {string} text
 */
export function sendMessage(text) {
    return [
        {
            content: `Type "${text}"`,
            trigger: `${COMPOSER_INPUT}:enabled`,
            run: `edit ${text}`,
        },
        {
            content: "Send it with Enter",
            // Not before the typed text was rendered: typing and sending within
            // one frame clears the composer's text before any render showed it,
            // and the textarea keeps displaying it (a person never types that fast).
            trigger: `${COMPOSER_WITH_TEXT} .o-mail-Composer-input`,
            run: "press Enter",
        },
    ];
}
