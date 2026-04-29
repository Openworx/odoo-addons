/** @odoo-module **/

import { Component, useState, useRef } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

/**
 * Text input component that parses comma-separated `"Name" <email>` recipient
 * strings and provides server-side autocomplete suggestions.
 *
 * The component is stateless with respect to the current value — it receives
 * `value` as a prop and notifies the parent of changes via the `onUpdate` prop
 * callback (parent-owns-state pattern). Suggestions are fetched from the
 * server via `this.mail.suggestRecipients()` and displayed in a dropdown.
 *
 * An `update` event is not a native OWL event; it is surfaced as the
 * `onUpdate` function prop so that the parent can update its own state
 * directly without an OWL event bus.
 */
export class RecipientInput extends Component {
    static template = "ow_mail.RecipientInput";
    static props = {
        placeholder: String,
        value: String,
        onUpdate: Function,
    };

    /**
     * OWL lifecycle setup hook.
     *
     * Hooks used: `useService` (ow_mail service), `useRef` (native input
     * element), `useState` (component-local suggestions/dropdown state).
     *
     * Suggestions are fetched asynchronously via
     * `this.mail.suggestRecipients(query)` — a debounced call initiated by
     * `_scheduleFetch`.
     */
    setup() {
        this.mail = useService("ow_mail");
        this.inputRef = useRef("input");
        this.local = useState({
            suggestions: [],
            open: false,
            highlight: 0,
            timer: null,
        });
    }

    /**
     * Extract the last comma-separated token from the input value.
     *
     * Used to feed the autocomplete query: only the token currently being
     * typed (after the last comma) is sent to `suggestRecipients`.
     *
     * @param {string} value - Full current input value.
     * @returns {string} The trimmed last token, or the full trimmed value if
     *   there are no commas.
     */
    _lastToken(value) {
        const idx = value.lastIndexOf(",");
        return idx >= 0 ? value.slice(idx + 1).trim() : value.trim();
    }

    /**
     * Replace the last comma-separated token with a completed suggestion.
     *
     * A trailing `", "` (comma + space) is appended so that the user can
     * immediately start typing the next recipient without manually inserting
     * a separator.
     *
     * @param {string} value - Full current input value.
     * @param {string} replacement - Formatted suggestion string to substitute.
     * @returns {string} Updated input value with the last token replaced.
     */
    _replaceLastToken(value, replacement) {
        const idx = value.lastIndexOf(",");
        if (idx < 0) return replacement + ", ";
        return value.slice(0, idx + 1) + " " + replacement + ", ";
    }

    /**
     * Format a suggestion object as an RFC 5322 address string.
     *
     * If the suggestion has a distinct display name, returns
     * `"Name" <email>`; otherwise returns just `email`.
     *
     * @param {{ name: string, email: string }} s - Suggestion object from the server.
     * @returns {string} Formatted address string.
     */
    _formatSuggestion(s) {
        return s.name && s.name !== s.email ? `"${s.name}" <${s.email}>` : s.email;
    }

    /**
     * Schedule a server-side recipient fetch after a 150 ms debounce.
     *
     * Any previously scheduled timer is cancelled on each call so that only
     * the fetch for the most recent keystroke is actually sent. On success,
     * updates `local.suggestions`, opens the dropdown if there are results,
     * and resets the highlight index to 0.
     *
     * @param {string} query - The current token being typed (output of `_lastToken`).
     */
    _scheduleFetch(query) {
        if (this.local.timer) clearTimeout(this.local.timer);
        this.local.timer = setTimeout(async () => {
            const results = await this.mail.suggestRecipients(query);
            this.local.suggestions = results;
            this.local.open = results.length > 0;
            this.local.highlight = 0;
        }, 150);
    }

    /**
     * Handle the `input` event: propagate the new value to the parent and
     * schedule an autocomplete fetch for the last token.
     *
     * Clears suggestions immediately if the current token is empty (the user
     * deleted back past a comma separator).
     *
     * @param {InputEvent} ev
     */
    onInput(ev) {
        const v = ev.target.value;
        this.props.onUpdate(v);
        const token = this._lastToken(v);
        if (token.length < 1) {
            this.local.suggestions = [];
            this.local.open = false;
            return;
        }
        this._scheduleFetch(token);
    }

    /**
     * Handle the `focus` event: show suggestions if the last token is
     * non-empty and a fetch hasn't already populated the dropdown.
     *
     * @param {FocusEvent} ev
     */
    onFocus(ev) {
        const token = this._lastToken(ev.target.value || "");
        if (token.length >= 1) this._scheduleFetch(token);
    }

    /**
     * Handle the `blur` event: hide the dropdown after a 150 ms delay.
     *
     * The delay allows a `click` event on a suggestion item to fire and
     * complete (`pick`) before the dropdown is hidden. Without the delay the
     * dropdown would disappear on `blur` before the click is processed,
     * making it impossible to select a suggestion with the mouse.
     */
    onBlur() {
        setTimeout(() => { this.local.open = false; }, 150);
    }

    /**
     * Handle keyboard navigation inside the dropdown.
     *
     * - `ArrowDown` — move highlight down (wraps around).
     * - `ArrowUp` — move highlight up (wraps around).
     * - `Enter` / `Tab` — pick the currently highlighted suggestion (or the
     *   first one if highlight is 0).
     * - `Escape` — close the dropdown without picking.
     *
     * Does nothing if the dropdown is closed or there are no suggestions.
     *
     * @param {KeyboardEvent} ev
     */
    onKeyDown(ev) {
        if (!this.local.open || !this.local.suggestions.length) return;
        if (ev.key === "ArrowDown") {
            ev.preventDefault();
            this.local.highlight =
                (this.local.highlight + 1) % this.local.suggestions.length;
        } else if (ev.key === "ArrowUp") {
            ev.preventDefault();
            const n = this.local.suggestions.length;
            this.local.highlight = (this.local.highlight - 1 + n) % n;
        } else if (ev.key === "Enter" || ev.key === "Tab") {
            ev.preventDefault();
            this.pick(this.local.suggestions[this.local.highlight]);
        } else if (ev.key === "Escape") {
            this.local.open = false;
        }
    }

    /**
     * Complete the current token with the selected suggestion.
     *
     * Replaces the last token in the input with the formatted suggestion via
     * `_replaceLastToken`, notifies the parent via `onUpdate`, closes the
     * dropdown, and returns focus to the input so the user can continue
     * typing recipients.
     *
     * @param {{ name: string, email: string }} s - Suggestion to pick.
     */
    pick(s) {
        if (!s) return;
        const current = this.props.value || "";
        const replacement = this._formatSuggestion(s);
        const next = this._replaceLastToken(current, replacement);
        this.props.onUpdate(next);
        this.local.suggestions = [];
        this.local.open = false;
        if (this.inputRef.el) this.inputRef.el.focus();
    }
}
