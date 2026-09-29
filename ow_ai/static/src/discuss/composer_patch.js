import { Composer } from "@mail/core/common/composer";
import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";

/** What the assistant can read from an attachment (`ir.attachment._ow_ai_to_parts`). */
export const OW_AI_ALLOWED_MIMETYPES = [
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "application/pdf",
    "text/plain",
    "text/csv",
    "text/markdown",
    "text/html",
];
export const OW_AI_ACCEPTED_FILE_TYPES = [...OW_AI_ALLOWED_MIMETYPES, ".txt", ".csv", ".md"].join(
    ","
);

/** @type {import("@mail/core/common/composer").Composer} */
const composerPatch = {
    get isOwAiChat() {
        return Boolean(this.thread?.isOwAiChat);
    },

    /** The assistant is answering (`thread.composerDisabled`, see the thread model patch). */
    get isOwAiLocked() {
        return this.isOwAiChat && Boolean(this.thread.composerDisabled);
    },

    get placeholder() {
        if (this.isOwAiLocked) {
            return _t("The assistant is answering…");
        }
        return super.placeholder;
    },

    get areAllActionsDisabled() {
        return super.areAllActionsDisabled || this.isOwAiLocked;
    },

    get isSendButtonDisabled() {
        return super.isSendButtonDisabled || this.isOwAiLocked;
    },

    /** `accept` of the file input: images, PDF and text files only. */
    get owAiAcceptedFileTypes() {
        return this.isOwAiChat ? OW_AI_ACCEPTED_FILE_TYPES : undefined;
    },

    get owAiAllowedMimeTypes() {
        return this.isOwAiChat ? OW_AI_ALLOWED_MIMETYPES.join(",") : undefined;
    },

    /** No draft is kept for an AI chat. */
    saveContent() {
        if (this.isOwAiChat) {
            return;
        }
        return super.saveContent(...arguments);
    },
};

patch(Composer.prototype, composerPatch);
