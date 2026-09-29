import { registry } from "@web/core/registry";

import {
    answer,
    CHAT_WINDOW,
    openChatFromSystray,
    sendMessage,
} from "@ow_ai/../tests/tours/ow_ai_tour_utils";

const STEPS = `${CHAT_WINDOW} .o_ow_ai_steps`;
const MESSAGING_MENU = ".o-mail-MessagingMenu";
const AI_TAB = `${MESSAGING_MENU} .o-mail-MessagingMenu-headerFilter.o_ow_ai_messaging_menu_tab`;

/**
 * Scenario A, a read-only question: the scripted model loads a skill, calls
 * `get_fields` and `read_group`, answers with a Markdown table, then names
 * the chat "Top customers this quarter" (see `test_tours.py`). The tour
 * starts on the Contacts list, where "Ask AI" opens the chat in a chat window,
 * and switches "Show steps" on (off by default).
 */
registry.category("web_tour.tours").add("ow_ai_chat_read", {
    steps: () => [
        ...openChatFromSystray(),
        {
            content: "Open the chat's settings",
            trigger: `${CHAT_WINDOW} .o-mail-Composer button[title='More Actions']`,
            run: "click",
        },
        {
            content: "Show the steps the assistant takes (off by default)",
            trigger: ".o-dropdown-item[name='ow_ai_show_steps']:has(input:not(:checked))",
            run: "click",
        },
        {
            content: "The steps are shown in this chat",
            trigger: ".o-dropdown-item[name='ow_ai_show_steps'] input:checked",
        },
        ...sendMessage("Top 5 customers by revenue this quarter"),
        {
            content: "The assistant is thinking",
            trigger: `${CHAT_WINDOW} .o_ow_ai_status:contains(Thinking)`,
        },
        {
            content: "The answer arrives, its Markdown table rendered as a table",
            trigger: `${answer("Top customers")} table`,
        },
        {
            content: "The assistant is done",
            trigger: `${CHAT_WINDOW}:not(:has(.o_ow_ai_status))`,
        },
        {
            content: "Unfold the steps the assistant took",
            trigger: `${STEPS}:not(.o_ow_ai_steps_open) .o_ow_ai_steps_header:contains(Steps taken)`,
            run: "click",
        },
        {
            content: "The steps list the tools the assistant used",
            trigger: `${STEPS}.o_ow_ai_steps_open .o_ow_ai_steps_list .o_ow_ai_tool_summary_row`,
        },
        {
            content: "Open the messaging menu",
            trigger: ".o_menu_systray i[aria-label='Messages']",
            run: "click",
        },
        {
            content: "Open its AI tab",
            trigger: AI_TAB,
            run: "click",
        },
        {
            content: "The AI tab is open",
            trigger: `${AI_TAB}.o-active`,
        },
        {
            // By its last message, not by the title it got (checked server-side):
            // the whole turn, title included, runs within a second of the chat's
            // creation here, and the bus does not deliver notifications of
            // different bus channels in order, so one still carrying the chat's
            // first name can arrive after its renaming.
            content: "The AI tab lists the chat",
            trigger: `${MESSAGING_MENU} .o-mail-NotificationItem:contains(Top customers)`,
        },
    ],
});
