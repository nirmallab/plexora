// The plot sidebar: the controls of the plot that is selected.
//
// It follows the selection (Customize on a plot's bar, then a click on any
// plot). Visualization is always open: a card per view, each a small drawing
// of what it plots. Below it, Data, Display and Export (Annotate too on a
// UMAP) fold as an accordion, one open at a time, and the open one is
// remembered. A row is a label and its control on one line, stacked when the
// sidebar is too narrow. The foot keeps Reset view, Copy the call and Figure
// Builder in reach.
//
// Nothing here runs a step: every control redraws the plot from what its run
// stored (scimapViews.js owns that state; this reads and writes it). The
// sidebar is rebuilt only when another plot or view is chosen; a change of a
// setting updates its controls in place, so nothing flickers and a thumb
// slides instead of jumping.
//
// Closed by default; it opens from Customize, the header button or "]". Under
// 1180 px wide it opens over the steps as a sheet (scimapWorkspace.js lays it
// out). Its edge drags to resize it.
window.ScimapInspector = (function () {
    "use strict";

    const { h, icon } = window.ScimapDom;
    const V = () => window.ScimapViews;
    const U = () => window.ScimapUi;
    const OPEN_KEY = "plexora.scimap.inspector.open";
    const WIDTH_KEY = "plexora.scimap.inspector.width";
    const NB_OPEN_KEY = "plexora.scimap.inspector.notebook";
    // Figure Builder's right column: 340 wide, 304 to 520 by its edge.
    const MIN_W = 304;
    const MAX_W = 520;
    const WIDTH = 340;
    const STATE = { done: "Done", running: "Running", queued: "Queued", failed: "Failed", outdated: "Inputs changed",
        cancelled: "Cancelled", idle: "Not run" };

    // Card names short enough for a card; the tooltip holds the full one.
    const SHORT = { grouped: "By sample column", where: "Where each is", binary: "More or less", similarity: "Similarity",
        correlation: "Correlation", holds: "Composition", radii: "Across radii", voronoi: "Voronoi" };

    function readWidth() {
        try {
            const v = Number(window.localStorage.getItem(WIDTH_KEY));
            if (v >= MIN_W && v <= MAX_W) return v;
        } catch (_) { /* private window */ }
        return WIDTH;
    }

    function tipText(view, i) {
        return view.name + (view.hint ? " — " + view.hint : "") + (i < 4 ? "  ·  key " + (i + 1) : "");
    }

    function set(opts) {
        return Object.keys(opts || {}).filter((k) => opts[k] !== null && opts[k] !== undefined);
    }

    class Inspector {
        constructor(ctx) {
            this.ctx = ctx;
            this.acc = new (U().Accordion)({ storeKey: OPEN_KEY, initial: "data", fallback: "data" });
            this.nbAcc = new (U().Accordion)({ storeKey: NB_OPEN_KEY, initial: "steps", fallback: "steps" });
            this.root = h("aside.sm-inspector", { "aria-label": "Plot settings" });
            this.setWidth(readWidth());
            this.grip = h("div.sm-insp-grip", { role: "separator", "aria-orientation": "vertical", "aria-label": "Resize the sidebar" });
            this.wireGrip();
            this.updaters = [];
            this.panelKey = null;
            this.viewKey = null;
            this.unwatch = V().watch(() => this.paint());
            this.onSelected = () => requestAnimationFrame(() => this.paintCount());
            window.addEventListener("plexora:cells-selected", this.onSelected);
            this.paint();
        }

        destroy() {
            window.removeEventListener("plexora:cells-selected", this.onSelected);
            if (this.unwatch) this.unwatch();
            U().close();
            this.detachAnnotate();
            this.root.remove();
        }

        // -- width ---------------------------------------------------------------

        setWidth(w) {
            this.width = Math.round(Math.max(MIN_W, Math.min(MAX_W, w)));
            this.root.style.setProperty("--sm-insp-w", this.width + "px");
        }

        wireGrip() {
            let start = null;
            const end = () => {
                if (!start) return;
                start = null;
                this.root.classList.remove("is-resizing");
                try { window.localStorage.setItem(WIDTH_KEY, String(this.width)); } catch (_) { /* private window */ }
                if (this.ctx.reflow) this.ctx.reflow();
            };
            this.grip.addEventListener("pointerdown", (e) => {
                if (e.button !== 0) return;
                e.preventDefault();
                start = { x: e.clientX, w: this.width };
                try { this.grip.setPointerCapture(e.pointerId); } catch (_) { /* a synthetic event */ }
                this.root.classList.add("is-resizing");
            });
            this.grip.addEventListener("pointermove", (e) => {
                if (start) this.setWidth(Math.min(start.w + (start.x - e.clientX), window.innerWidth * 0.6));
            });
            this.grip.addEventListener("pointerup", end);
            this.grip.addEventListener("lostpointercapture", end);
            this.grip.addEventListener("dblclick", () => {
                this.setWidth(WIDTH);
                start = { x: 0, w: WIDTH };
                end();
            });
        }

        // -- painting ------------------------------------------------------------

        paint() {
            const cur = V().current();
            if (!cur) {
                this.panelKey = this.viewKey = null;
                this.paintEmpty();
                return;
            }
            const panelKey = cur.s.cell.id + "/" + cur.p.id + "/" + cur.s.runId;
            const viewKey = panelKey + "/" + cur.p.view;
            if (panelKey !== this.panelKey) {
                this.panelKey = panelKey;
                this.viewKey = viewKey;
                this.build(cur);
            } else if (viewKey !== this.viewKey) {
                this.viewKey = viewKey;
                this.markCards(cur);
                this.paintSections(cur);
                this.paintFoot(cur);
            } else {
                this.updaters.forEach((fn) => { try { fn(cur); } catch (_) { /* one control's own problem */ } });
                this.paintFoot(cur);
            }
        }

        // No plot chosen: the notebook itself -- its steps to go to, the data
        // they read, Run all and Export.
        paintEmpty() {
            this.detachAnnotate();
            const keep = this.body && this.body.classList.contains("is-notebook") ? this.body.scrollTop : 0;
            this.root.textContent = "";
            const nb = (this.ctx.notebook && this.ctx.notebook()) || { cells: [], title: "Notebook" };
            const work = (this.ctx.working && this.ctx.working()) || {};
            this.nbAcc.reset();
            const steps = nb.cells.map((cell) => {
                const tool = this.ctx.tools[cell.tool] || { name: cell.tool };
                const n = V().step(cell.id) ? (V().step(cell.id).panels || []).length : 0;
                return h("button.sm-insp-step" + (this.ctx.selectedId && this.ctx.selectedId() === cell.id ? ".is-on" : ""), {
                    type: "button", onclick: () => this.ctx.goStep(cell.id) },
                h("span.sm-insp-step-num", null, String(this.ctx.index(cell.id)).padStart(2, "0")),
                icon(tool.icon || "grid", "sm"),
                h("span.sm-insp-step-name", null, tool.name),
                n ? h("span.sm-insp-step-n", { "data-tooltip": n + (n === 1 ? " plot" : " plots") }, String(n)) : null,
                h("span.sm-insp-step-state.is-" + (cell.state || "idle"), { "data-tooltip": STATE[cell.state] || STATE.idle,
                    "aria-label": STATE[cell.state] || STATE.idle }));
            });
            const fmt = (v) => Number(v).toLocaleString("en-US");
            const facts = work.exists ? [
                ["Cells", fmt(work.n_cells || 0)],
                ["Images", String((work.images || []).length)],
                ["Markers", String((work.markers || []).length)],
                ["File", work.file || ""],
            ] : [["Data", "Not prepared yet"]];
            const data = facts.map(([k, v]) => this.row(k, h("span.sm-insp-fact", { "data-tooltip": v.length > 26 ? v : null }, v)));
            const sections = [
                steps.length ? this.nbAcc.section("steps", "Steps", { content: h("div.sm-insp-steps", null, steps),
                    sub: steps.length + (steps.length === 1 ? " step" : " steps") }) : null,
                this.nbAcc.section("data", "Data", { content: data, sub: work.exists ? fmt(work.n_cells || 0) + " cells" : "" }),
            ];
            this.body = h("div.sm-insp-body.is-notebook", null,
                h("div.sm-insp-hint", null, icon("sliders", "sm"), h("span", null, "Select a plot to customize it")),
                sections);
            const foot = h("div.sm-insp-foot", null,
                nb.cells.length ? h("button.sm-insp-reset", { type: "button", "data-tooltip": "Run every step in order",
                    onclick: () => this.ctx.runAll() }, icon("playAll", "sm"), h("span", null, "Run all")) : null,
                h("span.sm-spacer"),
                h("button.sm-insp-fb", { type: "button", "data-tooltip": "Notebook, Python, report or native file",
                    "data-tooltip-align": "right", onclick: () => this.ctx.exportNotebook() }, icon("download", "sm"), h("span", null, "Export")));
            this.root.append(this.grip, h("div.sm-insp-head", null,
                h("div.sm-insp-titles", null, h("span.sm-insp-eyebrow", null, "Notebook"), h("strong", null, nb.title || "Notebook")),
                this.closeButton()), this.body, foot);
            this.nbAcc.settle();
            this.body.scrollTop = keep;
        }

        closeButton() {
            return h("button.sm-insp-icon", { type: "button", "aria-label": "Hide the sidebar", "data-tooltip": "Hide sidebar  ]",
                "data-tooltip-placement": "bottom", "data-tooltip-align": "right", onclick: () => this.ctx.closeInspector() }, icon("panelClose"));
        }

        build(cur) {
            const keep = this.body ? this.body.scrollTop : 0;
            this.detachAnnotate();
            this.root.textContent = "";
            const tool = this.ctx.tools[cur.s.cell.tool] || { name: cur.s.cell.tool };
            const index = this.ctx.index(cur.s.cell.id);
            const head = h("div.sm-insp-head", null,
                h("div.sm-insp-titles", null, h("span.sm-insp-eyebrow", null, "Step " + String(index).padStart(2, "0")),
                    h("strong", null, tool.name)),
                this.closeButton());
            this.cards = h("div.sm-insp-cards", { role: "radiogroup", "aria-label": "Visualization" });
            const views = cur.s.catalogue.views || [];
            this.viz = h("section.sm-insp-viz", null,
                h("div.sm-insp-viz-head", null, h("span.sm-ui-acc-title", null, "Visualization"),
                    h("span.sm-insp-viz-n", null, views.length + (views.length === 1 ? " view" : " views"))),
                this.cards);
            this.paintCards(cur);
            this.sectionsHost = h("div.sm-insp-sections");
            this.body = h("div.sm-insp-body", null, this.viz, this.sectionsHost);
            this.footHost = h("div.sm-insp-foot");
            this.footKey = null;
            this.root.append(this.grip, head, this.body, this.footHost);
            this.paintSections(cur);
            this.paintFoot(cur);
            this.body.scrollTop = keep;
        }

        // -- visualization -----------------------------------------------------------

        paintCards(cur) {
            const { s, p } = cur;
            this.cards.textContent = "";
            (s.catalogue.views || []).forEach((v, i) => {
                const kind = v.kind === "image" ? h("span.sm-insp-card-kind", { "aria-label": "Drawn as a picture" }, icon("image", "xs"))
                    : v.kind === "compute" ? h("span.sm-insp-card-kind", { "aria-label": "Computed when first opened" }, icon("radii", "xs")) : null;
                const tip = tipText(v, i) + (v.kind === "image" ? "  ·  a picture" : v.kind === "compute" ? "  ·  computes when opened" : "");
                this.cards.append(h("button.sm-insp-card", { type: "button", role: "radio", "data-view": v.id,
                    "data-tooltip": tip, "aria-label": v.name,
                    onclick: () => { const now = V().current(); if (now && v.id !== now.p.view) V().setView(s.cell.id, p.id, v.id); } },
                U().preview(v), h("span.sm-insp-card-name", null, SHORT[v.id] || v.name),
                kind, h("span.sm-insp-card-check", { "aria-hidden": "true" }, icon("check", "xs"))));
            });
            this.markCards(cur);
        }

        markCards(cur) {
            this.cards.querySelectorAll(".sm-insp-card").forEach((card) => {
                const on = card.dataset.view === cur.p.view;
                card.classList.toggle("is-on", on);
                card.setAttribute("aria-checked", String(on));
            });
        }

        // -- the folding sections ------------------------------------------------------

        paintSections(cur) {
            U().close();
            this.detachAnnotate();
            this.updaters = [];
            this.acc.reset();
            this.sectionsHost.textContent = "";
            const sections = cur.view.kind === "embedding" ? this.embeddingSections(cur) : this.plotSections(cur);
            sections.filter(Boolean).forEach((node) => this.sectionsHost.append(node));
            this.acc.settle();
        }

        section(id, title, rows, sub) {
            const content = (rows || []).flat().filter(Boolean);
            if (!content.length) return null;
            const text = typeof sub === "function" ? sub(V().current()) : sub;
            if (typeof sub === "function") this.updaters.push((now) => this.acc.setSub(id, sub(now)));
            return this.acc.section(id, title, { content, sub: text });
        }

        row(label, control, note) {
            return h("div.sm-ui-row", null,
                h("span.sm-ui-label", null, h("span", null, label),
                    note ? h("span.sm-ui-info", { tabindex: "0", role: "img", "data-tooltip": note, "aria-label": note }, icon("info", "xs")) : null),
                h("div.sm-ui-control", null, control));
        }

        plotSections(cur) {
            const { s, p, view } = cur;
            const data = [];
            const display = [];
            const dataKeys = [];
            const displayKeys = [];
            (view.controls || []).forEach((key) => {
                const c = V().control(s, key);
                if (!c) return;
                const made = this.control(s, p, key, c);
                if (!made) return;
                const row = this.row(c.label, made, c.note);
                if (c.section === "data") { data.push(row); if (c.type === "multi") dataKeys.push(key); }
                else { display.push(row); displayKeys.push(key); }
            });
            const text = (now, keys) => (now ? keys.map((k) => this.valueText(now.s, now.p, k, V().control(now.s, k)))
                .filter(Boolean).join(" · ") : "");
            return [
                this.section("data", "Data", data, (now) => text(now, dataKeys)),
                this.section("display", "Display", display, (now) => text(now, displayKeys)),
                this.exportSection(cur),
            ];
        }

        valueText(s, p, key, c) {
            const v = V().effective(s, p, key);
            if (c.type === "segment") return ((c.choices || []).find((x) => x.value === v) || {}).label || "";
            if (c.type === "toggle") return v ? c.label : "";
            if (c.type === "range") return Number(v).toFixed(1) + (c.unit || "");
            if (c.type === "multi") return V().multiText(c, V().valueOf(s, p, key));
            return V().choiceLabel(c, v);
        }

        fieldText(c, value) {
            const all = c.choices.length;
            if (!value || !value.length || value.length === all) return { text: "All " + all + " " + (c.noun || ""), n: 0 };
            return { text: value.join(", "), n: value.length };
        }

        control(s, p, key, c) {
            const cell = s.cell.id;
            const put = (value) => V().setOpt(cell, p.id, key, value);
            const live = () => V().current() || { s, p };
            if (c.type === "multi") {
                if (!(c.choices || []).length) return null;
                const f = U().field({ label: c.label, onOpen: (anchor) => {
                    const now = live();
                    U().pickList(anchor, { title: c.label, noun: c.noun || "values", items: c.choices, multiple: true,
                        value: V().valueOf(now.s, now.p, key), onChange: (values, every) => put(every ? null : values) });
                } });
                const update = (now) => { const t = this.fieldText(c, V().valueOf(now.s, now.p, key)); f.set(t.text, t.n); };
                update(live());
                this.updaters.push(update);
                return f.node;
            }
            if (c.type === "one") {
                if (!(c.choices || []).length) return null;
                const f = U().field({ label: c.label, onOpen: (anchor) => {
                    const now = live();
                    U().pickList(anchor, { title: c.label, noun: c.noun || "values",
                        items: c.choices.map((v) => ({ value: v, label: V().choiceLabel(c, v) })),
                        value: V().effective(now.s, now.p, key), onChange: (v) => put(v === "" ? null : v) });
                } });
                const update = (now) => f.set(V().choiceLabel(c, V().effective(now.s, now.p, key)));
                update(live());
                this.updaters.push(update);
                return f.node;
            }
            if (c.type === "segment") {
                const seg = U().segmented({ label: c.label, choices: c.choices.map((x) => ({ value: x.value, label: x.label })),
                    value: V().effective(s, p, key), onChange: (v) => put(v) });
                this.updaters.push((now) => seg.set(V().effective(now.s, now.p, key)));
                return seg.node;
            }
            if (c.type === "toggle") {
                // Shows what the view draws (a dot plot logs raw values by
                // default) and stores the choice either way.
                const t = U().toggle({ label: c.label, on: Boolean(V().effective(s, p, key)), onChange: (on) => put(on) });
                this.updaters.push((now) => t.set(Boolean(V().effective(now.s, now.p, key))));
                return t.node;
            }
            if (c.type === "range") {
                const slider = this.slider(c, V().effective(s, p, key), (v) => put(v), key === "points");
                this.updaters.push((now) => slider.set(V().effective(now.s, now.p, key)));
                return slider.node;
            }
            return null;
        }

        // Core's one slider (views/slider.js), the image panel's: rail, fill,
        // a thumb with its halo, and a typeable number. Point size applies as
        // it is dragged (it never redraws); the rest on release.
        slider(c, value, onChange, live) {
            const mount = h("div.sm-insp-slider");
            let inst = null;
            let last = Number(value);
            const fire = (v) => { last = Number(v); onChange(last); };
            if (typeof window.PlexoraSlider === "function") {
                inst = new window.PlexoraSlider(mount, { min: c.min, max: c.max, step: c.step, value: Number(value), unit: c.unit || "",
                    label: c.label, onInput: live ? fire : undefined, onChange: fire });
            } else {
                const input = h("input", { type: "range", min: c.min, max: c.max, step: c.step, value,
                    "aria-label": c.label, onchange: (e) => onChange(Number(e.target.value)) });
                mount.append(input);
                inst = { set: (v) => { input.value = v; } };
            }
            return { node: mount, set: (v) => {
                if (Math.abs(Number(v) - last) < 1e-9) return;
                last = Number(v);
                try { inst.set(Number(v), { silent: true }); } catch (_) { /* older slider */ }
            } };
        }

        exportSection(cur) {
            const { s, view } = cur;
            const cell = s.cell;
            const entry = () => {
                const now = V().current() || cur;
                return { name: now.p.id, view: now.p.view, opts: now.p.opts, title: now.view.name };
            };
            const formats = view.kind === "table" ? [] : ["png", "svg", "pdf"];
            const tiles = formats.map((fmt) => h("button.sm-insp-format", { type: "button",
                "data-tooltip": fmt.toUpperCase() + ", drawn by Scimap Pro",
                onclick: () => this.ctx.figureFile(cell, entry(), fmt) }, icon("download", "xs"), h("span", null, fmt.toUpperCase())));
            tiles.push(h("button.sm-insp-format", { type: "button", "data-tooltip": "The plotted numbers",
                onclick: async () => {
                    const now = V().current() || cur;
                    const done = await V().csv(cell.id, now.p.id).catch(() => false);
                    if (!done) this.ctx.csvFor(cell);
                } }, icon("table", "xs"), h("span", null, "CSV")));
            return this.section("export", "Export", [h("div.sm-insp-formats", null, tiles)]);
        }

        // -- the foot: the actions always in reach ----------------------------------------

        paintFoot(cur) {
            const { s, p, view } = cur;
            const cell = s.cell;
            const touched = set(p.opts).length > 0;
            const key = view.id + "/" + view.kind + "/" + touched;
            if (this.footKey === key) return;
            this.footKey = key;
            this.footHost.textContent = "";
            if (view.kind !== "embedding") {
                this.footHost.append(h("button.sm-insp-reset", { type: "button", disabled: !touched || null,
                    "data-tooltip": touched ? "Back to how this view first drew" : null,
                    onclick: () => { const now = V().current() || cur; V().reset(cell.id, now.p.id); } },
                icon("undo", "sm"), h("span", null, "Reset view")));
            }
            this.footHost.append(h("span.sm-spacer"));
            if (view.kind !== "table" && view.kind !== "embedding") {
                this.footHost.append(h("button.sm-insp-icon", { type: "button", "aria-label": "Copy the Scimap Pro call",
                    "data-tooltip": "Copy the Scimap Pro call", "data-tooltip-align": "right",
                    onclick: () => { const now = V().current() || cur; this.copyCall(cell, now.p); } }, icon("copy")));
            }
            if (view.kind !== "table") {
                this.footHost.append(h("button.sm-insp-fb", { type: "button", "data-tooltip": "Send this plot to a figure",
                    "data-tooltip-align": "right",
                    onclick: (e) => {
                        const now = V().current() || cur;
                        this.ctx.sendToFigure(e.currentTarget, cell, { name: now.p.id, view: now.p.view, opts: now.p.opts, title: now.view.name });
                    } }, icon("figure", "sm"), h("span", null, "Figure Builder")));
            }
        }

        async copyCall(cell, p) {
            const text = await V().call(cell.id, p.id).catch(() => "");
            if (!text) { this.ctx.toast("This view has no call to copy."); return; }
            try {
                await navigator.clipboard.writeText(text);
                this.ctx.toast("Copied: " + (text.length > 70 ? text.slice(0, 70) + "…" : text));
            } catch (_) {
                this.ctx.toast(text);
            }
        }

        // -- the UMAP's embedding ------------------------------------------------------------

        embeddingSections(cur) {
            const embed = cur.s.hooks.instance && cur.s.hooks.instance();
            const data = [];
            const display = [];
            if (embed && window.ScimapPicker) {
                const picker = window.ScimapPicker.create({ theme: "light", ariaLabel: "Color by",
                    className: "sm-insp-pick", searchPlaceholder: "Search columns and markers", accordion: true,
                    emptyText: "No column matches", menuWidth: 280, onChange: (value) => embed.setColumn(value) });
                const groups = embed.colorGroups ? embed.colorGroups() : [];
                if (groups.length) picker.setGroups(groups);
                picker.setValue(embed.state && embed.state.column);
                data.push(this.row("Color by", picker.root));
                this.picker = picker;
                this.updaters.push(() => picker.setValue(embed.state && embed.state.column));
                display.push(this.row("Point size", this.slider({ min: 0.3, max: 3, step: 0.1, unit: "×", label: "Point size" },
                    (embed.settings && embed.settings.pointScale) || 1, (v) => embed.setPointScale(v), true).node));
            }
            const bus = window.PlexoraCellSelection;
            if (bus) {
                display.push(this.row("Selection", U().segmented({ label: "How the selection shows", value: bus.mode(),
                    choices: [{ value: "highlight", label: "Highlight" }, { value: "dim", label: "Dim" }, { value: "only", label: "Only" }],
                    onChange: (v) => bus.setMode(v) }).node, "How the selected cells stand out on the plot"));
            }
            let annotate = null;
            if (embed && embed.attachPanel) {
                this.annotateHost = h("div.sm-emb.is-light.sm-insp-annotate");
                embed.attachPanel(this.annotateHost);
                this.attached = embed;
                annotate = this.section("annotate", "Annotate", [this.annotateHost], this.countText(embed));
            }
            return [this.section("data", "Data", data), this.section("display", "Display", display), annotate, this.exportSection(cur)];
        }

        countText(embed) {
            return embed.selected ? Number(embed.selected).toLocaleString("en-US") + " selected" : "";
        }

        paintCount() {
            if (this.attached) this.acc.setSub("annotate", this.countText(this.attached));
        }

        /** Open one section (Annotate, from the embedding's tag button). */
        openSection(id) {
            if (!this.acc.sections.has(id)) return;
            if (!this.acc.isOpen(id)) this.acc.open(id);
            const node = this.acc.sections.get(id).node;
            setTimeout(() => { if (node.isConnected) node.scrollIntoView({ block: "nearest", behavior: "smooth" }); }, 240);
        }

        detachAnnotate() {
            if (this.attached && this.attached.detachPanel) this.attached.detachPanel(this.annotateHost);
            this.attached = null;
            this.annotateHost = null;
            if (this.picker && this.picker.destroy) this.picker.destroy();
            this.picker = null;
        }
    }

    return { Inspector };
})();
