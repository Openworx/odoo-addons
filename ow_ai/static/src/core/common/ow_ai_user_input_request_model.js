import { fields, Record } from "@mail/model/export";

/**
 * The question or confirmation card a session waits on (`userInputRequest`
 * of the session Store payload). The server sends it without an id: it is
 * identified by its session, which is set through the inverse relation.
 */
export class OwAiUserInputRequest extends Record {
    static _name = "ow.ai.user.input.request";
    static id = "session";

    session = fields.One("ow.ai.session", { inverse: "userInputRequest" });
    /** @type {"confirmation"|"question"} */
    type;
    body = fields.Html("");
    /** @type {string[]} */
    choices = fields.Attr([]);
    /** @type {Object<string, string>} */
    labels = fields.Attr({});
    multiSelect = false;
    allowFreeText = false;
    /** @type {string|undefined} */
    resumeToken;

    /** The id of the session, the identity of the request. */
    get id() {
        return this.session?.id;
    }
}

OwAiUserInputRequest.register();
