/**
 * qcTree.js - the Regions / Cells / Channels tree, and its menus.
 *
 * The ROI panel's tree, restated for what QC has to show: the same row (a
 * chevron, a swatch, a label, a count, an eye and a kebab on one 24px line),
 * the same states (`is-hidden`, `is-selected`, `is-collapsed`), the same
 * glyphs. A user who has learned the ROI panel has learned this one. It is a
 * restatement and not a reuse because roi.css is loaded only while the ROI
 * tool is, and QC must look finished without it (a plugin may style only its
 * own classes -- tests/test_plugin_css_boundary.py).
 *
 * FLAT, and RECONCILED BY KEY. The controller describes every row in order
 * ({key, level, ...}); rows are built once, kept in a Map, updated in place
 * and moved only when out of position (`place`). A QC session repaints this
 * panel every time it closes a unit, and a list that rebuilt itself would
 * drop the hover that revealed a row's buttons and orphan an open menu on a
 * button that no longer exists. Folding is `hidden` on the rows under a
 * folded parent, not a wrapper, so a row never changes parent.
 *
 * What a click MEANS is decided here; what it DOES is the controller's
 * (`onActivate`, `onEye`, `onMenu`, `onColor`), because doing it touches the
 * viewer.
 *
 * A row whose spec is `colorable` has core's ColorSwatchPicker for its dot,
 * as an ROI category row does: the dot IS the picker's button, still a disc
 * for "excludes" and a ring for "flags". The picker stops its own clicks, so
 * a click on the dot never also activates the row.
 * Every string reaches the DOM through `textContent`.
 */
class QcTree {

    constructor(options = {}) {
        this.listId = options.listId || "qc_tree";
        this.onActivate = options.onActivate || (() => {});
        this.onEye = options.onEye || (() => {});
        this.onMenu = options.onMenu || (() => []);
        this.onColor = options.onColor || null;
        this.list = null;
        this.rows = new Map();
        //: Folded rows by key, kept across renders: a repaint is not a reason
        //: to unfold what somebody folded.
        this.collapsed = new Set(options.collapsed || []);
        //: Every expandable row this tree has drawn once. Each arrives folded,
        //: so opening the panel shows the groups and not every row under
        //: them; a first-sight decision only, never re-applied on a repaint.
        this.known = new Set();
        this._lastSelected = null;
        this.specs = new Map();
    }

    render(specs) {
        if (!this.list) this.list = document.getElementById(this.listId);
        const list = this.list;
        if (!list) return;
        if (!this._bound) this.bind();

        // A newly selected row -- a region clicked on the image, or just
        // drawn -- opens every group above it, or the click shows nothing.
        const selected = specs.find((spec) => spec.selected);
        const selectedKey = selected ? selected.key : null;
        const reveal = new Set();
        if (selectedKey && selectedKey !== this._lastSelected) {
            const path = [];
            for (const spec of specs) {
                path.length = spec.level;
                if (spec.key === selectedKey) {
                    path.forEach((key) => reveal.add(key));
                    break;
                }
                path[spec.level] = spec.key;
            }
        }
        this._lastSelected = selectedKey;

        const live = new Set();
        const folded = [];      // [level] -> is an ancestor at that level folded
        let previous = null;
        this.specs = new Map();
        for (const spec of specs) {
            live.add(spec.key);
            this.specs.set(spec.key, spec);
            if (spec.expandable && !this.known.has(spec.key)) {
                this.known.add(spec.key);
                this.collapsed.add(spec.key);
            }
            if (reveal.has(spec.key)) this.collapsed.delete(spec.key);
            let row = this.rows.get(spec.key);
            if (!row) {
                row = this.build(spec);
                this.rows.set(spec.key, row);
            }
            folded.length = spec.level;
            const underFolded = folded.some(Boolean);
            this.update(row, spec, underFolded);
            folded[spec.level] = Boolean(spec.expandable && this.collapsed.has(spec.key));
            QcTree.place(list, row.el, previous);
            previous = row.el;
        }
        for (const [key, row] of this.rows) {
            if (live.has(key)) continue;
            row.picker?.destroy?.();
            row.el.remove();
            this.rows.delete(key);
            this.known.delete(key);
            this.collapsed.delete(key);
        }
    }

    /** `node` after `previous`, only if it is not already there: moving a
     *  node that is in place still blurs whatever is focused inside it. */
    static place(parent, node, previous) {
        const expected = previous ? previous.nextSibling : parent.firstChild;
        if (node === expected) return;
        parent.insertBefore(node, expected);
    }

    build(spec) {
        const el = document.createElement("div");
        el.className = "qc-row";
        el.setAttribute("role", "treeitem");
        el.tabIndex = 0;
        el.dataset.key = spec.key;

        // Both chevrons ship and CSS picks: FontAwesome's SVG mode has replaced
        // these spans by the time anything is clicked (the ROI tree's note).
        const chevron = document.createElement("button");
        chevron.type = "button";
        chevron.tabIndex = -1;
        chevron.className = "qc-row-chevron";
        chevron.innerHTML = '<span class="fas fa-chevron-down"></span>'
            + '<span class="fas fa-chevron-right"></span>';

        const swatch = document.createElement("span");
        swatch.className = "qc-row-swatch";
        swatch.setAttribute("aria-hidden", "true");
        if (spec.icon) {
            swatch.classList.add("is-icon");
            swatch.innerHTML = `<span class="fas fa-${spec.icon}"></span>`;
        }

        // The label, and beside it an optional note in a quieter voice --
        // where a row's cells came from, say ("Derived from: QC: Fold 1").
        const label = document.createElement("span");
        label.className = "qc-row-label";
        const text = document.createElement("span");
        text.className = "qc-row-text";
        const note = document.createElement("span");
        note.className = "qc-row-note";
        label.append(text, note);

        const tag = document.createElement("span");
        tag.className = "qc-row-tag";

        const count = document.createElement("span");
        count.className = "qc-row-count";

        const lock = document.createElement("span");
        lock.className = "qc-row-lock";
        lock.title = "Approved and locked";
        lock.innerHTML = '<span class="fas fa-lock"></span>';

        const eye = QcTree.action("qc-row-eye",
            '<span class="fas fa-eye"></span><span class="fas fa-eye-slash"></span>');
        const more = QcTree.action("qc-row-more", '<span class="fas fa-ellipsis"></span>');
        more.title = "More";
        more.setAttribute("aria-haspopup", "true");
        more.setAttribute("aria-expanded", "false");

        el.append(chevron, swatch, label, tag, count, lock, eye, more);
        return { el, chevron, swatch, label, text, note, tag, count, lock, eye, more };
    }

    update(row, spec, underFolded) {
        const el = row.el;
        el.dataset.level = String(spec.level);
        el.dataset.kind = spec.kind || "item";
        el.hidden = underFolded;
        el.style.setProperty("--qc-row-color", spec.color || "var(--text-muted)");

        const collapsed = Boolean(spec.expandable && this.collapsed.has(spec.key));
        el.classList.toggle("is-expandable", Boolean(spec.expandable));
        el.classList.toggle("is-collapsed", collapsed);
        if (spec.expandable) el.setAttribute("aria-expanded", collapsed ? "false" : "true");
        else el.removeAttribute("aria-expanded");
        el.setAttribute("aria-level", String(spec.level + 1));

        // The swatch says what the row IS on the image: a filled dot excludes,
        // a ring only flags. Groups carry a glyph instead.
        row.swatch.dataset.shape = spec.icon ? "icon" : (spec.shape || "fill");
        this.syncPicker(row, spec);

        row.text.textContent = spec.label;
        row.note.textContent = spec.note || "";
        row.note.hidden = !spec.note;
        row.label.title = [spec.title || spec.label, spec.note].filter(Boolean).join("\n");
        row.tag.textContent = spec.tag || "";
        row.tag.hidden = !spec.tag;
        row.tag.dataset.tone = spec.tagTone || "";
        const hasCount = spec.count !== undefined && spec.count !== null && spec.count !== "";
        row.count.textContent = hasCount ? QcTree.number(spec.count) : "";
        row.count.hidden = !hasCount;

        el.classList.toggle("is-hidden", Boolean(spec.hidden));
        el.classList.toggle("is-selected", Boolean(spec.selected));
        el.classList.toggle("is-locked", Boolean(spec.locked));
        el.classList.toggle("is-muted", Boolean(spec.muted));
        el.setAttribute("aria-selected", spec.selected ? "true" : "false");
        el.dataset.ownHidden = spec.ownHidden ? "true" : "false";

        // No eye on a row QC draws nothing for (a channel): kept in the row,
        // invisible, so every label and count lines up down the column.
        row.eye.classList.toggle("is-absent", !spec.eye);
        row.eye.title = spec.ownHidden ? "Show" : "Hide";
        row.more.classList.toggle("is-absent", !spec.menu);
    }

    /** The dot as a colour picker, built on the first render that asks for
     *  one and kept in step with the spec's colour after. The handler looks
     *  the spec up by key when it fires, never the one it was built with: a
     *  repaint replaces every spec. */
    syncPicker(row, spec) {
        const wanted = Boolean(spec.colorable && this.onColor && !spec.icon
            && typeof ColorSwatchPicker !== "undefined");
        if (!wanted) {
            if (row.picker) {
                row.picker.destroy?.();
                row.picker = null;
                row.swatch.innerHTML = "";
                row.swatch.classList.remove("has-picker", "color-swatch-mount");
                row.swatch.setAttribute("aria-hidden", "true");
            }
            return;
        }
        const color = String(spec.color || "#9ca3af").toLowerCase();
        if (!row.picker) {
            const key = spec.key;
            row.swatch.classList.add("has-picker");
            row.swatch.removeAttribute("aria-hidden");
            row.picker = new ColorSwatchPicker(row.swatch, {
                value: color,
                title: spec.colorTitle || "Colour",
                onChange: (hex) => {
                    const live = this.specs.get(key);
                    if (live) this.onColor(live, hex);
                },
            });
        } else if (String(row.picker.value || "").toLowerCase() !== color) {
            row.picker.setValue(color);
        }
    }

    static action(className, glyphs) {
        const button = document.createElement("button");
        button.type = "button";
        button.tabIndex = -1;
        button.className = `qc-row-action ${className}`;
        button.innerHTML = glyphs;
        return button;
    }

    static number(value) {
        return typeof value === "number" ? value.toLocaleString() : String(value);
    }

    bind() {
        this._bound = true;
        this.list.addEventListener("click", (event) => {
            const el = event.target.closest(".qc-row");
            if (!el || !this.list.contains(el)) return;
            const spec = this.specs.get(el.dataset.key);
            if (!spec) return;
            if (event.target.closest(".qc-row-chevron")) {
                event.stopPropagation();
                this.toggle(spec.key);
                return;
            }
            if (event.target.closest(".qc-row-eye")) {
                event.stopPropagation();
                if (spec.eye) this.onEye(spec);
                return;
            }
            const more = event.target.closest(".qc-row-more");
            if (more) {
                event.stopPropagation();
                if (spec.menu) this.openMenu(spec, more);
                return;
            }
            this.activate(spec);
        });
        this.list.addEventListener("keydown", (event) => {
            const el = event.target;
            if (!el.classList || !el.classList.contains("qc-row")) return;
            const spec = this.specs.get(el.dataset.key);
            if (!spec) return;
            if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                event.stopPropagation();
                this.activate(spec);
            } else if (spec.expandable && event.key === "ArrowLeft"
                       && !this.collapsed.has(spec.key)) {
                event.preventDefault();
                this.toggle(spec.key);
            } else if (spec.expandable && event.key === "ArrowRight"
                       && this.collapsed.has(spec.key)) {
                event.preventDefault();
                this.toggle(spec.key);
            }
        });
    }

    /** A row with nothing to do on the image folds instead: a group's whole
     *  row is its fold, as a heading's is. */
    activate(spec) {
        if (spec.activatable === false) {
            if (spec.expandable) this.toggle(spec.key);
            return;
        }
        this.onActivate(spec);
    }

    toggle(key) {
        if (this.collapsed.has(key)) this.collapsed.delete(key);
        else this.collapsed.add(key);
        this.render([...this.specs.values()]);
    }

    openMenu(spec, anchor) {
        // Off the button's own aria-expanded: the row handler stops the click,
        // so the document listener that dismisses a menu never sees a second
        // click on the dots (the ROI tree's note).
        if (anchor.getAttribute("aria-expanded") === "true") {
            QcTree.closePopup();
            return;
        }
        const items = this.onMenu(spec, anchor) || [];
        if (items.length) QcTree.menu(anchor, items);
    }

    // -- floating menus ----------------------------------------------------------

    /**
     * Float `el` under `anchor` until something dismisses it. The ROI tree's
     * popup, restated: there is no core primitive that positions one
     * (`PopoverPortal` re-parents only). Fixed, because the tree scrolls.
     */
    static popup(anchor, el, options = {}) {
        QcTree.closePopup();
        el.classList.add("qc-popup");
        if (typeof PopoverPortal !== "undefined") PopoverPortal.attach(el);
        else document.body.appendChild(el);

        const box = anchor.getBoundingClientRect();
        el.style.position = "fixed";
        el.style.top = `${box.bottom + 4}px`;
        const width = el.offsetWidth || 200;
        const left = options.align === "left" ? box.left : box.right - width;
        el.style.left = `${Math.max(8, Math.min(left, window.innerWidth - width - 8))}px`;
        const height = el.offsetHeight || 0;
        if (box.bottom + 4 + height > window.innerHeight - 8 && box.top - 4 - height > 8) {
            el.style.top = `${box.top - 4 - height}px`;
        }
        anchor.setAttribute("aria-expanded", "true");

        const close = () => {
            if (typeof PopoverPortal !== "undefined") PopoverPortal.detach(el);
            else el.remove();
            document.removeEventListener("click", close);
            document.removeEventListener("keydown", onKey, true);
            document.removeEventListener("scroll", onScroll, true);
            window.removeEventListener("resize", close);
            anchor.setAttribute("aria-expanded", "false");
            if (QcTree._popup && QcTree._popup.el === el) QcTree._popup = null;
        };
        const onKey = (event) => {
            if (event.key !== "Escape") return;
            event.stopPropagation();
            close();
        };
        const onScroll = (event) => { if (!el.contains(event.target)) close(); };
        window.setTimeout(() => document.addEventListener("click", close), 0);
        document.addEventListener("keydown", onKey, true);
        document.addEventListener("scroll", onScroll, true);
        window.addEventListener("resize", close);
        QcTree._popup = { el, close };
        return close;
    }

    /** items: [{label, onSelect, disabled, className, color, shape, hint}];
     *  options: {heading, align, className, before} -- `before` a node put
     *  above the items (the category picker's "Custom" field). */
    static menu(anchor, items, options = {}) {
        const menu = document.createElement("div");
        menu.className = `qc-menu ${options.className || ""}`.trim();
        menu.setAttribute("role", "menu");
        menu.addEventListener("click", (event) => event.stopPropagation());
        if (options.heading) {
            const heading = document.createElement("div");
            heading.className = "qc-menu-heading";
            const words = document.createElement("span");
            words.className = "qc-menu-heading-text";
            words.textContent = options.heading;
            heading.appendChild(words);
            // A small button at the heading's end (the picker's help): its own
            // click, never the menu's.
            const action = options.headingAction;
            if (action) {
                const button = document.createElement("button");
                button.type = "button";
                button.className = "qc-menu-heading-action";
                button.title = action.title || "";
                button.setAttribute("aria-label", action.title || "");
                button.setAttribute("aria-expanded", "false");
                const icon = document.createElement("span");
                icon.className = `fas fa-${action.icon || "circle-question"}`;
                icon.setAttribute("aria-hidden", "true");
                button.appendChild(icon);
                button.addEventListener("click", (event) => {
                    event.stopPropagation();
                    action.onClick?.(button, event);
                });
                heading.appendChild(button);
            }
            menu.appendChild(heading);
        }
        if (options.help) menu.appendChild(options.help);
        if (options.before) menu.appendChild(options.before);
        for (const item of items) {
            // A heading between items: the popup's own sections.
            if (item.heading) {
                const heading = document.createElement("div");
                heading.className = "qc-menu-heading";
                heading.textContent = item.heading;
                menu.appendChild(heading);
                continue;
            }
            const button = document.createElement("button");
            button.type = "button";
            button.className = `qc-menu-item ${item.className || ""}`.trim();
            // `checked` makes it one of a set of choices, the chosen one marked.
            if (typeof item.checked === "boolean") {
                button.setAttribute("role", "menuitemradio");
                button.setAttribute("aria-checked", item.checked ? "true" : "false");
            } else {
                button.setAttribute("role", "menuitem");
            }
            button.title = item.hint || item.label;
            button.disabled = Boolean(item.disabled);
            if (item.color) {
                const dot = document.createElement("span");
                dot.className = "qc-menu-dot";
                dot.dataset.shape = item.shape || "fill";
                dot.style.setProperty("--qc-row-color", item.color);
                button.appendChild(dot);
            }
            const text = document.createElement("span");
            text.className = "qc-menu-text";
            text.textContent = item.label;
            button.appendChild(text);
            // A value at the right end (a score, a count), muted unless toned.
            if (item.note) {
                const note = document.createElement("span");
                note.className = "qc-menu-note";
                if (item.noteTone) note.dataset.tone = item.noteTone;
                note.textContent = item.note;
                button.appendChild(note);
            }
            button.addEventListener("click", () => {
                QcTree.closePopup();
                item.onSelect?.();
            });
            menu.appendChild(button);
        }
        QcTree.popup(anchor, menu, { align: options.align || "right" });
        menu.querySelector(".qc-menu-item:not([disabled])")?.focus();
        return menu;
    }

    static closePopup() {
        QcTree._popup?.close();
        QcTree._popup = null;
    }
}

QcTree._popup = null;

window.QcTree = QcTree;
