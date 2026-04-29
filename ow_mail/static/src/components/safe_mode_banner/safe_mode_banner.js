/** @odoo-module **/

import { Component } from "@odoo/owl";

export class SafeModeBanner extends Component {
    static template = "ow_mail.SafeModeBanner";
    static props = { onShowOnce: Function, onTrustSender: Function };
}
