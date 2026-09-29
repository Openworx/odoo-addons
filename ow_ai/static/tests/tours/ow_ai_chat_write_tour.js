import { registry } from "@web/core/registry";

import {
    answer,
    CHAT_WINDOW,
    openChatFromSystray,
    sendMessage,
} from "@ow_ai/../tests/tours/ow_ai_tour_utils";

const CARD = `${CHAT_WINDOW} .o_ow_ai_user_input_request[data-type='confirmation']`;
const NOTIFICATION = `${CHAT_WINDOW} .o-mail-NotificationMessage:contains(ACME Corp)`;

/**
 * Scenario B, a write: the scripted model loads a skill and proposes to
 * create the contact "ACME Corp" (`create_records`), which pauses the turn
 * on a confirmation card; once confirmed, the contact is created and the
 * model answers "Created the contact ACME Corp." (see `test_tours.py`).
 * The tour starts on the Contacts list, which the assistant reloads after
 * the creation (its `reload` client tool).
 */
registry.category("web_tour.tours").add("ow_ai_chat_write", {
    steps: () => [
        ...openChatFromSystray(),
        ...sendMessage("Create a contact for ACME Corp with email info@acme.test"),
        {
            content: "The assistant asks to confirm the creation",
            trigger: CARD,
        },
        {
            content: "The card previews the contact to create",
            trigger: `${CARD} .o_ow_ai_user_input_request_body:contains(ACME Corp)`,
        },
        {
            content: "Confirm",
            trigger: `${CARD} button[name='confirm_once']:contains("Yes, do it"):enabled`,
            run: "click",
        },
        {
            content: "The card is gone once answered",
            trigger: `${CHAT_WINDOW}:not(:has(.o_ow_ai_user_input_request))`,
        },
        {
            content: "The assistant reports the creation",
            trigger: answer("Created"),
        },
        {
            content: "A notification links to the new contact",
            trigger: `${NOTIFICATION} a[href*='/odoo/res.partner/']`,
        },
        {
            content: "The Contacts list behind the chat was reloaded and shows the new contact",
            trigger: ".o_list_view .o_data_row:contains(ACME Corp)",
        },
    ],
});
