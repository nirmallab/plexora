/**
 * keyboardShortcuts.js - app-level accelerators for the navbar.
 *
 * ONE mechanism drives two things that are usually written twice: what the menu
 * PRINTS beside a row, and what happens when the key is pressed. Both come from
 * a single `data-shortcut="mod+e"` attribute on the row itself, so a shortcut
 * cannot end up displayed as one key and bound to another -- which is the
 * failure mode of every hand-maintained accelerator table.
 *
 * The binding is a synthetic click on the element that declared it. That is
 * deliberately dumb: every row in the navbar already does the right thing when
 * clicked (a link navigates, the Tools links are intercepted by toolLoader, a
 * checkbox toggles and fires change), so there is nothing for this file to know
 * about what any particular shortcut MEANS. Adding a shortcut to a new row is
 * one attribute in the template and no JavaScript at all.
 *
 * `mod` resolves to Cmd on a Mac and Ctrl everywhere else, and is resolved HERE
 * rather than in the descriptor because one descriptor is served to every
 * client. The same split decides the printed form: "⌘E" on a Mac, "Ctrl+E"
 * elsewhere.
 *
 * NOT the same thing as a plugin's own keys. ROI's v/p/f/r and Figure Builder's
 * C/S are bare letters bound against the canvas while that plugin is on screen,
 * and they stay each plugin's business -- see roiTools.keyDown. These are
 * modified chords that work from anywhere, which is why they are core's.
 */
window.PlexoraShortcuts = (function () {
    // navigator.platform is deprecated but is still the only signal that works
    // in every browser this app runs in; userAgentData is Chromium-only. Read
    // once -- the answer cannot change while the page is open.
    const IS_MAC = /mac|iphone|ipad/i.test(
        navigator.userAgentData?.platform || navigator.platform || "");

    //: How each token prints. On a Mac the glyphs are the convention and carry
    //: no separator; elsewhere the words are, joined by "+".
    const GLYPHS = IS_MAC
        ? { mod: "⌘", shift: "⇧", alt: "⌥", sep: "" }
        : { mod: "Ctrl", shift: "Shift", alt: "Alt", sep: "+" };

    //: Print order, which is NOT the canonical spec order and differs by
    //: platform. Apple's is Control-Option-Shift-Command with Command adjacent
    //: to the key, so "⇧⌘Z" and never "⌘⇧Z"; Windows and Linux lead with Ctrl.
    //: Only the printed form moves -- the spec that plugins declare and that
    //: `bound` is keyed by stays in one order on every platform, which is the
    //: whole reason those are two separate lists.
    const PRINT_ORDER = IS_MAC ? ["alt", "shift", "mod"] : ["mod", "shift", "alt"];

    //: Punctuation whose glyph is worth more than its character. Everything
    //: else prints as itself, uppercased.
    const KEY_NAMES = { ",": ",", ".": ".", "\\": "\\", "[": "[", "]": "]" };

    //: normalized spec -> { element, label }. First registration wins; see
    //: register(). Mirrors the server-side clash warning in plugins.py.
    const bound = new Map();

    /** Canonical form, so "mod+E" and "MOD+e" are one shortcut. Modifier order
     *  is fixed by the server (plugin.normalize_shortcut); this repeats the
     *  sort so a hand-written attribute in a core template cannot drift. */
    function normalize(spec) {
        const parts = String(spec || "").toLowerCase().split("+")
            .map((part) => part.trim()).filter(Boolean);
        if (parts.length < 2) return "";
        const key = parts[parts.length - 1];
        const held = new Set(parts.slice(0, -1));
        const ordered = ["mod", "shift", "alt"].filter((mod) => held.has(mod));
        if (ordered.length !== held.size) return "";
        return [...ordered, key].join("+");
    }

    /** The printed form for this platform, e.g. "⌘E" or "Ctrl+Shift+E". */
    function format(spec) {
        const normalized = normalize(spec);
        if (!normalized) return "";
        const parts = normalized.split("+");
        const key = parts.pop();
        const printedKey = KEY_NAMES[key] || key.toUpperCase();
        const held = new Set(parts);
        const mods = PRINT_ORDER.filter((mod) => held.has(mod)).map((mod) => GLYPHS[mod]);
        return [...mods, printedKey].join(GLYPHS.sep);
    }

    /** The spec this event represents, or "" for a keystroke with no modifier
     *  we care about. `metaKey` on a Mac and `ctrlKey` elsewhere, never both:
     *  Ctrl+E on a Mac is a text-editing binding (move to end of line) and
     *  stealing it would break every field on the page. */
    function specFor(event) {
        const mod = IS_MAC ? event.metaKey : event.ctrlKey;
        if (!mod) return "";
        // The wrong-platform modifier disqualifies the stroke rather than being
        // ignored: Cmd+Ctrl+E is not Cmd+E and must not fire it.
        if (IS_MAC ? event.ctrlKey : event.metaKey) return "";
        const key = String(event.key || "").toLowerCase();
        // Length check, not isalnum: `event.key` is "Shift" for the modifier
        // itself and "ArrowLeft" for a cursor key, and neither is a shortcut.
        if (key.length !== 1) return "";
        const parts = ["mod"];
        if (event.shiftKey) parts.push("shift");
        if (event.altKey) parts.push("alt");
        parts.push(key);
        return normalize(parts.join("+"));
    }

    /**
     * Whether a keystroke is the user talking to the app rather than typing.
     *
     * Same guard as roiTools.acceptsKeys, and for the same reason: a project
     * named "Bright" must not open Thresholding on its B. Modified chords are
     * less exposed to this than bare letters -- nobody types Ctrl+B into a name
     * field by accident -- but a text input may well bind it for bold, and a
     * field that has focus outranks a menu that does not.
     */
    function isTyping() {
        const active = document.activeElement;
        if (!active) return false;
        const tag = active.tagName;
        return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT"
            || active.isContentEditable;
    }

    /** Whether this row can be triggered at all. A disabled menu item prints
     *  its key greyed and does nothing, which is what a disabled row in any
     *  desktop menu does -- the alternative, firing anyway, is a shortcut that
     *  ignores the state its own menu is showing. */
    function isEnabled(element) {
        if (element.disabled) return false;
        if (element.classList.contains("disabled")) return false;
        if (element.getAttribute("aria-disabled") === "true") return false;
        // A checkbox row is the LABEL, so the state that matters is the input's.
        const input = element.querySelector("input");
        return !(input && input.disabled);
    }

    /**
     * Bind one element's declared shortcut and print it into the row.
     *
     * First registration wins, and the loser keeps its label but prints no key:
     * two rows showing the same chord, only one of which responds to it, is a
     * worse outcome than one row quietly not offering a shortcut. The server
     * warns about the plugin half of this at install time (plugins.py), where
     * the plugin names are known and can be reported.
     */
    function register(element) {
        const spec = normalize(element.getAttribute("data-shortcut"));
        if (!spec) return;
        let existing = bound.get(spec);
        // A binding whose element has left the document is not a competing claim
        // to report, it is a page that has been swapped out (appRouter.js) --
        // and treating it as one would refuse every shortcut on the page that
        // replaced it, since handleKeydown already declines to fire at a
        // detached element. So the claim lapses with the element.
        if (existing && !document.contains(existing.element)) {
            bound.delete(spec);
            existing = undefined;
        }
        if (existing && existing.element !== element) {
            console.warn(`PlexoraShortcuts: ${format(spec)} is already bound to `
                + `"${existing.label}"; ignoring "${labelOf(element)}"`);
            return;
        }
        bound.set(spec, { element, label: labelOf(element) });
        paintKey(element, spec);
    }

    function labelOf(element) {
        return (element.querySelector(".nav-item-label")?.textContent
            || element.textContent || "").trim();
    }

    /**
     * Write the printed key into the row.
     *
     * Into an existing `.nav-item-key` if the template left one, and otherwise
     * appended: a row that only wants a shortcut should not have to grow a span
     * in the markup to get one. The text is set rather than the markup, so a
     * label can never arrive here as HTML.
     */
    function paintKey(element, spec) {
        let key = element.querySelector(".nav-item-key");
        if (!key) {
            key = document.createElement("span");
            key.className = "nav-item-key";
            element.appendChild(key);
        }
        key.textContent = format(spec);
    }

    function handleKeydown(event) {
        if (isTyping()) return;
        const entry = bound.get(specFor(event));
        if (!entry || !document.contains(entry.element)) return;
        // preventDefault before the enabled check, not after: the whole point of
        // choosing releasable keys is that the browser's own action is
        // suppressed, and suppressing it only when the row happens to be enabled
        // would make Ctrl+O open a file dialog on exactly the pages where the
        // menu item is greyed out.
        event.preventDefault();
        if (!isEnabled(entry.element)) return;
        entry.element.click();
    }

    /**
     * Bind every `[data-shortcut]` currently in the document.
     *
     * Re-runnable and idempotent -- re-registering the same element is a no-op
     * rather than a clash -- so a menu that grows a row later (a plugin's nav
     * entry, a tool list rebuilt after a project opens) can call this again
     * without knowing what was already bound.
     */
    function scan(root = document) {
        root.querySelectorAll("[data-shortcut]").forEach(register);
    }

    document.addEventListener("keydown", handleKeydown);

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", () => scan());
    } else {
        scan();
    }

    return { scan, register, format, normalize, isMac: () => IS_MAC };
})();
