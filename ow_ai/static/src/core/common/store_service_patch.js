import { Store } from "@mail/core/common/store_service";
import { patch } from "@web/core/utils/patch";

patch(Store.prototype, {
    setup() {
        super.setup(...arguments);
        /** Whether the user may use the assistant (`res.users._store_init_global_fields`). */
        this.has_access_ow_ai = false;
    },
});
