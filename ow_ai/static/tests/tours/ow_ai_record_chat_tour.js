import { registry } from "@web/core/registry";

import {
    answer,
    ASK_AI_BUTTON,
    CHAT_WINDOW,
    sendMessage,
} from "@ow_ai/../tests/tours/ow_ai_tour_utils";

/** Name of the contact whose form the tour starts on (created by `test_tours.py`). */
const PARTNER_NAME = "Tour Partner Ltd";
/** The scripted answer (`test_tours.py`). */
const SUMMARY = `Summary: ${PARTNER_NAME} is a company in Utrecht.`;
const ANSWER = answer(SUMMARY);
const FULL_COMPOSER = ".modal .o_mail_composer_form_view";
const LOG_NOTE_ACTION = ".o-mail-Message-moreMenu .o-dropdown-item[name='ow_ai_log_note']";

/**
 * Scenario C, a record chat: from a contact's form, "Ask AI" opens a chat
 * about that contact; the scripted model answers "Summary: …", which the
 * user then logs as a note on the contact through the full composer.
 */
registry.category("web_tour.tours").add("ow_ai_record_chat", {
    steps: () => [
        {
            content: "The contact's form is open",
            trigger: `.o_form_view .o_field_widget[name='name'] input:value("${PARTNER_NAME}")`,
        },
        {
            content: "Ask AI about this contact",
            trigger: ASK_AI_BUTTON,
            run: "click",
        },
        {
            content: "The chat is named after the contact",
            trigger: `${CHAT_WINDOW} .o-mail-ChatWindow-header:contains("${PARTNER_NAME}")`,
        },
        ...sendMessage("Summarize this record"),
        {
            content: "The assistant answers",
            trigger: ANSWER,
        },
        {
            content: "Open the answer's other actions",
            trigger: ANSWER,
            run: `hover && click ${ANSWER} [title='Expand']`,
        },
        {
            content: "Log the answer as a note on the contact",
            trigger: `${LOG_NOTE_ACTION}:contains(Log as note)`,
            run: "click",
        },
        {
            content: "The full composer opens, prefilled with the answer",
            trigger: `${FULL_COMPOSER} .o_field_html[name='body']:contains("${SUMMARY}")`,
        },
        {
            content: "Close it without logging anything",
            trigger: ".modal:has(.o_mail_composer_form_view) .btn-close",
            run: "click",
        },
        {
            content: "The full composer is closed",
            trigger: `body:not(:has(${FULL_COMPOSER}))`,
        },
    ],
});
