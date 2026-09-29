import { uuid } from "@web/core/utils/strings";

/**
 * Identifies this browser tab to the server: sent as `client_identifier` with
 * `/ow_ai/session/advance` and `/ow_ai/session/resume`, and echoed on the
 * `ow_ai.session/client_tools` notification, so that only the tab that sent
 * the message (or last answered a card) runs the assistant's client tools.
 */
export const OW_AI_CLIENT_IDENTIFIER = uuid();
