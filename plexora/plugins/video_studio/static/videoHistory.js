/**
 * videoHistory.js - undo and redo for a video's edits.
 *
 * An entry is a pair of operation batches, `{label, forward, inverse}`: the
 * server applies the forward batch and answers with its inverse
 * (server/operations.py), so the stack never needs to hold or diff whole
 * documents. Undo hands back the inverse to apply; redo the forward. The
 * caller applies them through the same `ops` route, and replaces the entry's
 * batches with what the server answered, so a redo after an undo is the exact
 * inverse of the inverse.
 *
 * Classic script: `window.PlexoraVideoHistory`, and `module.exports`.
 */
(function (global) {
    "use strict";

    const LIMIT = 200;

    function create({ limit = LIMIT, onChange = null } = {}) {
        const done = [];
        const undone = [];
        const changed = () => onChange?.({ canUndo: done.length > 0, canRedo: undone.length > 0,
                                          undoLabel: done.at(-1)?.label || "",
                                          redoLabel: undone.at(-1)?.label || "" });

        return {
            /** Record an applied edit. Clears the redo branch. */
            push(entry) {
                if (!entry || !Array.isArray(entry.forward) || !Array.isArray(entry.inverse)) return;
                done.push({ label: entry.label || "Edit", forward: entry.forward,
                            inverse: entry.inverse });
                if (done.length > limit) done.shift();
                undone.length = 0;
                changed();
            },
            /** The entry to undo (its `inverse` is what to apply), or null. */
            undo() {
                const entry = done.pop();
                if (!entry) return null;
                undone.push(entry);
                changed();
                return entry;
            },
            redo() {
                const entry = undone.pop();
                if (!entry) return null;
                done.push(entry);
                changed();
                return entry;
            },
            /** After applying an undo or redo, store what the server said the
             *  new inverse is (for undo: the forward to redo with). */
            settle(entry, { forward, inverse }) {
                if (forward) entry.forward = forward;
                if (inverse) entry.inverse = inverse;
            },
            /** Put an entry back where it came from when applying it failed. */
            restore(entry, from) {
                const source = from === "undo" ? undone : done;
                const target = from === "undo" ? done : undone;
                const index = source.lastIndexOf(entry);
                if (index >= 0) source.splice(index, 1);
                target.push(entry);
                changed();
            },
            clear() {
                done.length = 0;
                undone.length = 0;
                changed();
            },
            get canUndo() { return done.length > 0; },
            get canRedo() { return undone.length > 0; },
            get size() { return done.length; },
        };
    }

    const api = { create, LIMIT };
    global.PlexoraVideoHistory = api;
    if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
