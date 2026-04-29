/** @odoo-module **/

import { Component } from "@odoo/owl";

/** Color palette for initials badges. One of 8 colors is chosen deterministically from the email hash. */
const PALETTE = ["#F59E0B", "#EF4444", "#10B981", "#3B82F6", "#8B5CF6", "#EC4899", "#14B8A6", "#F97316"];

/**
 * Colored initials badge used as a fallback when no avatar image is available.
 *
 * The color is derived deterministically from the email address (or name) via
 * a simple hash, so the same sender always renders the same color across
 * sessions without needing to persist anything.
 */
export class AvatarInitials extends Component {
    static template = "ow_mail.AvatarInitials";
    static props = { name: String, email: { type: String, optional: true } };

    /**
     * Up to two initials from the display name or email.
     * For multi-word names the first character of the first and second words
     * are used (e.g. `"John Doe"` → `"JD"`). Single-word inputs use the
     * first two characters instead.
     * @returns {string}
     */
    get initials() {
        const name = (this.props.name || this.props.email || "?").trim();
        const parts = name.split(/\s+/);
        if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
        return name.slice(0, 2).toUpperCase();
    }

    /**
     * Background color for the badge, picked from `PALETTE` by hashing the
     * email (preferred) or name. The same address always maps to the same
     * color (hash mod 8), giving visual consistency without stored state.
     * @returns {string}
     */
    get color() {
        const src = (this.props.email || this.props.name || "");
        let h = 0;
        for (let i = 0; i < src.length; i++) h = (h * 31 + src.charCodeAt(i)) >>> 0;
        return PALETTE[h % PALETTE.length];
    }
}
