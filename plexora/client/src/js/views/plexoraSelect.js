/**
 * PlexoraSelect - every dropdown in the app drawn by Plexora rather than by
 * the operating system.
 *
 * A native <select> opens the system's list: its own background, its own
 * blue highlight, no search, and a look that changes from one OS to the
 * next. This upgrades every `<select>` on every page where it stands -- the
 * select stays in the DOM, hidden, and remains the one source of truth; a
 * button beside it shows the chosen option, and a list opens under it.
 * Choosing sets `select.value` and dispatches `input` and `change` on the
 * select, exactly as a native choice does, so no code that reads a select
 * had to change. A list of eight or more choices opens with a search field,
 * filtered as one types.
 *
 * The pattern is the Figure Builder's (plugins/figure_builder/static/
 * figureSelect.js), which already did this inside the builder and is left to
 * do so there; this is the same thing for the rest of the app, with search.
 *
 *   - A MutationObserver upgrades a select when it appears: sidebars and
 *     dialogs are redrawn by replacing their HTML.
 *   - `select.value = x` from code fires nothing; the instance's own
 *     accessors catch it, so the button never shows a stale answer. Options
 *     added, removed or relabelled, `disabled`, `hidden` and class changes
 *     are followed too: the button wears the select's classes, so a page's
 *     sizing and its show/hide classes apply to it.
 *   - The list lives in the top layer (the Popover API) where the browser has
 *     it -- above a modal dialog, outside any clipping -- and takes the
 *     colours of the place it opened from (the light notebook, the dark
 *     viewer), read from the theme tokens at the button.
 *
 * Left alone: `multiple` and `size > 1` lists (they are lists, not menus),
 * a select marked `data-native`, and one another upgrader already took (the
 * Figure Builder's, the SCIMAP notebook's).
 *
 * Classic script, `window.PlexoraSelect`.
 */
(function (global) {
    "use strict";

    const SEARCH_AT = 8;
    const CLOSE_MS = 110;
    const THEME_TOKENS = ["--surface-1", "--surface-2", "--surface-3", "--text-primary", "--text-secondary",
        "--text-muted", "--border-subtle", "--border-strong", "--accent-channel"];
    let current = null;
    let started = false;
    let pending = new Set();
    let frame = 0;

    function taken(select) {
        return select.plexoraSelect || select.fbSelect || select.dataset.smPicked || select.dataset.native !== undefined ||
            select.classList.contains("fb-dd-native") || select.classList.contains("sm-pick-native");
    }

    function eligible(select) {
        return select && select.tagName === "SELECT" && !select.multiple && !(select.size > 1) && !taken(select);
    }

    /** Upgrades wait a frame, so a plugin's own upgrader (which runs when the
     *  select is built) gets there first. */
    function queue(select) {
        pending.add(select);
        if (frame) return;
        frame = global.requestAnimationFrame(() => {
            frame = 0;
            const batch = pending;
            pending = new Set();
            batch.forEach((s) => { if (eligible(s) && s.isConnected) s.plexoraSelect = new Dropdown(s); });
        });
    }

    function escapeHtml(text) {
        return String(text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;" })[c]);
    }

    class Dropdown {
        constructor(select) {
            this.select = select;
            this.list = null;
            this.active = -1;
            this.typed = "";
            this.typedAt = 0;
            this.query = "";

            const button = global.document.createElement("button");
            button.type = "button";
            button.setAttribute("aria-haspopup", "listbox");
            button.setAttribute("aria-expanded", "false");
            const label = select.getAttribute("aria-label") || select.title;
            if (label) button.setAttribute("aria-label", label);
            if (select.title) button.title = select.title;
            const labelledBy = select.getAttribute("aria-labelledby");
            if (labelledBy) button.setAttribute("aria-labelledby", labelledBy);
            button.innerHTML = '<span class="plx-dd-value"></span>' +
                '<svg class="plx-dd-chev" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>';
            if (select.style.width) button.style.width = select.style.width;
            if (select.style.minWidth) button.style.minWidth = select.style.minWidth;
            if (select.style.maxWidth) button.style.maxWidth = select.style.maxWidth;
            this.button = button;
            select.classList.add("plx-dd-native");
            select.tabIndex = -1;
            select.setAttribute("aria-hidden", "true");
            select.after(button);

            // A label pointing at the select now points at the button.
            if (select.id) {
                const id = global.CSS && global.CSS.escape ? global.CSS.escape(select.id) : select.id;
                for (const target of global.document.querySelectorAll(`label[for="${id}"]`)) {
                    target.addEventListener("click", (event) => { event.preventDefault(); button.focus(); });
                }
            }
            // Code that focuses the select means the control.
            select.focus = (options) => button.focus(options);

            button.addEventListener("click", () => (this.list ? this.close() : this.open()));
            button.addEventListener("keydown", (event) => this.buttonKey(event));
            select.addEventListener("change", () => this.refresh());
            this.watch();
            new MutationObserver(() => this.refresh())
                .observe(select, { childList: true, subtree: true, attributes: true, characterData: true });
            this.refresh();
        }

        watch() {
            const select = this.select;
            const self = this;
            for (const key of ["value", "selectedIndex", "disabled"]) {
                const proto = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, key);
                if (!proto || !proto.set) continue;
                Object.defineProperty(select, key, {
                    configurable: true,
                    get() { return proto.get.call(this); },
                    set(value) { proto.set.call(this, value); self.refresh(); },
                });
            }
        }

        options() {
            return Array.from(this.select.options);
        }

        refresh() {
            const select = this.select;
            if (!select.isConnected) {
                // The select went: its button goes with it.
                if (this.button.isConnected && !select.parentNode) this.button.remove();
                return;
            }
            const classes = Array.from(select.classList).filter((c) => c !== "plx-dd-native");
            const open = this.button.classList.contains("is-open");
            this.button.className = classes.concat(["plx-dd"], open ? ["is-open"] : []).join(" ");
            const chosen = select.options[select.selectedIndex];
            const value = this.button.querySelector(".plx-dd-value");
            value.textContent = chosen ? chosen.textContent.trim() : (select.getAttribute("placeholder") || "");
            value.classList.toggle("is-empty", !chosen || chosen.value === "");
            this.button.disabled = select.disabled;
            this.button.hidden = select.hidden || select.style.display === "none";
            if (select.style.visibility) this.button.style.visibility = select.style.visibility;
        }

        /* -- the list ------------------------------------------------------ */

        open() {
            if (this.select.disabled) return;
            if (current) current.close(true);
            current = this;
            this.query = "";

            const list = global.document.createElement("div");
            list.className = "plx-dd-list";
            list.setAttribute("role", "listbox");
            list.tabIndex = -1;
            const usePopover = typeof list.showPopover === "function";
            if (usePopover) list.setAttribute("popover", "manual");
            // The colours of where it opened: the light notebook, the dark viewer.
            const style = global.getComputedStyle(this.button);
            for (const token of THEME_TOKENS) {
                const v = style.getPropertyValue(token);
                if (v && v.trim()) list.style.setProperty(token, v.trim());
            }
            const searchable = this.enabledIndexes().length >= SEARCH_AT;
            list.innerHTML = (searchable ? '<label class="plx-dd-search"><span class="plx-dd-glass" aria-hidden="true"></span>' +
                '<input type="text" autocomplete="off" spellcheck="false" placeholder="Search…" aria-label="Search the choices"></label>' : "") +
                '<div class="plx-dd-items"></div><div class="plx-dd-foot" hidden></div>';
            this.items = list.querySelector(".plx-dd-items");
            this.foot = list.querySelector(".plx-dd-foot");
            this.input = list.querySelector("input");
            const home = usePopover ? null : this.button.closest("dialog");
            (home || global.document.body).appendChild(list);
            if (usePopover) list.showPopover();
            this.list = list;
            this.paintItems();
            this.place();
            this.active = this.select.selectedIndex;
            this.mark(false);
            const selected = list.querySelector(".is-selected");
            if (selected) selected.scrollIntoView({ block: "nearest" });
            global.requestAnimationFrame(() => list.classList.add("is-open"));

            this.button.setAttribute("aria-expanded", "true");
            this.button.classList.add("is-open");

            list.addEventListener("pointerdown", (event) => {
                if (!event.target.closest("input")) event.preventDefault();
            });
            list.addEventListener("click", (event) => {
                const item = event.target.closest("[data-index]");
                if (item && !item.classList.contains("is-disabled")) this.choose(Number(item.dataset.index));
            });
            list.addEventListener("pointermove", (event) => {
                const item = event.target.closest("[data-index]");
                if (item && Number(item.dataset.index) !== this.active) {
                    this.active = Number(item.dataset.index);
                    this.mark(false);
                }
            });
            if (this.input) {
                this.input.addEventListener("input", () => {
                    this.query = this.input.value.trim().toLowerCase();
                    this.paintItems();
                    const first = this.visibleIndexes()[0];
                    this.active = first === undefined ? -1 : first;
                    this.mark(true);
                });
                this.input.addEventListener("keydown", (event) => this.listKey(event));
                global.requestAnimationFrame(() => this.input && this.input.focus({ preventScroll: true }));
            }
            this.outside = (event) => {
                if (!list.contains(event.target) && !this.button.contains(event.target)) this.close();
            };
            global.document.addEventListener("pointerdown", this.outside, true);
        }

        paintItems() {
            const tick = '<svg class="plx-dd-tick" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
            const q = this.query;
            let html = "";
            let group = null;
            let shown = 0;
            this.options().forEach((option, index) => {
                if (option.hidden) return;
                const text = option.textContent.trim();
                if (q && !text.toLowerCase().includes(q)) return;
                const parent = option.parentElement;
                if (parent && parent.tagName === "OPTGROUP" && parent !== group) {
                    group = parent;
                    html += `<div class="plx-dd-group">${escapeHtml(parent.label)}</div>`;
                }
                const classes = ["plx-dd-item"];
                if (index === this.select.selectedIndex) classes.push("is-selected");
                if (option.disabled || (parent && parent.disabled)) classes.push("is-disabled");
                let label = escapeHtml(text);
                if (q) {
                    const at = text.toLowerCase().indexOf(q);
                    label = escapeHtml(text.slice(0, at)) + "<b>" + escapeHtml(text.slice(at, at + q.length)) + "</b>" +
                        escapeHtml(text.slice(at + q.length));
                }
                const font = option.style.fontFamily ? ` style="font-family:${escapeHtml(option.style.fontFamily)}"` : "";
                html += `<div class="${classes.join(" ")}" role="option" data-index="${index}" ` +
                    `aria-selected="${index === this.select.selectedIndex}"${font}><span class="plx-dd-text">${label}</span>${tick}</div>`;
                shown += 1;
            });
            this.items.innerHTML = html || '<div class="plx-dd-empty">Nothing matches</div>';
            const total = this.options().filter((o) => !o.hidden).length;
            this.foot.hidden = !q;
            this.foot.textContent = q ? `${shown} of ${total}` : "";
        }

        place() {
            const list = this.list;
            const box = this.button.getBoundingClientRect();
            list.style.minWidth = `${Math.max(box.width, 160)}px`;
            const height = list.offsetHeight;
            const below = global.innerHeight - box.bottom - 8;
            const up = below < Math.min(height, 240) && box.top > below;
            let left = box.left;
            const listWidth = list.offsetWidth;
            if (left + listWidth > global.innerWidth - 8) left = Math.max(8, box.right - listWidth);
            list.style.left = `${Math.round(left)}px`;
            list.style.top = up ? "" : `${Math.round(box.bottom + 4)}px`;
            list.style.bottom = up ? `${Math.round(global.innerHeight - box.top + 4)}px` : "";
            list.style.maxHeight = `${Math.max(140, Math.min(360, (up ? box.top : below) - 12))}px`;
            list.classList.toggle("is-up", up);
        }

        close(instant = false) {
            const list = this.list;
            if (!list) return;
            this.list = null;
            this.input = null;
            if (current === this) current = null;
            global.document.removeEventListener("pointerdown", this.outside, true);
            this.button.setAttribute("aria-expanded", "false");
            this.button.classList.remove("is-open");
            list.classList.remove("is-open");
            const remove = () => {
                if (list.matches && list.matches(":popover-open")) list.hidePopover();
                list.remove();
            };
            if (instant) remove();
            else global.setTimeout(remove, CLOSE_MS);
        }

        choose(index) {
            const button = this.button;
            const changed = index !== this.select.selectedIndex;
            this.close();
            if (button.isConnected) button.focus({ preventScroll: true });
            if (!changed) return;
            this.select.selectedIndex = index;
            this.select.dispatchEvent(new Event("input", { bubbles: true }));
            this.select.dispatchEvent(new Event("change", { bubbles: true }));
        }

        /* -- keys ---------------------------------------------------------- */

        enabledIndexes() {
            return this.options().map((option, index) =>
                (option.disabled || option.hidden || (option.parentElement && option.parentElement.disabled) ? -1 : index))
                .filter((index) => index >= 0);
        }

        visibleIndexes() {
            const q = this.query;
            const options = this.options();
            return this.enabledIndexes().filter((i) => !q || options[i].textContent.trim().toLowerCase().includes(q));
        }

        step(delta) {
            const indexes = this.visibleIndexes();
            if (!indexes.length) return;
            const at = indexes.indexOf(this.active);
            const next = at < 0 ? (delta > 0 ? 0 : indexes.length - 1)
                : Math.max(0, Math.min(indexes.length - 1, at + delta));
            this.active = indexes[next];
            this.mark(true);
        }

        mark(scroll) {
            if (!this.list) return;
            for (const item of this.list.querySelectorAll("[data-index]")) {
                const on = Number(item.dataset.index) === this.active;
                item.classList.toggle("is-active", on);
                if (on && scroll) item.scrollIntoView({ block: "nearest" });
            }
        }

        typeAhead(key) {
            const now = Date.now();
            this.typed = (now - this.typedAt < 700 ? this.typed : "") + key.toLowerCase();
            this.typedAt = now;
            const options = this.options();
            const hit = this.enabledIndexes().find((index) =>
                options[index].textContent.trim().toLowerCase().startsWith(this.typed));
            if (hit === undefined) return;
            if (this.list) {
                this.active = hit;
                this.mark(true);
            } else {
                this.choose(hit);
            }
        }

        /** Keys in the search field: arrows move, Enter picks, Escape closes. */
        listKey(event) {
            switch (event.key) {
                case "ArrowDown":
                case "ArrowUp":
                    event.preventDefault();
                    this.step(event.key === "ArrowDown" ? 1 : -1);
                    break;
                case "Enter":
                    event.preventDefault();
                    if (this.active >= 0) this.choose(this.active);
                    break;
                case "Escape":
                    event.preventDefault();
                    this.close();
                    this.button.focus({ preventScroll: true });
                    break;
                case "Tab":
                    this.close(true);
                    break;
                default:
                    break;
            }
            event.stopPropagation();
        }

        buttonKey(event) {
            const open = Boolean(this.list);
            switch (event.key) {
                case "ArrowDown":
                case "ArrowUp":
                    event.preventDefault();
                    if (!open) this.open();
                    else this.step(event.key === "ArrowDown" ? 1 : -1);
                    return;
                case "Home":
                case "End":
                    if (!open) return;
                    event.preventDefault();
                    this.active = -1;
                    this.step(event.key === "Home" ? 1 : -1);
                    return;
                case "Enter":
                case " ":
                    if (event.key === " " && this.typed && Date.now() - this.typedAt < 700) {
                        event.preventDefault();
                        this.typeAhead(" ");
                        return;
                    }
                    event.preventDefault();
                    if (open && this.active >= 0) this.choose(this.active);
                    else if (!open) this.open();
                    return;
                case "Escape":
                    if (!open) return;
                    event.preventDefault();
                    event.stopPropagation();
                    this.close();
                    return;
                case "Tab":
                    if (open) this.close(true);
                    return;
                default:
                    if (event.key.length === 1 && !event.metaKey && !event.ctrlKey && !event.altKey) {
                        this.typeAhead(event.key);
                    }
            }
        }
    }

    function upgradeAll(root) {
        if (!root || !root.querySelectorAll) return;
        if (root.tagName === "SELECT") { queue(root); return; }
        for (const select of root.querySelectorAll("select")) queue(select);
    }

    function start() {
        if (started) return;
        started = true;
        upgradeAll(global.document);
        new MutationObserver((records) => {
            for (const record of records) {
                for (const node of record.addedNodes) {
                    if (node.nodeType === 1) upgradeAll(node);
                }
            }
        }).observe(global.document.documentElement, { childList: true, subtree: true });
        global.addEventListener("resize", () => current && current.close(true));
        global.document.addEventListener("scroll", (event) => {
            if (current && current.list && !current.list.contains(event.target)) current.close(true);
        }, true);
    }

    global.PlexoraSelect = {
        start,
        /** Upgrade one select now (it must already be in the page). */
        upgrade(select) {
            if (eligible(select)) select.plexoraSelect = new Dropdown(select);
            return select.plexoraSelect || null;
        },
        upgradeAll,
        Dropdown,
    };
    if (typeof module !== "undefined" && module.exports) module.exports = global.PlexoraSelect;

    if (global.document) {
        if (global.document.readyState === "loading") global.document.addEventListener("DOMContentLoaded", start);
        else start();
    }
})(typeof window !== "undefined" ? window : globalThis);
