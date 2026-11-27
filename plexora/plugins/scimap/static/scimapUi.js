// The shared controls of the plot sidebar and the plot bar.
//
// One floating layer (`float`): a popover tied to the control that opened it.
// A second press on that control closes it, as do a press outside and
// Escape; it flips above the control when there is no room below and never
// leaves the window.
//
// One list picker (`pickList`) for every dropdown that lists values: a search
// field, "2 of 38", for many values All / Selected, Select all and Clear
// selection (Select results and Clear results while a search is typed, which
// leave the rest alone), round selection marks, the whole row as the target,
// a drag across rows to select or deselect a run of them (the first row's
// state decides which), Undo for every change of more than one row, and a
// short-name grid when the names are short. A multi-select stays open and
// applies as it changes, batched so a drag redraws the plot once.
//
// Then the small pieces every section uses: a segmented control whose parts
// share the width evenly, a switch, a field that opens a picker, an accordion
// that keeps one section open, and the plot previews of the view cards.
// Tooltips are core's (`data-tooltip`, services/tooltip.js), not `title`.
window.ScimapUi = (function () {
    "use strict";

    const { h, icon } = window.ScimapDom;
    const reduced = () => window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    // -- the floating layer ---------------------------------------------------

    let current = null; // {anchor, node, o, onKey, onDown, onScroll, onResize}

    function host() {
        return document.querySelector("#sm_workspace .sm-workspace") || document.querySelector(".sm-workspace") || document.body;
    }

    function place(entry) {
        const { node, o } = entry;
        // The control was drawn again (a bar repaints as its plot changes):
        // hang from its replacement, else stay where it was.
        if (!entry.anchor.isConnected && o.reanchor) {
            const next = o.reanchor();
            if (next) {
                entry.anchor = next;
                next.classList.add("is-open");
                next.setAttribute("aria-expanded", "true");
            }
        }
        const anchor = entry.anchor;
        if (!anchor.isConnected) return;
        const r = anchor.getBoundingClientRect();
        const vw = window.innerWidth;
        const vh = window.innerHeight;
        const M = 8;
        const GAP = 6;
        const width = Math.min(o.width || Math.max(r.width, 240), vw - 2 * M);
        node.style.width = width + "px";
        node.style.maxHeight = "";
        let left = o.align === "end" ? r.right - width : r.left;
        if (left + width > vw - M) left = r.right - width;
        left = Math.max(M, Math.min(vw - width - M, left));
        const below = vh - r.bottom - GAP - M;
        const above = r.top - GAP - M;
        const natural = Math.min(node.scrollHeight, o.maxHeight || 520);
        const up = natural > below && above > below;
        node.style.maxHeight = Math.max(180, Math.min(o.maxHeight || 520, up ? above : below)) + "px";
        node.style.left = left + "px";
        node.style.top = up ? "" : (r.bottom + GAP) + "px";
        node.style.bottom = up ? (vh - r.top + GAP) + "px" : "";
        node.classList.toggle("is-up", up);
        // The control scrolled out of sight: nothing to hang from.
        if (r.bottom < 0 || r.top > vh) close();
    }

    /** Open `node` under `anchor`; a second call with the same anchor closes it. */
    function float(anchor, node, options) {
        if (current && current.anchor === anchor) { close(true); return null; }
        close();
        window.dispatchEvent(new Event("scimap:float"));
        const o = options || {};
        node.classList.add("sm-ui-pop");
        node.addEventListener("pointerdown", (e) => e.stopPropagation());
        const entry = { anchor, node, o };
        entry.onDown = (e) => { if (!node.contains(e.target) && !entry.anchor.contains(e.target)) close(); };
        entry.onKey = (e) => {
            if (e.key !== "Escape") return;
            e.preventDefault();
            e.stopPropagation();
            close(true);
        };
        entry.onScroll = (e) => { if (!node.contains(e.target)) place(entry); };
        entry.onResize = () => place(entry);
        current = entry;
        host().append(node);
        anchor.classList.add("is-open");
        anchor.setAttribute("aria-expanded", "true");
        place(entry);
        if (!reduced()) requestAnimationFrame(() => node.classList.add("is-in"));
        else node.classList.add("is-in");
        setTimeout(() => {
            if (current !== entry) return;
            document.addEventListener("pointerdown", entry.onDown, true);
        }, 0);
        document.addEventListener("keydown", entry.onKey, true);
        window.addEventListener("scroll", entry.onScroll, true);
        window.addEventListener("resize", entry.onResize);
        return { close: () => { if (current === entry) close(); }, place: () => place(entry), node };
    }

    function close(refocus) {
        const entry = current;
        if (!entry) return;
        current = null;
        document.removeEventListener("pointerdown", entry.onDown, true);
        document.removeEventListener("keydown", entry.onKey, true);
        window.removeEventListener("scroll", entry.onScroll, true);
        window.removeEventListener("resize", entry.onResize);
        entry.anchor.classList.remove("is-open");
        entry.anchor.setAttribute("aria-expanded", "false");
        if (entry.o.onClose) { try { entry.o.onClose(); } catch (_) { /* the caller's own problem */ } }
        const node = entry.node;
        node.classList.remove("is-in");
        node.classList.add("is-leaving");
        setTimeout(() => node.remove(), reduced() ? 0 : 130);
        if (refocus && entry.anchor.isConnected) entry.anchor.focus({ preventScroll: true });
    }

    function isOpen(anchor) { return Boolean(current && (!anchor || current.anchor === anchor)); }

    // -- the list picker --------------------------------------------------------

    const SHORT = 11; // names this long or shorter sit in a two-column grid
    const EDGE = 32; // px from the list's edge where a drag starts scrolling

    function mark() {
        return h("span.sm-ui-mark", { "aria-hidden": "true",
            html: '<svg viewBox="0 0 16 16"><path d="M4.2 8.3 6.9 11 11.9 5.6"/></svg>' });
    }

    function marked(text, q) {
        const span = h("span.sm-ui-opt-name");
        const at = q ? text.toLowerCase().indexOf(q) : -1;
        if (at < 0) { span.textContent = text; return span; }
        span.append(text.slice(0, at), h("b", null, text.slice(at, at + q.length)), text.slice(at + q.length));
        return span;
    }

    /**
     * A dropdown of values under `anchor`.
     *
     *   items     [{value, label}] (or plain values)
     *   value     multi: the chosen values, null for every one; single: the value
     *   multiple  a checklist that stays open (else one pick closes it)
     *   onChange  multi: (values, every) after a change settles; single: (value)
     *   allowEmpty  multi: an empty value means none (not every one), and
     *             choosing none is applied
     */
    function pickList(anchor, options) {
        const o = Object.assign({ title: "", noun: "values", multiple: false }, options || {});
        const items = (o.items || []).map((it) => (it && typeof it === "object" && "value" in it ? it
            : { value: it, label: it === "" || it === null || it === undefined ? "—" : String(it) }));
        const all = items.map((it) => it.value);
        const multiple = Boolean(o.multiple);
        const picked = new Set(multiple ? (Array.isArray(o.value) && (o.value.length || o.allowEmpty) ? o.value : all) : []);
        const grid = items.length >= 8 && items.every((it) => String(it.label).length <= SHORT);
        const searchable = items.length > 8;
        let query = "";
        let tab = "all";
        let tabSet = null; // the values the Selected tab lists, fixed when it opens
        let active = -1;
        let rows = [];
        let drag = null;
        let timer = null;
        let applied = JSON.stringify(all.filter((v) => picked.has(v)));
        const undo = [];

        const count = h("span.sm-ui-pop-count");
        const shut = h("button.sm-ui-pop-x", { type: "button", "aria-label": "Close", "data-tooltip": "Close  Esc",
            onclick: () => close(true) }, icon("x", "xs"));
        const input = h("input", { type: "text", placeholder: "Search " + o.noun, "aria-label": "Search " + o.noun,
            autocomplete: "off", spellcheck: "false" });
        const wipe = h("button.sm-ui-search-x", { type: "button", "aria-label": "Clear the search", hidden: true,
            onclick: () => { input.value = ""; query = ""; paint(); input.focus(); } }, icon("x", "xs"));
        const search = searchable ? h("label.sm-ui-search", null, icon("search", "xs"), input, wipe) : null;
        const tabs = multiple && items.length > 6 ? segmented({
            choices: [{ value: "all", label: "All " + o.noun }, { value: "selected", label: "Selected" }],
            value: "all", label: "Show", onChange: (v) => { tab = v; tabSet = v === "selected" ? new Set(picked) : null; paint(); },
        }) : null;
        const left = h("button.sm-ui-link", { type: "button" });
        const right = h("button.sm-ui-link.is-quiet", { type: "button" });
        const actions = multiple ? h("div.sm-ui-pop-actions", null, left, right) : null;
        const list = h("div.sm-ui-pop-list" + (grid ? ".is-grid" : ""), { role: "listbox", tabindex: "0",
            "aria-multiselectable": multiple ? "true" : null, "aria-label": o.title });
        const hint = h("span.sm-ui-pop-hint");
        const undoBtn = h("button.sm-ui-link", { type: "button", hidden: true, onclick: () => back() }, icon("undo", "xs"), "Undo");
        const foot = multiple ? h("div.sm-ui-pop-foot", null, hint, undoBtn, h("span.sm-ui-pop-live", null, "Changes apply instantly")) : null;
        const node = h("div.sm-ui-picker", { role: "dialog", "aria-label": o.title },
            h("div.sm-ui-pop-head", null, h("strong", null, o.title), count, shut),
            search, tabs && tabs.node, actions, list, foot);

        const visible = () => items.filter((it) => {
            if (tab === "selected" && !(tabSet && tabSet.has(it.value))) return false;
            return !query || String(it.label).toLowerCase().includes(query);
        });

        function paintCount() {
            count.textContent = multiple ? picked.size + " of " + all.length : (searchable ? all.length + " " + o.noun : "");
            if (tabs) tabs.setBadge("selected", picked.size);
            if (!multiple) return;
            const shown = visible();
            if (query) {
                left.textContent = "Select results";
                right.textContent = "Clear results";
                left.disabled = !shown.some((it) => !picked.has(it.value));
                right.disabled = !shown.some((it) => picked.has(it.value));
            } else {
                left.textContent = "Select all";
                right.textContent = "Clear selection";
                left.disabled = picked.size === all.length;
                right.disabled = picked.size === 0;
            }
            undoBtn.hidden = !undo.length;
            const empty = picked.size === 0 && !o.allowEmpty;
            hint.textContent = empty ? "Choose at least one" : "Esc to close";
            hint.classList.toggle("is-warn", empty);
        }

        function paintRow(row) {
            const on = multiple ? picked.has(row._value) : row._value === o.value;
            row.classList.toggle("is-on", on);
            row.setAttribute("aria-selected", String(on));
        }

        function paint() {
            wipe.hidden = !query;
            list.textContent = "";
            rows = visible().map((it, i) => {
                const row = h("div.sm-ui-opt", { role: "option" }, mark(), marked(String(it.label), query),
                    multiple && all.length > 2 ? h("button.sm-ui-only", { type: "button", tabindex: "-1",
                        "data-tooltip": "Only this one", onclick: (e) => { e.stopPropagation(); only(it.value); } }, "Only") : null);
                row._value = it.value;
                row._index = i;
                paintRow(row);
                return row;
            });
            if (rows.length) list.append(...rows);
            else list.append(h("div.sm-ui-pop-empty", null, tab === "selected" && !query ? "Nothing selected" : "No " + o.noun + " match"));
            if (active >= rows.length) active = rows.length - 1;
            if (!multiple && active < 0) active = Math.max(0, rows.findIndex((r) => r._value === o.value));
            highlight(active, false);
            paintCount();
        }

        function highlight(i, scroll) {
            active = rows.length ? Math.max(0, Math.min(rows.length - 1, i)) : -1;
            rows.forEach((r, k) => r.classList.toggle("is-active", k === active));
            if (scroll && rows[active]) rows[active].scrollIntoView({ block: "nearest" });
        }

        function remember(before) {
            undo.push(new Set(before));
            if (undo.length > 30) undo.shift();
        }

        function back() {
            if (!undo.length) return;
            const prev = undo.pop();
            picked.clear();
            prev.forEach((v) => picked.add(v));
            rows.forEach(paintRow);
            paintCount();
            commit(true);
        }

        function bulk(change) {
            const before = new Set(picked);
            change();
            if (before.size === picked.size && [...before].every((v) => picked.has(v))) return;
            remember(before);
            rows.forEach(paintRow);
            paintCount();
            commit(true);
        }

        function only(v) { bulk(() => { picked.clear(); picked.add(v); }); }

        left.addEventListener("click", () => bulk(() => (query ? visible() : items).forEach((it) => picked.add(it.value))));
        right.addEventListener("click", () => bulk(() => (query ? visible() : items).forEach((it) => picked.delete(it.value))));

        // A change settles before it reaches the plot; nothing chosen is never applied.
        function commit(soon) {
            clearTimeout(timer);
            const run = () => {
                const value = all.filter((v) => picked.has(v));
                if (!value.length && !o.allowEmpty) return;
                const key = JSON.stringify(value);
                if (key === applied) return;
                applied = key;
                o.onChange(value, value.length === all.length);
                if (current && current.node === node) requestAnimationFrame(() => current && current.node === node && place(current));
            };
            if (soon === "now") run();
            else timer = setTimeout(run, soon ? 140 : 260);
        }

        function pick(row) {
            if (!row) return;
            if (!multiple) {
                const v = row._value;
                close(true);
                if (v !== o.value) o.onChange(v);
                return;
            }
            const v = row._value;
            if (picked.has(v)) picked.delete(v); else picked.add(v);
            paintRow(row);
            paintCount();
            commit();
        }

        // -- drag to select ------------------------------------------------------

        function rowAt(x, y) {
            const el = document.elementFromPoint(x, y);
            const row = el && el.closest && el.closest(".sm-ui-opt");
            return row && list.contains(row) ? row : null;
        }

        function touch(row) {
            if (!row || drag.seen.has(row._value)) return;
            drag.seen.add(row._value);
            if (drag.on) picked.add(row._value); else picked.delete(row._value);
            paintRow(row);
        }

        // Every row the pointer crossed since the last move, not only where it is.
        function sweep(x, y) {
            const r = list.getBoundingClientRect();
            const cy = Math.max(r.top + 2, Math.min(r.bottom - 2, y));
            const cx = Math.max(r.left + 2, Math.min(r.right - 2, x));
            const dx = cx - drag.x;
            const dy = cy - drag.y;
            const steps = Math.max(1, Math.ceil(Math.hypot(dx, dy) / 6));
            for (let i = 1; i <= steps; i += 1) touch(rowAt(drag.x + dx * i / steps, drag.y + dy * i / steps));
            drag.x = cx;
            drag.y = cy;
            paintCount();
        }

        function scrollLoop() {
            if (!drag || !drag.speed) { if (drag) drag.raf = 0; return; }
            list.scrollTop += drag.speed;
            sweep(drag.px, drag.py);
            drag.raf = requestAnimationFrame(scrollLoop);
        }

        list.addEventListener("pointerdown", (e) => {
            if (e.button !== 0 || e.target.closest(".sm-ui-only")) return;
            const row = e.target.closest(".sm-ui-opt");
            if (!row) return;
            e.preventDefault();
            list.classList.remove("is-keys");
            list.focus({ preventScroll: true });
            highlight(row._index, false);
            if (!multiple) { pick(row); return; }
            clearTimeout(timer);
            drag = { on: !picked.has(row._value), seen: new Set(), before: new Set(picked), x: e.clientX, y: e.clientY,
                px: e.clientX, py: e.clientY, speed: 0, raf: 0 };
            list.classList.add("is-dragging");
            touch(row);
            paintCount();
            try { list.setPointerCapture(e.pointerId); } catch (_) { /* a synthetic event */ }
        });
        list.addEventListener("pointermove", (e) => {
            if (!drag) return;
            drag.px = e.clientX;
            drag.py = e.clientY;
            sweep(e.clientX, e.clientY);
            const r = list.getBoundingClientRect();
            const into = e.clientY < r.top + EDGE ? e.clientY - (r.top + EDGE) : e.clientY > r.bottom - EDGE ? e.clientY - (r.bottom - EDGE) : 0;
            drag.speed = into ? Math.sign(into) * Math.min(18, 2 + Math.abs(into) / 3) : 0;
            if (drag.speed && !drag.raf) drag.raf = requestAnimationFrame(scrollLoop);
        });
        const end = () => {
            if (!drag) return;
            if (drag.raf) cancelAnimationFrame(drag.raf);
            if (drag.seen.size > 1) remember(drag.before);
            drag = null;
            list.classList.remove("is-dragging");
            paintCount();
            commit(true);
        };
        list.addEventListener("pointerup", end);
        list.addEventListener("pointercancel", end);
        list.addEventListener("lostpointercapture", end);

        // -- keyboard ---------------------------------------------------------------

        function columns() {
            if (!grid || !rows.length) return 1;
            const top = rows[0].offsetTop;
            let n = 0;
            while (n < rows.length && rows[n].offsetTop === top) n += 1;
            return Math.max(1, n);
        }

        function key(e, fromInput) {
            if (!e.metaKey && !e.ctrlKey && /^(Arrow|Home|End)/.test(e.key)) list.classList.add("is-keys");
            const cols = columns();
            if (e.key === "ArrowDown") { e.preventDefault(); highlight(active < 0 ? 0 : active + cols, true); }
            else if (e.key === "ArrowUp") { e.preventDefault(); highlight(active - cols, true); }
            else if (grid && !fromInput && e.key === "ArrowRight") { e.preventDefault(); highlight(active + 1, true); }
            else if (grid && !fromInput && e.key === "ArrowLeft") { e.preventDefault(); highlight(active - 1, true); }
            else if (e.key === "Enter" || (e.key === " " && (!fromInput || !input.value))) { e.preventDefault(); pick(rows[active]); }
            else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "z" && multiple) { e.preventDefault(); back(); }
            else if (e.key === "Home" && !fromInput) { e.preventDefault(); highlight(0, true); }
            else if (e.key === "End" && !fromInput) { e.preventDefault(); highlight(rows.length - 1, true); }
        }
        input.addEventListener("input", () => { query = input.value.trim().toLowerCase(); active = 0; paint(); });
        input.addEventListener("keydown", (e) => key(e, true));
        list.addEventListener("keydown", (e) => key(e, false));

        paint();
        const opened = float(anchor, node, {
            width: o.width || (grid ? 300 : 280), maxHeight: 520, align: o.align, reanchor: o.reanchor,
            onClose: () => { clearTimeout(timer); if (multiple) commit("now"); if (o.onClose) o.onClose(); },
        });
        if (!opened) return null;
        requestAnimationFrame(() => {
            if (search) input.focus({ preventScroll: true });
            else list.focus({ preventScroll: true });
            if (rows[active]) rows[active].scrollIntoView({ block: "nearest" });
        });
        return opened;
    }

    // -- small pieces -------------------------------------------------------------

    /** Choices sharing the width evenly, a thumb sliding to the one chosen. */
    function segmented(options) {
        const o = options || {};
        const choices = o.choices || [];
        let value = o.value;
        const thumb = h("span.sm-ui-seg-thumb", { "aria-hidden": "true" });
        const node = h("div.sm-ui-seg" + (o.className ? "." + o.className : ""), { role: "radiogroup", "aria-label": o.label || null }, thumb);
        node.style.setProperty("--n", String(Math.max(1, choices.length)));
        const buttons = choices.map((c) => h("button.sm-ui-seg-opt", { type: "button", role: "radio",
            "data-tooltip": c.tip || null, "aria-label": c.icon && !c.label ? c.tip : null,
            onclick: () => choose(c.value, true) },
        c.icon ? icon(c.icon, "xs") : null, c.label ? h("span.sm-ui-seg-text", null, c.label) : null,
        h("span.sm-ui-seg-badge", { hidden: true })));
        node.append(...buttons);
        function choose(v, user) {
            const i = choices.findIndex((c) => c.value === v);
            if (i < 0) return;
            const changed = v !== value;
            value = v;
            node.style.setProperty("--i", String(i));
            buttons.forEach((b, k) => { b.setAttribute("aria-checked", String(k === i)); b.tabIndex = k === i ? 0 : -1; });
            if (user && changed && o.onChange) o.onChange(v);
        }
        node.addEventListener("keydown", (e) => {
            if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
            e.preventDefault();
            const i = choices.findIndex((c) => c.value === value);
            const next = choices[(i + (e.key === "ArrowRight" ? 1 : choices.length - 1)) % choices.length];
            choose(next.value, true);
            buttons[choices.indexOf(next)].focus();
        });
        choose(value, false);
        if (!choices.some((c) => c.value === value)) node.style.setProperty("--i", "0");
        return {
            node,
            set: (v) => choose(v, false),
            setBadge: (v, n) => {
                const b = buttons[choices.findIndex((c) => c.value === v)];
                const badge = b && b.querySelector(".sm-ui-seg-badge");
                if (badge) { badge.textContent = String(n); badge.hidden = !n; }
            },
        };
    }

    function toggle(options) {
        const o = options || {};
        let on = Boolean(o.on);
        const node = h("button.sm-ui-switch", { type: "button", role: "switch", "aria-label": o.label || null,
            onclick: () => { set(!on); if (o.onChange) o.onChange(on); } }, h("span.sm-ui-switch-knob"));
        function set(v) { on = Boolean(v); node.classList.toggle("is-on", on); node.setAttribute("aria-checked", String(on)); }
        set(on);
        return { node, set };
    }

    /** A control that shows a value and opens a picker; the chevron turns while open. */
    function field(options) {
        const o = options || {};
        const text = h("span.sm-ui-field-text");
        const badge = h("span.sm-ui-field-badge", { hidden: true });
        const node = h("button.sm-ui-field", { type: "button", "aria-haspopup": "dialog", "aria-expanded": "false",
            "aria-label": o.label || null, onclick: (e) => o.onOpen(e.currentTarget) },
        text, badge, h("span.sm-ui-field-chev", null, icon("chevD", "xs")));
        function set(value, n) {
            text.textContent = value;
            badge.textContent = n ? String(n) : "";
            badge.hidden = !n;
        }
        set(o.text || "", o.count);
        return { node, set };
    }

    /**
     * Sections of which one is open: opening one folds the one before, a press
     * on an open section's head folds it. The open one is remembered under
     * `storeKey`.
     */
    class Accordion {
        constructor(options) {
            this.o = Object.assign({ fallback: null }, options || {});
            this.sections = new Map();
            this.openId = this.read();
        }

        read() {
            try {
                const v = window.localStorage.getItem(this.o.storeKey);
                if (v !== null) return v === "" ? "" : v;
            } catch (_) { /* private window */ }
            return this.o.initial || "";
        }

        write() {
            try { window.localStorage.setItem(this.o.storeKey, this.openId); } catch (_) { /* private window */ }
        }

        reset() { this.sections.clear(); }

        section(id, title, options) {
            const o = options || {};
            const sub = h("span.sm-ui-acc-sub", null, o.sub || "");
            const head = h("button.sm-ui-acc-head", { type: "button", "aria-expanded": "false",
                onclick: () => this.toggle(id) },
            h("span.sm-ui-acc-title", null, title), sub, h("span.sm-ui-acc-chev", null, icon("chevD", "xs")));
            const inner = h("div.sm-ui-acc-inner", null, o.content);
            const body = h("div.sm-ui-acc-body", { role: "region" }, inner);
            const node = h("section.sm-ui-acc", { "data-section": id }, head, body);
            this.sections.set(id, { node, head, sub });
            return node;
        }

        // The remembered section, or the fallback when this plot has no such section.
        settle() {
            const id = this.sections.has(this.openId) ? this.openId : (this.openId === "" ? "" : this.o.fallback);
            this.sections.forEach((s, k) => this.mark(k, k === id, false));
        }

        mark(id, on, animate) {
            const s = this.sections.get(id);
            if (!s) return;
            if (!animate) s.node.classList.add("is-still");
            s.node.classList.toggle("is-open", on);
            s.head.setAttribute("aria-expanded", String(on));
            if (!animate) requestAnimationFrame(() => requestAnimationFrame(() => s.node.classList.remove("is-still")));
        }

        toggle(id) { this.open(this.isOpen(id) ? "" : id); }

        isOpen(id) { const s = this.sections.get(id); return Boolean(s && s.node.classList.contains("is-open")); }

        open(id) {
            this.sections.forEach((s, k) => this.mark(k, k === id, true));
            this.openId = id;
            this.write();
            if (this.o.onChange) this.o.onChange(id);
        }

        setSub(id, text) { const s = this.sections.get(id); if (s) s.sub.textContent = text || ""; }
    }

    // -- a text field with suggestions --------------------------------------------

    /**
     * A combobox: type anything, or pick one of `suggest()`'s values. The list
     * opens as one types (or on the chevron, or Alt+Down) and filters as one
     * types; Down/Up move in it, Enter takes the highlighted value. With the
     * list closed, Down/Up and Enter hand over to the neighbouring field
     * (`onNav(+1 | -1)`), so a column of these is filled from the keyboard.
     */
    function combo(options) {
        const o = Object.assign({ placeholder: "", value: "" }, options || {});
        const input = h("input.sm-ui-combo-input", { type: "text", value: o.value || "", placeholder: o.placeholder,
            autocomplete: "off", spellcheck: "false", "aria-label": o.label || o.placeholder, role: "combobox",
            "aria-expanded": "false", "aria-autocomplete": "list" });
        const chev = h("button.sm-ui-combo-chev", { type: "button", tabindex: "-1", "aria-label": "Show the labels" }, icon("chevD", "xs"));
        const node = h("div.sm-ui-combo", null, input, chev);
        let list = null;
        let rows = [];
        let active = -1;
        let last = input.value;
        const commit = () => {
            const v = input.value.trim();
            node.classList.toggle("has-value", Boolean(v));
            if (v !== last) { last = v; if (o.onCommit) o.onCommit(v); }
        };
        const choose = (v) => {
            input.value = v;
            if (o.onInput) o.onInput(v);
            close();
            commit();
        };
        const highlight = (i) => {
            active = rows.length ? (i + rows.length) % rows.length : -1;
            rows.forEach((r, k) => r.classList.toggle("is-active", k === active));
            if (rows[active]) rows[active].scrollIntoView({ block: "nearest" });
        };
        function open(all) {
            const q = all ? "" : input.value.trim().toLowerCase();
            const values = (o.suggest ? o.suggest() : []).filter((v) => v && (!q || v.toLowerCase().includes(q)) && v !== input.value.trim());
            if (!values.length) { close(); return; }
            rows = values.map((v) => {
                const row = h("div.sm-ui-opt.is-plain", { role: "option" }, icon("tag", "xs"), marked(v, q));
                row.addEventListener("pointerdown", (e) => { e.preventDefault(); choose(v); });
                return row;
            });
            if (!list || !isOpen(node)) {
                list = h("div.sm-ui-combo-list", { role: "listbox" });
                const head = h("div.sm-ui-combo-head", null, "Labels in this notebook");
                const box = h("div.sm-ui-pop-list.is-plain");
                list.append(head, box);
                list._box = box;
                float(node, list, { width: Math.max(node.offsetWidth, 220), maxHeight: 280,
                    onClose: () => { input.setAttribute("aria-expanded", "false"); list = null; } });
                input.setAttribute("aria-expanded", "true");
            }
            list._box.textContent = "";
            list._box.append(...rows);
            active = -1;
            if (current && current.node === list) place(current);
        }
        function close() { if (list && isOpen(node)) window.ScimapUi.close(); list = null; rows = []; active = -1; }
        input.addEventListener("input", () => { if (o.onInput) o.onInput(input.value); open(false); });
        input.addEventListener("blur", () => { setTimeout(() => { if (document.activeElement !== input) close(); }, 0); commit(); });
        input.addEventListener("keydown", (e) => {
            const listOpen = Boolean(list && rows.length);
            if (e.key === "ArrowDown" && e.altKey) { e.preventDefault(); open(true); }
            else if (e.key === "ArrowDown") { e.preventDefault(); if (listOpen) highlight(active + 1); else { commit(); o.onNav && o.onNav(1); } }
            else if (e.key === "ArrowUp") { e.preventDefault(); if (listOpen) highlight(active - 1); else { commit(); o.onNav && o.onNav(-1); } }
            else if (e.key === "Enter") {
                e.preventDefault();
                if (listOpen && active >= 0) choose(rows[active].textContent.trim());
                else { close(); commit(); }
                if (o.onNav) o.onNav(e.shiftKey ? -1 : 1);
            } else if (e.key === "Escape" && listOpen) { e.preventDefault(); e.stopPropagation(); close(); }
        });
        chev.addEventListener("pointerdown", (e) => e.preventDefault());
        chev.addEventListener("click", () => { input.focus(); if (list) close(); else open(true); });
        node.classList.toggle("has-value", Boolean(input.value.trim()));
        return { node, input, focus: () => { input.focus(); input.select(); },
            set: (v) => { input.value = v || ""; last = input.value.trim(); node.classList.toggle("has-value", Boolean(last)); } };
    }

    /** A small ring that fills as `done` of `total` are done. */
    function ring(done, total) {
        const R = 7;
        const C = 2 * Math.PI * R;
        const node = h("span.sm-ui-ring", { "aria-hidden": "true",
            html: '<svg viewBox="0 0 18 18"><circle class="sm-ui-ring-track" cx="9" cy="9" r="' + R + '"/>' +
                '<circle class="sm-ui-ring-fill" cx="9" cy="9" r="' + R + '" stroke-dasharray="' + C.toFixed(2) + '"/></svg>' });
        const fill = node.querySelector(".sm-ui-ring-fill");
        const set = (d, t) => {
            const f = t ? Math.max(0, Math.min(1, d / t)) : 0;
            fill.style.strokeDashoffset = (C * (1 - f)).toFixed(2);
            node.classList.toggle("is-done", Boolean(t) && d >= t);
        };
        set(done, total);
        return { node, set };
    }

    // -- plot previews -------------------------------------------------------------

    // Small drawings of each kind of plot, in four inks of the page's slate, so a
    // card says what it draws before its name is read.
    function rng(seed) {
        let s = seed >>> 0;
        return () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; };
    }

    const INK = ["var(--sm-pv-1)", "var(--sm-pv-2)", "var(--sm-pv-3)", "var(--sm-pv-4)"];

    function r(x, y, w, hh, fill, rx) {
        return '<rect x="' + x.toFixed(1) + '" y="' + y.toFixed(1) + '" width="' + w.toFixed(1) + '" height="' + hh.toFixed(1) +
            '" rx="' + (rx === undefined ? 0.8 : rx) + '" fill="' + fill + '"/>';
    }

    function c(x, y, rad, fill) {
        return '<circle cx="' + x.toFixed(1) + '" cy="' + y.toFixed(1) + '" r="' + rad.toFixed(2) + '" fill="' + fill + '"/>';
    }

    const PREVIEWS = {
        forest(rand) {
            // An effect and its interval per row, around a zero line.
            let s = r(31.6, 3, 0.8, 34, "var(--sm-pv-4)", 0);
            for (let i = 0; i < 5; i += 1) {
                const y = 6 + i * 7;
                const mid = 32 + (rand() - 0.5) * 34;
                const half = 4 + rand() * 7;
                s += r(Math.max(4, mid - half), y - 0.6, Math.min(56, half * 2), 1.2, INK[1], 0.6) + c(mid, y, 2.1, INK[0]);
            }
            return s;
        },
        grid(rand) {
            let s = "";
            for (let y = 0; y < 4; y += 1) for (let x = 0; x < 6; x += 1) s += r(6 + x * 8.8, 3 + y * 8.8, 7.6, 7.6, INK[Math.floor(rand() * 4)], 1);
            return s;
        },
        diverging(rand) {
            const tones = ["var(--sm-pv-warm)", "var(--sm-pv-1)", "var(--sm-pv-3)"];
            let s = "";
            for (let y = 0; y < 4; y += 1) for (let x = 0; x < 6; x += 1) {
                const t = x === y ? 2 : Math.floor(rand() * 3);
                s += r(6 + x * 8.8, 3 + y * 8.8, 7.6, 7.6, tones[t], 1);
            }
            return s;
        },
        binary(rand) {
            const tones = ["var(--sm-pv-4)", "var(--sm-pv-1)", "var(--sm-pv-warm)"];
            let s = "";
            for (let y = 0; y < 4; y += 1) for (let x = 0; x < 6; x += 1) s += r(6 + x * 8.8, 3 + y * 8.8, 7.6, 7.6, tones[Math.floor(rand() * 3)], 1);
            return s;
        },
        dots(rand) {
            let s = "";
            for (let y = 0; y < 3; y += 1) for (let x = 0; x < 7; x += 1) s += c(8 + x * 8, 9 + y * 11, 1 + rand() * 2.7, INK[1 + Math.floor(rand() * 3)]);
            return s;
        },
        violin(rand) {
            let s = "";
            [14, 32, 50].forEach((cx, i) => {
                const w = 5 + rand() * 3;
                const mid = 16 + rand() * 10;
                const d = "M" + cx + " 4 C" + (cx + w * 0.4) + " 9 " + (cx + w) + " " + (mid - 6) + " " + (cx + w) + " " + mid +
                    " C" + (cx + w) + " " + (mid + 8) + " " + (cx + 1.5) + " 31 " + cx + " 36 C" + (cx - 1.5) + " 31 " +
                    (cx - w) + " " + (mid + 8) + " " + (cx - w) + " " + mid + " C" + (cx - w) + " " + (mid - 6) + " " +
                    (cx - w * 0.4) + " 9 " + cx + " 4Z";
                s += '<path d="' + d + '" fill="' + INK[i + 1] + '" opacity="0.85"/>';
                s += '<path d="M' + cx + " 9V31" + '" stroke="var(--sm-pv-0)" stroke-width="1" opacity="0.7"/>';
            });
            return s;
        },
        map(rand) {
            let s = '<path d="M9 21c0-9 9-16 22-16s25 4 26 13-6 17-22 17S9 30 9 21z" fill="var(--sm-pv-0)" stroke="var(--sm-pv-1)" stroke-width="1"/>';
            for (let i = 0; i < 46; i += 1) {
                const a = rand() * Math.PI * 2;
                const d = Math.sqrt(rand());
                s += c(33 + Math.cos(a) * 21 * d, 20 + Math.sin(a) * 11 * d, 1.1, INK[1 + Math.floor(rand() * 3)]);
            }
            return s;
        },
        columns(rand) {
            let s = '<path d="M5 36.5h54" stroke="var(--sm-pv-2)" stroke-width="1"/>';
            for (let i = 0; i < 6; i += 1) {
                const hh = 9 + rand() * 24;
                s += r(8 + i * 8.6, 36 - hh, 6, hh, i % 2 ? INK[3] : INK[2], 1);
            }
            return s;
        },
        stacked(rand) {
            let s = "";
            for (let i = 0; i < 5; i += 1) {
                let y = 36;
                [0.2 + rand() * 0.3, 0.2 + rand() * 0.3].concat([1]).forEach((f, k) => {
                    const hh = k === 2 ? y - 4 : 32 * f * 0.6;
                    s += r(8 + i * 10.4, y - hh, 8, hh, INK[3 - k], 0.6);
                    y -= hh;
                });
            }
            return s;
        },
        hbars(rand) {
            let s = '<path d="M7.5 3v34" stroke="var(--sm-pv-2)" stroke-width="1"/>';
            for (let i = 0; i < 5; i += 1) s += r(8, 4.5 + i * 6.6, 12 + rand() * 40, 4.6, INK[1 + (i % 3)], 1);
            return s;
        },
        grouped(rand) {
            let s = '<path d="M5 36.5h54" stroke="var(--sm-pv-2)" stroke-width="1"/>';
            for (let i = 0; i < 4; i += 1) {
                const a = 8 + rand() * 22;
                const b = 8 + rand() * 22;
                s += r(8 + i * 13, 36 - a, 5, a, INK[2], 1) + r(13.6 + i * 13, 36 - b, 5, b, INK[3], 1);
            }
            return s;
        },
        hist(rand) {
            const hs = [3, 7, 13, 21, 27, 25, 18, 11, 6, 3];
            let s = '<path d="M5 36.5h54" stroke="var(--sm-pv-2)" stroke-width="1"/>';
            hs.forEach((v, i) => { s += r(7 + i * 5.1, 36 - v - rand() * 3, 4.6, v, INK[2], 0.5); });
            s += '<path d="M7 34C16 33 20 10 29 8s15 20 28 26" fill="none" stroke="var(--sm-pv-4)" stroke-width="1.3"/>';
            return s;
        },
        pie(rand) {
            let s = "";
            [[15, 20, 9], [34, 20, 8], [51, 20, 7]].forEach(([cx, cy, rad]) => {
                let a0 = -Math.PI / 2;
                [0.45 + rand() * 0.2, 0.2 + rand() * 0.1, 1].forEach((f, k) => {
                    const a1 = k === 2 ? Math.PI * 1.5 : a0 + f * Math.PI * 2;
                    const large = a1 - a0 > Math.PI ? 1 : 0;
                    s += '<path d="M' + cx + " " + cy + "L" + (cx + rad * Math.cos(a0)).toFixed(1) + " " + (cy + rad * Math.sin(a0)).toFixed(1) +
                        "A" + rad + " " + rad + " 0 " + large + " 1 " + (cx + rad * Math.cos(a1)).toFixed(1) + " " + (cy + rad * Math.sin(a1)).toFixed(1) +
                        'Z" fill="' + INK[3 - k] + '"/>';
                    a0 = a1;
                });
            });
            return s;
        },
        scatter(rand) {
            let s = "";
            [[18, 14, 3], [42, 12, 2], [34, 28, 1], [52, 27, 3]].forEach(([cx, cy, ink]) => {
                for (let i = 0; i < 16; i += 1) {
                    const a = rand() * Math.PI * 2;
                    const d = Math.sqrt(rand()) * 7;
                    s += c(cx + Math.cos(a) * d * 1.3, cy + Math.sin(a) * d, 1, INK[ink]);
                }
            });
            return s;
        },
        density() {
            let s = "";
            [[16, 9, "var(--sm-pv-0)"], [11, 6.4, "var(--sm-pv-1)"], [6.5, 3.8, "var(--sm-pv-2)"], [2.6, 1.6, "var(--sm-pv-4)"]].forEach(([rx, ry, fill]) => {
                s += '<ellipse cx="32" cy="20" rx="' + (rx * 1.5) + '" ry="' + (ry * 1.5) + '" fill="' + fill + '" transform="rotate(-24 32 20)"/>';
            });
            return s;
        },
        network(rand) {
            const nodes = [[12, 12], [30, 7], [50, 12], [20, 30], [40, 27], [55, 32]];
            const edges = [[0, 1], [1, 2], [0, 3], [1, 4], [3, 4], [4, 5], [2, 4]];
            let s = "";
            edges.forEach(([a, b], i) => {
                s += '<path d="M' + nodes[a][0] + " " + nodes[a][1] + "L" + nodes[b][0] + " " + nodes[b][1] +
                    '" stroke="' + (i % 3 ? "var(--sm-pv-2)" : "var(--sm-pv-warm)") + '" stroke-width="' + (1 + rand() * 1.6).toFixed(1) + '"/>';
            });
            nodes.forEach(([x, y], i) => { s += c(x, y, 2.6 + rand() * 2, INK[1 + (i % 3)]); });
            return s;
        },
        radii() {
            let s = '<path d="M6 4v32h54" fill="none" stroke="var(--sm-pv-2)" stroke-width="1"/>';
            s += '<path d="M8 30C18 28 24 12 34 10s16 6 24 5" fill="none" stroke="var(--sm-pv-4)" stroke-width="1.5"/>';
            s += '<path d="M8 33C18 32 28 24 38 22s14 0 20 1" fill="none" stroke="var(--sm-pv-2)" stroke-width="1.5"/>';
            s += '<path d="M8 20C20 22 30 30 40 31s12-1 18-1" fill="none" stroke="var(--sm-pv-warm)" stroke-width="1.5"/>';
            [8, 23, 38, 58].forEach((x) => { s += c(x, 36, 1.2, "var(--sm-pv-3)"); });
            return s;
        },
        voronoi() {
            const polys = [
                "5,4 22,4 19,16 6,19", "22,4 40,4 37,13 19,16", "40,4 59,4 59,15 46,18 37,13",
                "6,19 19,16 25,27 14,36 5,36", "19,16 37,13 46,18 41,29 25,27", "46,18 59,15 59,36 44,36 41,29",
                "14,36 25,27 41,29 44,36",
            ];
            const fills = [1, 3, 2, 2, 0, 3, 1];
            return polys.map((p, i) => '<polygon points="' + p + '" fill="' + (fills[i] ? INK[fills[i]] : "var(--sm-pv-0)") +
                '" stroke="var(--sm-pv-bg)" stroke-width="1.4" stroke-linejoin="round"/>').join("");
        },
        table() {
            let s = r(6, 4, 52, 6, "var(--sm-pv-2)", 1.2);
            for (let i = 0; i < 4; i += 1) {
                s += r(6, 13 + i * 6.4, 18, 3, "var(--sm-pv-2)", 1) + r(30, 13 + i * 6.4, 10, 3, "var(--sm-pv-1)", 1) +
                    r(46, 13 + i * 6.4, 12, 3, "var(--sm-pv-1)", 1);
            }
            return s;
        },
    };

    const BY_VIEW = { correlation: "diverging", similarity: "diverging", binary: "binary" };

    function preview(view) {
        const kind = BY_VIEW[view.id] || (PREVIEWS[view.icon] ? view.icon : "grid");
        let seed = 7;
        String(view.id).split("").forEach((ch) => { seed = (seed * 31 + ch.charCodeAt(0)) >>> 0; });
        return h("span.sm-ui-preview", { "aria-hidden": "true",
            html: '<svg viewBox="0 0 64 40" preserveAspectRatio="xMidYMid meet">' + PREVIEWS[kind](rng(seed)) + "</svg>" });
    }

    return { float, close, isOpen, pickList, segmented, toggle, field, Accordion, preview, combo, ring };
})();
