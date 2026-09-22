/** @odoo-module **/

import { Component, t, useProps } from "@odoo/owl";

export class SafeModeBanner extends Component {
    static template = "ow_mail.SafeModeBanner";
    props = useProps({
        onShowOnce: t.function(),
        onTrustSender: t.function(),
    });
}
