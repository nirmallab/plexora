// The views of a step's result: one bar on every plot, the same controls on
// every tool, a click on a mark to drill down.
//
// A step's run stores its result once; every view here is a different
// `scimappro.pl` call reading it (server/views.py), so switching a view or
// narrowing it never runs the analysis again. A step shows one to three
// panels; each has a view and its settings (`opts`), kept with the notebook as
// display settings (`set_views`), which never mark a result outdated.
//
// The bar over a plot holds its title, what it is narrowed to (a chip that
// clears) and three quiet tools: Customize (the sidebar, scimapInspector.js),
// fullscreen and a menu (download, reset, remove). The view itself is chosen
// in the sidebar's cards, or with keys 1-4; every setting is there too, and
// the sidebar reads and writes the same state here.
window.ScimapViews = (function () {
    "use strict";

    const { h, icon, iconButton } = window.ScimapDom;

    // Views that need the row to themselves.
    const WIDE = new Set(["heatmap", "dotplot", "embedding", "markers", "correlation"]);

    const steps = new Map(); // cellId -> {cell, run, ctx, catalogue, panels, hooks, grid}
    const drawn = new Map(); // view key -> Promise<entry>
    const blocks = new Map(); // cellId/panelId -> {block, bar, caption, host, token}
    const watchers = new Set();
    const saving = new Map();
    let selected = null;
    let picking = false;
    let drillAnchor = null;

    // Settings the page applies to a drawn plot itself (no redraw), so they
    // are not part of what was drawn.
    const LOCAL = new Set(["points"]);

    function stable(opts) {
        const out = {};
        Object.keys(opts || {}).sort().forEach((k) => {
            if (LOCAL.has(k)) return;
            const v = opts[k];
            if (v === null || v === undefined || v === "" || (Array.isArray(v) && !v.length)) return;
            out[k] = v;
        });
        return JSON.stringify(out);
    }

    function viewKey(cellId, runId, view, opts) {
        return cellId + ":" + runId + ":" + view + ":" + stable(opts);
    }

    function notify() {
        watchers.forEach((fn) => { try { fn(); } catch (_) { /* a watcher's own problem */ } });
    }

    // -- state -------------------------------------------------------------

    function seed(cell, run, ctx, figs, hooks) {
        let s = steps.get(cell.id);
        const catalogue = (figs && figs.catalogue) || (s && s.catalogue) || { views: [], controls: {}, max_panels: 3 };
        if (!s || s.runId !== run.run_id) {
            const panels = ((figs && figs.figures) || []).map((f) => ({ id: f.name, view: f.view, opts: Object.assign({}, f.opts || {}) }));
            s = { runId: run.run_id, panels };
            steps.set(cell.id, s);
        }
        ((figs && figs.figures) || []).forEach((f) => {
            const k = viewKey(cell.id, run.run_id, f.view, f.opts || {});
            if (!drawn.has(k) && !f.needs_compute) drawn.set(k, Promise.resolve(f));
        });
        Object.assign(s, { cell, run, ctx, hooks: hooks || {}, catalogue });
        return s;
    }

    function step(cellId) { return steps.get(cellId) || null; }

    function panelOf(s, panelId) { return s && s.panels.find((p) => p.id === panelId); }

    function viewOf(s, viewId) {
        return (s.catalogue.views || []).find((v) => v.id === viewId) || { id: viewId, name: viewId, controls: [], quick: [], axes: {}, actions: [] };
    }

    function control(s, key) { return (s.catalogue.controls || {})[key] || null; }

    // What a "one" control shows when it is not set: what the server draws.
    function effective(s, p, key) {
        const c = control(s, key);
        const value = p.opts[key];
        if (value !== undefined && value !== null && value !== "") return value;
        const view = viewOf(s, p.view);
        if (view.defaults && view.defaults[key] !== undefined) return view.defaults[key];
        if (!c) return null;
        if (c.type === "segment") return c.choices[0] && c.choices[0].value;
        if (c.type === "toggle") return false;
        if (c.type === "range") return c.min;
        if (key === "markerB") return c.choices[1] || c.choices[0] || null;
        return c.choices[0] !== undefined ? c.choices[0] : null;
    }

    // A multi control's value: what is set, else what the view draws by
    // default (a violin's first three markers), else every choice (null).
    function valueOf(s, p, key) {
        if (p.opts[key] !== undefined) return p.opts[key];
        const d = viewOf(s, p.view).defaults || {};
        return d[key] !== undefined ? d[key] : null;
    }

    function choiceLabel(c, value) {
        if (c.labels && c.labels[value] !== undefined) return c.labels[value];
        return value === "" || value === null || value === undefined ? "—" : String(value);
    }

    function multiText(c, value) {
        const all = c.choices.length;
        if (!value || !value.length || value.length === all) return "All " + all;
        if (value.length <= 2) return value.join(", ");
        return value.length + " of " + all;
    }

    // -- changes -----------------------------------------------------------

    function save(s) {
        clearTimeout(saving.get(s.cell.id));
        saving.set(s.cell.id, setTimeout(() => {
            saving.delete(s.cell.id);
            s.ctx.op({ op: "set_views", id: s.cell.id, views: s.panels.map((p) => ({ id: p.id, view: p.view, opts: p.opts })) }, { quiet: true });
        }, 450));
    }

    function update(cellId, panelId, patch) {
        const s = step(cellId);
        const p = panelOf(s, panelId);
        if (!p) return;
        const wasView = p.view;
        if (patch.view) p.view = patch.view;
        if (patch.opts) p.opts = patch.opts;
        save(s);
        const kind = (v) => viewOf(s, v).kind;
        if (wasView !== p.view && (kind(wasView) === "embedding" || kind(p.view) === "embedding" ||
            WIDE.has(wasView) !== WIDE.has(p.view))) paintGrid(cellId);
        else repaint(cellId, panelId);
        notify();
    }

    function setView(cellId, panelId, viewId) {
        update(cellId, panelId, { view: viewId });
    }

    function setOpt(cellId, panelId, key, value) {
        const p = panelOf(step(cellId), panelId);
        if (!p) return;
        const opts = Object.assign({}, p.opts);
        if (value === null || value === undefined || (Array.isArray(value) && !value.length)) delete opts[key];
        else opts[key] = value;
        if (LOCAL.has(key)) {
            // Point size: the drawn plot is resized in place, then kept.
            const s = step(cellId);
            p.opts = opts;
            save(s);
            const reg = blocks.get(cellId + "/" + panelId);
            const div = reg && reg.host.querySelector(".sm-plotly");
            if (div) applyPoints(div, Number(opts.points) || 1);
            notify();
            return;
        }
        update(cellId, panelId, { opts });
    }

    // Every marker size of a plot times `factor`, from the sizes it was drawn
    // with (a dot plot's sizes carry its data, so they scale, never reset).
    function applyPoints(div, factor) {
        if (!window.Plotly || !div.data) return;
        if (!div.__smBase) {
            // plotly's own decoded copy: a figure's sizes may arrive as base64
            // typed arrays ({dtype, bdata}), which only _fullData has unpacked.
            const full = new Map((div._fullData || []).map((t) => [t.index, t]));
            div.__smBase = div.data.map((t, i) => {
                const f = full.get(i);
                const size = f && f.marker ? f.marker.size : t.marker && t.marker.size;
                if (size === undefined || size === null) return null;
                if (ArrayBuffer.isView(size) || Array.isArray(size)) return Array.from(size, Number);
                return typeof size === "number" && isFinite(size) ? size : null;
            });
        }
        const sizes = [];
        const index = [];
        div.__smBase.forEach((base, i) => {
            if (base === null) return;
            index.push(i);
            sizes.push(Array.isArray(base) ? base.map((v) => v * factor) : base * factor);
        });
        if (index.length && div.__smFactor !== factor) {
            div.__smFactor = factor;
            window.Plotly.restyle(div, { "marker.size": sizes }, index);
        }
    }

    function hasPoints(div) {
        return (div.data || []).some((t) => (t.type === "scatter" || t.type === "scattergl") &&
            String(t.mode || "markers").includes("markers"));
    }

    function isMap(div) {
        const fl = div._fullLayout;
        return Boolean(hasPoints(div) && fl && fl.yaxis && fl.xaxis && (fl.yaxis.scaleanchor === "x" || fl.xaxis.scaleanchor === "y"));
    }

    // Zoom about the middle, eased over a few frames.
    function zoomBy(div, f) {
        const fl = div._fullLayout;
        if (!fl || !window.Plotly) return;
        const xr = fl.xaxis.range.slice();
        const yr = fl.yaxis.range.slice();
        const cx = (xr[0] + xr[1]) / 2;
        const cy = (yr[0] + yr[1]) / 2;
        const tx = [cx - (xr[1] - xr[0]) / 2 / f, cx + (xr[1] - xr[0]) / 2 / f];
        const ty = [cy - (yr[1] - yr[0]) / 2 / f, cy + (yr[1] - yr[0]) / 2 / f];
        const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        const frames = reduce ? 1 : 6;
        let k = 0;
        const step = () => {
            k += 1;
            const t = k / frames;
            const e = 1 - Math.pow(1 - t, 3);
            const mix = (a, b) => [a[0] + (b[0] - a[0]) * e, a[1] + (b[1] - a[1]) * e];
            window.Plotly.relayout(div, { "xaxis.range": mix(xr, tx), "yaxis.range": mix(yr, ty) }).then(() => {
                if (k < frames) requestAnimationFrame(step);
            });
        };
        step();
    }

    // The tools inside a plot of points, as the UMAP has them: zoom and fit
    // on a map, a point-size slider on every plot of points.
    function plotTools(s, p, reg, div) {
        const old = reg.host.querySelector(".sm-plot-tools");
        if (old) old.remove();
        if (!hasPoints(div)) {
            if (reg.host.classList.contains("has-tools")) {
                reg.host.classList.remove("has-tools");
                window.ScimapFigures.refit(div);
            }
            return;
        }
        const map = isMap(div);
        const tool = (name, tip, onclick) => h("button.sm-plot-tool", { type: "button", "aria-label": tip.split("  ")[0],
            "data-tooltip": tip, "data-tooltip-align": "right", onclick }, icon(name, "sm"));
        const size = tool("sizes", "Point size", (e) => sizePop(e.currentTarget, s, p));
        const tools = map ? [
            tool("plus", "Zoom in", () => zoomBy(div, 1.5)),
            tool("minus", "Zoom out", () => zoomBy(div, 1 / 1.5)),
            tool("fit", "Fit  ·  double-click", () => window.Plotly.relayout(div, { "xaxis.autorange": true, "yaxis.autorange": true })),
            size,
        ] : [size];
        const bar = h("div.sm-plot-tools", { role: "toolbar", "aria-label": "Plot tools" }, tools);
        reg.host.append(bar);
        // The tools keep a strip of their own beside the figure, so they
        // never sit on the data, a legend or a color bar.
        if (!reg.host.classList.contains("has-tools")) {
            reg.host.classList.add("has-tools");
            window.ScimapFigures.refit(div);
        }
    }

    function sizePop(anchor, s, p) {
        const mount = h("div.sm-plot-size-slider");
        const node = h("div.sm-plot-size", { role: "dialog", "aria-label": "Point size" },
            h("div.sm-ui-pop-head", null, h("strong", null, "Point size")), mount);
        const value = Number(p.opts.points) || 1;
        const put = (v) => setOpt(s.cell.id, p.id, "points", Math.round(Number(v) * 10) / 10);
        if (typeof window.PlexoraSlider === "function") {
            new window.PlexoraSlider(mount, { min: 0.2, max: 4, step: 0.1, value, unit: "×", label: "Point size",
                onInput: put, onChange: put });
        } else {
            mount.append(h("input", { type: "range", min: 0.2, max: 4, step: 0.1, value, "aria-label": "Point size",
                oninput: (e) => put(e.target.value) }));
        }
        window.ScimapUi.float(anchor, node, { width: 240, align: "end" });
    }

    function reset(cellId, panelId) {
        update(cellId, panelId, { opts: {} });
    }

    function add(cellId) {
        const s = step(cellId);
        if (!s || s.panels.length >= (s.catalogue.max_panels || 3)) return;
        const shown = new Set(s.panels.map((p) => p.view));
        const next = (s.catalogue.views || []).find((v) => !shown.has(v.id)) || s.catalogue.views[0];
        if (!next) return;
        const id = "p" + Math.random().toString(36).slice(2, 7);
        s.panels.push({ id, view: next.id, opts: {} });
        save(s);
        paintGrid(cellId);
        select(cellId, id);
    }

    function remove(cellId, panelId) {
        const s = step(cellId);
        if (!s || s.panels.length < 2) return;
        s.panels = s.panels.filter((p) => p.id !== panelId);
        blocks.delete(cellId + "/" + panelId);
        save(s);
        if (selected && selected.cellId === cellId && selected.panelId === panelId) selected = { cellId, panelId: s.panels[0].id };
        paintGrid(cellId);
        notify();
    }

    function select(cellId, panelId) {
        const same = selected && selected.cellId === cellId && selected.panelId === panelId;
        selected = { cellId, panelId };
        markSelected();
        if (!same) notify();
    }

    function setPicking(on) {
        picking = Boolean(on);
        markSelected();
    }

    function markSelected() {
        for (const [k, reg] of blocks) {
            const [cellId, panelId] = k.split("/");
            const on = Boolean(selected && selected.cellId === cellId && selected.panelId === panelId);
            reg.block.classList.toggle("is-picked", on && picking);
            const btn = reg.block.querySelector(".sm-view-customize");
            if (btn) {
                btn.classList.toggle("is-on", on && picking);
                btn.setAttribute("aria-pressed", String(on && picking));
            }
        }
    }

    // -- drawing -----------------------------------------------------------

    function fetchEntry(s, p, compute) {
        const k = viewKey(s.cell.id, s.run.run_id, p.view, p.opts);
        if (!drawn.has(k) || compute) {
            const promise = s.ctx.drawView(s.cell.id, p.view, p.opts, compute);
            drawn.set(k, promise);
            promise.then((entry) => { if (entry && (entry.needs_compute || entry.error)) drawn.delete(k); })
                .catch(() => drawn.delete(k));
        }
        return drawn.get(k);
    }

    // Every panel of one step, in a row that wraps; a panel's menu adds one.
    function grid(cell, ctx, run, figs, hooks) {
        const s = seed(cell, run, ctx, figs, hooks);
        s.grid = h("div.sm-figures.sm-views");
        s.row = h("div.sm-views-row", null, s.grid);
        paintGrid(cell.id);
        return s.row;
    }

    function paintGrid(cellId) {
        const s = step(cellId);
        if (!s || !s.grid) return;
        for (const k of Array.from(blocks.keys())) if (k.indexOf(cellId + "/") === 0) blocks.delete(k);
        s.grid.textContent = "";
        s.panels.forEach((p) => s.grid.append(panelBlock(s, p)));
        markSelected();
    }

    function panelBlock(s, p) {
        const block = h("figure.sm-figure.sm-view", { "data-panel": p.id });
        // One line over the plot: its title (the view is chosen in the
        // sidebar's cards, so the bar does not repeat it), what it is
        // narrowed to, and three quiet tools.
        const caption = h("figcaption.sm-view-title");
        const chipsHost = h("div.sm-view-chips");
        const toolsHost = h("span.sm-view-tools");
        const bar = h("div.sm-view-bar", null, caption, chipsHost, toolsHost);
        const host = h("div.sm-figure-plot");
        block.append(bar, host);
        block.addEventListener("pointerdown", () => select(s.cell.id, p.id));
        const reg = { block, bar, caption, chipsHost, toolsHost, host, token: 0 };
        blocks.set(s.cell.id + "/" + p.id, reg);
        paint(s, p, reg);
        return block;
    }

    function repaint(cellId, panelId) {
        const s = step(cellId);
        const reg = blocks.get(cellId + "/" + panelId);
        const p = panelOf(s, panelId);
        if (!s || !reg || !p) return;
        paint(s, p, reg);
        markSelected();
    }

    function paint(s, p, reg) {
        const view = viewOf(s, p.view);
        const one = s.panels.length === 1;
        reg.block.classList.toggle("is-wide", one || WIDE.has(p.view));
        reg.block.classList.toggle("is-embedding", view.kind === "embedding");
        paintBar(s, p, reg);
        paintPlot(s, p, reg);
    }

    // What a view is narrowed to, one chip each that opens its list or clears
    // it. A view showing everything has no chips: its controls are in the
    // sidebar.
    function chips(s, p) {
        const view = viewOf(s, p.view);
        const out = [];
        (view.controls || []).forEach((k) => {
            const c = control(s, k);
            const v = p.opts[k];
            if (!c || c.type !== "multi" || !Array.isArray(v) || !v.length || v.length >= c.choices.length) return;
            out.push(h("span.sm-vfocus", null,
                h("button.sm-vfocus-body", { type: "button", "data-sm-opt": s.cell.id + "/" + p.id + "/" + k,
                    "aria-haspopup": "dialog", "data-tooltip": "Change what " + c.label.toLowerCase() + " shows",
                    onclick: (e) => multiPicker(e.currentTarget, s, p, k) }, h("em", null, c.label), " ", multiText(c, v)),
                h("button.sm-vfocus-x", { type: "button", "aria-label": "Show every " + (c.noun || "value").replace(/s$/, ""),
                    "data-tooltip": "Show all", onclick: () => setOpt(s.cell.id, p.id, k, null) }, icon("x", "xs"))));
        });
        return out;
    }

    function paintBar(s, p, reg) {
        const view = viewOf(s, p.view);
        reg.chipsHost.textContent = "";
        reg.toolsHost.textContent = "";
        reg.bar.hidden = view.kind === "embedding";
        if (view.kind === "embedding") return;
        reg.chipsHost.append(...chips(s, p));
        reg.chipsHost.hidden = !reg.chipsHost.childNodes.length;
        reg.toolsHost.append(...tools(s, p, reg));
    }

    function tools(s, p, reg) {
        const view = viewOf(s, p.view);
        const out = [customizeButton(s, p), quiet(iconButton("full", "Fullscreen", () => fullscreen(reg)), "Fullscreen")];
        const more = iconButton("dots", "More for this view", (e) => {
            const anchor = e.currentTarget;
            const items = [];
            if (s.panels.length < (s.catalogue.max_panels || 3) && (s.catalogue.views || []).length > 1) {
                items.push({ label: "Add a view beside", icon: "plus", onSelect: () => add(s.cell.id) });
            }
            if (view.kind !== "table") items.push({ label: "Download…", icon: "download", onSelect: () => download(anchor, s, p) });
            items.push({ label: "Reset this view", icon: "undo", disabled: !Object.keys(p.opts || {}).length, onSelect: () => reset(s.cell.id, p.id) });
            if (s.panels.length > 1) items.push({ label: "Remove this view", icon: "x", onSelect: () => remove(s.cell.id, p.id) });
            s.ctx.menu(anchor, items);
        });
        out.push(quiet(more, "More"));
        return out;
    }

    // Core's fast tooltip in place of the slow native one.
    function quiet(b, tip) {
        b.removeAttribute("title");
        b.setAttribute("data-tooltip", tip);
        return b;
    }

    function customizeButton(s, p) {
        const b = iconButton("sliders", "Customize", (e) => {
            e.stopPropagation();
            select(s.cell.id, p.id);
            s.ctx.inspect(s.cell.id, p.id);
        });
        b.classList.add("sm-view-customize");
        return quiet(b, "Customize  ]");
    }

    function caption(reg, title, note) {
        reg.caption.textContent = "";
        if (!title) return;
        reg.caption.append(h("b", null, title));
        if (note) reg.caption.append(h("span", { "data-tooltip": note.length > 60 ? note : null }, note));
    }

    function paintPlot(s, p, reg) {
        const view = viewOf(s, p.view);
        const host = reg.host;
        const token = ++reg.token;
        const held = host.offsetHeight;
        if (held) host.style.minHeight = held + "px";
        if (view.kind === "table") {
            host.textContent = "";
            host.style.minHeight = "";
            caption(reg, view.name === "Table" ? "The numbers" : view.name, "");
            host.append(h("div.sm-view-table", null, s.hooks.table ? s.hooks.table() : h("p.sm-figure-error", null, "No table for this step.")));
            return;
        }
        if (view.kind === "embedding") {
            caption(reg, "", "");
            host.textContent = "";
            host.style.minHeight = "";
            if (s.hooks.embedding) s.hooks.embedding(host, { leading: null, trailing: [customizeButton(s, p)] });
            return;
        }
        if (!host.firstChild) host.append(h("p.sm-figure-wait", null, "Drawing…"));
        fetchEntry(s, p).then((entry) => {
            if (token !== reg.token) return;
            render(s, p, reg, entry);
        }).catch((err) => {
            if (token !== reg.token) return;
            host.textContent = "";
            host.style.minHeight = "";
            host.append(h("p.sm-figure-error", null, err.message));
        });
    }

    function render(s, p, reg, entry) {
        const host = reg.host;
        const done = () => requestAnimationFrame(() => { host.style.minHeight = ""; });
        if (entry.needs_compute) {
            caption(reg, entry.title, "");
            host.textContent = "";
            const go = h("button.sm-btn.sm-btn-primary", { type: "button", onclick: () => {
                go.disabled = true;
                go.textContent = "Computing…";
                fetchEntry(s, p, true).then((made) => render(s, p, reg, made)).catch((err) => {
                    go.disabled = false;
                    go.textContent = "Compute";
                    s.ctx.toast(err.message);
                });
            } }, "Compute");
            host.append(h("div.sm-vcompute", null,
                h("p", null, h("b", null, viewOf(s, p.view).name), " needs the pairs scored at several radii, which this step did not compute."),
                h("div.sm-vcompute-row", null, go, h("span", null, (entry.cost || "a few seconds") +
                    " · computed in memory; the step's result is unchanged"))));
            done();
            return;
        }
        if (entry.error) {
            caption(reg, entry.title, "");
            host.textContent = "";
            host.append(h("p.sm-figure-error", null, "Scimap Pro could not draw this view: " + entry.error));
            done();
            return;
        }
        if (entry.image) {
            caption(reg, entry.title, "a picture drawn by Scimap Pro");
            host.textContent = "";
            host.append(h("img.sm-view-img", { src: entry.image, alt: entry.title }));
            done();
            return;
        }
        caption(reg, entry.title, entry.note || "");
        const k = viewKey(s.cell.id, s.run.run_id, p.view, p.opts);
        host.textContent = "";
        window.ScimapFigures.mount(host, entry, k).then((div) => {
            div.__smPanel = { cellId: s.cell.id, panelId: p.id };
            applyPoints(div, Number(p.opts.points) || 1);
            plotTools(s, p, reg, div);
            if (!div.__smWired && div.on) {
                div.__smWired = true;
                div.on("plotly_click", (ev) => drill(div.__smPanel, ev));
            }
            done();
        }).catch((err) => {
            host.textContent = "";
            host.append(h("p.sm-figure-error", null, err.message));
            done();
        });
    }

    // -- pickers -----------------------------------------------------------

    // The chip that opened a picker, drawn again after the plot changed.
    function chipFor(s, p, key) {
        return () => document.querySelector('[data-sm-opt="' + s.cell.id + "/" + p.id + "/" + key + '"]');
    }

    function onePicker(anchor, s, p, key) {
        const c = control(s, key);
        window.ScimapUi.pickList(anchor, { title: c.label, noun: c.noun || "values", reanchor: chipFor(s, p, key),
            items: c.choices.map((v) => ({ value: v, label: choiceLabel(c, v) })),
            value: effective(s, p, key), onChange: (v) => setOpt(s.cell.id, p.id, key, v === "" ? null : v) });
    }

    // A checklist that applies as it changes: what a view is narrowed to
    // (scimapUi.js: search, All / Selected, drag to select, Undo).
    function multiPicker(anchor, s, p, key) {
        const c = control(s, key);
        window.ScimapUi.pickList(anchor, { title: c.label, noun: c.noun || "values", items: c.choices, multiple: true,
            reanchor: chipFor(s, p, key),
            value: valueOf(s, p, key), onChange: (values, every) => setOpt(s.cell.id, p.id, key, every ? null : values) });
    }

    // -- drill-down --------------------------------------------------------

    function fmt(value) {
        if (typeof value !== "number" || !isFinite(value)) return value === null || value === undefined ? "" : String(value);
        const a = Math.abs(value);
        return a >= 100 ? String(Math.round(value)) : a >= 1 ? value.toFixed(1) : value.toPrecision(2);
    }

    function onlyLabel(opt, value) {
        if (opt === "from") return "Only from " + value;
        if (opt === "to") return "Only to " + value;
        return "Only " + value;
    }

    function drill(where, ev) {
        const s = step(where.cellId);
        const p = panelOf(s, where.panelId);
        const pt = ev && ev.points && ev.points[0];
        if (!s || !p || !pt) return;
        const view = viewOf(s, p.view);
        const axes = view.axes || {};
        if (!Object.keys(axes).length) return;
        const horizontal = pt.data && pt.data.orientation === "h";
        const vals = {
            x: horizontal ? pt.y : pt.x,
            y: horizontal ? pt.x : pt.y,
            series: pt.data && pt.data.name,
        };
        const value = pt.z !== undefined ? pt.z : (axes.y ? null : vals.y);
        const parts = [];
        if (axes.y && vals.y !== undefined) parts.push(String(vals.y));
        if (axes.x && vals.x !== undefined) parts.push(String(vals.x));
        let head = parts.join(axes.x && axes.x.role === "to" ? " → " : " · ");
        if (axes.series && vals.series) head += " · " + vals.series;
        if (value !== null && value !== undefined && value !== "") head += "   " + fmt(value);
        const items = [{ label: head, heading: true }];
        const fill = (text) => String(text).replace(/\{(\w+)\}/g, (_, a) => (vals[a] === undefined ? "" : String(vals[a])));
        const offered = new Set((s.catalogue.views || []).map((v) => v.id));
        (view.actions || []).forEach((a) => {
            if (a.view && !offered.has(a.view)) return;
            if (Object.values(a.set || {}).some((axis) => axis && vals[axis] === undefined)) return;
            items.push({ label: fill(a.label), icon: a.view ? (viewOf(s, a.view).icon || "grid") : "check", onSelect: () => {
                const opts = Object.assign({}, p.opts);
                Object.entries(a.set || {}).forEach(([opt, axis]) => {
                    const c = control(s, opt);
                    if (!axis) delete opts[opt];
                    else opts[opt] = c && c.type === "multi" ? [String(vals[axis])] : String(vals[axis]);
                });
                update(s.cell.id, p.id, { view: a.view || p.view, opts });
            } });
        });
        // What an action above already narrows (from, by this axis) is not
        // offered again as a plain "Only".
        const covered = new Set();
        (view.actions || []).forEach((a) => Object.entries(a.set || {}).forEach(([opt, axis]) => {
            if (axis && !a.view) covered.add(opt + ":" + axis);
        }));
        const tissue = new Map();
        ["y", "x", "series"].forEach((axis) => {
            const a = axes[axis];
            const v = vals[axis];
            if (!a || v === undefined || v === null) return;
            const c = control(s, a.opt);
            if (a.opt && (view.controls || []).includes(a.opt) && c && c.type === "multi" && c.choices.includes(String(v)) &&
                !covered.has(a.opt + ":" + axis) && !items.some((it) => it.label === onlyLabel(a.opt, v))) {
                items.push({ label: onlyLabel(a.opt, v), icon: "check", onSelect: () => setOpt(s.cell.id, p.id, a.opt, [String(v)]) });
            }
            if (a.column && s.ctx.viewColumn) {
                if (!tissue.has(a.column)) tissue.set(a.column, []);
                if (!tissue.get(a.column).includes(String(v))) tissue.get(a.column).push(String(v));
            }
        });
        // Both kinds of a pair are one look at the tissue, not two.
        tissue.forEach((values, column) => items.push({ label: "Show " + values.join(" and ") + " in the tissue", icon: "image",
            onSelect: () => s.ctx.viewColumn(column, values) }));
        if (items.length < 2) return;
        const e = ev.event || {};
        if (!drillAnchor || !drillAnchor.isConnected) {
            drillAnchor = h("span.sm-drill-anchor", { "aria-hidden": "true" });
            (document.getElementById("sm_workspace") || document.body).append(drillAnchor);
        }
        drillAnchor.style.left = (e.clientX || 0) + "px";
        drillAnchor.style.top = (e.clientY || 0) + "px";
        s.ctx.closeFloating();
        const plot = ev.event && ev.event.target && ev.event.target.closest && ev.event.target.closest(".sm-plotly");
        if (plot && window.Plotly && window.Plotly.Fx) window.Plotly.Fx.unhover(plot);
        s.ctx.menu(drillAnchor, items);
    }

    // -- download, fullscreen -------------------------------------------------

    function entryFor(s, p) {
        const view = viewOf(s, p.view);
        return { name: p.id, view: p.view, opts: p.opts, title: view.name };
    }

    function download(anchor, s, p) {
        s.ctx.figureMenu(anchor, s.cell, entryFor(s, p));
    }

    function fullscreen(reg, force) {
        const on = force === undefined ? !reg.full : Boolean(force);
        if (on === Boolean(reg.full)) return;
        const block = reg.block;
        if (on) {
            reg.placeholder = h("div.sm-view-placeholder", { style: { height: block.offsetHeight + "px" } });
            block.before(reg.placeholder);
            reg.full = h("div.sm-view-full", { role: "dialog", "aria-modal": "true", "aria-label": "Fullscreen view" });
            reg.full.append(block);
            (document.getElementById("sm_workspace") || document.body).append(reg.full);
            reg.onKey = (e) => { if (e.key === "Escape" && !document.querySelector(".sm-menu, .sm-vpick")) fullscreen(reg, false); };
            document.addEventListener("keydown", reg.onKey);
        } else {
            reg.placeholder.replaceWith(block);
            reg.full.remove();
            reg.full = null;
            reg.placeholder = null;
            document.removeEventListener("keydown", reg.onKey);
        }
        block.classList.toggle("is-full", on);
        const full = block.querySelector('[aria-label="Fullscreen"], [aria-label="Exit fullscreen (Esc)"]');
        if (full) {
            full.setAttribute("aria-label", on ? "Exit fullscreen (Esc)" : "Fullscreen");
            full.title = full.getAttribute("aria-label");
            full.textContent = "";
            full.append(icon(on ? "unfull" : "full"));
        }
        // plotly keeps the height it was drawn at: give it the room, then back.
        requestAnimationFrame(() => {
            const plot = block.querySelector(".sm-plotly");
            if (!plot || !window.Plotly || !plot.layout) return;
            if (on) {
                plot.__smHeight = plot.layout.height;
                const room = Math.max(plot.__smHeight || 0, reg.host.clientHeight - 12);
                window.Plotly.relayout(plot, { height: room });
            } else if (plot.__smHeight) {
                window.Plotly.relayout(plot, { height: plot.__smHeight });
            } else {
                window.Plotly.Plots.resize(plot);
            }
        });
    }

    // -- the plotted numbers, as CSV -----------------------------------------

    const TYPED = { f8: Float64Array, f4: Float32Array, i4: Int32Array, i2: Int16Array, i1: Int8Array,
        u4: Uint32Array, u2: Uint16Array, u1: Uint8Array };

    function decode(values) {
        if (values && typeof values === "object" && values.bdata) {
            const raw = atob(values.bdata);
            const bytes = new Uint8Array(raw.length);
            for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
            const Typed = TYPED[values.dtype] || Float64Array;
            const flat = Array.from(new Typed(bytes.buffer));
            if (values.shape) {
                const [rows, cols] = String(values.shape).split(",").map(Number);
                if (cols) return Array.from({ length: rows }, (_, r) => flat.slice(r * cols, (r + 1) * cols));
            }
            return flat;
        }
        return values || [];
    }

    // `[header, rows]` from a drawn figure: a heatmap's matrix, bars' heights.
    function plotted(figure) {
        const data = (figure && figure.data) || [];
        const heat = data.find((t) => t.type === "heatmap");
        if (heat) {
            const z = decode(heat.z);
            const x = decode(heat.x);
            const y = decode(heat.y);
            return [[""].concat(x.map(String)), z.map((row, i) => [y[i]].concat(Array.from(row)))];
        }
        const bars = data.filter((t) => t.type === "bar");
        if (bars.length) {
            const rows = [];
            bars.forEach((t) => {
                const x = decode(t.x);
                const y = decode(t.y);
                const horizontal = t.orientation === "h";
                const cats = horizontal ? y : x;
                const vals = horizontal ? x : y;
                cats.forEach((c, i) => rows.push([t.name || "", c, vals[i]]));
            });
            return [["series", "category", "value"], rows];
        }
        return null;
    }

    async function csv(cellId, panelId) {
        const s = step(cellId);
        const p = panelOf(s, panelId);
        if (!s || !p) return false;
        const view = viewOf(s, p.view);
        if (view.kind === "table" || view.kind === "embedding") return false;
        const entry = await fetchEntry(s, p);
        const table = entry && entry.figure ? plotted(entry.figure) : null;
        if (!table) return false;
        const stem = ((s.run.effective || {}).label || s.cell.tool) + "_" + p.view;
        window.ScimapPlots.csv(stem + ".csv", table[0], table[1]);
        return true;
    }

    async function call(cellId, panelId) {
        const s = step(cellId);
        const p = panelOf(s, panelId);
        if (!s || !p) return "";
        const entry = await fetchEntry(s, p);
        return (entry && entry.call) || "";
    }

    // -- for the sidebar and the keys -----------------------------------------

    function watch(fn) {
        watchers.add(fn);
        return () => watchers.delete(fn);
    }

    function current() {
        if (!selected) return null;
        const s = step(selected.cellId);
        const p = panelOf(s, selected.panelId);
        if (!s || !p) return null;
        return { s, p, view: viewOf(s, p.view), reg: blocks.get(selected.cellId + "/" + selected.panelId) };
    }

    // The 1-4 keys: the selected plot's views, in the bar's order.
    function nth(index) {
        const cur = current();
        if (!cur) return false;
        const v = (cur.s.catalogue.views || [])[index];
        if (!v) return false;
        setView(cur.s.cell.id, cur.p.id, v.id);
        return true;
    }

    // A step that re-ran or left drops what was drawn for it.
    function forget(cellId) {
        steps.delete(cellId);
        for (const k of Array.from(drawn.keys())) if (k.indexOf(cellId + ":") === 0) drawn.delete(k);
        for (const k of Array.from(blocks.keys())) if (k.indexOf(cellId + "/") === 0) blocks.delete(k);
        if (selected && selected.cellId === cellId) selected = null;
        notify();
    }

    function firstPanel(cellId) {
        const s = step(cellId);
        return s && s.panels[0] ? s.panels[0].id : null;
    }

    return {
        grid, step, viewOf, control, effective, valueOf, choiceLabel, multiText, setView, setOpt, reset, add, remove,
        select, setPicking, watch, current, nth, forget, firstPanel, csv, call, download, multiPicker, onePicker,
        selected: () => selected, WIDE,
    };
})();
