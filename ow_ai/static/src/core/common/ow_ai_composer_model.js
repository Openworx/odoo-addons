import { Record } from "@mail/model/export";

/** The `ow.ai.composer` a session was launched from (only its key is published). */
export class OwAiComposer extends Record {
    static _name = "ow.ai.composer";
    static id = "id";

    /** @type {number} */
    id;
    /** @type {string} */
    interface_key;
}

OwAiComposer.register();
