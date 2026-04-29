/** @odoo-module **/

import { useEffect } from "@odoo/owl";
import { useHotkey } from "@web/core/hotkeys/hotkey_hook";

/**
 * Return `true` when the keyboard event target is an element where the user
 * is actively typing. Used to suppress global mail hotkeys when focus is
 * inside a text input, textarea, select, or a `contenteditable` div (e.g.
 * the compose window body or a toolbar button that carries `contenteditable`
 * on a parent).  Without this guard, pressing "j" or "c" inside the compose
 * body would both type the character AND trigger navigation or compose.
 * @param {Element|null} target
 * @returns {boolean}
 */
function isEditableTarget(target) {
    if (!target) return false;
    if (target.isContentEditable) return true;
    const tag = target.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
}

/**
 * Register keyboard shortcuts for the mail client on the given component.
 *
 * Hotkeys registered via Odoo's `useHotkey` service:
 * - `j` / `k`       — move selection down / up in the message list
 * - `Escape`         — clear the selected message
 * - `c`             — open a new compose window
 * - `r`             — reply to the selected message
 * - `a`             — reply-all to the selected message
 * - `f`             — forward the selected message
 * - `e`             — archive the selected message
 * - `Delete`         — move selected message to Trash
 * - `Shift+3` (`#`) — move selected message to Trash (Gmail-style shortcut)
 * - `s`             — toggle star (flagged) on the selected message
 * - `u`             — toggle read/unread on the selected message
 *
 * The `"/"` key (focus search) is intentionally registered via a raw DOM
 * `keydown` listener rather than `useHotkey`. Odoo's hotkey service intercepts
 * keydown events before they reach input fields, so using it for `"/"` would
 * prevent the character from being typed in search inputs on some browsers.
 * The raw listener checks `isEditableTarget` itself and calls `ev.preventDefault`
 * only when the key should trigger the action.
 *
 * @param {{ mail: Object, state: Object }} component  - The `Mailclient` component instance
 */
export function registerMailHotkeys(component) {
    const mail = component.mail;
    const state = component.state;

    const hasSelection = () => !!state.selectedMessage;

    useHotkey("j", () => { mail.moveSelection(1); });
    useHotkey("k", () => { mail.moveSelection(-1); });
    useHotkey("escape", () => { if (hasSelection()) mail.clearSelection(); });
    useHotkey("c", () => { mail.openCompose(); });
    useHotkey("r", () => {
        if (hasSelection()) mail.openReply(state.selectedMessage, { all: false });
    });
    useHotkey("a", () => {
        if (hasSelection()) mail.openReply(state.selectedMessage, { all: true });
    });
    useHotkey("f", () => {
        if (hasSelection()) mail.openForward(state.selectedMessage);
    });
    useHotkey("e", () => { if (hasSelection()) mail.archiveSelected(); });
    useHotkey("delete", () => { if (hasSelection()) mail.deleteSelected(); });
    useHotkey("shift+3", () => { if (hasSelection()) mail.deleteSelected(); });
    useHotkey("s", () => { if (hasSelection()) mail.toggleStarSelected(); });
    useHotkey("u", () => { if (hasSelection()) mail.toggleReadSelected(); });

    // "/" is not in Odoo's hotkey whitelist, use a raw keydown listener.
    useEffect(
        () => {
            const handler = (ev) => {
                if (ev.key !== "/" || ev.ctrlKey || ev.metaKey || ev.altKey) return;
                if (isEditableTarget(ev.target)) return;
                ev.preventDefault();
                mail.focusSearch();
            };
            document.addEventListener("keydown", handler);
            return () => document.removeEventListener("keydown", handler);
        },
        () => []
    );
}
