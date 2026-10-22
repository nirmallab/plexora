/**
 * backButton.js - the Back pill and B: back to the view a derived view came
 * from (services/viewHistory.js).
 *
 * The pill floats at the top centre of the canvas (canvasLayout's top-center
 * dock, so it never lands on another control) and shows only while there is
 * somewhere to go back to. B does the same from the keyboard:
 *
 *   PRECEDENCE. B is also the dataset chip's "previous sample"
 *   (views/datasetNav.js). While there is a view to go back to, Back wins:
 *   this listens in the capture phase and says so with preventDefault, which
 *   the chip respects. With no history, B walks the samples as before.
 *
 * Not while typing, with a modifier, with a dialog or a confirm up.
 *
 * Classic script, `window.PlexoraBackButton`.
 */
(function (global) {
    "use strict";

    function el(tag, className, attrs = {}) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        for (const [key, value] of Object.entries(attrs)) {
            if (value === null || value === undefined) continue;
            if (key === "text") node.textContent = value;
            else node.setAttribute(key, value);
        }
        return node;
    }

    function typing() {
        if (global.PlexoraShortcuts?.isTyping) return global.PlexoraShortcuts.isTyping();
        const active = document.activeElement;
        const tag = active?.tagName;
        return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || Boolean(active?.isContentEditable);
    }

    /** Whether B, as the key event `event`, is Back's to take. */
    function isBackKey(event) {
        if (!event || event.repeat || event.defaultPrevented) return false;
        if (event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return false;
        if (String(event.key || "").toLowerCase() !== "b") return false;
        if (typing()) return false;
        if (document.querySelector("dialog[open]") || global.PlexoraConfirm?.modalOpen?.()) return false;
        return true;
    }

    class BackButton {
        constructor({ history } = {}) {
            //: The view history (services/viewHistory.js ViewHistory).
            this.history = history;
            this.root = null;
            this._off = [];
        }

        mount() {
            if (this.root || !this.history) return this;
            const root = el("button", "plx-view-back", {
                type: "button", title: "Back to the previous view (B)",
                "aria-label": "Back to the previous view", "aria-keyshortcuts": "B",
                "data-viewer-furniture": "true",
            });
            root.append(el("span", "fas fa-arrow-left", { "aria-hidden": "true" }),
                        el("span", "plx-view-back-label", { text: "Back" }),
                        el("kbd", "plx-view-back-key", { text: "B", "aria-hidden": "true" }));
            root.hidden = true;
            root.addEventListener("click", () => this.history.back());
            this.root = root;
            const layout = global.PlexoraCanvasLayout;
            const unplace = layout?.place?.(root, { corner: "top-center", order: 5, id: "view-back" });
            if (!unplace) document.getElementById("openseadragon_wrapper")?.appendChild(root);
            this._off.push(() => { unplace?.(); root.remove(); });
            this._off.push(this.history.onChange(() => this.paint()));
            const onKey = (event) => {
                if (!this.history.canGoBack() || !isBackKey(event)) return;
                event.preventDefault();
                this.history.back();
            };
            // Capture: before the dataset chip's B (views/datasetNav.js).
            document.addEventListener("keydown", onKey, true);
            this._off.push(() => document.removeEventListener("keydown", onKey, true));
            this.paint();
            return this;
        }

        paint() {
            if (!this.root) return;
            // The dock sees its size change (canvasLayout's ResizeObserver).
            this.root.hidden = !this.history.canGoBack();
        }

        destroy() {
            for (const off of this._off.splice(0)) off?.();
            this.root = null;
        }
    }

    function attach(options = {}) {
        if (!options.history) return null;
        const button = new BackButton(options).mount();
        api.instance = button;
        return button;
    }

    const api = { BackButton, attach, isBackKey, instance: null };
    global.PlexoraBackButton = api;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = api;
    }
})(typeof window !== "undefined" ? window : globalThis);
