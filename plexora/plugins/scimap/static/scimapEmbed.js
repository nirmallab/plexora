// An embedding to select cells on, in the notebook and in a window over the
// tissue.
//
// One controller, two skins: `theme: "light"` is a UMAP step's result on the
// notebook page (a toolbar above the plot, an Annotate panel beside it);
// `theme: "glass"` is the floating window in the viewer (its head holds the
// two menus, a footer under the plot holds the count, Highlight / Dim others /
// Only and Annotate). Inside the plot both are the same: a column of tools on
// the left (Navigate, Lasso, Polygon, Brush, Eraser, add / take away, Undo,
// Redo, Clear), zoom and the point size on the right, the legend at the
// bottom right -- a group list that hides and shows groups, or a scale.
//
// Every cell is drawn (scimapScatterGL.js) and every tool tests every cell,
// not a sample. The data comes once, as binary (server/embedding.py): the
// coordinates, each cell's identity in the viewer (project + mask label), and
// a colour column when it is chosen.
//
// The selection is not this view's. It lives in core's selection
// (services/cellSelection.js), keyed by project and mask label, with its Undo
// history, so the tissue (Cell Explorer draws it), the other view and the
// next page all see the same cells, and Undo steps back whichever of them
// made the change. A tool here writes it; a change made elsewhere is read
// back into the plot.
//
// Depends on nothing of the notebook page, so the viewer can load it alone
// (with scimapPicker.js for its menus).
window.ScimapEmbed = (function () {
    "use strict";

    const MISSING = 0xFFFF;
    // The continuous ramp of the boards (dark violet to yellow), as stops.
    const RAMP = ["#2b1d55", "#4a2a7a", "#7c3a8f", "#b0457f", "#e5525b", "#f6905a", "#fbd55b"];
    const SETTINGS_KEY = "plexora:scimap-embed:settings";
    const kept = new Map();
    const dataCache = new Map();

    // -- small helpers ------------------------------------------------------

    function h(tag, attrs, ...children) {
        const [name, ...classes] = tag.split(".");
        const node = document.createElement(name || "div");
        if (classes.length) node.className = classes.join(" ");
        for (const [k, v] of Object.entries(attrs || {})) {
            if (v === null || v === undefined || v === false) continue;
            if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
            else if (k === "style" && typeof v === "object") Object.assign(node.style, v);
            else if (k === "value") node.value = v;
            else node.setAttribute(k, v === true ? "" : v);
        }
        children.flat(Infinity).forEach((c) => {
            if (c === null || c === undefined || c === false) return;
            node.append(c instanceof Node ? c : document.createTextNode(String(c)));
        });
        return node;
    }

    /** `node.append` that skips null: append(null) writes the text "null". */
    function append(node, ...children) {
        node.append(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
        return node;
    }

    const ICONS = {
        navigate: '<path d="M5 3l14 7-6 2-2 6-6-15Z" stroke-linejoin="round"/>',
        lasso: '<path d="M6 5c4-3 12-2 13 4s-6 9-11 8-5-9-2-12Z" stroke-dasharray="2.5 3"/>',
        polygon: '<path d="M5 9 12 4l7 5-3 10H8L5 9Z" stroke-linejoin="round"/><circle cx="5" cy="9" r="1.4" fill="currentColor"/><circle cx="12" cy="4" r="1.4" fill="currentColor"/><circle cx="19" cy="9" r="1.4" fill="currentColor"/>',
        brush: '<path d="M18 3 9 12M7 14c-2 0-3 2-3 4 0 1-1 2-2 2 2 1 6 1 7-2 1-1 1-3-2-4Z"/>',
        eraser: '<path d="m14 4 6 6-9 9H6l-3-3 11-12ZM9 9l6 6M11 19h9" stroke-linejoin="round"/>',
        add: '<path d="M12 6v12M6 12h12"/>',
        sub: '<path d="M6 12h12"/>',
        undo: '<path d="M9 14 4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 0 12h-3"/>',
        redo: '<path d="m15 14 5-5-5-5"/><path d="M20 9H10a6 6 0 0 0 0 12h3"/>',
        clear: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/>',
        more: '<circle cx="5" cy="12" r="1.6" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.6" fill="currentColor" stroke="none"/><circle cx="19" cy="12" r="1.6" fill="currentColor" stroke="none"/>',
        plus: '<path d="M12 5v14M5 12h14"/>',
        minus: '<path d="M5 12h14"/>',
        fit: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
        dots: '<circle cx="6" cy="16" r="1.6" fill="currentColor" stroke="none"/><circle cx="12" cy="13" r="2.4" fill="currentColor" stroke="none"/><circle cx="18.5" cy="9" r="3.4" fill="currentColor" stroke="none"/>',
        eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
        eyeOff: '<path d="M3 3l18 18M10.6 5.1A10 10 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.2 4M6.6 6.6C3.9 8.3 2 12 2 12s3.6 7 10 7a9.6 9.6 0 0 0 4.3-1"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
        tag: '<path d="M3 12V4h8l10 10-8 8L3 12Z" stroke-linejoin="round"/><circle cx="7.5" cy="7.5" r="1.4" fill="currentColor"/>',
        help: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.7.3-1 .9-1 1.7M12 17v.5"/>',
        external: '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
        download: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
        full: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
        unfull: '<path d="M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5"/>',
        x: '<path d="M6 6l12 12M18 6 6 18"/>',
        chevD: '<path d="m6 9 6 6 6-6"/>',
        chevR: '<path d="m9 6 6 6-6 6"/>',
        bars: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
        bookmark: '<path d="M5 3h14v18l-7-5-7 5V3Z" stroke-linejoin="round"/>',
        layers: '<path d="m12 3 9 5-9 5-9-5 9-5Z" stroke-linejoin="round"/><path d="m3 13 9 5 9-5"/>',
        embed: '<path d="M4 4v16h16"/><circle cx="9" cy="14" r="1.2" fill="currentColor"/><circle cx="13" cy="9" r="1.2" fill="currentColor"/><circle cx="17" cy="12" r="1.2" fill="currentColor"/>',
    };

    function icon(name, size) {
        const span = document.createElement("span");
        span.className = "sm-emb-ico";
        span.setAttribute("aria-hidden", "true");
        span.innerHTML = '<svg viewBox="0 0 24 24" width="' + (size || 15) + '" height="' + (size || 15) +
            '" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">' + (ICONS[name] || "") + "</svg>";
        return span;
    }

    const fmt = (n) => Number(n || 0).toLocaleString("en-US");
    const pct = (part, whole) => {
        if (!whole) return "0%";
        const v = (100 * part) / whole;
        return (v >= 10 || v === 0 ? v.toFixed(1) : v < 0.1 ? v.toFixed(2) : v.toFixed(1)) + "%";
    };
    const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || "");
    const MOD = isMac ? "⌘" : "Ctrl+";
    const ALT = isMac ? "⌥" : "Alt";

    function hex(color) {
        const m = /^#?([0-9a-f]{6})$/i.exec(String(color || "").trim());
        if (!m) return [150, 156, 168];
        const v = parseInt(m[1], 16);
        return [(v >> 16) & 255, (v >> 8) & 255, v & 255];
    }

    const RAMP_LUT = (function () {
        const stops = RAMP.map(hex);
        const out = new Uint8Array(256 * 3);
        for (let i = 0; i < 256; i += 1) {
            const t = (i / 255) * (stops.length - 1);
            const a = Math.floor(t);
            const b = Math.min(stops.length - 1, a + 1);
            const f = t - a;
            for (let k = 0; k < 3; k += 1) out[i * 3 + k] = Math.round(stops[a][k] * (1 - f) + stops[b][k] * f);
        }
        return out;
    })();

    function defaultBase() {
        const root = document.getElementById("sm_workspace");
        if (root && root.dataset.base) return root.dataset.base;
        return typeof window.plexoraUrl === "function" ? window.plexoraUrl("/plugins/scimap") : "/plugins/scimap";
    }

    function readSettings() {
        try {
            return Object.assign({ pointScale: 1, brush: 18, tool: "lasso", panel: true, legend: true },
                JSON.parse(window.localStorage.getItem(SETTINGS_KEY) || "{}"));
        } catch (err) {
            return { pointScale: 1, brush: 18, tool: "lasso", panel: true, legend: true };
        }
    }

    function writeSettings(settings) {
        try { window.localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings)); } catch (err) { /* this page only */ }
    }

    // -- the data, binary --------------------------------------------------

    function unpack(buffer) {
        const view = new DataView(buffer);
        const magic = String.fromCharCode(view.getUint8(0), view.getUint8(1), view.getUint8(2), view.getUint8(3));
        if (magic !== "PXB1") throw new Error("Unexpected answer from the server.");
        const length = view.getUint32(4, true);
        const meta = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, 8, length)));
        const start = 8 + length;
        const arrays = {};
        const types = { float32: Float32Array, uint16: Uint16Array, uint32: Uint32Array, uint8: Uint8Array, int32: Int32Array };
        (meta.arrays || []).forEach((a) => {
            const Type = types[a.dtype];
            arrays[a.name] = Type ? new Type(buffer, start + a.offset, a.length) : null;
        });
        return { meta, arrays };
    }

    class Data {
        constructor(base, notebookId) {
            this.base = base.replace(/\/$/, "");
            this.nb = "/api/notebooks/" + encodeURIComponent(notebookId);
        }

        async json(path, init) {
            const response = await fetch(this.base + path, init);
            let body = null;
            try { body = await response.json(); } catch (err) { body = null; }
            if (!response.ok || !body || body.success === false) {
                throw new Error((body && body.message) || ("The server answered " + response.status));
            }
            return body;
        }

        async bin(path) {
            const response = await fetch(this.base + path);
            if (!response.ok) {
                let message = "The server answered " + response.status;
                try { message = (await response.json()).message || message; } catch (err) { /* keep */ }
                throw new Error(message);
            }
            return unpack(await response.arrayBuffer());
        }

        cached(key, stamp, load) {
            const hit = dataCache.get(key);
            if (hit && hit.stamp === stamp) return hit.promise;
            const promise = load().catch((err) => { dataCache.delete(key); throw err; });
            dataCache.set(key, { stamp, promise });
            if (dataCache.size > 24) dataCache.delete(dataCache.keys().next().value);
            return promise;
        }

        post(path, body) {
            return this.json(this.nb + path, { method: "POST", headers: { "Content-Type": "application/json" },
                                               body: JSON.stringify(body || {}) });
        }

        info() { return this.json(this.nb + "/embedding").then((a) => a.embedding); }
        coords(key, stamp) {
            return this.cached(this.nb + "|xy|" + key, stamp, () => this.bin(this.nb + "/embedding/coords?key=" + encodeURIComponent(key)));
        }
        identity(stamp) {
            return this.cached(this.nb + "|id", stamp, () => this.bin(this.nb + "/embedding/identity"));
        }
        color(column, stamp) {
            return this.cached(this.nb + "|c|" + column, stamp,
                () => this.bin(this.nb + "/embedding/color?column=" + encodeURIComponent(column)));
        }
        label(body) { return this.post("/labels", body).then((a) => a.labels); }
        undo() { return this.post("/labels/undo").then((a) => a.labels); }
        overlay(column) { return this.post("/overlay", { column }); }
        saveSelection(body) { return this.post("/selections", body).then((a) => a.saved); }
    }

    function bitmaskBase64(rows) {
        const bytes = new Uint8Array((rows.length + 7) >> 3);
        for (let i = 0; i < rows.length; i += 1) if (rows[i]) bytes[i >> 3] |= 1 << (i & 7);
        let text = "";
        for (let i = 0; i < bytes.length; i += 0x8000) {
            text += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
        }
        return btoa(text);
    }

    const TOOLS = [
        ["navigate", "Navigate", "V", "drag to pan, scroll to zoom"],
        ["lasso", "Lasso", "L", "hold ⇧ to add, " + ALT + " to take away"],
        ["polygon", "Polygon", "P", "click points; Enter or the first point closes"],
        ["brush", "Brush", "B", "paint cells in; [ and ] resize"],
        ["eraser", "Eraser", "E", "paint cells out; [ and ] resize"],
    ];

    // -- the controller ------------------------------------------------------

    class Embed {
        constructor(options) {
            this.o = Object.assign({ theme: "light", base: defaultBase() }, options || {});
            this.data = new Data(this.o.base, this.o.notebookId);
            this.glass = this.o.theme === "glass";
            this.settings = readSettings();
            this.state = { info: null, key: this.o.key || null, column: this.o.color || null, error: null, loading: true };
            this.rows = null;
            this.region = null;
            this.lassos = [];
            this.tool = TOOLS.some((t) => t[0] === this.settings.tool) ? this.settings.tool : "lasso";
            this.op = "new";
            this.hidden = new Map();
            this.live = { press: null, poly: null, hover: null };
            this.ownRev = -1;
            this.labelState = { text: "", column: null, newName: "", overwrite: false, busy: false };
            // In the notebook the Annotate panel lives in the plot sidebar
            // (scimapInspector.js), which takes it with attachPanel().
            this.external = Boolean(this.o.onAnnotate) && !this.glass;
            this.panelOpen = !this.glass && !this.external && this.settings.panel !== false;
            this.root = h("div.sm-emb" + (this.glass ? ".is-glass" : ".is-light"));
            this.onBus = (e) => this.fromBus(e && e.detail);
            this.onMode = () => this.applyStyle();
            this.onRegion = () => this.applyRegion();
            window.addEventListener("plexora:cells-selected", this.onBus);
            window.addEventListener("plexora:cells-selection-mode", this.onMode);
            window.addEventListener("plexora:cells-region", this.onRegion);
            this.build();
            this.load();
        }

        bus() { return window.PlexoraCellSelection || null; }

        saveSettings(patch) {
            Object.assign(this.settings, patch);
            writeSettings(this.settings);
        }

        // -- skeleton ---------------------------------------------------------

        iconButton(name, label, onclick, extra) {
            const button = h("button.sm-emb-ib" + (extra || ""), { type: "button", "aria-label": label, "data-tip": label,
                onclick }, icon(name));
            return button;
        }

        build() {
            const P = window.ScimapPicker;
            this.canvas = h("canvas.sm-emb-gl", { "aria-label": "Embedding: every cell, colored", role: "img" });
            this.overlay = h("canvas.sm-emb-overlay", { "aria-hidden": "true" });
            this.regionChip = h("div.sm-emb-region", { hidden: true });
            this.message = h("div.sm-emb-message", null, "Reading the embedding…");
            this.legend = h("div.sm-emb-legend", { hidden: true });
            this.buildRail();
            this.buildZoom();
            this.plot = h("div.sm-emb-plot", { tabindex: "0", "aria-label": "Embedding plot" }, this.canvas, this.overlay,
                this.rail, this.zoom, this.legend, this.regionChip, this.message);
            this.tipNode = h("div.sm-emb-tip", { role: "tooltip", hidden: true });
            this.toast = h("div.sm-emb-toast", { hidden: true, role: "status" });

            const pickerTheme = this.glass ? "glass" : "light";
            this.colorPicker = P.create({ theme: pickerTheme, ariaLabel: "Color by", title: "Color by",
                className: "sm-emb-pick is-color", searchPlaceholder: "Search columns and markers", accordion: true,
                emptyText: "No column matches", menuWidth: 260, icon: icon("layers", 14),
                onChange: (value) => this.setColumn(value) });
            this.keyPicker = P.create({ theme: pickerTheme, ariaLabel: "Embedding", title: "Embedding",
                className: "sm-emb-pick is-key", searchPlaceholder: "Search embeddings", menuWidth: 200,
                icon: icon("embed", 14), onChange: (value) => this.setKey(value) });
            this.colorSelect = this.colorPicker.root;
            this.keySelect = this.keyPicker.root;
            this.modeSeg = h("div.sm-emb-seg", { role: "radiogroup", "aria-label": "How the selection shows" });

            if (this.glass) {
                this.countNode = h("div.sm-emb-count");
                this.annotateButton = h("button.sm-emb-annotate", { type: "button", "aria-haspopup": "menu",
                    onclick: (e) => this.annotateMenu(e.currentTarget) }, icon("tag", 13), h("span.sm-emb-annotate-text", null, "Annotate"),
                    h("span.sm-emb-annotate-rule"), icon("chevD", 11));
                this.clearButton = this.iconButton("x", "Clear the selection", () => this.clear(), ".is-clear");
                this.foot = h("div.sm-emb-foot", null, h("span.sm-emb-accent"), this.countNode, h("span.sm-emb-spacer"),
                    this.modeSeg, this.annotateButton, this.clearButton);
                this.root.append(this.plot, this.foot, this.toast, this.tipNode);
            } else {
                this.selPill = h("div.sm-emb-pill", { hidden: true });
                this.plot.append(this.selPill);
                this.panel = h("aside.sm-emb-panel", { "aria-label": "Annotate" });
                this.panelToggle = this.iconButton("tag", this.external ? "Annotate, in the sidebar (A)" : "Annotate panel (A)",
                    () => (this.external ? this.o.onAnnotate() : this.togglePanel()), ".is-box");
                this.fullButton = this.iconButton("full", "Fullscreen (F)", () => this.toggleFull(), ".is-box");
                const help = this.iconButton("help", "How to select", (e) => this.showHelp(e.currentTarget));
                const openViewer = this.o.onOpenInViewer ? h("button.sm-emb-btn", { type: "button",
                    "data-tip": "This embedding in a window over the tissue; a selection there shows on the image",
                    onclick: () => this.o.onOpenInViewer(this.handoff()) }, icon("external", 14), "Open in viewer") : null;
                this.downloadButton = this.iconButton("download", "Download the figure", (e) => this.o.download && this.o.download(e.currentTarget), ".is-box");
                this.keyField = h("span.sm-emb-keyfield", { hidden: true }, this.keySelect);
                // Beside the notebook's plot sidebar (external), Color by, how
                // the selection shows, Annotate and Download are the sidebar's;
                // the bar keeps only what the sidebar has not.
                this.bar = this.external
                    ? h("div.sm-emb-bar.is-lean", null, this.keyField, help, h("span.sm-emb-spacer"), openViewer, this.fullButton)
                    : h("div.sm-emb-bar", null, h("span.sm-emb-bar-label", null, "Color by"), this.colorSelect,
                        this.keyField, this.modeSeg, help, h("span.sm-emb-spacer"), openViewer, this.panelToggle,
                        this.o.download ? this.downloadButton : null, this.fullButton);
                this.body = h("div.sm-emb-body" + (this.panelOpen ? "" : ".is-closed"), null,
                    h("div.sm-emb-stage", null, this.plot), this.external ? null : h("div.sm-emb-panel-wrap", null, this.panel));
                append(this.root, this.bar, this.body, this.toast, this.tipNode);
                this.paintPanelToggle();
            }
            this.paintMode();
            this.paintTools();
            this.wirePlot();
            this.wireKeys();
            this.wireTips();
            this.wireLegend();
            if (typeof ResizeObserver === "function") {
                this.sizes = new ResizeObserver(() => this.resize());
                this.sizes.observe(this.plot);
            }
        }

        buildRail() {
            this.toolButtons = {};
            this.rail = h("div.sm-emb-rail", { role: "toolbar", "aria-label": "Selection tools", "aria-orientation": "vertical" });
            TOOLS.forEach(([id, name, key, how]) => {
                const b = h("button.sm-emb-tool.is-pick", { type: "button", "aria-label": name + " (" + key + ")",
                    "data-tip": name + "|" + key + "|" + how, "aria-pressed": "false",
                    onclick: () => { this.setTool(id); this.rail.classList.remove("is-open"); } }, icon(id));
                this.toolButtons[id] = b;
            });
            this.moreButton = h("button.sm-emb-tool.is-more", { type: "button", "aria-label": "More tools",
                "data-tip": "More tools", onclick: () => this.rail.classList.toggle("is-open") }, icon("more"));
            this.addButton = h("button.sm-emb-tool.is-op", { type: "button", "aria-label": "Add to the selection",
                "data-tip": "Add to the selection|⇧|or hold Shift while drawing", "aria-pressed": "false",
                onclick: () => this.setOp(this.op === "add" ? "new" : "add") }, icon("add"));
            this.subButton = h("button.sm-emb-tool.is-op", { type: "button", "aria-label": "Take from the selection",
                "data-tip": "Take from the selection|" + ALT + "|or hold " + ALT + " while drawing", "aria-pressed": "false",
                onclick: () => this.setOp(this.op === "sub" ? "new" : "sub") }, icon("sub"));
            this.undoButton = h("button.sm-emb-tool", { type: "button", "aria-label": "Undo", "data-tip": "Undo|" + MOD + "Z",
                onclick: () => this.undo() }, icon("undo"));
            this.redoButton = h("button.sm-emb-tool", { type: "button", "aria-label": "Redo", "data-tip": "Redo|⇧" + MOD + "Z",
                onclick: () => this.redo() }, icon("redo"));
            this.railClear = h("button.sm-emb-tool.is-clear", { type: "button", "aria-label": "Clear the selection",
                "data-tip": "Clear the selection", onclick: () => this.clear() }, icon("clear"));
            append(this.rail, TOOLS.map((t) => this.toolButtons[t[0]]), this.moreButton, h("span.sm-emb-rule"),
                this.addButton, this.subButton, h("span.sm-emb-rule"), this.undoButton, this.redoButton, this.railClear);
        }

        buildZoom() {
            const z = (name, label, fn) => h("button.sm-emb-zoom", { type: "button", "aria-label": label, "data-tip": label,
                onclick: fn }, icon(name, 14));
            this.sizeButton = z("dots", "Point size", (e) => this.showSize(e.currentTarget));
            this.zoom = h("div.sm-emb-zooms", null,
                z("plus", "Zoom in|+", () => this.gl && this.gl.zoomBy(1.5)),
                z("minus", "Zoom out|−", () => this.gl && this.gl.zoomBy(1 / 1.5)),
                z("fit", "Fit|0|or double-click", () => this.gl && this.gl.fit(true)),
                this.sizeButton);
        }

        // -- tools, ops, modes ------------------------------------------------

        setTool(id) {
            if (this.live.poly) this.cancelLive();
            this.tool = id;
            this.saveSettings({ tool: id });
            this.paintTools();
            this.paintOverlay();
        }

        setOp(op) {
            this.op = op;
            this.paintTools();
        }

        paintTools() {
            Object.entries(this.toolButtons).forEach(([id, b]) => {
                b.setAttribute("aria-pressed", String(this.tool === id));
                b.classList.toggle("is-on", this.tool === id);
            });
            this.addButton.classList.toggle("is-on", this.op === "add");
            this.subButton.classList.toggle("is-on", this.op === "sub");
            this.addButton.setAttribute("aria-pressed", String(this.op === "add"));
            this.subButton.setAttribute("aria-pressed", String(this.op === "sub"));
            this.plot.dataset.tool = this.tool;
            this.paintHistory();
        }

        paintHistory() {
            const bus = this.bus();
            this.undoButton.disabled = !(bus && bus.canUndo && bus.canUndo());
            this.redoButton.disabled = !(bus && bus.canRedo && bus.canRedo());
            const k = this.selected || 0;
            this.railClear.disabled = !k;
            if (this.clearButton) this.clearButton.disabled = !k;
        }

        paintMode() {
            const mode = this.bus() ? this.bus().mode() : "dim";
            this.modeSeg.textContent = "";
            const labels = [["highlight", "Highlight", "H"], ["dim", "Dim others", "D"], ["only", "Only", "O"]];
            labels.forEach(([id, text, short], i) => this.modeSeg.append(h("button", { type: "button", role: "radio",
                "aria-checked": String(mode === id), "data-tip": i === 0 ? "Selected cells drawn over the rest" :
                    i === 1 ? "Everything else darkened" : "Only the selected cells",
                onclick: () => { if (this.bus()) this.bus().setMode(id); } },
            i === 0 ? icon("eye", 13) : null, h("span.sm-emb-seg-long", null, text), h("span.sm-emb-seg-short", null, short))));
        }

        // -- loading ----------------------------------------------------------

        async load() {
            try {
                const info = await this.data.info();
                this.state.info = info;
                if (!info.embeddings.length) throw new Error("This notebook has no embedding yet. Add a UMAP step and run it.");
                if (!this.state.key || !info.embeddings.some((e) => e.key === this.state.key)) {
                    this.state.key = info.embeddings[0].key;
                }
                const all = this.columns();
                if (!this.state.column || !all.includes(this.state.column)) {
                    // A step's result (a clustering) before a column made by
                    // labelling: the first is what an embedding is read by.
                    const made = new Set(info.label_columns || []);
                    const steps = info.columns.labels.filter((c) => !made.has(c));
                    this.state.column = steps[steps.length - 1] || info.columns.labels[0] || info.columns.metadata[0] || all[0] || null;
                }
                this.paintSelects();
                const [xy, identity] = await Promise.all([
                    this.data.coords(this.state.key, info.stamp), this.data.identity(info.stamp)]);
                this.identity = { projects: identity.meta.projects, codes: identity.arrays.codes, labels: identity.arrays.labels };
                this.totals = new Uint32Array(this.identity.projects.length);
                for (let i = 0; i < this.identity.codes.length; i += 1) {
                    const c = this.identity.codes[i];
                    if (c < this.totals.length) this.totals[c] += 1;
                }
                if (!this.gl) {
                    this.gl = window.ScimapScatterGL.create(this.canvas);
                    this.gl.onView = () => this.paintOverlay();
                }
                this.gl.setPoints(xy.arrays.xy, xy.meta.extent);
                this.gl.setStyle({ scale: this.settings.pointScale });
                this.rows = new Uint8Array(this.gl.n);
                this.applyStyle();
                await this.applyColor();
                this.state.loading = false;
                this.message.hidden = true;
                if (this.bus()) await this.bus().ready();
                this.fromBus(null, true);
                this.applyRegion();
                this.paintAll();
            } catch (err) {
                this.state.error = err.message;
                this.message.textContent = err.message;
                this.message.hidden = false;
                this.message.classList.add("is-error");
            }
        }

        columns() {
            const c = (this.state.info && this.state.info.columns) || {};
            return [].concat(c.labels || [], c.metadata || [], c.markers || [], c.numbers || []);
        }

        paintSelects() {
            const info = this.state.info;
            const c = info.columns;
            const made = new Set(info.label_columns || []);
            const groups = [
                { title: "Labels", items: (c.labels || []).map((v) => ({ value: v, label: v, mono: true, hint: made.has(v) ? "labels" : "" })) },
                // Numbers (areas, scores) sit with the samples and metadata.
                { title: "Samples and metadata", items: (c.metadata || []).map((v) => ({ value: v, label: v, mono: true }))
                    .concat((c.numbers || []).map((v) => ({ value: v, label: v, mono: true, ramp: RAMP }))) },
                { title: "Markers", items: (c.markers || []).map((v) => ({ value: v, label: v, mono: true, ramp: RAMP })) },
            ];
            this.colorPicker.setGroups(groups);
            this.lastGroups = groups;
            this.colorPicker.setValue(this.state.column);
            this.keyPicker.setGroups([{ title: null, items: info.embeddings.map((e) => ({ value: e.key, label: e.label })) }]);
            this.keyPicker.setValue(this.state.key);
            if (this.keyField) this.keyField.hidden = info.embeddings.length < 2;
        }

        async setKey(key) {
            if (key === this.state.key || !this.gl) return;
            this.state.key = key;
            this.lassos = [];
            const xy = await this.data.coords(key, this.state.info.stamp);
            this.gl.setPoints(xy.arrays.xy, xy.meta.extent);
            this.gl.setFlags(this.flags(), this.count1() > 0);
            await this.applyColor();
            this.paintOverlay();
            if (this.o.onKey) this.o.onKey(key);
        }

        async setColumn(column) {
            if (!this.gl) return;
            this.state.column = column;
            this.colorPicker.setValue(column);
            await this.applyColor();
            this.paintAll();
            if (this.o.onColor) this.o.onColor(column, this.colorMeta);
        }

        async applyColor() {
            const column = this.state.column;
            this.colorMeta = null;
            this.colorValues = null;
            if (column) {
                const got = await this.data.color(column, this.state.info.stamp);
                this.colorMeta = got.meta;
                this.colorValues = got.arrays.values;
            }
            this.paintColors();
            this.paintLegend();
        }

        hiddenSet() {
            const key = this.state.column || "";
            if (!this.hidden.has(key)) this.hidden.set(key, new Set());
            return this.hidden.get(key);
        }

        /** Colours from the column, with hidden groups not drawn. */
        paintColors() {
            if (!this.gl) return;
            const n = this.gl.n;
            const rgba = new Uint8Array(n * 4);
            const meta = this.colorMeta;
            const values = this.colorValues;
            const alpha = this.glass ? 230 : 215;
            const none = this.glass ? [120, 130, 146] : [201, 206, 216];
            let shown = null;
            if (meta && meta.kind === "category") {
                const table = meta.colors.map(hex);
                const hidden = this.hiddenSet();
                const off = new Uint8Array(65536);
                hidden.forEach((code) => { off[code] = 1; });
                if (hidden.size) shown = new Uint8Array(n);
                for (let i = 0, o = 0; i < n; i += 1, o += 4) {
                    const code = values[i];
                    if (off[code]) continue;
                    if (shown) shown[i] = 1;
                    const c = code === MISSING ? none : table[code] || none;
                    rgba[o] = c[0]; rgba[o + 1] = c[1]; rgba[o + 2] = c[2];
                    rgba[o + 3] = code === MISSING ? 110 : alpha;
                }
            } else if (meta) {
                const [lo, hi] = meta.range;
                const span = hi > lo ? hi - lo : 1;
                for (let i = 0, o = 0; i < n; i += 1, o += 4) {
                    const v = values[i];
                    if (!(v === v)) { rgba[o] = none[0]; rgba[o + 1] = none[1]; rgba[o + 2] = none[2]; rgba[o + 3] = 90; continue; }
                    const k = Math.max(0, Math.min(255, Math.round(((v - lo) / span) * 255))) * 3;
                    rgba[o] = RAMP_LUT[k]; rgba[o + 1] = RAMP_LUT[k + 1]; rgba[o + 2] = RAMP_LUT[k + 2]; rgba[o + 3] = alpha;
                }
            } else {
                for (let o = 0; o < n * 4; o += 4) { rgba[o] = 78; rgba[o + 1] = 121; rgba[o + 2] = 167; rgba[o + 3] = alpha; }
            }
            this.gl.setShown(shown);
            this.gl.setColors(rgba);
        }

        applyStyle() {
            this.paintMode();
            if (!this.gl) return;
            const mode = this.bus() ? this.bus().mode() : "dim";
            if (!this.glass) {
                const ink = [43, 50, 75, 245];
                if (mode === "highlight") this.gl.setStyle({ sel: ink, rest: 1, selOwn: false });
                else if (mode === "only") this.gl.setStyle({ sel: ink, rest: 0.04, selOwn: true });
                else this.gl.setStyle({ sel: ink, rest: 0.22, selOwn: false });
            } else {
                const white = [255, 255, 255, 255];
                if (mode === "highlight") this.gl.setStyle({ sel: white, rest: 1, selOwn: false });
                else if (mode === "only") this.gl.setStyle({ sel: white, rest: 0.06, selOwn: true });
                else this.gl.setStyle({ sel: white, rest: 0.22, selOwn: false });
            }
        }

        // -- the selection: rows here, project + mask label in core ------------

        count1() {
            if (!this.rows) return 0;
            let k = 0;
            for (let i = 0; i < this.rows.length; i += 1) k += this.rows[i];
            return k;
        }

        flags() {
            const out = new Uint8Array(this.rows.length);
            for (let i = 0; i < out.length; i += 1) out[i] = this.rows[i] | (this.region && this.region[i] ? 2 : 0);
            return out;
        }

        /** Rows of this file whose (project, label) is in `byProject`. */
        rowsFor(byProject) {
            const { projects, codes, labels } = this.identity;
            const out = new Uint8Array(codes.length);
            projects.forEach((name, index) => {
                const ids = byProject[name];
                if (!ids || !ids.length) return;
                const max = ids[ids.length - 1];
                if (max < 64 * 1024 * 1024) {
                    const mark = new Uint8Array(max + 1);
                    for (let i = 0; i < ids.length; i += 1) mark[ids[i]] = 1;
                    for (let i = 0; i < codes.length; i += 1) {
                        if (codes[i] === index && labels[i] <= max && mark[labels[i]]) out[i] = 1;
                    }
                } else {
                    const set = new Set(ids);
                    for (let i = 0; i < codes.length; i += 1) if (codes[i] === index && set.has(labels[i])) out[i] = 1;
                }
            });
            return out;
        }

        fromBus(detail, initial) {
            const bus = this.bus();
            if (!bus || !this.identity || !this.rows) { this.paintHistory(); return; }
            // Our own write: core announces it before `set` returns.
            if (this.publishing || (detail && detail.rev === this.ownRev)) { this.paintHistory(); return; }
            const sel = bus.current();
            const mine = this.identity.projects;
            if (!sel || !(sel.scope || []).some((p) => mine.includes(p))) {
                if (!initial || sel) this.rows.fill(0);
            } else {
                this.rows = this.rowsFor(sel.projects || {});
            }
            if (!initial) this.lassos = [];
            this.gl.setFlags(this.flags(), this.count1() > 0);
            this.paintAll();
        }

        publish() {
            const bus = this.bus();
            if (!bus) return;
            const { projects, codes, labels } = this.identity;
            const counts = new Uint32Array(projects.length);
            let total = 0;
            for (let i = 0; i < this.rows.length; i += 1) {
                if (this.rows[i] && codes[i] < projects.length && labels[i]) { counts[codes[i]] += 1; total += 1; }
            }
            this.publishing = true;
            try {
                if (!total) {
                    // Nothing left selected: no selection, so the tissue is as it was.
                    this.ownRev = bus.clear({ source: "scimap:" + this.o.notebookId });
                    return;
                }
                const arrays = projects.map((_, k) => new Uint32Array(counts[k]));
                const fill = new Uint32Array(projects.length);
                for (let i = 0; i < this.rows.length; i += 1) {
                    const c = codes[i];
                    if (this.rows[i] && c < projects.length && labels[i]) arrays[c][fill[c]++] = labels[i];
                }
                const byProject = {};
                projects.forEach((name, k) => { byProject[name] = arrays[k]; });
                this.ownRev = bus.set(byProject, { scope: projects, source: "scimap:" + this.o.notebookId,
                                                   label: (this.state.info && this.state.info.title) || "Embedding" });
            } finally {
                this.publishing = false;
                this.paintHistory();
            }
        }

        clear() {
            if (!this.rows || !this.selected) return;
            this.cancelLive();
            const bus = this.bus();
            if (bus) bus.clear({ source: "scimap:" + this.o.notebookId });
            else {
                this.rows.fill(0);
                this.gl.setFlags(this.flags(), false);
                this.paintAll();
            }
        }

        undo() {
            this.cancelLive();
            if (this.bus() && this.bus().undo) this.bus().undo();
        }

        redo() {
            this.cancelLive();
            if (this.bus() && this.bus().redo) this.bus().redo();
        }

        /** Apply what a tool caught: replace, add or take away. */
        combine(inside, op, outline) {
            let caught = 0;
            for (let i = 0; i < inside.length; i += 1) caught += inside[i];
            if (op === "new" && !caught) {
                // A shape over empty space never wipes a selection.
                this.paintOverlay();
                if (this.selected) this.flash("No cells inside; the selection is unchanged.", null, 2500, true);
                return;
            }
            if (op === "new") {
                this.rows = inside;
                this.lassos = outline ? [{ op: "add", poly: outline }] : [];
            } else if (op === "add") {
                for (let i = 0; i < inside.length; i += 1) if (inside[i]) this.rows[i] = 1;
                if (outline) this.lassos.push({ op, poly: outline });
            } else {
                for (let i = 0; i < inside.length; i += 1) if (inside[i]) this.rows[i] = 0;
                if (outline) this.lassos.push({ op, poly: outline });
            }
            if (this.lassos.length > 24) this.lassos.shift();
            this.gl.setFlags(this.flags(), this.count1() > 0);
            this.publish();
            this.paintAll();
        }

        /** Select (or add) every cell of a legend group. */
        selectGroup(code, add) {
            if (!this.colorValues) return;
            const inside = new Uint8Array(this.rows.length);
            const values = this.colorValues;
            for (let i = 0; i < inside.length; i += 1) if (values[i] === code) inside[i] = 1;
            this.combine(inside, add ? "add" : "new", null);
        }

        applyRegion() {
            const bus = this.bus();
            const region = bus ? bus.region() : null;
            if (!this.identity || !this.gl) return;
            if (region && this.identity.projects.includes(region.project)) {
                this.region = this.rowsFor({ [region.project]: region.ids });
                let k = 0;
                for (let i = 0; i < this.region.length; i += 1) k += this.region[i];
                this.regionInfo = { name: region.name, count: k, project: region.project, ids: region.ids };
            } else {
                this.region = null;
                this.regionInfo = null;
            }
            this.gl.setFlags(this.flags(), this.count1() > 0);
            this.paintRegion();
        }

        // -- pointer: tools, pan, zoom -----------------------------------------

        local(e) {
            const r = this.overlay.getBoundingClientRect();
            return [e.clientX - r.left, e.clientY - r.top];
        }

        opFor(e) {
            if (this.tool === "eraser") return "sub";
            if (e && e.altKey) return "sub";
            if (e && e.shiftKey) return "add";
            if (this.tool === "brush") return this.op === "sub" ? "sub" : "add";
            return this.op;
        }

        cancelLive() {
            this.live.press = null;
            this.live.poly = null;
            this.paintOverlay();
        }

        closePolygon(e) {
            const poly = this.live.poly;
            this.live.poly = null;
            if (!poly || poly.pts.length < 3) { this.paintOverlay(); return; }
            const screen = poly.pts.map(([x, y]) => this.gl.toScreen(x, y));
            const op = e && (e.altKey || e.shiftKey) ? this.opFor(e) : poly.op;
            this.combine(this.gl.inside(screen), op, poly.pts);
        }

        wirePlot() {
            let space = false;
            this.plot.addEventListener("keydown", (e) => { if (e.code === "Space") { space = true; e.preventDefault(); } });
            this.plot.addEventListener("keyup", (e) => { if (e.code === "Space") space = false; });
            this.plot.addEventListener("contextmenu", (e) => e.preventDefault());
            this.plot.addEventListener("pointerdown", (e) => {
                if (!this.gl || this.state.loading) return;
                if (e.target.closest("button, a, select, input, .sm-emb-legend, .sm-emb-pill, .sm-emb-region")) return;
                this.plot.focus({ preventScroll: true });
                this.hideTip();
                const pan = e.button === 1 || e.button === 2 || space || (this.tool === "navigate" && e.button === 0);
                if (!pan && e.button !== 0) return;
                e.preventDefault();
                const p = this.local(e);
                if (!pan && this.tool === "polygon") {
                    const poly = this.live.poly;
                    if (poly && poly.pts.length >= 3) {
                        const first = this.gl.toScreen(poly.pts[0][0], poly.pts[0][1]);
                        if (Math.hypot(first[0] - p[0], first[1] - p[1]) < 9) { this.closePolygon(e); return; }
                    }
                    if (!poly) this.live.poly = { op: this.opFor(e), pts: [] };
                    this.live.poly.pts.push(this.gl.toData(p[0], p[1]));
                    this.paintOverlay();
                    return;
                }
                this.plot.setPointerCapture(e.pointerId);
                this.live.press = { pan, op: this.opFor(e), last: p, path: [p], tool: this.tool };
                this.plot.classList.toggle("is-panning", pan);
                if (!pan && (this.tool === "brush" || this.tool === "eraser")) this.paintOverlay();
            });
            this.plot.addEventListener("pointermove", (e) => {
                const p = this.local(e);
                this.live.hover = p;
                const press = this.live.press;
                if (!press) {
                    if (this.tool === "brush" || this.tool === "eraser" || this.live.poly) this.paintOverlay();
                    return;
                }
                if (press.pan) {
                    this.gl.panBy(p[0] - press.last[0], p[1] - press.last[1]);
                    press.last = p;
                    return;
                }
                const last = press.path[press.path.length - 1];
                if (Math.hypot(p[0] - last[0], p[1] - last[1]) < 2) return;
                press.path.push(p);
                this.paintOverlay();
            });
            this.plot.addEventListener("pointerleave", () => {
                this.live.hover = null;
                if (!this.live.press) this.paintOverlay();
            });
            const up = () => {
                const done = this.live.press;
                if (!done) return;
                this.live.press = null;
                this.plot.classList.remove("is-panning");
                if (done.pan) { this.paintOverlay(); return; }
                if (done.tool === "brush" || done.tool === "eraser") {
                    this.combine(this.gl.stroke(done.path, this.settings.brush), done.op, null);
                    return;
                }
                // A click, or a scribble too small to be a shape: never a change.
                if (done.path.length < 4) { this.paintOverlay(); return; }
                const polygon = done.path.map(([x, y]) => this.gl.toData(x, y));
                this.combine(this.gl.inside(done.path), done.op, polygon);
            };
            this.plot.addEventListener("pointerup", up);
            this.plot.addEventListener("pointercancel", () => this.cancelLive());
            this.plot.addEventListener("wheel", (e) => {
                if (!this.gl) return;
                e.preventDefault();
                const p = this.local(e);
                this.gl.zoomAt(p[0], p[1], Math.exp(-e.deltaY * 0.0015));
            }, { passive: false });
            this.plot.addEventListener("dblclick", (e) => {
                if (!this.gl || e.target.closest("button, .sm-emb-legend, .sm-emb-pill")) return;
                if (this.tool === "polygon") { this.closePolygon(e); return; }
                this.gl.fit(true);
            });
        }

        wireKeys() {
            this.root.addEventListener("keydown", (e) => {
                if (e.target.closest("input, textarea, select, [contenteditable]")) return;
                const mod = e.metaKey || e.ctrlKey;
                const key = e.key;
                if (mod && (key === "z" || key === "Z")) {
                    e.preventDefault();
                    if (e.shiftKey) this.redo(); else this.undo();
                    return;
                }
                if (mod && (key === "y" || key === "Y")) { e.preventDefault(); this.redo(); return; }
                if (mod || e.altKey) return;
                if (key === "Escape") {
                    // Cancels what is in hand; never clears the selection.
                    if (this.pop) { this.closePop(); e.preventDefault(); return; }
                    if (this.live.press || this.live.poly) { this.cancelLive(); e.preventDefault(); return; }
                    if (this.rail.classList.contains("is-open")) { this.rail.classList.remove("is-open"); return; }
                    if (this.isFull()) { this.toggleFull(false); e.preventDefault(); }
                    return;
                }
                if (key === "Enter" && this.live.poly) { e.preventDefault(); this.closePolygon(e); return; }
                if (key === "Backspace" && this.live.poly) {
                    e.preventDefault();
                    this.live.poly.pts.pop();
                    if (!this.live.poly.pts.length) this.live.poly = null;
                    this.paintOverlay();
                    return;
                }
                const tool = { v: "navigate", l: "lasso", p: "polygon", b: "brush", e: "eraser" }[key.toLowerCase()];
                if (tool && !e.shiftKey) { this.setTool(tool); e.preventDefault(); return; }
                if (key === "[" || key === "]") {
                    const brush = Math.max(4, Math.min(80, this.settings.brush + (key === "]" ? 4 : -4)));
                    this.saveSettings({ brush });
                    this.paintOverlay();
                    return;
                }
                if ((key === "+" || key === "=") && this.gl) { this.gl.zoomBy(1.5); return; }
                if ((key === "-" || key === "_") && this.gl) { this.gl.zoomBy(1 / 1.5); return; }
                if (key === "0" && this.gl) { this.gl.fit(true); return; }
                if (!this.glass && (key === "a" || key === "A")) {
                    if (this.external) this.o.onAnnotate(); else this.togglePanel();
                    return;
                }
                if (!this.glass && (key === "f" || key === "F")) { this.toggleFull(); }
            });
        }

        // -- tooltips: name, shortcut, how ---------------------------------------

        wireTips() {
            let timer = 0;
            this.root.addEventListener("pointerover", (e) => {
                const target = e.target.closest("[data-tip]");
                if (!target || !this.root.contains(target)) return;
                clearTimeout(timer);
                timer = setTimeout(() => this.showTip(target), this.tipNode.hidden ? 380 : 40);
            });
            this.root.addEventListener("pointerout", (e) => {
                const target = e.target.closest("[data-tip]");
                if (!target || (e.relatedTarget && target.contains(e.relatedTarget))) return;
                clearTimeout(timer);
                timer = setTimeout(() => this.hideTip(), 80);
            });
            this.root.addEventListener("pointerdown", () => { clearTimeout(timer); this.hideTip(); }, true);
            this.root.addEventListener("focusin", (e) => {
                if (e.target.matches && e.target.matches("[data-tip]:focus-visible")) this.showTip(e.target);
            });
            this.root.addEventListener("focusout", () => this.hideTip());
        }

        showTip(target) {
            if (!target.isConnected || target.closest(".sm-emb-rail.is-dragging")) return;
            const [name, key, how] = String(target.dataset.tip).split("|");
            const tip = this.tipNode;
            tip.textContent = "";
            append(tip, h("span", null, name), key ? h("kbd", null, key) : null, how ? h("span.sm-emb-tip-how", null, " · " + how) : null);
            tip.hidden = false;
            const r = target.getBoundingClientRect();
            const box = this.root.getBoundingClientRect();
            const t = tip.getBoundingClientRect();
            const inRail = target.closest(".sm-emb-rail");
            let left;
            let top;
            if (inRail) {
                left = r.right - box.left + 8;
                top = r.top - box.top + (r.height - t.height) / 2;
            } else {
                left = r.left - box.left + (r.width - t.width) / 2;
                top = r.bottom - box.top + 6;
                if (top + t.height > box.height - 4) top = r.top - box.top - t.height - 6;
            }
            tip.style.left = Math.max(4, Math.min(box.width - t.width - 4, left)) + "px";
            tip.style.top = Math.max(4, top) + "px";
        }

        hideTip() {
            this.tipNode.hidden = true;
        }

        // -- the overlay: kept outlines, the shape in hand, the brush ring ------

        paintOverlay() {
            const canvas = this.overlay;
            const dpr = window.devicePixelRatio || 1;
            const w = canvas.clientWidth;
            const hgt = canvas.clientHeight;
            if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(hgt * dpr)) {
                canvas.width = Math.round(w * dpr);
                canvas.height = Math.round(hgt * dpr);
            }
            const g = canvas.getContext("2d");
            g.setTransform(dpr, 0, 0, dpr, 0, 0);
            g.clearRect(0, 0, w, hgt);
            if (!this.gl || this.state.loading) return;
            const ink = this.glass ? "#ffffff" : "#2b324b";
            const accent = this.glass ? "#38bdf8" : "#0284c7";
            const red = this.glass ? "#ff8a80" : "#b42318";
            const path = (points, close) => {
                g.beginPath();
                g.moveTo(points[0][0], points[0][1]);
                points.slice(1).forEach(([x, y]) => g.lineTo(x, y));
                if (close) g.closePath();
            };
            const shape = (points, stroke, fill, dash) => {
                if (points.length < 2) return;
                path(points, true);
                g.setLineDash(dash);
                g.lineWidth = 1.4;
                g.strokeStyle = stroke;
                g.fillStyle = fill;
                g.fill();
                g.stroke();
            };
            this.lassos.forEach((l) => {
                const pts = l.poly.map(([x, y]) => this.gl.toScreen(x, y));
                if (l.op === "sub") shape(pts, red, "rgba(255,255,255,0.12)", [3, 3]);
                else shape(pts, ink, this.glass ? "rgba(255,255,255,0.04)" : "rgba(43,50,75,0.05)", [5, 4]);
            });
            const press = this.live.press;
            const brushing = this.tool === "brush" || this.tool === "eraser";
            if (press && !press.pan && press.path.length) {
                const sub = press.op === "sub";
                if (press.tool === "brush" || press.tool === "eraser") {
                    path(press.path, false);
                    g.setLineDash([]);
                    g.lineCap = "round";
                    g.lineJoin = "round";
                    g.lineWidth = this.settings.brush * 2;
                    g.strokeStyle = sub ? "rgba(214,69,69,0.22)" : (this.glass ? "rgba(56,189,248,0.25)" : "rgba(2,132,199,0.2)");
                    g.stroke();
                } else if (press.path.length > 1) {
                    path(press.path, false);
                    g.setLineDash([4, 3]);
                    g.lineWidth = 1.5;
                    g.strokeStyle = sub ? red : ink;
                    g.stroke();
                }
            }
            const poly = this.live.poly;
            if (poly && poly.pts.length) {
                const pts = poly.pts.map(([x, y]) => this.gl.toScreen(x, y));
                const sub = poly.op === "sub";
                const color = sub ? red : accent;
                path(pts, false);
                g.setLineDash([]);
                g.lineWidth = 1.5;
                g.strokeStyle = color;
                g.fillStyle = sub ? "rgba(214,69,69,0.06)" : (this.glass ? "rgba(56,189,248,0.07)" : "rgba(2,132,199,0.06)");
                if (pts.length > 2) g.fill();
                g.stroke();
                if (this.live.hover) {
                    g.beginPath();
                    g.moveTo(pts[pts.length - 1][0], pts[pts.length - 1][1]);
                    g.lineTo(this.live.hover[0], this.live.hover[1]);
                    g.setLineDash([4, 3]);
                    g.stroke();
                }
                pts.forEach(([x, y], i) => {
                    g.beginPath();
                    g.setLineDash([]);
                    g.arc(x, y, i === 0 ? 5 : 2.6, 0, Math.PI * 2);
                    if (i === 0) { g.strokeStyle = ink; g.lineWidth = 1.5; g.stroke(); } else { g.fillStyle = color; g.fill(); }
                });
            }
            if (brushing && this.live.hover) {
                g.beginPath();
                g.setLineDash([]);
                g.arc(this.live.hover[0], this.live.hover[1], this.settings.brush, 0, Math.PI * 2);
                g.lineWidth = 1.3;
                g.strokeStyle = this.tool === "eraser" ? red : ink;
                g.stroke();
            }
        }

        // -- painting the rest ---------------------------------------------------

        paintAll() {
            if (!this.identity) return;
            const n = this.rows ? this.rows.length : 0;
            const k = this.count1();
            const perImage = new Uint32Array(this.identity.projects.length);
            for (let i = 0; i < n; i += 1) if (this.rows[i]) perImage[this.identity.codes[i]] += 1;
            this.perImage = perImage;
            this.selected = k;
            this.root.classList.toggle("has-selection", k > 0);
            const images = Array.from(perImage).filter(Boolean).length;
            if (this.countNode) {
                this.countNode.textContent = "";
                if (k) {
                    append(this.countNode, h("strong", null, fmt(k) + " cells"),
                        h("span", null, pct(k, n) + (this.identity.projects.length > 1 ? " · " + images + (images === 1 ? " image" : " images") : "")));
                } else {
                    this.countNode.append(h("span.sm-emb-empty", null, "No cells selected"));
                }
                this.annotateButton.disabled = !k;
            }
            if (this.selPill) {
                this.selPill.hidden = !k;
                this.selPill.textContent = "";
                if (k) {
                    append(this.selPill, h("span.sm-emb-pill-key"), h("strong", null, fmt(k) + " cells"),
                        h("span.sm-emb-muted", null, pct(k, n)),
                        h("button.sm-emb-pill-x", { type: "button", "aria-label": "Clear the selection", "data-tip": "Clear the selection",
                            onclick: () => this.clear() }, icon("x", 11)));
                }
            }
            this.paintHistory();
            if (!this.glass) this.paintPanel();
            else if (this.popView === "label") this.paintAnnotate(this.pop.body, true);
            this.paintOverlay();
            this.paintRegion();
            if (this.o.onSelection) this.o.onSelection({ count: k, total: n, perImage, projects: this.identity.projects });
        }

        // -- the legend -------------------------------------------------------

        paintLegend() {
            const meta = this.colorMeta;
            const legend = this.legend;
            legend.textContent = "";
            legend.hidden = !meta;
            if (!meta) return;
            const collapsed = this.settings.legend === false;
            legend.classList.toggle("is-collapsed", collapsed);
            legend.classList.toggle("is-scale", meta.kind !== "category");
            const n = this.gl ? this.gl.n : 0;
            if (meta.kind !== "category") {
                const lo = meta.range[0];
                const hi = meta.range[1];
                const digits = Math.abs(hi - lo) >= 10 ? 1 : 2;
                legend.setAttribute("data-tip", meta.column + "|" + "|" + (meta.marker ? "marker, " : "") + "1st–99th percentile");
                append(legend, h("span.sm-emb-scale-name", null, meta.column),
                    h("span.sm-emb-scale-end", null, lo.toFixed(digits)),
                    h("span.sm-emb-ramp", { style: { background: "linear-gradient(90deg," + RAMP.join(",") + ")" } }),
                    h("span.sm-emb-scale-end", null, hi.toFixed(digits)));
                return;
            }
            legend.removeAttribute("data-tip");
            const hidden = this.hiddenSet();
            const codes = meta.categories.map((_, i) => i);
            if (meta.missing) codes.push(MISSING);
            const anyHidden = hidden.size > 0;
            const head = h("div.sm-emb-legend-head", null,
                h("button.sm-emb-legend-title", { type: "button", "aria-expanded": String(!collapsed),
                    "data-tip": collapsed ? "Show the legend" : "Fold the legend",
                    onclick: () => { this.saveSettings({ legend: collapsed }); this.paintLegend(); } },
                icon(collapsed ? "chevR" : "chevD", 10), h("span", null, meta.column)),
                h("button.sm-emb-legend-all", { type: "button", "data-tip": anyHidden ? "Show every group" : "Hide every group",
                    "aria-label": anyHidden ? "Show every group" : "Hide every group",
                    onclick: () => this.setHidden(anyHidden ? [] : codes) }, icon(anyHidden ? "eyeOff" : "eye", 13)));
            legend.append(head);
            if (collapsed) return;
            const list = h("div.sm-emb-legend-list", { role: "listbox", "aria-multiselectable": "true", "aria-label": "Groups shown" });
            codes.forEach((code) => {
                const name = code === MISSING ? "No value" : meta.categories[code];
                const count = code === MISSING ? meta.missing : (meta.counts ? meta.counts[code] : 0);
                const color = code === MISSING ? (this.glass ? "#78829a" : "#c9ced8") : meta.colors[code];
                const off = hidden.has(code);
                list.append(h("div.sm-emb-legend-row" + (off ? ".is-off" : ""), { role: "option", "aria-selected": String(!off),
                    "data-code": String(code), title: name },
                h("span.sm-emb-swatch", { style: { background: color } }), h("span.sm-emb-legend-name", null, name),
                h("span.sm-emb-legend-n", null, fmt(count)), h("span.sm-emb-legend-p", null, pct(count, n))));
            });
            legend.append(list);
        }

        setHidden(codes) {
            const set = this.hiddenSet();
            set.clear();
            codes.forEach((c) => set.add(c));
            this.paintColors();
            this.paintLegend();
        }

        /** Click a group to hide or show it; drag over groups to do the same to
         *  each; Alt-click shows only that group; double-click selects it. */
        wireLegend() {
            let drag = null;
            const rowAt = (e) => {
                const node = document.elementFromPoint(e.clientX, e.clientY);
                const row = node && node.closest && node.closest(".sm-emb-legend-row");
                return row && this.legend.contains(row) ? row : null;
            };
            const setRow = (row, show) => {
                const code = Number(row.dataset.code);
                const set = this.hiddenSet();
                if (show) set.delete(code); else set.add(code);
                row.classList.toggle("is-off", !show);
                row.setAttribute("aria-selected", String(show));
            };
            const flush = () => {
                cancelAnimationFrame(this.legendFrame || 0);
                this.legendFrame = requestAnimationFrame(() => { this.paintColors(); this.paintLegendHead(); });
            };
            this.legend.addEventListener("pointerdown", (e) => {
                const row = e.target.closest(".sm-emb-legend-row");
                if (!row || e.button !== 0) return;
                e.preventDefault();
                e.stopPropagation();
                const code = Number(row.dataset.code);
                const set = this.hiddenSet();
                if (e.altKey) {
                    const all = Array.from(this.legend.querySelectorAll(".sm-emb-legend-row")).map((r) => Number(r.dataset.code));
                    const solo = set.size === all.length - 1 && !set.has(code);
                    this.setHidden(solo ? [] : all.filter((c) => c !== code));
                    return;
                }
                drag = { show: set.has(code), seen: new Set([row]) };
                setRow(row, drag.show);
                flush();
                this.legend.setPointerCapture(e.pointerId);
            });
            this.legend.addEventListener("pointermove", (e) => {
                if (!drag) return;
                const row = rowAt(e);
                if (!row || drag.seen.has(row)) return;
                drag.seen.add(row);
                setRow(row, drag.show);
                flush();
            });
            const end = () => { drag = null; };
            this.legend.addEventListener("pointerup", end);
            this.legend.addEventListener("pointercancel", end);
            this.legend.addEventListener("dblclick", (e) => {
                const row = e.target.closest(".sm-emb-legend-row");
                if (!row) return;
                const code = Number(row.dataset.code);
                // The two clicks of a double-click toggled the group twice; it
                // is shown again, then its cells are selected.
                this.hiddenSet().delete(code);
                this.paintColors();
                this.paintLegend();
                this.selectGroup(code, e.shiftKey);
            });
        }

        paintLegendHead() {
            const all = this.legend.querySelector(".sm-emb-legend-all");
            if (!all) return;
            const any = this.hiddenSet().size > 0;
            all.textContent = "";
            all.append(icon(any ? "eyeOff" : "eye", 13));
            all.dataset.tip = any ? "Show every group" : "Hide every group";
            all.setAttribute("aria-label", all.dataset.tip);
            all.onclick = () => {
                const codes = Array.from(this.legend.querySelectorAll(".sm-emb-legend-row")).map((r) => Number(r.dataset.code));
                this.setHidden(this.hiddenSet().size ? [] : codes);
            };
        }

        paintRegion() {
            const info = this.regionInfo;
            this.regionChip.hidden = !info;
            this.regionChip.textContent = "";
            if (!info) return;
            this.regionChip.append(h("span.sm-emb-region-dot"), "In " + info.name + " · " + fmt(info.count) + " cells ",
                h("button", { type: "button", onclick: () => this.useRegion() }, "Use as selection"));
        }

        useRegion() {
            const info = this.regionInfo;
            if (!info || !this.bus()) return;
            this.bus().set({ [info.project]: info.ids }, { scope: [info.project], source: "region", label: info.name });
            // Now the selection: shown as one, not as the gold hint.
            this.bus().setRegion(null);
        }

        // -- popovers inside the view -------------------------------------------

        /** A small card anchored to `anchor`, inside this view. */
        showPop(anchor, view, build, placement) {
            if (this.pop && this.popView === view && this.pop.anchor === anchor) { this.closePop(); return null; }
            this.closePop();
            const body = h("div.sm-emb-pop-body");
            const card = h("div.sm-emb-pop", { role: "dialog" }, body);
            this.root.append(card);
            this.pop = { card, body, anchor };
            this.popView = view;
            build(body);
            this.placePop(placement);
            this.onPopOutside = (e) => {
                if (card.contains(e.target) || anchor.contains(e.target)) return;
                if (e.target.closest && e.target.closest(".sm-pick-menu")) return;
                this.closePop();
            };
            setTimeout(() => document.addEventListener("pointerdown", this.onPopOutside, true), 0);
            return body;
        }

        placePop(placement) {
            if (!this.pop) return;
            const { card, anchor } = this.pop;
            const box = this.root.getBoundingClientRect();
            const r = anchor.getBoundingClientRect();
            const c = card.getBoundingClientRect();
            let left;
            let top;
            if (placement === "left") {
                left = r.left - box.left - c.width - 8;
                top = r.top - box.top;
            } else if (placement === "above" || r.bottom - box.top + c.height + 8 > box.height) {
                left = r.right - box.left - c.width;
                top = r.top - box.top - c.height - 6;
            } else {
                left = r.left - box.left;
                top = r.bottom - box.top + 6;
            }
            card.style.left = Math.max(6, Math.min(box.width - c.width - 6, left)) + "px";
            card.style.top = Math.max(6, top) + "px";
            this.pop.placement = placement;
        }

        closePop() {
            if (!this.pop) return;
            document.removeEventListener("pointerdown", this.onPopOutside, true);
            const card = this.pop.card;
            card.classList.add("is-leaving");
            setTimeout(() => card.remove(), 100);
            this.pop = null;
            this.popView = null;
        }

        showHelp(anchor) {
            this.showPop(anchor, "help", (body) => append(body, h("div.sm-emb-pop-title", null, "Selecting cells"),
                h("p.sm-emb-pop-text", null, "Draw round cells with the Lasso; hold ⇧ to add to the selection, " + ALT +
                    " to take away. Polygon places points; Brush and Eraser paint cells in and out."),
                h("p.sm-emb-pop-text", null, "Scroll zooms, right-drag (or Navigate) pans, double-click fits. " + MOD +
                    "Z undoes, Esc cancels a shape in progress. In the legend, click a group to hide it, drag over groups, " +
                    ALT + "-click to show one alone, double-click to select it."),
                h("p.sm-emb-pop-text", null, "The selection is shared with the viewer and every other view of these cells.")));
        }

        showSize(anchor) {
            this.showPop(anchor, "size", (body) => {
                // Core's one slider (views/slider.js): rail, fill, thumb and
                // a typeable number; a plain number field where it is absent.
                const row = (label, min, max, step, value, unit, onInput) => {
                    const mount = h("div.sm-emb-slider");
                    if (typeof window.PlexoraSlider === "function") {
                        new window.PlexoraSlider(mount, { min, max, step, value, unit, label,
                            onInput: (v) => onInput(Number(v)), onChange: (v) => onInput(Number(v)) });
                    } else {
                        mount.append(h("input.sm-emb-input", { type: "number", min, max, step, value, "aria-label": label,
                            onchange: (e) => onInput(Math.min(max, Math.max(min, Number(e.target.value) || value))) }));
                    }
                    // The core slider names itself; the fallback field needs a name beside it.
                    return h("div.sm-emb-range-row", null, mount.querySelector("input[type=number]:only-child") ? h("span", null, label) : null, mount);
                };
                append(body, h("div.sm-emb-pop-title", null, "Display"),
                    row("Point size", 0.3, 3, 0.1, this.settings.pointScale, "×", (v) => {
                        this.saveSettings({ pointScale: v });
                        if (this.gl) this.gl.setStyle({ scale: v });
                    }),
                    row("Brush", 4, 80, 2, this.settings.brush, "px", (v) => {
                        this.saveSettings({ brush: v });
                    }));
            }, "left");
        }

        // -- annotate ---------------------------------------------------------

        labelColumns() {
            return (this.state.info && this.state.info.label_columns) || [];
        }

        async labelTarget() {
            const ls = this.labelState;
            const columns = this.labelColumns();
            if (!ls.column && !ls.newName && columns.length) ls.column = columns[columns.length - 1];
            const isNew = !ls.column || !columns.includes(ls.column);
            let already = 0;
            let categories = [];
            let colors = [];
            let counts = [];
            if (!isNew) {
                const got = await this.data.color(ls.column, this.state.info.stamp);
                categories = got.meta.categories || [];
                colors = got.meta.colors || [];
                counts = got.meta.counts || [];
                const v = got.arrays.values;
                for (let i = 0; i < this.rows.length; i += 1) if (this.rows[i] && v[i] !== MISSING) already += 1;
            }
            return { isNew, already, categories, colors, counts };
        }

        async submitLabel() {
            const ls = this.labelState;
            const column = ls.column || ls.newName;
            if (!column) { this.flash("Name a column first."); return; }
            if (!ls.text.trim()) { this.flash("Choose or type a label first."); return; }
            if (!this.selected) { this.flash("Select some cells first."); return; }
            const isNew = !this.labelColumns().includes(column);
            ls.busy = true;
            this.repaintAnnotate();
            try {
                const out = await this.data.label({ column, label: ls.text.trim(), overwrite: ls.overwrite, new: isNew,
                                                    cells: bitmaskBase64(this.rows) });
                ls.column = column;
                ls.newName = "";
                await this.refreshInfo();
                if (this.state.column === column) await this.applyColor();
                if (this.glass) this.closePop();
                this.showSaved(out);
                if (this.o.onLabeled) this.o.onLabeled(out);
            } catch (err) {
                this.flash(err.message);
            } finally {
                ls.busy = false;
                this.repaintAnnotate();
            }
        }

        async undoLabel() {
            try {
                const out = await this.data.undo();
                await this.refreshInfo();
                if (this.labelState.column === out.column && out.removed) this.labelState.column = null;
                if (this.state.column === out.column) {
                    if (out.removed) this.state.column = null;
                    await this.applyColor();
                }
                this.paintSelects();
                this.flash("Undone: " + out.column + (out.removed ? " removed." : " as it was."), null, 5000, true);
                this.repaintAnnotate();
                if (this.o.onLabeled) this.o.onLabeled(out);
            } catch (err) {
                this.flash(err.message);
            }
        }

        async refreshInfo() {
            const info = await this.data.info();
            this.state.info = info;
            this.paintSelects();
        }

        showSaved(out) {
            const message = h("span", null, h("strong", null, fmt(out.labelled) + " cells labeled " + out.label),
                h("span.sm-emb-muted", null, " in " + out.column),
                out.kept ? h("span.sm-emb-muted", null, " · " + fmt(out.kept) + " kept their label") : null);
            this.flash(message, [h("button.sm-emb-link", { type: "button", onclick: () => this.undoLabel() }, "Undo")], 9000, true);
        }

        flash(message, actions, ms, ok) {
            const host = this.toast;
            host.textContent = "";
            host.classList.toggle("is-error", !ok);
            append(host, h("span.sm-emb-toast-icon", { "aria-hidden": "true" }, ok ? "✓" : "!"), message,
                h("span.sm-emb-spacer"), ...(actions || []));
            host.hidden = false;
            clearTimeout(this.toastTimer);
            this.toastTimer = setTimeout(() => { host.hidden = true; }, ms || 5000);
        }

        /** What the viewer's window needs to open this same view. */
        handoff() {
            return { notebook_id: this.o.notebookId, key: this.state.key, color: this.state.column };
        }

        repaintAnnotate() {
            if (!this.glass) this.paintPanel();
            else if (this.popView === "label" && this.pop) this.paintAnnotate(this.pop.body, true);
        }

        // The notebook's Annotate panel.
        paintPanel() {
            if (!this.panel || !this.identity) return;
            const panel = this.panel;
            if (!panel.firstChild) {
                this.panelForm = h("div.sm-emb-form");
                this.panelMore = h("div.sm-emb-panel-more");
                if (this.external) append(panel, this.panelForm, this.panelMore);
                else {
                    append(panel, h("div.sm-emb-panel-head", null, icon("tag", 15), h("strong", null, "Annotate"),
                        h("span.sm-emb-spacer"), this.iconButton("x", "Close (A)", () => this.togglePanel(false))),
                    this.panelForm, h("span.sm-emb-spacer"), this.panelMore);
                }
            }
            this.paintAnnotate(this.panelForm, false);
            const k = this.selected || 0;
            this.panelMore.textContent = "";
            const composition = h("button.sm-emb-row-btn", { type: "button", disabled: !k || null,
                onclick: (e) => this.showComposition(e.currentTarget, "left") }, icon("bars", 13), h("span", null, "What's in the selection"),
            h("span.sm-emb-spacer"), h("span.sm-emb-muted", null, k ? this.compositionHint() : ""), icon("chevR", 10));
            const save = h("button.sm-emb-row-btn", { type: "button", disabled: !k || null,
                onclick: (e) => this.showSave(e.currentTarget, "left") }, icon("bookmark", 13), h("span", null, "Save as a selection"));
            append(this.panelMore, composition, save);
        }

        compositionHint() {
            const parts = [];
            const meta = this.colorMeta;
            if (meta && meta.kind === "category") {
                const seen = new Set();
                for (let i = 0; i < this.rows.length && seen.size < 99; i += 1) if (this.rows[i]) seen.add(this.colorValues[i]);
                parts.push(seen.size + (seen.size === 1 ? " group" : " groups"));
            }
            const images = this.perImage ? Array.from(this.perImage).filter(Boolean).length : 0;
            if (this.identity.projects.length > 1) parts.push(images + (images === 1 ? " image" : " images"));
            return parts.join(" · ");
        }

        /** The label form: label, column, the counts, overwrite, the button. */
        async paintAnnotate(host, compact) {
            if (!host || !this.identity) return;
            const token = (this.annotateToken = (this.annotateToken || 0) + 1);
            const ls = this.labelState;
            const target = await this.labelTarget().catch(() => ({ isNew: true, already: 0, categories: [], colors: [], counts: [] }));
            if (token !== this.annotateToken) return;
            const k = this.selected || 0;
            const columns = this.labelColumns();
            if (target.isNew && !ls.newName && !ls.column) ls.newName = "cell_label";
            const columnValue = ls.column || ls.newName;
            const P = window.ScimapPicker;
            const theme = this.glass ? "glass" : "light";
            const labelPicker = P.create({ theme, ariaLabel: "Label", placeholder: "Type a new label or pick one",
                className: "sm-emb-field", searchPlaceholder: "Search or type a new label", menuWidth: 240,
                value: ls.text || null, emptyText: "Type to create a label",
                groups: [{ title: target.categories.length ? "In " + columnValue : null, items: target.categories.map((c, i) => ({
                    value: c, label: c, swatch: target.colors[i], hint: target.counts[i] !== undefined ? fmt(target.counts[i]) : "" })) }],
                create: (q) => "+ Create “" + q + "”",
                onChange: (value) => { ls.text = value; this.repaintAnnotate(); } });
            if (ls.text && !target.categories.includes(ls.text)) labelPicker.label.textContent = ls.text;
            const columnItems = columns.map((c) => ({ value: c, label: c, mono: true }));
            if (!columns.includes(columnValue) && columnValue) columnItems.push({ value: columnValue, label: columnValue, mono: true, hint: "new" });
            const columnPicker = P.create({ theme, ariaLabel: "Column", className: "sm-emb-field", mono: true,
                searchPlaceholder: "Search or name a new column", menuWidth: 240, value: columnValue,
                groups: [{ title: null, items: columnItems }], emptyText: "Type to name a new column",
                create: (q) => "+ New column “" + q + "”",
                onChange: (value, item, created) => {
                    if (created || !columns.includes(value)) { ls.column = null; ls.newName = value; } else { ls.column = value; ls.newName = ""; }
                    ls.overwrite = false;
                    this.repaintAnnotate();
                } });
            if (!target.isNew) {
                const chip = h("span.sm-emb-chip", null, target.categories.length + (target.categories.length === 1 ? " label" : " labels"));
                columnPicker.tail.hidden = false;
                columnPicker.tail.append(chip);
            } else if (columnValue) {
                columnPicker.tail.hidden = false;
                columnPicker.tail.append(h("span.sm-emb-chip", null, "new"));
            }
            const fresh = Math.max(0, k - target.already);
            const willLabel = ls.overwrite ? k : fresh;
            const counts = h("div.sm-emb-counts", null,
                h("div.sm-emb-counts-row", null, h("span.sm-emb-swatch.is-new"), h("span", null, "Not labeled yet"),
                    h("span.sm-emb-spacer"), h("strong", null, fmt(fresh))),
                h("div.sm-emb-counts-row", null, h("span.sm-emb-swatch.is-old"), h("span", null, "Already labeled"),
                    h("span.sm-emb-spacer"), h("strong", null, fmt(target.already))));
            const overwrite = target.already ? h("label.sm-emb-check", null,
                h("input", { type: "checkbox", checked: ls.overwrite || null,
                    onchange: (e) => { ls.overwrite = e.target.checked; this.repaintAnnotate(); } }),
                h("span.sm-emb-check-box", { "aria-hidden": "true" }),
                h("span", null, "Overwrite existing labels")) : null;
            const waits = (window.ScimapApi && window.ScimapApi.needs && window.ScimapApi.needs.labels) || "";
            const go = h("button.sm-emb-primary", { type: "button", disabled: waits || ls.busy || !k || willLabel <= 0 || !ls.text || null,
                onclick: () => this.submitLabel() },
            ls.busy ? "Labeling…" : !k ? "Select cells to label" : "Label " + fmt(willLabel) + (willLabel === 1 ? " cell" : " cells"));
            const note = waits ? h("span.sm-emb-note", null, waits)
                : k && target.already && !ls.overwrite
                ? h("span.sm-emb-note", null, fmt(target.already) + " keep their label. Tick Overwrite to relabel all " + fmt(k) + ".")
                : !k ? h("span.sm-emb-note", null, "Draw around cells in the plot first. Saved labels stay when the selection is cleared.") : null;
            host.textContent = "";
            if (compact) host.append(h("div.sm-emb-pop-title", null, "Label " + fmt(k) + " cells"));
            append(host,
                h("label.sm-emb-form-row", null, h("span.sm-emb-form-label", null, "Label"), labelPicker.root),
                h("label.sm-emb-form-row", null, h("span.sm-emb-form-label", null, "Column"), columnPicker.root),
                counts, overwrite, go, note);
            if (this.pop && this.popView === "label") this.placePop(this.pop.placement);
        }

        annotateMenu(anchor) {
            this.showPop(anchor, "menu", (body) => {
                body.classList.add("is-menu");
                const item = (name, text, fn) => h("button.sm-emb-menu-item", { type: "button", onclick: fn }, icon(name, 13), h("span", null, text));
                append(body,
                    item("tag", "Label these cells…", () => this.openLabel(anchor)),
                    item("bars", "What's in the selection", () => this.showComposition(anchor, "above")),
                    item("bookmark", "Save as a selection…", () => this.showSave(anchor, "above")),
                    h("div.sm-emb-pop-note", null, "Saved labels are kept in the data; clearing the selection never touches them."));
            }, "above");
        }

        openLabel(anchor) {
            this.closePop();
            this.showPop(anchor, "label", (body) => this.paintAnnotate(body, true), "above");
            setTimeout(() => this.placePop("above"), 30);
        }

        showComposition(anchor, placement) {
            if (this.glass) this.closePop();
            this.showPop(anchor, "composition", (body) => {
                body.classList.add("is-wide");
                append(body, h("div.sm-emb-pop-title", null, fmt(this.selected) + " cells selected"));
                const meta = this.colorMeta;
                if (meta && meta.kind === "category" && this.selected) {
                    const counts = new Uint32Array(meta.categories.length + 1);
                    for (let i = 0; i < this.rows.length; i += 1) {
                        if (!this.rows[i]) continue;
                        const v = this.colorValues[i];
                        counts[v === MISSING ? meta.categories.length : v] += 1;
                    }
                    const order = Array.from(counts.keys()).filter((i) => counts[i]).sort((a, b) => counts[b] - counts[a]).slice(0, 12);
                    body.append(h("div.sm-emb-pop-group", null, "By " + meta.column));
                    order.forEach((i) => {
                        const name = i === meta.categories.length ? "No value" : meta.categories[i];
                        const share = counts[i] / this.selected;
                        body.append(h("div.sm-emb-bar-row", null,
                            h("span.sm-emb-swatch", { style: { background: i === meta.categories.length ? "#9aa3b2" : meta.colors[i] } }),
                            h("span.sm-emb-bar-name", null, name),
                            h("span.sm-emb-bar-track", null, h("span", { style: { width: (share * 100).toFixed(1) + "%",
                                background: i === meta.categories.length ? "#9aa3b2" : meta.colors[i] } })),
                            h("span.sm-emb-bar-v", null, pct(counts[i], this.selected))));
                    });
                }
                if (this.identity.projects.length) {
                    body.append(h("div.sm-emb-pop-group", null, "By image"));
                    this.identity.projects.forEach((p, i) => {
                        if (!this.perImage[i] && this.identity.projects.length > 6) return;
                        body.append(h("div.sm-emb-kv", null, h("span", null, p),
                            h("span", null, fmt(this.perImage[i]) + " · " + pct(this.perImage[i], this.totals[i]))));
                    });
                }
            }, placement);
        }

        showSave(anchor, placement) {
            if (this.glass) this.closePop();
            const stamp = new Date();
            const two = (v) => String(v).padStart(2, "0");
            const suggested = "embedding_" + two(stamp.getMonth() + 1) + two(stamp.getDate()) + "_" + two(stamp.getHours()) + two(stamp.getMinutes());
            this.showPop(anchor, "save", (body) => {
                const input = h("input.sm-emb-input", { value: suggested, "aria-label": "Selection name",
                    onkeydown: (e) => { if (e.key === "Enter") go.click(); } });
                const go = h("button.sm-emb-primary", { type: "button", onclick: async () => {
                    const name = input.value.trim();
                    if (!name) return;
                    go.disabled = true;
                    go.textContent = "Saving…";
                    try {
                        const saved = await this.data.saveSelection({ name, cells: bitmaskBase64(this.rows) });
                        this.closePop();
                        const total = saved.reduce((s, x) => s + x.n, 0);
                        this.flash(h("span", null, h("strong", null, "Saved " + (saved[0] ? saved[0].name : name)), h("span.sm-emb-muted", null, " · " + fmt(total) +
                            " cells on " + saved.length + (saved.length === 1 ? " image" : " images"))), null, 6000, true);
                    } catch (err) {
                        go.disabled = false;
                        go.textContent = "Save";
                        this.flash(err.message);
                    }
                } }, "Save");
                append(body, h("div.sm-emb-pop-title", null, "Save " + fmt(this.selected) + " cells as a selection"), input,
                    h("p.sm-emb-pop-text", null, "Kept with each image's saved selections, for later steps, the viewer and agents. Letters, digits, _ . and -."), go);
                setTimeout(() => { input.focus(); input.select(); }, 30);
            }, placement);
        }

        // -- notebook: the sidebar's Annotate, the step's view bar -------------

        /** The Annotate panel, drawn into `host` (the plot sidebar). */
        attachPanel(host) {
            if (!this.panel || !host) return;
            this.panel.classList.add("is-external");
            host.append(this.panel);
            this.paintPanel();
            // The sidebar is put on the page after its sections are built.
            requestAnimationFrame(() => this.paintPanel());
        }

        detachPanel(host) {
            if (this.panel && (!host || this.panel.parentNode === host)) this.panel.remove();
        }

        /** The step's view switch before Color by, Customize before the rest. */
        setBarExtras(leading, trailing) {
            if (!this.bar) return;
            (this.barExtras || []).forEach((node) => node.remove());
            this.barExtras = [];
            if (leading) {
                const lead = h("span.sm-emb-lead", null, leading);
                this.bar.prepend(lead);
                this.barExtras.push(lead);
            }
            (trailing || []).forEach((node) => {
                const before = this.panelToggle && this.panelToggle.parentNode === this.bar ? this.panelToggle : this.fullButton;
                this.bar.insertBefore(node, before && before.parentNode === this.bar ? before : null);
                this.barExtras.push(node);
            });
        }

        /** The Color by choices, grouped as the plot's own picker shows them. */
        colorGroups() {
            return (this.colorPicker && this.colorPicker.groups) || this.lastGroups || [];
        }

        setPointScale(value) {
            this.saveSettings({ pointScale: value });
            if (this.gl) this.gl.setStyle({ scale: value });
        }

        // -- notebook: panel, fullscreen ----------------------------------------

        togglePanel(force) {
            if (!this.body) return;
            this.panelOpen = force === undefined ? !this.panelOpen : Boolean(force);
            this.saveSettings({ panel: this.panelOpen });
            this.body.classList.toggle("is-closed", !this.panelOpen);
            this.paintPanelToggle();
            if (this.panelOpen) this.paintPanel();
            const start = performance.now();
            const follow = () => { this.resize(); if (performance.now() - start < 320) requestAnimationFrame(follow); };
            requestAnimationFrame(follow);
        }

        paintPanelToggle() {
            if (!this.panelToggle) return;
            this.panelToggle.classList.toggle("is-on", this.panelOpen);
            this.panelToggle.setAttribute("aria-pressed", String(this.panelOpen));
        }

        isFull() {
            return this.root.classList.contains("is-full");
        }

        toggleFull(force) {
            if (this.glass) return;
            const on = force === undefined ? !this.isFull() : Boolean(force);
            this.root.classList.toggle("is-full", on);
            if (this.fullButton) {
                this.fullButton.textContent = "";
                this.fullButton.append(icon(on ? "unfull" : "full"));
                this.fullButton.dataset.tip = on ? "Exit fullscreen (Esc)" : "Fullscreen (F)";
                this.fullButton.setAttribute("aria-label", this.fullButton.dataset.tip);
            }
            if (on) {
                this.placeholder = h("div.sm-emb-placeholder", { style: { height: this.root.offsetHeight + "px" } });
                this.root.before(this.placeholder);
                document.body.append(this.root);
                this.plot.focus({ preventScroll: true });
            } else if (this.placeholder) {
                this.placeholder.replaceWith(this.root);
                this.placeholder = null;
            }
            requestAnimationFrame(() => this.resize());
        }

        resize() {
            if (this.gl) { this.gl.redraw(); this.paintOverlay(); }
            const r = this.plot.getBoundingClientRect();
            this.root.classList.toggle("is-compact", r.height > 0 && (r.height < 380 || r.width < 360));
            this.root.classList.toggle("is-narrow", this.root.offsetWidth > 0 && this.root.offsetWidth < 440);
            if (this.pop) this.placePop(this.pop.placement);
        }

        destroy() {
            window.removeEventListener("plexora:cells-selected", this.onBus);
            window.removeEventListener("plexora:cells-selection-mode", this.onMode);
            window.removeEventListener("plexora:cells-region", this.onRegion);
            this.closePop();
            if (this.sizes) this.sizes.disconnect();
            if (this.gl) this.gl.destroy();
            this.gl = null;
            this.colorPicker.destroy();
            this.keyPicker.destroy();
            if (this.placeholder) this.placeholder.remove();
            this.root.remove();
        }
    }

    /** The controller for `key`, kept across re-renders of its step. */
    function mount(host, key, options) {
        let embed = key ? kept.get(key) : null;
        if (!embed) {
            embed = new Embed(options);
            if (key) {
                kept.set(key, embed);
                if (kept.size > 6) {
                    for (const [k, e] of kept) {
                        if (!e.root.isConnected && k !== key) { e.destroy(); kept.delete(k); break; }
                    }
                }
            }
        } else {
            Object.assign(embed.o, { onViewInTissue: options.onViewInTissue, onOpenInViewer: options.onOpenInViewer,
                                     onColor: options.onColor, onLabeled: options.onLabeled, download: options.download,
                                     onAnnotate: options.onAnnotate || embed.o.onAnnotate });
        }
        if (options.leading || options.trailing) embed.setBarExtras(options.leading, options.trailing);
        // A view in fullscreen stays on the page body until it leaves it.
        if (!embed.isFull()) host.append(embed.root);
        requestAnimationFrame(() => embed.resize());
        return embed;
    }

    function forget(prefix) {
        for (const [k, e] of kept) {
            if (k.startsWith(prefix)) { e.destroy(); kept.delete(k); }
        }
    }

    return { Embed, mount, forget, create: (options) => new Embed(options), unpack, bitmaskBase64 };
})();
