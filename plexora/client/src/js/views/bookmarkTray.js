/**
 * bookmarkTray.js - the bookmarks tray, its toolbar icon, and K.
 *
 * A floating glass tray along the bottom of the canvas (canvasLayout's
 * bottom-center dock, so the legend, the minimap, the cut panel and the
 * sections are never under it, and the chat's lane rises above it): a
 * horizontal row of the image's bookmarks (services/bookmarks.js), newest
 * first, each a thumbnail and a name. A click goes back to one; the one on
 * screen is ringed; hovering one enlarges it without moving its neighbours
 * (a transform). × takes the bookmark off -- the capture stays in Figure
 * Builder -- a double-click renames it, and ⋯ also offers "Show in Figure
 * Builder". `+ Save View` bookmarks what is on screen.
 *
 *   THE ICON (#viewer_bookmark_button, the sidebar header, beside the AI
 *   button) only opens and closes the tray. It shows only where bookmarks
 *   can be kept (a store registered).
 *
 *   K bookmarks the view from anywhere on the viewer, the tray open or not:
 *   not while typing, with a modifier, held, with a dialog or a confirm up,
 *   or when another handler took it first (Video Studio's K, in its editor).
 *
 *   SAVING SHOWS WHERE IT WENT: a brief flash over the view and a bookmark
 *   mark, then the view's own picture flies into its new place in the tray
 *   -- or, with the tray closed, into the icon, which pulses, with a short
 *   notice. Under reduced motion there is no flight: the new item or the
 *   icon is simply marked. The save never waits for any of it.
 *
 * The tray's open state is kept per tab (sessionStorage), as the cut panel's.
 *
 * Classic script, `window.PlexoraBookmarkTray`.
 */
(function (global) {
    "use strict";

    const OPEN_KEY = "plexora.bookmarkTray.open";
    const BUTTON_ID = "viewer_bookmark_button";
    const FLIGHT_MS = 420;
    const FLASH_MS = 320;

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

    function icon(name) {
        return el("span", `fas fa-${name}`, { "aria-hidden": "true" });
    }

    function typing() {
        if (global.PlexoraShortcuts?.isTyping) return global.PlexoraShortcuts.isTyping();
        const active = document.activeElement;
        const tag = active?.tagName;
        return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || Boolean(active?.isContentEditable);
    }

    function readOpen() {
        try {
            return global.sessionStorage?.getItem(OPEN_KEY) === "1";
        } catch (error) {
            return false;
        }
    }

    function writeOpen(open) {
        try {
            global.sessionStorage?.setItem(OPEN_KEY, open ? "1" : "0");
        } catch (error) {
            // A private window: the tray opens closed next time.
        }
    }

    /** Whether to move things: the viewer's own Motion setting, and the
     *  system's. */
    function smooth() {
        if (document.documentElement?.classList?.contains("plx-reduced-motion")) return false;
        const prefs = global.PlexoraViewerPrefs;
        if (prefs?.smoothMotion) return prefs.smoothMotion();
        return !global.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
    }

    /** Whether K, as the key event `event`, is the bookmark key. */
    function isBookmarkKey(event) {
        if (!event || event.repeat || event.defaultPrevented) return false;
        if (event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return false;
        if (String(event.key || "").toLowerCase() !== "k") return false;
        if (typing()) return false;
        if (document.querySelector("dialog[open]") || global.PlexoraConfirm?.modalOpen?.()) return false;
        return true;
    }

    class BookmarkTray {
        constructor({ bookmarks = global.PlexoraBookmarks, imageViewer = null, viewState = null } = {}) {
            this.bookmarks = bookmarks;
            this.imageViewer = imageViewer;
            this.viewState = viewState;
            this.root = null;
            this.button = null;
            this.items = new Map();       // bookmark id -> {node, img, label, entry}
            this.open = false;
            //: False while a routed page (Settings) is over the viewer: K is
            //: not the viewer's then (views/datasetNav.js does the same).
            this.viewerShown = true;
            this._off = [];
        }

        mount() {
            const wrapper = document.getElementById("openseadragon_wrapper");
            if (this.root || !wrapper || !this.bookmarks) return false;
            const root = el("section", "plx-bookmark-tray", {
                role: "region", "aria-label": "Bookmarks", "data-viewer-furniture": "true" });
            root.hidden = true;
            this.saveButton = el("button", "plx-bookmark-save", {
                type: "button", title: "Bookmark this view (K)", "aria-keyshortcuts": "K" });
            this.saveButton.append(icon("plus"), el("span", null, { text: "Save View" }));
            this.saveButton.addEventListener("click", () => this.save());
            this.list = el("ul", "plx-bookmark-list", { "aria-label": "Bookmarks" });
            this.empty = el("p", "plx-bookmark-empty", { text: "No bookmarks yet. Press K to keep this view." });
            const close = el("button", "plx-bookmark-close", {
                type: "button", title: "Close bookmarks", "aria-label": "Close bookmarks" });
            close.appendChild(icon("xmark"));
            close.addEventListener("click", () => this.setOpen(false, { focus: true }));
            root.append(this.saveButton, this.list, this.empty, close);
            root.addEventListener("keydown", (event) => {
                if (event.key !== "Escape" || typing()) return;
                event.preventDefault();
                event.stopPropagation();
                if (this.menu) this.closeMenu();
                else this.setOpen(false, { focus: true });
            });
            this.root = root;
            const layout = global.PlexoraCanvasLayout;
            const unplace = layout?.place?.(root, {
                corner: "bottom-center", order: 20, id: "bookmarks",
                // Short of room, the row folds away and the Save button stays.
                collapse: (folded) => root.classList.toggle("is-compact", folded),
            });
            if (!unplace) wrapper.appendChild(root);
            this._off.push(() => { unplace?.(); root.remove(); });

            this.button = document.getElementById(BUTTON_ID);
            if (this.button) {
                // Its one job: the tray open or closed.
                const toggle = () => this.setOpen(!this.open);
                this.button.addEventListener("click", toggle);
                this._off.push(() => this.button?.removeEventListener("click", toggle));
            }
            this._off.push(this.bookmarks.onChange((detail) => this.changed(detail)));

            const onKey = (event) => {
                if (!this.viewerShown || !this.bookmarks.available() || !isBookmarkKey(event)) return;
                event.preventDefault();
                this.save();
            };
            document.addEventListener("keydown", onKey);
            this._off.push(() => document.removeEventListener("keydown", onKey));
            for (const [name, shown] of [["plexora:viewer-hidden", false], ["plexora:viewer-shown", true]]) {
                const fn = () => { this.viewerShown = shown; };
                global.addEventListener?.(name, fn);
                this._off.push(() => global.removeEventListener?.(name, fn));
            }

            // The view moved: the ringed bookmark is not what is on screen any more.
            const moved = () => this.bookmarks.viewMoved();
            if (typeof this.imageViewer?.onViewportChange === "function") {
                this._off.push(this.imageViewer.onViewportChange(moved));
            }
            if (this.viewState?.subscribe) this._off.push(this.viewState.subscribe(moved));

            const outside = (event) => {
                if (this.menu && !this.menu.contains(event.target)) this.closeMenu();
            };
            document.addEventListener("pointerdown", outside, true);
            this._off.push(() => document.removeEventListener("pointerdown", outside, true));

            this.available();
            if (this.bookmarks.available() && readOpen()) this.setOpen(true);
            return true;
        }

        destroy() {
            for (const off of this._off.splice(0)) off?.();
            this.root = null;
        }

        /** The icon shows only where bookmarks can be kept. */
        available() {
            const can = this.bookmarks.available();
            if (this.button) this.button.hidden = !can;
            if (!can && this.open) this.setOpen(false);
            return can;
        }

        // -- open and closed ----------------------------------------------------

        setOpen(open, { focus = false } = {}) {
            if (!this.root) return;
            this.open = Boolean(open) && this.bookmarks.available();
            this.root.hidden = !this.open;
            this.button?.setAttribute("aria-pressed", String(this.open));
            this.button?.classList.toggle("is-active", this.open);
            writeOpen(this.open);
            if (!this.open) {
                this.closeMenu();
                if (focus) this.button?.focus?.();
                return;
            }
            this.paint();
            this.bookmarks.list().then(() => this.paint());
        }

        toggle() {
            this.setOpen(!this.open);
            return this.open;
        }

        changed(detail = {}) {
            if (detail.reason === "store") {
                this.available();
                if (readOpen()) this.setOpen(true);
            }
            if (this.open) this.paint();
        }

        // -- the row -----------------------------------------------------------

        paint() {
            if (!this.root || !this.open) return;
            const entries = this.bookmarks.cached();
            const active = this.bookmarks.active();
            const seen = new Set();
            let before = null;
            // Newest first, each node made once and moved only when its place
            // changes: a hundred bookmarks repaint as a hundred class flips.
            for (const entry of entries) {
                seen.add(entry.id);
                let item = this.items.get(entry.id);
                if (!item) {
                    item = this.item(entry);
                    this.items.set(entry.id, item);
                }
                item.entry = entry;
                this.fill(item);
                item.node.classList.toggle("is-active", entry.id === active);
                item.node.classList.toggle("is-pending", Boolean(entry.pending));
                const next = before ? before.nextSibling : this.list.firstChild;
                if (next !== item.node) this.list.insertBefore(item.node, next);
                before = item.node;
            }
            for (const [id, item] of this.items) {
                if (seen.has(id)) continue;
                item.node.remove();
                this.items.delete(id);
            }
            this.empty.hidden = entries.length > 0;
            this.list.hidden = entries.length === 0;
        }

        item(entry) {
            const node = el("li", "plx-bookmark-item", { "data-bookmark": entry.id });
            const open = el("button", "plx-bookmark-open", { type: "button" });
            const frame = el("span", "plx-bookmark-frame");
            const img = el("img", "plx-bookmark-thumb", { alt: "", loading: "lazy", decoding: "async",
                                                          draggable: "false" });
            const blank = el("span", "plx-bookmark-blank", { "aria-hidden": "true" });
            blank.appendChild(icon("bookmark"));
            frame.append(img, blank);
            const label = el("span", "plx-bookmark-label");
            open.append(frame, label);
            open.addEventListener("click", () => this.bookmarks.restore(entry.id).then(() => this.paint()));
            label.addEventListener("dblclick", (event) => {
                event.preventDefault();
                event.stopPropagation();
                this.rename(entry.id);
            });
            const remove = el("button", "plx-bookmark-remove", {
                type: "button", title: "Remove bookmark (the capture stays in Figure Builder)" });
            remove.appendChild(icon("xmark"));
            remove.addEventListener("click", (event) => {
                event.stopPropagation();
                this.bookmarks.unbookmark(entry.id);
            });
            const more = el("button", "plx-bookmark-more", { type: "button", title: "More",
                                                             "aria-label": "More", "aria-haspopup": "menu" });
            more.appendChild(icon("ellipsis"));
            more.addEventListener("click", (event) => {
                event.stopPropagation();
                this.openMenu(entry.id, more);
            });
            node.append(open, remove, more);
            return { node, open, img, blank, label, remove, entry };
        }

        fill(item) {
            const { entry } = item;
            if (item.label.textContent !== entry.label) item.label.textContent = entry.label;
            const title = `${entry.label}: go back to this view`;
            if (item.open.title !== title) {
                item.open.title = title;
                item.open.setAttribute("aria-label", title);
                item.remove.setAttribute("aria-label", `Remove the bookmark ${entry.label}`);
            }
            const src = entry.preview_url || "";
            if ((item.img.getAttribute("src") || "") !== src) {
                if (src) item.img.setAttribute("src", src);
                else item.img.removeAttribute("src");
            }
            item.img.hidden = !src;
            item.blank.hidden = Boolean(src);
        }

        rename(id) {
            const item = this.items.get(id);
            if (!item) return;
            const input = el("input", "plx-bookmark-rename", { type: "text", value: item.entry.label,
                                                               "aria-label": "Bookmark name" });
            input.maxLength = 64;
            let done = false;
            const finish = (keep) => {
                if (done) return;
                done = true;
                input.replaceWith(item.label);
                if (keep) this.bookmarks.rename(id, input.value);
                this.paint();
            };
            input.addEventListener("keydown", (event) => {
                event.stopPropagation();
                if (event.key === "Enter") finish(true);
                else if (event.key === "Escape") finish(false);
            });
            input.addEventListener("blur", () => finish(true));
            item.label.replaceWith(input);
            input.focus();
            input.select?.();
        }

        // -- the item menu -------------------------------------------------------

        openMenu(id, anchor) {
            this.closeMenu();
            const menu = el("div", "plx-bookmark-menu", { role: "menu" });
            const row = (text, glyph, run) => {
                const button = el("button", "plx-bookmark-menu-item", { type: "button", role: "menuitem" });
                button.append(icon(glyph), el("span", null, { text }));
                button.addEventListener("click", () => {
                    this.closeMenu();
                    run();
                });
                menu.appendChild(button);
                return button;
            };
            const first = row("Rename", "pen", () => this.rename(id));
            const url = this.bookmarks.locateUrl(id);
            if (url) row("Show in Figure Builder", "arrow-up-right-from-square", () => global.open?.(url, "_blank", "noopener"));
            row("Remove bookmark", "xmark", () => this.bookmarks.unbookmark(id));
            this.root.appendChild(menu);
            const box = this.root.getBoundingClientRect();
            const at = anchor.getBoundingClientRect();
            menu.style.left = `${Math.max(0, at.left - box.left - 8)}px`;
            this.menu = menu;
            first.focus?.();
        }

        closeMenu() {
            this.menu?.remove();
            this.menu = null;
        }

        // -- saving --------------------------------------------------------------

        /** Bookmark the view, and show where it went. */
        save() {
            const made = this.bookmarks.bookmark();
            if (!made) return null;
            if (this.open) this.paint();
            this.celebrate(made);
            return made;
        }

        async celebrate(made) {
            const wrapper = document.getElementById("openseadragon_wrapper");
            const moving = smooth();
            if (wrapper && moving) {
                const flash = el("div", "plx-bookmark-flash", { "aria-hidden": "true" });
                const mark = el("div", "plx-bookmark-mark", { "aria-hidden": "true" });
                mark.appendChild(icon("bookmark"));
                wrapper.append(flash, mark);
                setTimeout(() => { flash.remove(); mark.remove(); }, FLASH_MS + 400);
            }
            const target = this.open ? this.items.get(made.id)?.node : this.button;
            if (!this.open) {
                global.PlexoraToast?.show?.({ title: `${made.entry.label} bookmarked`,
                                              note: "Press the bookmark icon to see your bookmarks.",
                                              timeout: 2200 });
            }
            const picture = moving ? await made.preview.catch(() => null) : null;
            if (picture?.blob && target && wrapper) await this.fly(picture.blob, wrapper, target);
            this.emphasise(this.open ? this.items.get(made.id)?.node : this.button);
        }

        /** The view's picture, from where the view is to `target`. */
        fly(blob, wrapper, target) {
            const to = target.getBoundingClientRect();
            const from = wrapper.getBoundingClientRect();
            if (!to.width || !from.width || typeof global.URL?.createObjectURL !== "function") {
                return Promise.resolve();
            }
            const url = global.URL.createObjectURL(blob);
            const img = el("img", "plx-bookmark-flight", { alt: "", src: url, "aria-hidden": "true" });
            Object.assign(img.style, { left: `${from.left}px`, top: `${from.top}px`,
                                       width: `${from.width}px`, height: `${from.height}px` });
            document.body.appendChild(img);
            const dx = to.left + to.width / 2 - (from.left + from.width / 2);
            const dy = to.top + to.height / 2 - (from.top + from.height / 2);
            const scale = Math.max(0.02, Math.min(to.width / from.width, to.height / from.height));
            const done = () => {
                img.remove();
                global.URL.revokeObjectURL?.(url);
            };
            if (typeof img.animate !== "function") {
                done();
                return Promise.resolve();
            }
            const animation = img.animate([
                { transform: "translate(0, 0) scale(1)", opacity: 0.95, borderRadius: "4px" },
                { transform: `translate(${dx}px, ${dy}px) scale(${scale})`, opacity: 0.9, borderRadius: "24px" },
            ], { duration: FLIGHT_MS, easing: "cubic-bezier(0.2, 0, 0, 1)", fill: "forwards" });
            return animation.finished.then(done, done);
        }

        /** Where the bookmark landed, marked for a moment. */
        emphasise(node) {
            if (!node) return;
            node.classList.remove("is-arrived");
            void node.offsetWidth;
            node.classList.add("is-arrived");
            setTimeout(() => node.classList.remove("is-arrived"), 900);
        }
    }

    function attach(options = {}) {
        const tray = new BookmarkTray(options);
        if (!tray.mount()) return null;
        api.instance = tray;
        return tray;
    }

    const api = {
        BookmarkTray, attach, isBookmarkKey, instance: null,
        toggle: () => Boolean(api.instance?.toggle()),
        save: () => api.instance?.save() || null,
    };
    global.PlexoraBookmarkTray = api;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = api;
    }
})(typeof window !== "undefined" ? window : globalThis);
