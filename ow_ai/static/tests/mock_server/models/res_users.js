import { mailModels } from "@mail/../tests/mail_test_helpers";

import { serverState } from "@web/../tests/web_test_helpers";

export const OW_AI_USER_GROUP = "ow_ai.group_ai_user";

/**
 * `res.users` of the AI tests: the internal user group implies
 * `ow_ai.group_ai_user` (security/ow_ai_groups.xml), so a user is an AI user
 * while its `group_ids` hold `serverState.groupId`; tests remove it to get a
 * user without access.
 */
export class ResUsers extends mailModels.ResUsers {
    has_group(id, group_ext_id) {
        if (group_ext_id === OW_AI_USER_GROUP) {
            return this._ow_ai_has_access(id);
        }
        return super.has_group(...arguments);
    }

    _ow_ai_has_access(id) {
        const [user] = this.browse(id);
        return Boolean(user?.group_ids?.includes(serverState.groupId));
    }

    /** Mirrors `res.users._store_init_global_fields` (models/res_users.py). */
    _init_store_data(store) {
        super._init_store_data(...arguments);
        store.add_global_values({ has_access_ow_ai: this._ow_ai_has_access(this.env.uid) });
    }
}
