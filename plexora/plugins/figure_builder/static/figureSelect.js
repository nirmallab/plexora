/**
 * FigureSelect - the Figure Builder's dropdowns, drawn by the page rather than
 * by the operating system.
 *
 * Every `<select>` inside the Figure Builder is upgraded where it stands: the
 * select stays in the DOM, hidden, and remains the one source of truth. A
 * button is put beside it that shows the chosen option, and a list opens under
 * the button. Choosing sets `select.value` and dispatches `input` and `change`
 * on the select, exactly as a native choice does, so none of the panels that
 * read these selects had to change.
 *
 * Why a MutationObserver: the sidebars redraw by replacing innerHTML, on every
 * document change. A select that appears is upgraded when it appears; one that
 * goes takes its button with it, because the button is its sibling.
 *
 * The list lives in the top layer (the Popover API) where the browser has it,
 * so it opens above the export dialog, which is itself in the top layer. Where
 * it does not, the list is put inside the nearest dialog, or on the body.
 */
class FigureSelect {

    /** Where selects are upgraded: the builder, its dialogs, the library. */
    static get SCOPE() {
        return ".fb-workspace, .fb-dialog, dialog[id^='fb_'], .fb-library, [class^='fb-'], [class*=' fb-']";
    }

    static get CLOSE_MS() { return 120; }

    static start() {
        if (FigureSelect.started) return;
        FigureSelect.started = true;
        FigureSelect.upgradeAll(document);
        new MutationObserver((records) => {
            for (const record of records) {
                for (const node of record.addedNodes) {
                    if (node.nodeType !== 1) continue;
                    if (node.tagName === "SELECT") FigureSelect.upgrade(node);
                    else if (node.querySelector) FigureSelect.upgradeAll(node);
                }
            }
        }).observe(document.documentElement, { childList: true, subtree: true });
        window.addEventListener("resize", () => FigureSelect.current?.close(true));
        document.addEventListener("scroll", (event) => {
            const open = FigureSelect.current;
            if (open && !open.list.contains(event.target)) open.close(true);
        }, true);
    }

    static upgradeAll(root) {
        for (const select of root.querySelectorAll("select")) FigureSelect.upgrade(select);
    }

    static upgrade(select) {
        if (select.multiple || select.size > 1 || select.fbSelect) return;
        if (select.dataset.native !== undefined) return;
        const fb = /\bfb-/.test(select.className) || select.closest(FigureSelect.SCOPE);
        if (!fb) return;
        select.fbSelect = new FigureSelect(select);
    }

    constructor(select) {
        this.select = select;
        this.list = null;
        this.active = -1;
        this.typed = "";
        this.typedAt = 0;

        const button = document.createElement("button");
        button.type = "button";
        button.className = `${select.className} fb-dd`;
        button.setAttribute("aria-haspopup", "listbox");
        button.setAttribute("aria-expanded", "false");
        const label = select.getAttribute("aria-label") || select.title;
        if (label) button.setAttribute("aria-label", label);
        if (select.title) button.title = select.title;
        button.innerHTML = `<span class="fb-dd-value"></span>
            <svg class="fb-dd-chev" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>`;
        this.button = button;
        select.classList.add("fb-dd-native");
        select.tabIndex = -1;
        select.setAttribute("aria-hidden", "true");
        select.after(button);

        // A label pointing at the select now points at the button.
        if (select.id) {
            for (const target of document.querySelectorAll(`label[for="${CSS.escape(select.id)}"]`)) {
                target.addEventListener("click", (event) => { event.preventDefault(); button.focus(); });
            }
        }

        button.addEventListener("click", () => (this.list ? this.close() : this.open()));
        button.addEventListener("keydown", (event) => this.buttonKey(event));
        select.addEventListener("change", () => this.refresh());
        this.watchValue();
        new MutationObserver(() => this.refresh())
            .observe(select, { childList: true, subtree: true, attributes: true, characterData: true });
        this.refresh();
    }

    /** `select.value = x` from code fires nothing; the instance's own
     *  accessors catch it so the button never shows a stale answer. */
    watchValue() {
        const select = this.select;
        const self = this;
        for (const key of ["value", "selectedIndex"]) {
            const proto = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, key);
            if (!proto) continue;
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
        if (!this.button.isConnected && !this.select.isConnected) return;
        const chosen = this.select.options[this.select.selectedIndex];
        this.button.querySelector(".fb-dd-value").textContent = chosen ? chosen.textContent.trim() : "";
        this.button.disabled = this.select.disabled;
        this.button.hidden = this.select.hidden;
        if (chosen?.style.fontFamily) this.button.querySelector(".fb-dd-value").style.fontFamily = chosen.style.fontFamily;
    }

    /* -- the list -------------------------------------------------------------- */

    open() {
        if (this.select.disabled) return;
        FigureSelect.current?.close(true);
        FigureSelect.current = this;

        const list = document.createElement("div");
        list.className = "fb-dd-list";
        list.setAttribute("role", "listbox");
        list.tabIndex = -1;
        const usePopover = typeof list.showPopover === "function";
        if (usePopover) list.setAttribute("popover", "manual");
        list.innerHTML = this.itemsHtml();
        // Inside the builder, so the list takes the builder's light theme
        // tokens rather than the app's; the top layer lifts it out of any
        // overflow either way.
        const home = usePopover
            ? this.button.closest("dialog, .fb-workspace, .fb-library, [data-theme]")
            : this.button.closest("dialog");
        (home || document.body).appendChild(list);
        if (usePopover) list.showPopover();
        this.list = list;

        this.place();
        this.active = this.select.selectedIndex;
        this.mark(false);
        list.querySelector(".is-selected")?.scrollIntoView({ block: "nearest" });
        requestAnimationFrame(() => list.classList.add("is-open"));

        this.button.setAttribute("aria-expanded", "true");
        this.button.classList.add("is-open");

        list.addEventListener("pointerdown", (event) => event.preventDefault());
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
        this.outside = (event) => {
            if (!list.contains(event.target) && !this.button.contains(event.target)) this.close();
        };
        document.addEventListener("pointerdown", this.outside, true);
    }

    itemsHtml() {
        const escape = (text) => String(text).replace(/[&<>"]/g, (c) =>
            ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;" })[c]);
        const tick = `<svg class="fb-dd-tick" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>`;
        let html = "";
        let group = null;
        this.options().forEach((option, index) => {
            const parent = option.parentElement;
            if (parent?.tagName === "OPTGROUP" && parent !== group) {
                group = parent;
                html += `<div class="fb-dd-group">${escape(parent.label)}</div>`;
            }
            if (option.hidden) return;
            const classes = ["fb-dd-item"];
            if (index === this.select.selectedIndex) classes.push("is-selected");
            if (option.disabled || parent?.disabled) classes.push("is-disabled");
            const font = option.style.fontFamily ? ` style="font-family:${escape(option.style.fontFamily)}"` : "";
            html += `<div class="${classes.join(" ")}" role="option" data-index="${index}"
                aria-selected="${index === this.select.selectedIndex}"${font}>
                <span class="fb-dd-text">${escape(option.textContent.trim())}</span>${tick}</div>`;
        });
        return html;
    }

    place() {
        const list = this.list;
        const box = this.button.getBoundingClientRect();
        const width = Math.max(box.width, 140);
        list.style.minWidth = `${width}px`;
        const height = list.offsetHeight;
        const below = window.innerHeight - box.bottom - 8;
        const up = below < Math.min(height, 240) && box.top > below;
        let left = box.left;
        const listWidth = list.offsetWidth;
        if (left + listWidth > window.innerWidth - 8) left = Math.max(8, box.right - listWidth);
        list.style.left = `${Math.round(left)}px`;
        list.style.top = up ? "" : `${Math.round(box.bottom + 4)}px`;
        list.style.bottom = up ? `${Math.round(window.innerHeight - box.top + 4)}px` : "";
        list.style.maxHeight = `${Math.max(120, Math.min(320, (up ? box.top : below) - 12))}px`;
        list.classList.toggle("is-up", up);
    }

    close(instant = false) {
        const list = this.list;
        if (!list) return;
        this.list = null;
        if (FigureSelect.current === this) FigureSelect.current = null;
        document.removeEventListener("pointerdown", this.outside, true);
        this.button.setAttribute("aria-expanded", "false");
        this.button.classList.remove("is-open");
        list.classList.remove("is-open");
        const remove = () => {
            if (list.matches?.(":popover-open")) list.hidePopover();
            list.remove();
        };
        if (instant) remove();
        else window.setTimeout(remove, FigureSelect.CLOSE_MS);
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

    /* -- keys ------------------------------------------------------------------ */

    enabledIndexes() {
        return this.options().map((option, index) =>
            (option.disabled || option.hidden || option.parentElement?.disabled ? -1 : index))
            .filter((index) => index >= 0);
    }

    step(delta) {
        const indexes = this.enabledIndexes();
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

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => FigureSelect.start());
} else {
    FigureSelect.start();
}
