/** @odoo-module **/

/**
 * 12-entry color palette shared by account accents, tag dots and tag chips.
 * Mirrors Odoo's standard tag color wheel. Index 0 is intentionally null
 * ("no color") so records with `color == 0` can render a neutral fallback.
 */
export const OW_PALETTE = [
    null,
    "#F06050",
    "#F4A460",
    "#F7CD1F",
    "#6CC1ED",
    "#814968",
    "#EB7E7F",
    "#2C8397",
    "#475577",
    "#D6145F",
    "#30C381",
    "#9365B8",
];

/** Neutral color used when a record has no palette color assigned. */
export const OW_COLOR_FALLBACK = "#6c757d";

/**
 * Resolve a palette index to a CSS color.
 * @param {number} colorIndex - Palette index (0 = none).
 * @returns {string} CSS color; gray fallback for index 0 / out of range.
 */
export function owColor(colorIndex) {
    return OW_PALETTE[(colorIndex || 0) % OW_PALETTE.length] || OW_COLOR_FALLBACK;
}
