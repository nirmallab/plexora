/**
 * FigureInspector - the top and the foot of the properties column.
 *
 * The column always describes the selection. With nothing selected it is the
 * PAGE: its size, margins and background, the guides, the figure-wide style
 * and the actions that act on everything on it. With one object it is that
 * object's position and size, in the figure's unit; the detailed settings of
 * each kind (an image's scale bar and captions, a caption's type, a line's
 * heads) follow underneath, in the four object panels this column hosts. With
 * several objects it is how to line them up -- align, distribute, match, lay
 * out -- and, when they are a layout, that layout's own settings.
 *
 * The foot (`tailRoot`) is a single image's source and output quality: last,
 * because it is read rather than changed.
 *
 * Every field commits on `change`, never on `input`, for the reason
 * `FigureTextPanel`'s size field gives: every prefix of a number is a number,
 * and committing them would put "2" in the undo history on the way to "20".
 * Fields are `type="text"` with `inputmode="decimal"` for the same reason.
 */
class FigureInspector {

    constructor({ root, tailRoot, titleEl, subEl, state, canvas, workspace }) {
        this.root = root;
        this.tailRoot = tailRoot;
        this.titleEl = titleEl;
        this.subEl = subEl;
        this.state = state;
        this.canvas = canvas;
        this.workspace = workspace;
        this.ids = [];
        //: Whether typing a panel's width also sets its height. On, because a
        //: panel's shape is the shape of the field it shows, and changing one
        //: side alone crops it.
        this.keepAspect = true;
    }

    /** Let go of the window: the workspace is a routed page the window
     *  outlives, and a listener left here pins the whole workspace. */
    destroy() {
        window.removeEventListener("plexora:fb-folds", this._onFolds);
    }

    setup() {
        this._onFolds = () => { if (this.root?.isConnected) this.refresh(); };
        window.addEventListener("plexora:fb-folds", this._onFolds);
        for (const root of [this.root, this.tailRoot]) {
            if (!root) continue;
            root.addEventListener("change", (event) => this.changed(event));
            root.addEventListener("click", (event) => this.clicked(event));
            root.addEventListener("keydown", (event) => this.keyDown(event));
        }
    }

    // -- units ---------------------------------------------------------------

    get unit() {
        const name = this.workspace.viewOptions?.prefs.units || "mm";
        return { name: name, ...(FigureViewOptions.UNITS[name] || FigureViewOptions.UNITS.mm) };
    }

    /** Millimetres as the figure's unit, written briefly. */
    show(mm) {
        if (mm === null || mm === undefined || !Number.isFinite(mm)) return "";
        const value = mm / this.unit.per;
        const digits = this.unit.name === "in" ? 3 : (this.unit.name === "pt" ? 1 : 1);
        return String(Number(value.toFixed(digits)));
    }

    /** A typed number in the figure's unit, as millimetres, or null. */
    read(text) {
        const value = parseFloat(String(text).replace(",", "."));
        return Number.isFinite(value) ? value * this.unit.per : null;
    }

    // -- what it shows -------------------------------------------------------

    update(ids) {
        this.ids = (ids || []).filter((id) => this.canvas.objectById(id));
        this.render();
    }

    get kind() {
        const objects = this.ids.map((id) => this.canvas.objectById(id)).filter(Boolean);
        if (!objects.length) return "page";
        if (objects.length > 1) return "multi";
        const only = objects[0];
        if (this.state.panel(this.ids[0])) return only.placement ? "image" : "page";
        if (FigureCanvas.isStrokeType(only.type)) return "line";
        return only.type === "text" ? "text" : "shape";
    }

    render() {
        if (!this.root || !this.state.document) return;
        // A field being typed into is not torn out by the document change its
        // own previous commit caused. The caret goes back where it was.
        const focus = this.focused();
        const kind = this.kind;
        const titles = {
            page: "Page", image: "Image", text: "Text", shape: "Shape", line: "Line",
            multi: `${this.ids.length} objects`,
        };
        let title = titles[kind];
        let sub = "";
        let meta = "";
        if (kind === "page") {
            const page = this.canvas.page;
            const index = this.state.pages.findIndex((p) => p.page_id === page?.page_id);
            const preset = page && FigureSchema.PAGE_PRESETS[page.preset];
            sub = "Document properties";
            meta = page ? `${index + 1} of ${this.state.pages.length}` : "";
        } else if (kind === "multi") {
            const panels = this.ids.filter((id) => this.state.panel(id));
            const group = this.canvas.groupOfExactly(this.ids);
            if (panels.length === this.ids.length) {
                // Images only: say how many, and which panels by letter.
                title = `${panels.length} images selected`;
                const rows = this.workspace.layers?.rows() || [];
                const letters = panels.map((id) => rows.find((row) => row.id === id)?.letter).filter(Boolean);
                sub = letters.length ? `Panels ${FigureInspector.letterList(letters)}` : "";
                if (group && group.name) sub = sub ? `${sub} · ${group.name}` : group.name;
            } else if (group) {
                const preset = group.layout && FigureLayouts.byId(group.layout.preset);
                sub = (group.name || (group.layout ? "Layout" : "Group"))
                    + (preset ? ` · ${preset.label} layout` : "");
            }
        } else {
            const row = this.workspace.layers?.rows().find((each) => each.id === this.ids[0]);
            sub = row ? (row.letter ? `Panel ${row.letter}` : row.name) : "";
        }
        if (this.titleEl) this.titleEl.textContent = title;
        if (this.subEl) this.subEl.textContent = sub;
        // Which page, and the page's menu: only while the page is what the
        // inspector shows.
        const metaEl = this.workspace.el?.("fb_inspector_meta");
        if (metaEl) metaEl.textContent = meta;
        const more = this.workspace.el?.("fb_inspector_more");
        if (more) more.hidden = kind !== "page";

        const body = {
            page: () => this.pageSections(),
            image: () => this.boxSection(false),
            // A caption's or a shape's own settings come first and its box
            // last, as the design orders them: the box is in the tail.
            text: () => "",
            shape: () => "",
            line: () => "",
            multi: () => (this.allImages ? this.multiImageSections() : this.multiSections()),
        }[kind]();
        // Written only when it changed: most document changes (a nudge, a
        // drag) change a field or two, and an untouched inspector keeps its
        // DOM -- and whatever the keyboard is on -- exactly as it was.
        if (body !== this._body) {
            this._body = body;
            this.root.innerHTML = body;
            this.root.hidden = !body;
        }
        if (this.tailRoot) {
            const tail = kind === "image" ? this.qualitySection(this.state.panel(this.ids[0]))
                : (kind === "multi" ? (this.allImages ? this.multiOrderFold() : this.orderSection())
                    : (kind === "text" || kind === "shape" ? this.boxSection(true) : ""));
            if (tail !== this._tail) {
                this._tail = tail;
                this.tailRoot.innerHTML = tail;
                this.tailRoot.hidden = !tail;
            }
        }
        FigureInspector.playSwitch(this.root);
        FigureInspector.playSwitch(this.tailRoot);
        this.restore(focus);
    }

    /** Redraw even though nothing in the document changed -- to put a
     *  field's stored value back over what was typed into it. */
    refresh() {
        this._body = null;
        this._tail = null;
        this.render();
    }

    focused() {
        const active = document.activeElement;
        if (!active || !(this.root?.contains(active) || this.tailRoot?.contains(active))) return null;
        // Buttons too, not only fields: a section header or a segment that
        // was pressed from the keyboard is rebuilt by the redraw its press
        // caused, and focus fell to <body> -- the next Tab started over at
        // the top of the page.
        const keys = ["field", "ifold", "style", "seg", "switch", "arrange", "act"];
        const key = keys.find((name) => active.dataset[name] !== undefined);
        if (!key) return null;
        return { key: key, value: active.dataset[key], pick: active.dataset.value,
                 field: active.dataset.field, start: active.selectionStart, end: active.selectionEnd };
    }

    restore(focus) {
        if (!focus || !focus.key) return;
        const attr = focus.key.replace(/[A-Z]/g, (c) => "-" + c.toLowerCase());
        const escape = (value) => FigureSchema.cssEscape(String(value));
        let selector = `[data-${attr}="${escape(focus.value)}"]`;
        if (focus.pick !== undefined && focus.key !== "field") selector += `[data-value="${escape(focus.pick)}"]`;
        const el = this.root.querySelector(selector) || this.tailRoot?.querySelector(selector);
        if (!el || el === document.activeElement) return;
        el.focus();
        if (typeof el.setSelectionRange === "function" && focus.start !== null) {
            el.setSelectionRange(focus.start, focus.end);
        }
    }

    // -- small markup helpers --------------------------------------------------

    static escape(value) { return FigureSchema.escapeHtml(String(value ?? "")); }

    section(title, body, aside = "") {
        return `<section class="fb-insp-section">
            <header class="fb-insp-section-head"><span>${FigureInspector.escape(title)}</span>${aside}</header>
            ${body}</section>`;
    }

    /**
     * A number in one box, as the design draws it: an optional name (or icon)
     * on the left, the value, an optional unit on the right. The unit of a
     * position or size is in the section's head, not in every box.
     */
    field(name, label, value, options = {}) {
        const escape = FigureInspector.escape;
        const lead = options.icon
            ? FigureIcons.svg(options.icon, 12, "fb-insp-field-icon")
            : (label ? `<span class="fb-insp-field-label">${escape(label)}</span>` : "");
        return `<label class="fb-insp-field${options.cls ? ` ${options.cls}` : ""}"
                       ${options.style ? `style="${options.style}"` : ""}
                       title="${escape(options.title || "")}">
            ${lead}
            <input type="text" inputmode="decimal" class="fb-insp-input" data-field="${name}"
                   value="${escape(value)}" placeholder="${escape(options.placeholder || "")}"
                   aria-label="${escape(options.aria || label)}" autocomplete="off" spellcheck="false"
                   ${options.disabled ? "disabled" : ""}>
            ${options.suffix ? `<span class="fb-insp-suffix">${escape(options.suffix)}</span>` : ""}
        </label>`;
    }

    toggle(name, label, on) {
        return `<label class="fb-insp-switch-row">
            <span>${FigureInspector.escape(label)}</span>
            <button type="button" role="switch" class="fb-switch${on ? " is-on" : ""}"
                    data-switch="${name}" aria-checked="${on ? "true" : "false"}"
                    aria-label="${FigureInspector.escape(label)}"><span></span></button>
        </label>`;
    }

    segmented(name, options, current, label, style = "") {
        return `<div class="fb-insp-seg" role="group" aria-label="${FigureInspector.escape(label)}"
                     ${style ? `style="${style}"` : ""}>
            ${options.map(([value, text, icon]) => `<button type="button"
                class="${value === current ? "is-on" : ""}" data-seg="${name}" data-value="${value}"
                aria-pressed="${value === current ? "true" : "false"}"
                ${icon ? `title="${FigureInspector.escape(text)}" aria-label="${FigureInspector.escape(text)}"` : ""}
                >${icon ? FigureIcons.svg(icon, 14) : FigureInspector.escape(text)}</button>`).join("")}
        </div>`;
    }

    /** The design's outlined button: white, a hairline, 11px medium. */
    action(act, label, icon, extra = "") {
        return `<button type="button" class="fb-insp-btn" data-act="${act}" ${extra}>
            ${icon ? FigureIcons.svg(FigureIcons.PATHS[icon] ? icon : (FigureIcons.fromFontAwesome(icon) || icon), 14) : ""}${FigureInspector.escape(label)}</button>`;
    }

    // -- nothing selected: the page ---------------------------------------------

    /**
     * Which of the Page view's folding parts are open, remembered in this
     * browser. Page setup, Layout and Figure style start open; the less used
     * settings (more page settings, borders & typography, guides) start shut.
     */
    static get OPEN_DEFAULTS() {
        return { page: true, layout: false, style: false, guides: false,
                 page_more: false, style_more: false, letters: false, bars: false,
                 // One image: the scale bar open, everything else folded.
                 io_bar: true, io_labels: false, io_letter: false,
                 io_bar_more: false, io_letter_more: false,
                 io_crop: false, io_box: false, io_look: false, io_source: false, io_render: false,
                 // Several images: layout open, the rest folded.
                 io_m_layout: true, io_m_bars: false, io_m_labels: false, io_m_letters: false,
                 io_m_order: false, io_m_look: false, io_m_render: false, io_m_bar_more: false };
    }

    /** Sections that fold as an accordion: opening one shuts its siblings. */
    static get ACCORDIONS() {
        return [
            ["page", "layout", "style", "guides"],
            ["io_bar", "io_labels", "io_letter", "io_crop", "io_box", "io_look", "io_source", "io_render"],
            ["io_m_layout", "io_m_bars", "io_m_labels", "io_m_letters", "io_m_order", "io_m_look", "io_m_render"],
        ];
    }

    /** What each disclosure sits inside: it shows only while that is open. */
    static get FOLD_PARENTS() {
        return { page_more: "page",
                 io_bar_more: "io_bar", io_letter_more: "io_letter", io_m_bar_more: "io_m_bars" };
    }

    /** One view's folds -- the Page view's or one image's. Opening any of
     *  them shuts every other one in the view except those it sits inside. */
    static get FOLD_VIEWS() {
        return [
            ["page", "layout", "style", "guides", "page_more"],
            ["io_bar", "io_labels", "io_letter", "io_crop", "io_box", "io_look", "io_source", "io_render",
             "io_bar_more", "io_letter_more"],
            ["io_m_layout", "io_m_bars", "io_m_labels", "io_m_letters", "io_m_order", "io_m_look",
             "io_m_render", "io_m_bar_more"],
        ];
    }

    static foldAncestors(id) {
        const out = [];
        for (let at = FigureInspector.FOLD_PARENTS[id]; at; at = FigureInspector.FOLD_PARENTS[at]) out.push(at);
        return out;
    }

    static foldRaw(id) {
        if (!FigureInspector.openState) {
            try {
                FigureInspector.openState = JSON.parse(
                    window.localStorage.getItem("plexora.figure_builder.inspector_open") || "{}") || {};
            } catch (error) {
                FigureInspector.openState = {};
            }
        }
        const state = FigureInspector.openState;
        return id in state ? Boolean(state[id]) : Boolean(FigureInspector.OPEN_DEFAULTS[id]);
    }

    /** Open or shut, kept for every inspector on the page (the image panel
     *  reads the same store), in this browser. In an accordion only the first
     *  open member counts, so an older saved state with several open shows one. */
    static foldOpen(id) {
        const parent = FigureInspector.FOLD_PARENTS[id];
        if (parent && !FigureInspector.foldOpen(parent)) return false;
        const group = FigureInspector.ACCORDIONS.find((members) => members.includes(id));
        if (!group) return FigureInspector.foldRaw(id);
        return group.find((member) => FigureInspector.foldRaw(member)) === id;
    }

    /**
     * Motion that plays once, for the change the user just made, and never
     * again on the redraws that follow it (the sidebars redraw on every
     * document change, and an animation in the markup replays every time).
     */
    static entering(id) {
        const last = FigureInspector.lastOpened;
        return Boolean(last && last.id === id && performance.now() - last.at < 450);
    }

    /** A switch is about to be pressed: remember which, and which way. */
    static noteSwitch(el) {
        if (!el) return;
        FigureInspector.lastSwitch = {
            selector: el.dataset.act ? `[role="switch"][data-act="${el.dataset.act}"]`
                : `[role="switch"][data-switch="${el.dataset.switch}"]`,
            on: el.getAttribute("aria-checked") !== "true",
            at: performance.now(),
        };
    }

    /** After a redraw: slide the pressed switch's knob, and fade in what
     *  turning it on revealed. Once. */
    static playSwitch(root) {
        const last = FigureInspector.lastSwitch;
        if (!root || !last || performance.now() - last.at > 800) return;
        const el = root.querySelector(last.selector);
        if (!el) return;
        FigureInspector.lastSwitch = null;
        el.classList.add(last.on ? "is-turning-on" : "is-turning-off");
        if (!last.on) return;
        const head = el.closest(".fb-insp-section-head, .fb-io-head");
        let next = head && head.nextElementSibling;
        while (next) {
            next.classList.add("fb-reveal");
            next = next.nextElementSibling;
        }
    }

    static foldFlip(id) {
        const open = FigureInspector.foldOpen(id);
        FigureInspector.lastOpened = open ? null : { id: id, at: performance.now() };
        // Opening shuts everything else in the view but what this sits in;
        // shutting shuts what is inside it too.
        const view = FigureInspector.FOLD_VIEWS.find((members) => members.includes(id)) || [id];
        const keep = open ? [] : FigureInspector.foldAncestors(id);
        for (const member of view) {
            if (member === id || keep.includes(member)) continue;
            if (!open || FigureInspector.foldAncestors(member).includes(id)) {
                FigureInspector.openState[member] = false;
            }
        }
        for (const member of keep) FigureInspector.openState[member] = true;
        FigureInspector.openState[id] = !open;
        // Both halves of the sidebar draw folds (this and FigureImagePanel);
        // each redraws when either changes, so a section the other half drew
        // shuts on screen too.
        window.setTimeout(() => window.dispatchEvent(new CustomEvent("plexora:fb-folds")), 0);
        try {
            window.localStorage.setItem("plexora.figure_builder.inspector_open",
                JSON.stringify(FigureInspector.openState));
        } catch (error) {
            /* Private-browsing modes throw: open for this visit only. */
        }
    }

    /**
     * One folding row of the image inspector: an icon, the name, what it is set
     * to now (so it need not be opened to be read), and a caret. `body` is a
     * thunk, so a shut section costs nothing. `flag` is shown even while shut
     * (a resolution warning).
     */
    static foldRow({ id, cls, icon, title, summary, body, flag = "" }) {
        const escape = FigureInspector.escape;
        const open = FigureInspector.foldOpen(id);
        return `<section class="fb-io fb-isec ${cls}${open ? " is-open" : ""}">
            <button type="button" class="fb-isec-row" data-ifold="${id}" aria-expanded="${open}">
                <span class="fb-isec-icon">${icon}</span>
                <span class="fb-isec-title">${escape(title)}</span>
                <span class="fb-isec-summary" title="${escape(summary)}">${escape(summary)}</span>${flag}
                ${FigureIcons.svg("chevR", 14, "fb-isec-caret")}
            </button>
            ${open ? `<div class="fb-isec-body${FigureInspector.entering(id) ? " is-entering" : ""}">${body()}</div>` : ""}
        </section>`;
    }

    /** Line icons for the image inspector's sections, one stroke weight. */
    static ioIcon(name) {
        const paths = {
            bar: '<path d="M4 8.5v7M20 8.5v7M4 12h16"/>',
            grid: '<rect x="3" y="3" width="7.5" height="7.5" rx="1.5"/><rect x="13.5" y="3" width="7.5" height="7.5" rx="1.5"/><rect x="3" y="13.5" width="7.5" height="7.5" rx="1.5"/><rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1.5"/>',
            labels: '<path d="M12 3 3 7.5l9 4.5 9-4.5L12 3z"/><path d="m3 12 9 4.5 9-4.5"/><path d="m3 16.5 9 4.5 9-4.5"/>',
            crop: '<path d="M6 2v14a2 2 0 0 0 2 2h14"/><path d="M18 22V8a2 2 0 0 0-2-2H2"/>',
            move: '<path d="M12 2v20M2 12h20"/><path d="m9 5 3-3 3 3M9 19l3 3 3-3M5 9l-3 3 3 3M19 9l3 3-3 3"/>',
            look: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 15h18"/>',
            info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 7.5v.5"/>',
            wand: '<path d="m15 4 5 5L9 20l-5-5L15 4z"/><path d="M5 3v3M3.5 4.5h3M19 15v3M17.5 16.5h3"/>',
        };
        return `<svg viewBox="0 0 24 24" width="17" height="17" aria-hidden="true" fill="none" stroke="currentColor"
            stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${paths[name] || ""}</svg>`;
    }

    isOpen(id) {
        return FigureInspector.foldOpen(id);
    }

    flip(id) {
        FigureInspector.foldFlip(id);
    }

    /** A Page-view section: an icon, a name, a quiet note on the right, and
     *  (when it folds) a caret; the whole head opens and shuts it. */
    pageSection(id, icon, title, aside, body, foldable = false, cls = "", control = "") {
        const escape = FigureInspector.escape;
        const open = !foldable || this.isOpen(id);
        // An icon is a FigureIcons name, or (starting with "<") markup of its own.
        const glyph = String(icon).startsWith("<") ? icon : FigureIcons.svg(icon, 17, "fb-pg-icon");
        const head = `${glyph}
            <span class="fb-pg-title">${escape(title)}</span>
            ${aside ? `<span class="fb-pg-aside">${escape(aside)}</span>` : ""}
            ${foldable ? FigureIcons.svg(open ? "chevD" : "chevR", 13, "fb-pg-caret") : ""}`;
        return `<section class="fb-pg-section${open ? " is-open" : ""}${cls ? ` ${cls}` : ""}">
            ${foldable && control
                ? `<div class="fb-pg-headrow"><button type="button" class="fb-pg-head" data-style="${id}"
                        aria-expanded="${open}">${head}</button>${control}</div>`
                : foldable ? `<button type="button" class="fb-pg-head" data-style="${id}" aria-expanded="${open}">${head}</button>`
                : `<header class="fb-pg-head">${head}</header>`}
            ${open ? `<div class="fb-pg-body${FigureInspector.entering(id) ? " is-entering" : ""}">${body}</div>` : ""}
        </section>`;
    }

    /** A property row: the name on the left, the control on the right. */
    prop(label, control) {
        return `<div class="fb-pg-prop"><span class="fb-pg-label">${FigureInspector.escape(label)}</span>
            <span class="fb-pg-control">${control}</span></div>`;
    }

    /** A quiet link that opens more settings in place. */
    disclose(id, label, body) {
        const open = this.isOpen(id);
        return `<button type="button" class="fb-pg-more" data-style="${id}" aria-expanded="${open}">
                <span>${FigureInspector.escape(label)}</span>${FigureIcons.svg(open ? "chevD" : "chevR", 12)}</button>
            ${open ? `<div class="fb-pg-more-body">${body}</div>` : ""}`;
    }

    // -- nothing selected: the page ---------------------------------------------

    pageSections() {
        const page = this.canvas.page;
        if (!page) return "";
        const escape = FigureInspector.escape;
        const u = this.unit.label;
        const presets = Object.entries(FigureSchema.PAGE_PRESETS);
        const margins = page.margins_mm;
        const same = margins.top === margins.right && margins.top === margins.bottom
            && margins.top === margins.left;
        const clear = page.background === FigureCanvas.TRANSPARENT;
        const white = !clear && /^#fff(fff)?$/i.test(page.background);
        const prefs = this.workspace.viewOptions?.prefs || {};
        const settings = this.state.document.settings;
        const style = settings.style;
        const preset = FigureSchema.PAGE_PRESETS[page.preset];
        const mm = (value) => Number(value.toFixed(1));

        // -- Page setup: size, orientation, background; the rest folded.
        const ratio = page.size_mm.w / page.size_mm.h;
        const thumb = `<span class="fb-pg-thumb" aria-hidden="true"><span style="${ratio >= 1
            ? `width:30px;height:${Math.max(10, Math.round(30 / ratio))}px`
            : `height:34px;width:${Math.max(10, Math.round(34 * ratio))}px`};background:${clear
            ? "repeating-conic-gradient(#e5e7eb 0% 25%, #fff 0% 50%) 0 0/8px 8px" : escape(page.background)}"></span></span>`;
        const sizeRow = `<div class="fb-pg-size">${thumb}
            <span class="fb-pg-size-pick">
                <select class="fb-insp-select" data-field="page_preset" aria-label="Page size">
                    ${presets.map(([id, spec]) => `<option value="${id}"
                        ${page.preset === id ? "selected" : ""}>${escape(spec.label)}</option>`).join("")}
                    <option value="custom" ${page.preset === "custom" ? "selected" : ""}>Custom</option>
                </select>
                <span class="fb-pg-sub">${mm(page.size_mm.w)} × ${mm(page.size_mm.h)} mm</span>
            </span></div>`;
        const shape = (w, h) => `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><rect x="${(16 - w) / 2}" y="${(16 - h) / 2}" width="${w}" height="${h}" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>`;
        const orientation = `<span class="fb-pg-seg" role="group" aria-label="Orientation">
            ${[["portrait", "Portrait", shape(9, 13)], ["landscape", "Landscape", shape(13, 9)]].map(([value, text, svg]) => `
                <button type="button" class="${page.orientation === value ? "is-on" : ""}" data-seg="orientation"
                        data-value="${value}" aria-pressed="${page.orientation === value}" title="${text}">
                    ${svg}<span>${text}</span></button>`).join("")}</span>`;
        // Four round choices: white, black, any color (the wheel opens the
        // app's palette, and shows the chosen color in its middle), and
        // Transparent, the one background that is not a color.
        const hex = clear ? "" : FigureColorField.hex(page.background);
        const black = hex === "#000000";
        const custom = !clear && !white && !black;
        const dot = (value, cls, label, on) => `<button type="button" class="fb-pg-dot ${cls}${on ? " is-on" : ""}"
                data-seg="background" data-value="${value}" aria-pressed="${on}"
                title="${label}" aria-label="${label}"></button>`;
        const background = `<span class="fb-pg-bg" role="group" aria-label="Background">
            ${dot("#ffffff", "is-white", "White", white)}
            ${dot("#000000", "is-black", "Black", black)}
            <button type="button" class="fb-pg-dot is-wheel${custom ? " is-on" : ""}" data-swatch="page_background"
                    aria-pressed="${custom}" title="${custom ? `Color ${escape(String(page.background).toUpperCase())}` : "Choose a color"}"
                    aria-label="Choose a background color">${custom
                ? `<span class="fb-pg-dot-core" style="background:${escape(page.background)}"></span>` : ""}</button>
            ${dot(FigureCanvas.TRANSPARENT, "is-clear", "Transparent (PNG export keeps it transparent)", clear)}
        </span>`;
        const more = `<div class="fb-insp-grid2">
                ${this.field("page_w", "W", this.show(page.size_mm.w), { suffix: u, aria: "Page width" })}
                ${this.field("page_h", "H", this.show(page.size_mm.h), { suffix: u, aria: "Page height" })}
            </div>
            ${this.prop("Margins", this.field("page_margins", "", same ? this.show(margins.top) : "",
                        { suffix: u, placeholder: same ? "" : "Mixed", aria: "Margins", cls: "fb-pg-num" }))}
            ${this.prop("Units", `<select class="fb-insp-select" data-field="units" aria-label="Units">
                ${Object.entries(FigureViewOptions.UNITS).map(([name, spec]) => `<option value="${name}"
                    ${this.unit.name === name ? "selected" : ""}>${{ mm: "Millimetres", in: "Inches", pt: "Points" }[name] || spec.label}</option>`).join("")}
            </select>`)}`;
        const pageBody = sizeRow
            + this.prop("Orientation", orientation)
            + this.prop("Background", background)
            + this.disclose("page_more", "Margins, dimensions & units", more);

        // -- Layout: three commands and the gap.
        const command = (act, icon, label, title) => `<button type="button" class="fb-pg-cmd"
                data-act="${act}" title="${escape(title)}">${FigureIcons.svg(icon, 16)}<span>${escape(label)}</span></button>`;
        const layoutBody = `<div class="fb-pg-cmds">
                ${command("grid_images", "grid", "Grid", "Arrange the images on this page in a grid")}
                ${command("fit_content", "fit", "Fit", "Fit everything on this page inside its margins")}
                ${command("center_content", "alignC", "Center", "Center everything on the page")}
            </div>
            <div class="fb-pg-prop fb-pg-spacing">
                <span class="fb-pg-label">Panel spacing</span>
                <span class="fb-pg-control">${this.field("gutter", "", String(Number(style.gutter_mm.toFixed(2))),
                        { suffix: "mm", aria: "Space between panels", cls: "fb-pg-num",
                          title: "The space between panels in new layouts and arrangements" })}</span>
                <button type="button" class="fb-link fb-pg-all-layouts" data-act="layouts"
                        title="Every layout preset, with its gaps and options">All layouts${
                    FigureIcons.svg("chevR", 12)}</button>
            </div>`;

        // -- Panel labels: the figure's lettering scheme and size. Scale bars,
        // borders and type are set with images selected.
        const scheme = (FigureWorkspace.LABEL_STYLES.find(([value]) => value === settings.label_style)
            || [null, "A, B, C"])[1];
        // The switch is on when every image on this page shows its letter.
        const pageImages = FigureSchema.panelsOnPage(this.state.document, this.canvas.pageId);
        const letterCount = pageImages.filter((panel) => panel.label.visible).length;
        const lettersOn = pageImages.length > 0 && letterCount === pageImages.length;
        const labelsBody = `<div class="fb-pg-style-body">
                ${this.segmented("label_style", FigureWorkspace.LABEL_STYLES, settings.label_style, "Panel letters")}
                ${this.prop("Size", this.field("label_size", "", String(style.label_size_pt),
                            { suffix: "pt", aria: "Panel letter size", cls: "fb-pg-num" }))}
            </div>`;

        // -- Guides & snapping, folded, with how many are on.
        const guideKeys = [["margins", "Margins", prefs.margins], ["rulers", "Rulers", prefs.rulers],
                           ["grid", "Grid", prefs.grid], ["smartGuides", "Snap to objects", prefs.smartGuides],
                           ["snapGrid", "Snap to grid", prefs.snapGrid],
                           ["alignGuides", "Alignment guides", prefs.alignGuides !== false]];
        const active = guideKeys.filter(([, , on]) => on).length;
        // What is drawn over the page, then what the pointer snaps to.
        const switches = (keys) => `<div class="fb-pg-switches">${
            keys.map(([key, label, on]) => this.toggle(key, label, on)).join("")}</div>`;
        const guidesBody = switches(guideKeys.slice(0, 3))
            + '<hr class="fb-pg-rule">'
            + switches(guideKeys.slice(3))
            + '<p class="fb-pg-note">Guides are never exported.</p>';
        // Crop marks round a frame: the page's guides, drawn as guides.
        // A line icon like every other section's: no tile, the same size.
        const guidesGlyph = "guides";

        return `<div class="fb-pg">`
            + this.pageSection("page", "file", "Page setup", preset ? "" : "Custom", pageBody, true)
            + this.pageSection("layout", "grid", "Layout", "Arrange", layoutBody, true)
            + this.pageSection("style", "label", "Panel labels",
                letterCount ? `${scheme} · ${style.label_size_pt} pt` : "Off", labelsBody, true, "",
                `<button type="button" role="switch" class="fb-switch${lettersOn ? " is-on" : ""}"
                    data-switch="all:labels" aria-checked="${lettersOn}"
                    ${pageImages.length ? "" : "disabled"}
                    title="${lettersOn ? "Hide the panel labels on this page" : "Show panel labels on every image on this page"}"
                    aria-label="Panel labels"><span></span></button>`)
            + this.pageSection("guides", guidesGlyph, "Guides & snapping", `${active} active`, guidesBody, true,
                "fb-pg-guides")
            + `</div>`;
    }

    // -- one object: where it is and how big --------------------------------------

    boxSection(rotatable) {
        const object = this.canvas.objectById(this.ids[0]);
        const panel = this.state.panel(this.ids[0]);
        const box = panel ? panel.placement : object.geometry;
        const u = this.unit.label;
        const lock = panel ? `<button type="button" class="fb-insp-ib is-bare is-small${this.keepAspect ? " is-on" : ""}"
                data-act="aspect" aria-pressed="${this.keepAspect}"
                title="${this.keepAspect ? "Keep proportions (on)" : "Keep proportions (off)"}"
                aria-label="Keep proportions">${FigureIcons.svg("link", 12)}</button>` : "";
        const last = rotatable
            ? `<div class="fb-insp-grid2">
                ${this.field("rotation", "", String(Number((box.rotation || 0).toFixed(1))),
                             { icon: "rotate", suffix: "°", aria: "Rotation" })}
                <span></span>
            </div>`
            : `<div class="fb-io-turn">
                <div class="fb-insp-seg" role="group" aria-label="Flip">
                    <button type="button" class="${box.flip_h ? "is-on" : ""}" data-arrange="flip_h"
                            aria-pressed="${Boolean(box.flip_h)}" title="Flip horizontally"
                            aria-label="Flip horizontally">${FigureIcons.svg("flipH", 14)}</button>
                    <button type="button" class="${box.flip_v ? "is-on" : ""}" data-arrange="flip_v"
                            aria-pressed="${Boolean(box.flip_v)}" title="Flip vertically"
                            aria-label="Flip vertically">${FigureIcons.svg("flipV", 14)}</button>
                </div>
                ${this.field("rotation", "", String(Number((box.rotation || 0).toFixed(1))),
                             { icon: "rotate", suffix: "°", aria: "Rotation",
                               title: "Angle, clockwise. Or drag just outside a corner of the image." })}
            </div>
            ${this.action("reset_shape", "Reset size", "", 'title="Back to the shape of the field it captured"')}`;
        const fields = `
            <div class="fb-insp-grid2">
                ${this.field("x", "X", this.show(box.x_mm), { aria: "X position" })}
                ${this.field("y", "Y", this.show(box.y_mm), { aria: "Y position" })}
            </div>
            ${panel ? `<div class="fb-insp-row fb-insp-wh">
                ${this.field("w", "W", this.show(box.w_mm), { aria: "Width" })}
                ${lock}
                ${this.field("h", "H", this.show(box.h_mm), { aria: "Height" })}
            </div>` : `<div class="fb-insp-grid2">
                ${this.field("w", "W", this.show(box.w_mm), { aria: "Width" })}
                ${this.field("h", "H", this.show(box.h_mm), { aria: "Height" })}
            </div>`}
            ${last}
            ${object.type === "text" ? `<div class="fb-insp-switch-row"><span>Height follows the text</span>
                <button type="button" role="switch" class="fb-switch${object.style?.autofit ? " is-on" : ""}"
                        data-act="text_autofit" aria-checked="${Boolean(object.style?.autofit)}"
                        aria-label="Height follows the text"><span></span></button></div>` : ""}`;
        const aside = object.locked
            ? `<span class="fb-insp-aside">${FigureIcons.svg("lock", 12)} Locked</span>`
            : `<span class="fb-insp-aside">${u}</span>`;
        if (!panel) return this.section(object.type === "text" ? "Box" : "Position and size", fields, aside);
        // One image: its identity at the top; crop and position fold, below
        // the scale bar, channel labels and panel label (see .fb-io order).
        return this.imageLead(panel)
            + FigureInspector.foldRow({
                id: "io_box", cls: "fb-io-box", icon: FigureInspector.ioIcon("move"),
                title: "Position & size",
                summary: `X ${this.show(box.x_mm)} · Y ${this.show(box.y_mm)} ${u}`,
                body: () => `${object.locked ? `<p class="fb-insp-note">${FigureIcons.svg("lock", 12)} Locked</p>` : ""}
                    ${fields}`,
            });
    }

    /** The image's own head: what it is, and the three things done to it. */
    imageLead(panel) {
        const escape = FigureInspector.escape;
        const source = this.state.source(panel.source_id);
        const channels = (panel.scene.channels || []).filter((channel) => channel.visible !== false);
        const origin = source ? (source.kind === "imported_asset" ? "Imported image"
            : (source.display_name || source.datasource || "Image")) : "Missing source";
        const quality = FigureCanvas.panelQuality(panel, this.state.document);
        const dims = quality ? `${quality.frame_w || quality.source_w} × ${quality.frame_h || quality.source_h} px` : "";
        const name = this.workspace.layers?.names().get(panel.panel_id) || origin;
        const reopenable = FigureActions.reopenable(panel, { state: this.state });
        const svg = (d) => `<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" fill="none"
            stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
        const layersIcon = svg('<path d="M12 3 3 7.5l9 4.5 9-4.5L12 3z"/><path d="m3 12 9 4.5 9-4.5"/><path d="m3 16.5 9 4.5 9-4.5"/>');
        const pictureIcon = svg('<rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="8.5" cy="10" r="1.5"/><path d="m21 16-5-5-9 8"/>');
        const what = channels.length ? `${channels.length} channel${channels.length === 1 ? "" : "s"}`
            : (source && source.kind === "imported_asset" ? "Imported" : origin);
        const meta = `<span class="fb-io-meta">
                <span class="fb-io-meta-item">${layersIcon}${escape(what)}</span>
                ${dims ? `<span class="fb-io-meta-sep" aria-hidden="true"></span>
                <span class="fb-io-meta-item">${pictureIcon}${escape(dims)}</span>` : ""}
            </span>`;
        return `<section class="fb-io fb-io-lead fb-insp-lead">
            <div class="fb-insp-ident">
                <span class="fb-insp-thumb"><img alt="" src="${escape(this.canvas.panelImageUrl(panel, source))}"></span>
                <span class="fb-insp-ident-text"><strong title="${escape(name)}">${escape(name)}</strong>${meta}</span>
                <button type="button" class="fb-io-replace" data-act="replace"
                        title="Replace: put another image from this figure in this place"
                        aria-label="Replace image">${FigureIcons.svg("swap", 15)}</button>
            </div>
            <div class="fb-io-actions">
                <button type="button" class="fb-io-primary" data-act="quick_edit"
                        ${reopenable ? 'title="Adjust channels and crop in Quick Edit"' : 'disabled title="An imported image has no view to adjust"'}>
                    ${FigureIcons.svg("sliders", 16)}<span>Quick Edit</span></button>
                <button type="button" class="fb-io-secondary" data-act="viewer"
                        ${reopenable ? 'title="Open in the main viewer"' : 'disabled title="An imported image has no view to open"'}>
                    ${FigureIcons.svg("viewer", 16)}<span>Open Viewer</span></button>
            </div>
        </section>`;
    }

    /** What part of its field the panel shows, and the ways to change that. */
    cropSection(panel) {
        const quality = FigureCanvas.panelQuality(panel, this.state.document);
        const source = this.state.source(panel.source_id);
        const pixel = source && source.pixel_size && source.pixel_size.value;
        const cropped = Boolean(quality && quality.cropped);
        let field = "";
        if (quality) {
            field = pixel
                ? `${Math.round(quality.source_w * pixel)} × ${Math.round(quality.source_h * pixel)} µm`
                : `${quality.source_w} × ${quality.source_h} px`;
        }
        const reopenable = FigureActions.reopenable(panel, { state: this.state });
        return FigureInspector.foldRow({
            id: "io_crop", cls: "fb-io-crop", icon: FigureInspector.ioIcon("crop"), title: "Crop & framing",
            summary: [cropped ? "Fill" : "Fit", field].filter(Boolean).join(" · "),
            body: () => `
            <div class="fb-insp-row">
                ${this.segmented("panel_shape", [["fit", "Fit"], ["fill", "Fill"]], cropped ? "fill" : "fit",
                                 "How the image fills the panel", "flex:1 1 auto")}
                ${this.action("quick_edit", "Crop", "crop", reopenable ? 'title="Reframe the field in Quick Edit"'
                    : 'disabled title="An imported image has no view to reframe; drag an edge to crop it"')}
            </div>
            <div class="fb-io-note-row">
                <p class="fb-insp-note">${field ? `Shows ${field} of the source. ` : ""}Cropping changes the field, never the source or its calibration.</p>
                <button type="button" class="fb-link" data-act="reset_shape" ${cropped ? ""
                    : 'disabled title="The panel shows its whole field"'}>Reset crop</button>
            </div>`,
        });
    }

    // -- several objects: lining them up ------------------------------------------

    multiSections() {
        const sel = FigureSelection.describe(this.ids, this.state, this.canvas);
        const can = sel.arrangeable;
        const icon = (command, label, glyph, min = 2, cls = "fb-insp-ib is-bare is-align") => `<button type="button"
            class="${cls}" data-arrange="${command}" title="${label}" aria-label="${label}"
            ${can >= min ? "" : "disabled"}>${FigureIcons.svg(glyph)}</button>`;
        const align = `<div class="fb-insp-iconrow" role="group" aria-label="Align">
            ${icon("left", "Align left", "alignL")}
            ${icon("center", "Align centers", "alignC")}
            ${icon("right", "Align right", "alignR")}
            ${icon("top", "Align top", "alignT")}
            ${icon("middle", "Align middles", "alignM")}
            ${icon("bottom", "Align bottom", "alignB")}
        </div>`;
        const gap = (axis) => {
            const value = this.canvas.currentGap(axis);
            return value === null ? "" : String(Number(value.toFixed(2)));
        };
        const distribute = `<div class="fb-insp-row">
            ${icon("distribute_h", "Distribute across", "distH", 3, "fb-insp-ib")}
            ${this.field("space_x", "Gap", gap("x"), { suffix: "mm", aria: "Gap across",
                         placeholder: "–", title: "Space them this far apart, left to right" })}
            ${icon("distribute_v", "Distribute down", "distV", 3, "fb-insp-ib")}
            ${this.field("space_y", "Gap", gap("y"), { suffix: "mm", aria: "Gap down",
                         placeholder: "–", title: "Space them this far apart, top to bottom" })}
        </div>`;
        const match = `<div class="fb-insp-seg is-actions" role="group" aria-label="Match size">
            <button type="button" data-arrange="same_width" ${can >= 2 ? "" : "disabled"}>Width</button>
            <button type="button" data-arrange="same_height" ${can >= 2 ? "" : "disabled"}>Height</button>
            <button type="button" data-arrange="same_size" ${can >= 2 ? "" : "disabled"}>Both</button>
        </div>`;

        const panels = this.ids.filter((id) => this.state.panel(id)?.placement);
        let layout = "";
        if (panels.length >= 2 && panels.length === this.ids.length) {
            const group = this.canvas.groupOfExactly(panels);
            const current = group?.layout;
            // Four, as the design shows them: the current one first, then the
            // suggestions for this many. The rest are a click away.
            const presets = FigureLayouts.suggested(panels.length)
                .filter((preset) => !current || preset.id !== current.preset);
            if (current && FigureLayouts.byId(current.preset)) presets.unshift(FigureLayouts.byId(current.preset));
            presets.splice(4);
            const tiles = presets.map((preset) => `<button type="button"
                    class="fb-layout-tile${current && current.preset === preset.id ? " is-on" : ""}"
                    data-layout="${preset.id}" title="${FigureInspector.escape(preset.label)}"
                    aria-pressed="${Boolean(current && current.preset === preset.id)}">
                    ${FigureLayouts.diagram(preset)}<span>${FigureInspector.escape(preset.label)}</span></button>`).join("");
            const links = `<div class="fb-insp-switch-row">
                <button type="button" class="fb-link" data-act="layouts">All layouts and gaps</button>
                ${current ? '<button type="button" class="fb-link" data-act="detach">Detach from layout</button>' : ""}
            </div>`;
            layout = this.section("Layout", `<div class="fb-layout-tiles">${tiles}</div>${links}`,
                `<span class="fb-insp-aside">${current ? FigureInspector.escape(FigureLayouts.byId(current.preset)?.label || "")
                    : "Not laid out"}</span>`);
        }

        return this.section("Align", align)
            + this.section("Distribute", distribute)
            + this.section("Match size", match, '<span class="fb-insp-aside">to the first selected</span>')
            + layout;
    }

    /** Two or more objects, every one of them an image. */
    get allImages() {
        return this.ids.length > 1 && this.ids.every((id) => this.state.panel(id)?.placement);
    }

    /** "A–C" for a run, "A, C, E" otherwise. */
    static letterList(letters) {
        const sorted = letters.slice().sort();
        const codes = sorted.map((letter) => (letter.length === 1 ? letter.charCodeAt(0) : NaN));
        const run = codes.every((code, index) => index === 0 || code === codes[index - 1] + 1);
        return run && sorted.length > 2 ? `${sorted[0]}–${sorted[sorted.length - 1]}` : sorted.join(", ");
    }

    /**
     * Several images: a strip of what is selected, then Layout & arrangement
     * (open by default). Scale bars, channel labels, panel labels, appearance
     * and rendering are FigureImagePanel's; order & grouping is the tail. CSS
     * `order` on .fb-io-m* puts them in that sequence.
     */
    multiImageSections() {
        const escape = FigureInspector.escape;
        const panels = this.ids.map((id) => this.state.panel(id)).filter(Boolean);
        const shown = panels.slice(0, 3);
        const strip = `<section class="fb-io fb-io-mlead">
            <div class="fb-io-thumbs">
                ${shown.map((panel) => `<span class="fb-io-thumb" title="${escape(
                    this.workspace.layers?.names().get(panel.panel_id) || "")}"><img alt="" src="${escape(
                    this.canvas.panelImageUrl(panel, this.state.source(panel.source_id)))}"></span>`).join("")}
                ${panels.length > shown.length ? `<span class="fb-io-thumb-more">+${panels.length - shown.length}</span>` : ""}
            </div>
        </section>`;

        const sel = FigureSelection.describe(this.ids, this.state, this.canvas);
        const can = sel.arrangeable;
        // One title: a second `title` attribute is dropped by the parser, so
        // the reason a tool is unavailable never showed.
        const icon = (command, label, glyph, min = 2) => `<button type="button" class="fb-io-tool"
            data-arrange="${command}" aria-label="${label}"
            title="${can >= min ? label : `${label} needs ${min} or more objects that can move`}"
            ${can >= min ? "" : "disabled"}>${FigureIcons.svg(glyph, 15)}</button>`;
        const gap = (axis) => {
            const value = this.canvas.currentGap(axis);
            return value === null ? "" : String(Number(value.toFixed(2)));
        };
        const group = this.canvas.groupOfExactly(this.ids);
        const current = group?.layout;
        const presets = FigureLayouts.suggested(panels.length)
            .filter((preset) => !current || preset.id !== current.preset);
        if (current && FigureLayouts.byId(current.preset)) presets.unshift(FigureLayouts.byId(current.preset));
        presets.splice(4);
        const tiles = presets.map((preset) => `<button type="button"
                class="fb-layout-tile${current && current.preset === preset.id ? " is-on" : ""}"
                data-layout="${preset.id}" title="${escape(preset.label)}"
                aria-pressed="${Boolean(current && current.preset === preset.id)}">
                ${FigureLayouts.diagram(preset)}<span>${escape(preset.label)}</span></button>`).join("")
            + `<button type="button" class="fb-layout-tile is-more" data-act="layouts" title="All layouts and gaps">
                ${FigureIcons.svg("more", 16)}<span>More</span></button>`;
        const body = () => `
            <div class="fb-io-stack">
                <span class="fb-io-cap">Layout${current ? ` · ${escape(FigureLayouts.byId(current.preset)?.label || "")}` : ""}</span>
                <div class="fb-io-layouts">${tiles}</div>
                ${current ? '<button type="button" class="fb-link fb-io-detach" data-act="detach">Detach from layout</button>' : ""}
            </div>
            <div class="fb-io-stack">
                <span class="fb-io-cap">Align</span>
                <div class="fb-io-toolbar" role="group" aria-label="Align">
                    ${icon("left", "Align left", "alignL")}
                    ${icon("center", "Align centers", "alignC")}
                    ${icon("right", "Align right", "alignR")}
                    <span class="fb-io-toolsep" aria-hidden="true"></span>
                    ${icon("top", "Align top", "alignT")}
                    ${icon("middle", "Align middles", "alignM")}
                    ${icon("bottom", "Align bottom", "alignB")}
                </div>
            </div>
            <div class="fb-io-stack">
                <span class="fb-io-cap">Distribute</span>
                <div class="fb-io-distribute">
                    ${icon("distribute_h", "Distribute across (3 or more)", "distH", 3)}
                    ${this.field("space_x", "Gap", gap("x"), { suffix: "mm", aria: "Gap across",
                                 placeholder: "–", title: "Space them this far apart, left to right" })}
                    ${icon("distribute_v", "Distribute down (3 or more)", "distV", 3)}
                    ${this.field("space_y", "Gap", gap("y"), { suffix: "mm", aria: "Gap down",
                                 placeholder: "–", title: "Space them this far apart, top to bottom" })}
                </div>
            </div>
            <div class="fb-io-stack">
                <span class="fb-io-cap">Match size <span class="fb-io-cap-note">to the first selected</span></span>
                <div class="fb-insp-seg is-actions fb-io-match" role="group" aria-label="Match size">
                    <button type="button" data-arrange="same_width" ${can >= 2 ? "" : "disabled"}>Width</button>
                    <button type="button" data-arrange="same_height" ${can >= 2 ? "" : "disabled"}>Height</button>
                    <button type="button" data-arrange="same_size" ${can >= 2 ? "" : "disabled"}>Both</button>
                </div>
            </div>`;
        const layout = FigureInspector.foldRow({
            id: "io_m_layout", cls: "fb-io-mlayout", icon: FigureInspector.ioIcon("grid"),
            title: "Layout & arrangement",
            summary: current ? (FigureLayouts.byId(current.preset)?.label || "Laid out") : "Not laid out",
            body: body,
        });
        return strip + layout;
    }

    /** Several images: grouping and stacking, folded. */
    multiOrderFold() {
        const sel = FigureSelection.describe(this.ids, this.state, this.canvas);
        const key = (label, act, shortcut, glyph, enabled = true) => `<button type="button" class="fb-insp-btn fb-io-orderbtn"
                data-act="${act}" ${enabled ? "" : "disabled"}>${glyph ? FigureIcons.svg(glyph, 14) : ""}<span>${label}</span>${shortcut
                    ? `<span class="fb-insp-key">${shortcut}</span>` : ""}</button>`;
        return FigureInspector.foldRow({
            id: "io_m_order", cls: "fb-io-morder", icon: FigureInspector.ioIcon("labels"),
            title: "Order & grouping", summary: sel.grouped ? "Grouped" : "Not grouped",
            body: () => `<div class="fb-io-order">
                ${key("Group", "group", "⌘G", "group", !sel.grouped)}
                ${key("Ungroup", "ungroup", "⇧⌘G", "ungroup", Boolean(sel.grouped))}
                ${key("Bring forward", "forward", "", "front")}
                ${key("Send backward", "backward", "", "back")}
            </div>`,
        });
    }

    /** The foot of a multiple selection: grouping and stacking, with keys. */
    orderSection() {
        const sel = FigureSelection.describe(this.ids, this.state, this.canvas);
        const key = (label, act, shortcut, enabled = true) => `<button type="button" class="fb-insp-btn"
                data-act="${act}" ${enabled ? "" : "disabled"}>${label}${shortcut
                    ? `<span class="fb-insp-key">${shortcut}</span>` : ""}</button>`;
        return this.section("Order and grouping", `<div class="fb-insp-grid2">
            ${key("Group", "group", "⌘G", !sel.grouped)}
            ${key("Ungroup", "ungroup", "⇧⌘G", Boolean(sel.grouped))}
            ${key("Bring forward", "forward", "")}
            ${key("Send backward", "backward", "")}
        </div>`);
    }

    // -- one image: where it came from and how sharp it will be ---------------------

    qualitySection(panel) {
        if (!panel || !panel.placement) return "";
        const escape = FigureInspector.escape;
        const source = this.state.source(panel.source_id);
        const quality = FigureCanvas.panelQuality(panel, this.state.document);
        const place = panel.placement;
        const pixel = source && source.pixel_size && source.pixel_size.value;
        const channels = (panel.scene.channels || []).filter((channel) => channel.visible !== false);
        const swatches = channels.length ? `<div class="fb-insp-channels">
            ${channels.slice(0, 12).map((channel) => `<span class="fb-insp-channel">
                <span class="fb-insp-swatch" style="background:${escape(FigureSchema.channelHex(channel.color))}"></span>
                ${escape(channel.fullname_at_capture || channel.key)}</span>`).join("")}
        </div>` : "";
        const kv = (k, v, strong) => `<div class="fb-insp-kv"><span>${escape(k)}</span>
            <span${strong ? ' class="is-strong"' : ""}>${escape(v)}</span></div>`;
        let status = "";
        if (quality) {
            status = quality.low
                ? `<div class="fb-insp-alert">
                    ${FigureIcons.svg("warn", 12)}
                    <span>Fewer pixels than a ${quality.dpi} DPI export needs</span>
                    <button type="button" class="fb-link" data-act="show_low"
                            data-panel="${escape(panel.panel_id)}">Details</button></div>`
                : `<div class="fb-insp-ok">${FigureIcons.svg("check", 12)}
                    Enough pixels for a ${quality.dpi} DPI export</div>`;
        }
        // The calibration is said in the scale bar's note while the bar is
        // on; with it off, here.
        const calibration = panel.scalebar.visible ? "" : kv("Calibration", pixel
            ? `${Number(pixel.toPrecision(4))} µm/px` : "None");
        const body = () => `${swatches}
            ${quality ? kv("Source pixels", `${quality.source_w} × ${quality.source_h}`
                + (quality.cropped ? ` of ${quality.frame_w} × ${quality.frame_h}` : "")) : ""}
            ${kv("Size on page", `${this.show(place.w_mm)} × ${this.show(place.h_mm)} ${this.unit.label}`)}
            ${quality ? kv("Effective resolution", `${quality.ppi} ppi`, true) : ""}
            ${calibration}
            ${status}`;
        // The verdict is on the row itself, so a low resolution is seen with
        // the section shut.
        const flag = !quality ? ""
            : (quality.low
                ? `<span class="fb-isec-flag is-low" title="Fewer pixels than a ${quality.dpi} DPI export needs">${FigureIcons.svg("warn", 13)}</span>`
                : `<span class="fb-isec-flag is-ok" title="Enough pixels for a ${quality.dpi} DPI export">${FigureIcons.svg("check", 11)}</span>`);
        return FigureInspector.foldRow({
            id: "io_source", cls: "fb-io-source", icon: FigureInspector.ioIcon("info"), title: "Source & quality",
            summary: quality ? `${quality.ppi} ppi · ${quality.low ? "Low" : "Good"}` : "No source",
            body: body, flag: flag,
        });
    }

    // -- answering -----------------------------------------------------------------

    keyDown(event) {
        const input = event.target.closest?.(".fb-insp-input");
        if (!input) return;
        if (event.key === "Enter") {
            event.preventDefault();
            input.blur();
            return;
        }
        if (event.key === "Escape") {
            event.stopPropagation();
            this.refresh();
            return;
        }
        const step = { ArrowUp: 1, ArrowDown: -1 }[event.key];
        if (!step) return;
        event.preventDefault();
        const value = parseFloat(input.value);
        if (!Number.isFinite(value)) return;
        input.value = String(Number((value + step * (event.shiftKey ? 10 : 1)).toFixed(3)));
        input.dispatchEvent(new Event("change", { bubbles: true }));
    }

    changed(event) {
        const el = event.target;
        const field = el.dataset.field;
        if (!field) return;
        const page = this.canvas.page;
        const raw = el.value;

        if (field === "units") {
            this.workspace.setUnits(raw);
            this.render();
            return;
        }
        if (field === "page_background") {
            // "Custom color…" opens the palette on the swatch beside it; the
            // dropdown shows the color once one is chosen.
            if (raw === "custom") {
                this.refresh();
                this.workspace.openPageColor(
                    this.root?.querySelector?.('[data-swatch="page_background"]')
                    || document.querySelector('[data-swatch="page_background"]'));
                return;
            }
            this.workspace.setPageBackground(raw);
            return;
        }
        if (field === "page_preset") {
            if (raw === "custom") {
                this.workspace.updatePage({ preset: "custom" });
                return;
            }
            const spec = FigureSchema.PAGE_PRESETS[raw];
            if (!spec) return;
            const landscape = page.orientation === "landscape";
            this.workspace.updatePage({ preset: raw, size_mm: {
                w: landscape ? spec.h : spec.w, h: landscape ? spec.w : spec.h } });
            return;
        }
        if (field === "page_w" || field === "page_h") {
            const mm = this.read(raw);
            if (!(mm > 0)) return this.refresh();
            const size = { ...page.size_mm, [field === "page_w" ? "w" : "h"]: mm };
            this.workspace.updatePage({ preset: "custom", size_mm: size,
                                        orientation: size.w > size.h ? "landscape" : "portrait" });
            return;
        }
        if (field === "page_margins") {
            const mm = this.read(raw);
            if (mm === null || mm < 0) return this.refresh();
            this.workspace.updatePage({ margins_mm: { top: mm, right: mm, bottom: mm, left: mm } });
            return;
        }
        const settings = this.state.document.settings;
        const number = parseFloat(String(raw).replace(",", "."));
        if (field === "label_size" || field === "text_size" || field === "gutter") {
            if (!(number > 0) && !(field === "gutter" && number === 0)) return this.refresh();
            if (field === "label_size") {
                // 4-72 pt, the range the server keeps for a letter.
                this.workspace.setLetterSize(Math.min(72, Math.max(4, number)));
                return;
            }
            const key = { label_size: "label_size_pt", text_size: "font_size_pt", gutter: "gutter_mm" }[field];
            this.workspace.updateSettings({ style: { ...settings.style, [key]: number } });
            return;
        }
        if (field === "dpi") {
            if (!(number >= 72 && number <= 1200)) return this.refresh();
            this.workspace.updateSettings({ dpi_default: Math.round(number) });
            return;
        }
        if (field === "space_x" || field === "space_y") {
            if (!(number >= 0)) return this.refresh();
            this.canvas.spaceBy(field === "space_x" ? "x" : "y", number);
            return;
        }
        if (field === "gap_x" || field === "gap_y") {
            if (!(number >= 0)) return this.refresh();
            const group = this.canvas.groupOfExactly(this.ids);
            // 0-50 mm, the range the server keeps (normalize_group_layout).
            if (group) this.canvas.relayout(group.group_id, { [field === "gap_x" ? "gap_x_mm" : "gap_y_mm"]: Math.min(50, number) });
            return;
        }
        if (["x", "y", "w", "h", "rotation"].includes(field)) {
            this.boxChanged(field, raw);
        }
    }

    boxChanged(field, raw) {
        const id = this.ids[0];
        const panel = this.state.panel(id);
        const object = this.canvas.objectById(id);
        if (!object || object.locked) return this.refresh();
        const box = panel ? panel.placement : object.geometry;
        if (field === "rotation") {
            const degrees = parseFloat(raw);
            if (!Number.isFinite(degrees)) return this.refresh();
            this.canvas.setBox(id, { rotation: FigureSchema.normalizeDegrees(degrees) });
            return;
        }
        const mm = this.read(raw);
        if (mm === null || ((field === "w" || field === "h") && !(mm > 0))) return this.refresh();
        const key = { x: "x_mm", y: "y_mm", w: "w_mm", h: "h_mm" }[field];
        const change = { [key]: mm };
        if (panel && this.keepAspect && (field === "w" || field === "h")) {
            const aspect = box.w_mm / box.h_mm;
            if (field === "w") change.h_mm = mm / aspect;
            else change.w_mm = mm * aspect;
        }
        this.canvas.setBox(id, change);
    }

    clicked(event) {
        const style = event.target.closest("[data-style]");
        if (style) {
            this.flip(style.dataset.style);
            this.refresh();
            return;
        }
        FigureInspector.noteSwitch(event.target.closest('[role="switch"]'));
        const sw = event.target.closest("[data-switch]");
        if (sw && sw.dataset.switch.startsWith("all:")) {
            // Set on every image on the page at once, as one undo step.
            const key = sw.dataset.switch.slice(4);
            const images = FigureSchema.panelsOnPage(this.state.document, this.canvas.pageId);
            const on = sw.getAttribute("aria-checked") !== "true";
            this.workspace.updatePanels(images.map((panel) => ({
                panel_id: panel.panel_id,
                changes: key === "scalebar"
                    ? { scalebar: { ...panel.scalebar, visible: on } }
                    : key === "labels" ? { label: { ...panel.label, visible: on } }
                    : { border: { width_pt: 0.75, color: "#ffffff", ...(panel.border || {}), visible: on } },
            })));
            return;
        }
        if (sw) {
            this.workspace.viewOptions?.pick(sw.dataset.switch);
            this.render();
            return;
        }
        const ifold = event.target.closest("[data-ifold]");
        if (ifold) {
            FigureInspector.foldFlip(ifold.dataset.ifold);
            this.refresh();
            return;
        }
        const well = event.target.closest('[data-swatch="page_background"]');
        if (well) {
            this.workspace.openPageColor(well);
            return;
        }
        const seg = event.target.closest("[data-seg]");
        if (seg) {
            this.segment(seg.dataset.seg, seg.dataset.value);
            return;
        }
        const arrange = event.target.closest("[data-arrange]");
        if (arrange && !arrange.disabled) {
            this.workspace.arrange(arrange.dataset.arrange);
            return;
        }
        const layout = event.target.closest("[data-layout]");
        if (layout) {
            const panels = this.ids.filter((id) => this.state.panel(id)?.placement);
            const group = this.canvas.groupOfExactly(panels);
            if (group?.layout) this.canvas.relayout(group.group_id, { preset: layout.dataset.layout });
            else this.workspace.layoutSelection(layout.dataset.layout);
            return;
        }
        const act = event.target.closest("[data-act]");
        if (!act) return;
        const name = act.dataset.act;
        const group = this.canvas.groupOfExactly(this.ids);
        const run = {
            aspect: () => { this.keepAspect = !this.keepAspect; this.render(); },
            reset_shape: () => this.canvas.resetPanelShape(this.ids),
            fit_content: () => this.canvas.fitContentToPage(),
            center_content: () => this.canvas.centerContent(),
            grid_images: () => this.workspace.layoutAllImages(),
            add_page: () => this.workspace.addPage(),
            show_low: () => this.workspace.showResolution(act.dataset.panel),
            quick_edit: () => this.workspace.quickEditPanel(this.ids[0]),
            viewer: () => this.workspace.onEditPanel(this.ids[0]),
            replace: () => this.workspace.openReplace(this.ids[0], act),
            duplicate: () => this.canvas.duplicateSelection(),
            group: () => this.canvas.groupSelection(),
            ungroup: () => this.canvas.ungroupSelection(),
            forward: () => this.canvas.reorderZ("forward"),
            backward: () => this.canvas.reorderZ("backward"),
            layouts: () => this.workspace.openLayouts(),
            detach: () => group && this.canvas.detachLayout(group.group_id),
            layout_width: () => group && this.canvas.relayout(group.group_id, {}, { fullWidth: true }),
            relabel: () => group && this.workspace.relabel(group.member_ids),
            text_autofit: () => this.workspace.textPanel?.applyStyle({
                autofit: act.getAttribute("aria-checked") !== "true" }),
        }[name];
        run?.();
    }

    segment(name, value) {
        if (name === "orientation") {
            const page = this.canvas.page;
            if (!page || page.orientation === value) return;
            const { w, h } = page.size_mm;
            const wide = value === "landscape";
            this.workspace.updatePage({ orientation: value,
                size_mm: { w: wide ? Math.max(w, h) : Math.min(w, h),
                           h: wide ? Math.min(w, h) : Math.max(w, h) } });
        } else if (name === "background") {
            this.workspace.setPageBackground(value);
        } else if (name === "label_style") {
            this.workspace.updateSettings({ label_style: value });
        } else if (name === "panel_shape") {
            // Fit shows the whole field again; Fill keeps the panel's present
            // shape when it already crops, and otherwise is reached by giving
            // the panel another shape -- an edge drag or a filled layout cell.
            if (value === "fit") this.canvas.resetPanelShape(this.ids);
            else this.workspace.toast("Drag an edge of the panel to crop it to another shape.");
        } else if (name === "layout_fit") {
            const group = this.canvas.groupOfExactly(this.ids);
            if (group) this.canvas.relayout(group.group_id, { fit: value });
        }
    }
}
