import { SuggestionService } from "@mail/core/common/suggestion_service";
import { patch } from "@web/core/utils/patch";

patch(SuggestionService.prototype, {
    /** No @mention, #channel, /command, ::canned response or :emoji: in an AI chat. */
    getSupportedDelimiters(thread) {
        if (thread?.isOwAiChat) {
            return [];
        }
        return super.getSupportedDelimiters(...arguments);
    },
});
