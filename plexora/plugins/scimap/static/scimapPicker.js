// A searchable menu in place of a native <select>.
//
// A native select draws the operating system's list -- its own background,
// its own blue highlight -- and cannot be searched, which a list of sixty
// markers or a column's hundred labels needs. This is the plugin's one menu:
// a button showing the value, and a menu holding a search field (filtered as
// one types, the match in bold), groups with titles, a swatch or a colour
// ramp per item, and, where asked, a last row that creates what was typed.
// Reached from the keyboard: arrows move, Enter picks, Escape closes and
// gives the focus back.
//
// `accordion: true` folds the groups: one is open at a time (the one holding
// the value, at first), a click on another's title opens it and closes the
// rest; while a search is typed every group with a match is shown open.
//
//     const picker = ScimapPicker.create({
//         value: "kmeans_k5", groups: [{ title: "Labels", items: [{ value, label, swatch, ramp, hint }] }],
//         theme: "light" | "glass", ariaLabel: "Color by", icon: node, mono: true,
//         create: (query) => "Create “" + query + "”",   // optional
//         onChange: (value, item, created) => {},
//     });
//     host.append(picker.root); picker.setValue(v); picker.setGroups(groups);
//
// `ScimapPicker.replace(select)` swaps a native select already in the page
// for one: the select stays (hidden) as the value, and a pick sets it and
// fires its `change`, so the code reading it does not change.
//
// The menu is parked on the page (core's PopoverPortal when the page has it),
// not inside the control, so a window's or a card's clipping cannot cut it.
window.ScimapPicker = (function () {
    "use strict";

    const LIMIT = 300;
    let openOne = null;

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function chevron() {
        const ns = "http://www.w3.org/2000/svg";
        const svg = document.createElementNS(ns, "svg");
        svg.setAttribute("viewBox", "0 0 24 24");
        svg.setAttribute("class", "sm-pick-chev");
        svg.setAttribute("aria-hidden", "true");
        const path = document.createElementNS(ns, "path");
        path.setAttribute("d", "m6 9 6 6 6-6");
        svg.append(path);
        return svg;
    }

    function portal(node) {
        // PopoverPortal is a top-level const in core (views/popoverPortal.js).
        try {
            // eslint-disable-next-line no-undef
            if (typeof PopoverPortal !== "undefined" && PopoverPortal.attach) { PopoverPortal.attach(node); return; }
        } catch (err) { /* not on this page */ }
        document.body.append(node);
    }

    function unportal(node) {
        try {
            // eslint-disable-next-line no-undef
            if (typeof PopoverPortal !== "undefined" && PopoverPortal.detach) PopoverPortal.detach(node);
        } catch (err) { /* not on this page */ }
        node.remove();
    }

    /** `text` with the first `query` match in bold. */
    function marked(text, query) {
        const span = el("span", "sm-pick-text");
        const at = query ? text.toLowerCase().indexOf(query) : -1;
        if (at < 0) { span.textContent = text; return span; }
        span.append(document.createTextNode(text.slice(0, at)), el("b", null, text.slice(at, at + query.length)),
            document.createTextNode(text.slice(at + query.length)));
        return span;
    }

    function swatchOf(item) {
        if (item.ramp) {
            const ramp = el("span", "sm-pick-ramp");
            ramp.style.background = "linear-gradient(90deg," + item.ramp.join(",") + ")";
            return ramp;
        }
        if (item.swatches) {
            const box = el("span", "sm-pick-swatches");
            item.swatches.slice(0, 3).forEach((c) => { const s = el("i"); s.style.background = c; box.append(s); });
            return box;
        }
        if (item.swatch) {
            const s = el("span", "sm-pick-swatch");
            s.style.background = item.swatch;
            return s;
        }
        return null;
    }

    class Picker {
        constructor(options) {
            this.o = Object.assign({ theme: "light", placeholder: "Choose…", searchPlaceholder: "Search…",
                                     emptyText: "Nothing matches" }, options || {});
            this.value = this.o.value === undefined ? null : this.o.value;
            this.groups = [];
            this.query = "";
            this.active = -1;
            this.flat = [];
            this.root = el("button", "sm-pick" + (this.o.className ? " " + this.o.className : "") +
                (this.o.theme === "glass" ? " is-glass" : " is-light"));
            this.root.type = "button";
            this.root.setAttribute("aria-haspopup", "listbox");
            this.root.setAttribute("aria-expanded", "false");
            if (this.o.ariaLabel) this.root.setAttribute("aria-label", this.o.ariaLabel);
            if (this.o.title) this.root.title = this.o.title;
            this.lead = el("span", "sm-pick-lead");
            this.label = el("span", "sm-pick-label" + (this.o.mono ? " is-mono" : ""));
            this.tail = el("span", "sm-pick-tail");
            this.root.append(this.lead, this.label, this.tail, chevron());
            this.root.addEventListener("click", () => (this.isOpen ? this.close() : this.open()));
            this.root.addEventListener("keydown", (e) => {
                if (["ArrowDown", "ArrowUp"].includes(e.key) && !this.isOpen) { e.preventDefault(); this.open(); }
            });
            this.setGroups(this.o.groups || [{ title: null, items: this.o.items || [] }]);
        }

        setGroups(groups) {
            this.groups = (groups || []).filter((g) => g && g.items && g.items.length);
            this.paintTrigger();
            if (this.isOpen) this.paintList();
        }

        item(value) {
            for (const g of this.groups) for (const it of g.items) if (it.value === value) return it;
            return null;
        }

        setValue(value) {
            this.value = value;
            this.paintTrigger();
        }

        setDisabled(off) {
            this.root.disabled = Boolean(off);
        }

        paintTrigger() {
            const it = this.item(this.value);
            this.lead.textContent = "";
            this.tail.textContent = "";
            if (this.o.icon) this.lead.append(this.o.icon.cloneNode(true));
            const sw = it ? swatchOf(Object.assign({}, it, { ramp: null })) : null;
            if (sw && !this.o.icon) this.lead.append(sw);
            this.lead.hidden = !this.lead.childNodes.length;
            this.label.textContent = it ? (it.short || it.label) : (this.value || this.o.placeholder);
            this.label.classList.toggle("is-empty", !it && !this.value);
            if (it && it.ramp && this.o.showRamp !== false) this.tail.append(swatchOf({ ramp: it.ramp }));
            this.tail.hidden = !this.tail.childNodes.length;
        }

        open() {
            if (this.isOpen || this.root.disabled) return;
            if (openOne && openOne !== this) openOne.close();
            openOne = this;
            this.isOpen = true;
            this.query = "";
            if (this.o.accordion) {
                const holding = this.groups.find((g) => g.items.some((it) => it.value === this.value));
                this.openGroup = (holding || this.groups[0] || {}).title || null;
            }
            this.root.setAttribute("aria-expanded", "true");
            this.root.classList.add("is-open");
            const menu = el("div", "sm-pick-menu " + (this.o.theme === "glass" ? "is-glass" : "is-light"));
            menu.setAttribute("role", "dialog");
            const search = el("label", "sm-pick-search");
            const glass = el("span", "sm-pick-glass");
            glass.setAttribute("aria-hidden", "true");
            this.input = el("input");
            this.input.type = "text";
            this.input.placeholder = this.o.searchPlaceholder;
            this.input.setAttribute("aria-label", this.o.searchPlaceholder);
            this.input.setAttribute("autocomplete", "off");
            this.input.setAttribute("spellcheck", "false");
            search.append(glass, this.input);
            this.list = el("div", "sm-pick-list");
            this.list.setAttribute("role", "listbox");
            this.foot = el("div", "sm-pick-foot");
            menu.append(search, this.list, this.foot);
            this.menu = menu;
            portal(menu);
            this.input.addEventListener("input", () => { this.query = this.input.value.trim().toLowerCase(); this.paintList(true); });
            this.input.addEventListener("keydown", (e) => this.key(e));
            menu.addEventListener("pointerdown", (e) => e.stopPropagation());
            this.onOutside = (e) => { if (!this.root.contains(e.target)) this.close(); };
            this.onScroll = (e) => { if (!menu.contains(e.target)) this.place(); };
            this.onResize = () => this.place();
            document.addEventListener("pointerdown", this.onOutside, true);
            window.addEventListener("scroll", this.onScroll, true);
            window.addEventListener("resize", this.onResize);
            this.paintList(true);
            this.place();
            requestAnimationFrame(() => this.input && this.input.focus({ preventScroll: true }));
        }

        place() {
            if (!this.menu) return;
            const r = this.root.getBoundingClientRect();
            const vw = window.innerWidth;
            const vh = window.innerHeight;
            const width = Math.max(r.width, this.o.menuWidth || 240);
            const room = vh - r.bottom - 8;
            const up = room < 220 && r.top > room;
            const maxH = Math.max(140, Math.min(360, (up ? r.top : room) - 12));
            Object.assign(this.menu.style, {
                left: Math.max(8, Math.min(vw - width - 8, r.left)) + "px",
                width: width + "px",
                maxHeight: maxH + "px",
                top: up ? "" : (r.bottom + 4) + "px",
                bottom: up ? (vh - r.top + 4) + "px" : "",
            });
            this.menu.classList.toggle("is-up", up);
        }

        paintList(reset) {
            const q = this.query;
            this.list.textContent = "";
            this.flat = [];
            let total = 0;
            let shown = 0;
            let exact = false;
            this.groups.forEach((g) => {
                const hits = g.items.filter((it) => {
                    total += 1;
                    const text = (it.label + " " + (it.hint || "")).toLowerCase();
                    if (q && it.label.toLowerCase() === q) exact = true;
                    return !q || text.includes(q);
                });
                if (!hits.length) return;
                const folding = this.o.accordion && !q && this.groups.length > 1 && g.title;
                const folded = folding && this.openGroup !== g.title;
                if (folding) {
                    const head = el("button", "sm-pick-group is-fold" + (folded ? "" : " is-open"));
                    head.type = "button";
                    head.setAttribute("aria-expanded", String(!folded));
                    head.append(el("span", "sm-pick-fold-chev"), el("span", "sm-pick-fold-title", g.title),
                        el("span", "sm-pick-fold-n", String(g.items.length)));
                    head.addEventListener("click", () => {
                        this.openGroup = folded ? g.title : null;
                        this.paintList(true);
                        this.input && this.input.focus({ preventScroll: true });
                    });
                    this.list.append(head);
                    if (folded) return;
                } else if (g.title && this.groups.length > 1) {
                    this.list.append(el("div", "sm-pick-group", g.title));
                }
                hits.forEach((it) => {
                    if (shown >= LIMIT) return;
                    shown += 1;
                    const row = el("div", "sm-pick-item");
                    row.setAttribute("role", "option");
                    row.setAttribute("aria-selected", String(it.value === this.value));
                    const sw = swatchOf(it);
                    if (sw) row.append(sw);
                    const text = marked(it.label, q);
                    if (this.o.mono || it.mono) text.classList.add("is-mono");
                    row.append(text);
                    if (it.hint) row.append(el("span", "sm-pick-hint", it.hint));
                    if (it.value === this.value) row.append(el("span", "sm-pick-check"));
                    const index = this.flat.length;
                    row.addEventListener("pointermove", () => this.highlight(index, false));
                    row.addEventListener("click", () => this.pick(it));
                    this.flat.push({ row, item: it });
                    this.list.append(row);
                });
            });
            const createText = this.o.create && q && !exact ? this.o.create(this.input.value.trim()) : null;
            if (createText) {
                if (this.flat.length) this.list.append(el("div", "sm-pick-rule"));
                const row = el("div", "sm-pick-item is-create", createText);
                row.setAttribute("role", "option");
                const index = this.flat.length;
                row.addEventListener("pointermove", () => this.highlight(index, false));
                row.addEventListener("click", () => this.pick(null, this.input.value.trim()));
                this.flat.push({ row, create: true });
                this.list.append(row);
            }
            if (!this.flat.length) this.list.append(el("div", "sm-pick-empty", this.o.emptyText));
            const matched = this.flat.filter((f) => !f.create).length;
            this.foot.textContent = total > 8 ? (q ? matched + " of " + total : total + " items") +
                (shown >= LIMIT ? " · type to narrow" : " · ↑↓ to move, Enter to choose") : "";
            this.foot.hidden = !this.foot.textContent;
            if (reset) {
                const current = this.flat.findIndex((f) => f.item && f.item.value === this.value);
                this.highlight(q ? 0 : Math.max(0, current), true);
            }
        }

        highlight(index, scroll) {
            if (!this.flat.length) { this.active = -1; return; }
            this.active = Math.max(0, Math.min(this.flat.length - 1, index));
            this.flat.forEach((f, i) => f.row.classList.toggle("is-active", i === this.active));
            if (scroll) {
                const row = this.flat[this.active].row;
                const top = row.offsetTop;
                if (top < this.list.scrollTop) this.list.scrollTop = top - 4;
                else if (top + row.offsetHeight > this.list.scrollTop + this.list.clientHeight) {
                    this.list.scrollTop = top + row.offsetHeight - this.list.clientHeight + 4;
                }
            }
        }

        key(e) {
            if (e.key === "ArrowDown") { e.preventDefault(); this.highlight(this.active + 1, true); }
            else if (e.key === "ArrowUp") { e.preventDefault(); this.highlight(this.active - 1, true); }
            else if (e.key === "Enter") {
                e.preventDefault();
                const f = this.flat[this.active];
                if (f) this.pick(f.item || null, f.create ? this.input.value.trim() : undefined);
            } else if (e.key === "Escape") {
                e.preventDefault();
                e.stopPropagation();
                this.close(true);
            } else if (e.key === "Tab") {
                this.close();
            }
            e.stopPropagation();
        }

        pick(item, created) {
            const value = item ? item.value : created;
            this.close(true);
            if (value === undefined || value === null || value === "") return;
            const changed = value !== this.value;
            this.value = value;
            this.paintTrigger();
            if (changed || created !== undefined) this.o.onChange && this.o.onChange(value, item, created !== undefined && !item);
        }

        close(refocus) {
            if (!this.isOpen) return;
            this.isOpen = false;
            if (openOne === this) openOne = null;
            this.root.setAttribute("aria-expanded", "false");
            this.root.classList.remove("is-open");
            document.removeEventListener("pointerdown", this.onOutside, true);
            window.removeEventListener("scroll", this.onScroll, true);
            window.removeEventListener("resize", this.onResize);
            const menu = this.menu;
            this.menu = null;
            this.input = null;
            if (menu) {
                menu.classList.add("is-leaving");
                setTimeout(() => unportal(menu), 90);
            }
            if (refocus) this.root.focus({ preventScroll: true });
        }

        destroy() {
            this.close();
            this.root.remove();
        }
    }

    /** A picker standing in for `select`, which stays as the value. */
    function replace(select, options) {
        if (!select || select.dataset.smPicked) return null;
        select.dataset.smPicked = "1";
        const read = () => {
            const groups = [];
            let loose = null;
            Array.from(select.children).forEach((child) => {
                if (child.tagName === "OPTGROUP") {
                    groups.push({ title: child.label, items: Array.from(child.children).map(optionItem) });
                } else if (child.tagName === "OPTION") {
                    if (!loose) { loose = { title: null, items: [] }; groups.push(loose); }
                    loose.items.push(optionItem(child));
                }
            });
            return groups;
        };
        const optionItem = (option) => ({ value: option.value, label: option.textContent, disabled: option.disabled });
        const picker = new Picker(Object.assign({
            value: select.value, groups: read(), ariaLabel: select.getAttribute("aria-label") || "",
            mono: select.classList.contains("is-mono"),
            theme: select.closest(".sm-workspace") ? "light" : "glass",
            onChange: (value) => {
                select.value = value;
                select.dispatchEvent(new Event("change", { bubbles: true }));
                select.dispatchEvent(new Event("input", { bubbles: true }));
            },
        }, options || {}));
        if (select.style.minWidth) picker.root.style.minWidth = select.style.minWidth;
        select.hidden = true;
        select.classList.add("sm-pick-native");
        select.after(picker.root);
        // Options or the value changed by code: follow.
        new MutationObserver(() => picker.setGroups(read())).observe(select, { childList: true, subtree: true });
        select.addEventListener("change", () => picker.setValue(select.value));
        return picker;
    }

    /** Every native select under `root` that is not yet replaced. */
    function upgrade(root, options) {
        if (!root || !root.querySelectorAll) return [];
        return Array.from(root.querySelectorAll("select:not([data-sm-picked]):not([multiple])"))
            .map((select) => replace(select, options));
    }

    return { create: (options) => new Picker(options), replace, upgrade, Picker };
})();
