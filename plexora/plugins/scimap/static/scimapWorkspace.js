// The SCIMAP page: header, banners, tool sidebar, the column of steps, and
// the page-wide states (no project, no licence, not installed, website).
//
// The server is the source of truth for the notebook; this file renders its
// view, sends edits as operations (settings debounced, structure at once),
// and polls while a step is queued or running. A step is re-rendered only
// when its own server view changes, so a poll never takes focus from a field.
window.ScimapWorkspace = (function () {
    "use strict";
    const { h, icon, button, iconButton, n, bytes, tildePath } = window.ScimapDom;
    const API = window.ScimapApi;
    const Cells = () => window.ScimapCells;
    const STARTER_KEY = "plexora.scimap.starter";
    const SIDEBAR_KEY = "plexora.scimap.sidebar";
    const INSPECTOR_KEY = "plexora.scimap.inspector";
    const RETURN_KEY = "plexora:scimap-return";

    function store(key, value) {
        try {
            if (value === undefined) return window.localStorage.getItem(key);
            if (value === null) window.localStorage.removeItem(key);
            else window.localStorage.setItem(key, value);
        } catch (err) { /* private mode: a preference, not state */ }
        return null;
    }

    class Workspace {
        constructor(root) {
            this.root = root;
            this.base = root.dataset.base || "/plugins/scimap";
            API.setBase(this.base);
            this.notebookId = root.dataset.notebookId || "";
            try { this.handoff = JSON.parse(root.dataset.handoff || "{}"); } catch (e) { this.handoff = {}; }
            this.locals = new Map();
            this.signatures = new Map();
            this.sliding = new Set();
            this.cellNodes = new Map();
            this.results = new Map();
            this.tickers = new Map();
            this.columns = new Map();
            this.pending = new Map();
            this.flushTimer = null;
            this.pollTimer = null;
            this.saveState = "saved";
            this.collapsed = store(SIDEBAR_KEY) === "collapsed" || window.innerWidth < 1100;
            // Both sidebars open by default (Figure Builder's layout), the
            // right one unless it was closed; a window too narrow for three
            // columns opens it only on demand, as a sheet.
            this.inspectorOpen = store(INSPECTOR_KEY) !== "closed" && window.innerWidth >= 1180;
            this.search = "";
            this.destroyed = false;
            this.onKey = (e) => this.key(e);
            this.onFloat = () => this.closeOwnFloating();
            this.onVisibility = () => this.schedulePoll();
            this.onHide = () => this.flush();
        }

        // The notebook's own catalogue arrives with every view: the tools that
        // apply to its data, in its words (markers or genes; cells, spots or
        // bins). A tool it leaves out stays known by id, so a step made with
        // it still draws.
        get view() { return this._view; }

        set view(value) {
            this._view = value;
            if (!value || !value.catalog || !this.allTools) return;
            this.catalog = value.catalog;
            this.tools = Object.assign({}, this.allTools);
            this.catalog.forEach((c) => c.tools.forEach((t) => { this.tools[t.id] = t; }));
        }

        // -- boot ------------------------------------------------------------

        async boot() {
            document.addEventListener("keydown", this.onKey);
            window.addEventListener("scimap:float", this.onFloat);
            document.addEventListener("visibilitychange", this.onVisibility);
            window.addEventListener("pagehide", this.onHide);
            if (this.handoff && this.handoff.title) {
                this.renderWebsite();
                return;
            }
            try {
                const [status, tools] = await Promise.all([API.status(), API.tools()]);
                this.status = status.status;
                this.catalog = tools.categories;
                this.tools = {};
                this.catalog.forEach((c) => c.tools.forEach((t) => { this.tools[t.id] = t; }));
                this.allTools = Object.assign({}, this.tools);
                window.ScimapPlots.setPalette(this.status.palette);
            } catch (err) {
                this.renderError(err);
                return;
            }
            if (!this.status.install.installed || !this.status.install.compatible) {
                this.renderMissing();
                return;
            }
            if (!this.notebookId) {
                this.renderNoContext();
                return;
            }
            if (this.needsLicenceChoice()) {
                await this.loadView().catch(() => null);
                this.renderLicence();
                return;
            }
            await this.openNotebook();
        }

        needsLicenceChoice() {
            const lic = this.status.licence;
            return !lic.licensed && store(STARTER_KEY) !== "1";
        }

        async openNotebook() {
            try {
                await this.loadView();
            } catch (err) {
                this.renderError(err);
                return;
            }
            this.renderShell();
            this.restoreReturn();
            this.schedulePoll();
            this.watchRecords();
            API.warm().catch(() => null);
        }

        async loadView() {
            const answer = await API.view(this.notebookId);
            this.view = answer.view;
            return this.view;
        }

        destroy() {
            this.destroyed = true;
            this.flush();
            clearTimeout(this.pollTimer);
            clearTimeout(this.recordsTimer);
            this.tickers.forEach((t) => clearInterval(t));
            document.removeEventListener("keydown", this.onKey);
            window.removeEventListener("scimap:float", this.onFloat);
            document.removeEventListener("visibilitychange", this.onVisibility);
            window.removeEventListener("pagehide", this.onHide);
            if (this.onResize) window.removeEventListener("resize", this.onResize);
            if (this.inspector) this.inspector.destroy();
            this.closeFloating();
        }

        // -- page-wide states --------------------------------------------------

        clear() {
            this.root.innerHTML = "";
            this.cellNodes.clear();
            this.signatures.clear();
        }

        header(kind, options) {
            const o = options || {};
            // Beside a notebook the brand stands at the foot of the tool
            // list; a page without that list keeps it here.
            const parts = o.notebook ? [] : [
                h("div.sm-brand", null, window.ScimapIcons.mark(), h("span.sm-brand-name", null, "SCIMAP Pro")),
                h("span.sm-vrule"),
            ];
            const context = this.view && this.view.notebook && this.view.notebook.context;
            if (context && kind !== "none") {
                const w = this.view.working || {};
                const meta = context.kind === "dataset"
                    ? context.projects.length + " projects" + (w.exists ? " · " + n(w.n_cells) + " cells" : "")
                    : (w.exists ? n(w.n_cells) + " cells · " + (w.images || []).length + ((w.images || []).length === 1 ? " image" : " images") : "Not prepared yet");
                const chip = h("button.sm-chip-btn" + (context.kind === "dataset" ? "" : ".is-static"), {
                    type: "button", "aria-haspopup": context.kind === "dataset" ? "menu" : null,
                    title: context.kind === "dataset" ? "Projects in this dataset" : context.name,
                    onclick: context.kind === "dataset" ? (e) => this.projectPicker(e.currentTarget) : null,
                }, icon(context.kind === "dataset" ? "layers" : "image"),
                h("span.sm-chip-name", null, context.name),
                h("span.sm-kind.sm-kind-" + context.kind, null, context.kind === "dataset" ? "Dataset" : "Project"),
                h("span.sm-chip-meta", null, meta),
                context.kind === "dataset" ? icon("chevD", "sm") : null);
                parts.push(chip);
            } else {
                parts.push(h("span", { style: { fontSize: "13px", color: "var(--text-muted)" } }, "No project open"));
            }
            if (o.notebook && this.view) {
                parts.push(h("span.sm-vrule"));
                parts.push(h("button.sm-chip-btn", { type: "button", "aria-haspopup": "menu",
                    onclick: (e) => this.notebookMenu(e.currentTarget) },
                icon("book"), h("span.sm-chip-name", null, this.view.notebook.title), icon("chevD", "sm")));
                this.savedNode = h("span.sm-saved", { role: "status", "aria-live": "polite" });
                this.paintSaved();
                parts.push(this.savedNode);
            }
            parts.push(h("span.sm-spacer"));
            if (o.actions && context) {
                if (context.kind === "project") {
                    parts.push(button("Create dataset", { icon: "layers", onclick: () => this.createDataset() }));
                } else {
                    parts.push(button("Manage dataset", { icon: "layers", onclick: () => this.manageDataset() }));
                }
                parts.push(button("Export", { icon: "download", onclick: () => this.exportDialog() }));
            }
            if (context && kind !== "none") {
                parts.push(button("Image viewer", { kind: "primary", icon: "image", onclick: () => this.openViewer() }));
            }
            if (o.notebook) {
                this.inspectorButton = iconButton("panelOpen", "Plot sidebar (])", () => this.toggleInspector(), "is-box");
                this.inspectorButton.classList.add("sm-insp-toggle");
                this.paintInspectorButton();
                parts.push(this.inspectorButton);
            }
            return h("header.sm-header", null, parts);
        }

        renderWebsite() {
            this.clear();
            const notice = this.handoff;
            const actions = h("div.sm-card-actions");
            const card = h("div.sm-card", null,
                h("div.sm-card-icon", null, icon("lock")),
                h("h1", null, notice.why || "Currently available only in the Plexora desktop app."),
                h("p.sm-lede", null, "SCIMAP runs Scimap Pro on your computer, next to your data. Open this project in the desktop app to start a notebook."),
                actions);
            this.root.append(this.header("none"), h("div.sm-state-page", null, card));
            if (window.PlexoraWebHandoff && typeof window.PlexoraWebHandoff.render === "function") {
                try {
                    window.PlexoraWebHandoff.render(actions, notice);
                    return;
                } catch (err) { /* fall back to plain links */ }
            }
            actions.append(
                h("a.sm-btn.sm-btn-primary", { href: "plexora://open" }, "Open in Desktop App"),
                h("a.sm-btn", { href: "https://plexoraapp.com/download", target: "_blank", rel: "noopener" }, "Get the Desktop App"));
        }

        renderError(err) {
            this.clear();
            this.root.append(this.header("none"), h("div.sm-state-page", null, h("div.sm-card", null,
                h("h1", null, "SCIMAP could not open"),
                h("p.sm-lede", null, err && err.message ? err.message : String(err)),
                h("div.sm-card-actions", null, button("Try again", { kind: "primary", icon: "refresh", onclick: () => window.location.reload() })))));
        }

        renderMissing() {
            this.clear();
            const install = this.status.install;
            const desktop = this.status.desktop;
            const card = h("div.sm-card", null,
                h("div.sm-card-icon", null, icon("warning")),
                h("h1", null, install.installed ? "This Scimap Pro is too old for SCIMAP" : "Scimap Pro is not installed in this copy of Plexora"),
                h("p.sm-lede", null, "SCIMAP runs its analyses with Scimap Pro. The desktop app includes it; reinstalling the app puts it back. Your notebooks and results are kept and open again once it is installed."),
                h("div.sm-card-actions", null,
                    desktop ? h("a.sm-btn.sm-btn-primary", { href: "https://plexoraapp.com/download", target: "_blank", rel: "noopener" }, "Get the desktop app") : null,
                    button("Check again", { icon: "refresh", onclick: () => window.location.reload() })),
                desktop ? null : h("div.sm-card-rule"),
                desktop ? null : h("div", { style: { display: "flex", flexDirection: "column", gap: "6px" } },
                    h("strong", { style: { fontSize: "13px", color: "var(--sm-ink)" } }, "Running Plexora from Python?"),
                    h("p", { style: { color: "var(--text-secondary)" } }, "Install Scimap Pro into the same environment, then restart Plexora."),
                    h("div.sm-details", null, install.hint),
                    install.installed ? h("p.sm-muted", { style: { fontSize: "12px" } },
                        "Found Scimap Pro " + install.version + "; it is older than " + install.minimum + ". The same command updates it.") : null));
            this.root.append(this.header("none"), h("div.sm-state-page", null, card));
        }

        async renderNoContext() {
            this.clear();
            const recent = h("div.sm-recent", null, h("span.sm-form-help", null, "Loading…"));
            const card = h("div.sm-card", null,
                h("h1", null, "No project open"),
                h("p.sm-lede", null, "Open a project or a dataset to start a SCIMAP notebook. Each project and dataset keeps its own notebooks."),
                h("div.sm-card-actions", null,
                    button("Open project", { kind: "primary", icon: "image", onclick: (e) => this.pickPlace(e.currentTarget, "project") }),
                    button("Open dataset", { icon: "layers", onclick: (e) => this.pickPlace(e.currentTarget, "dataset") }),
                    button("Create dataset", { kind: "quiet", onclick: () => this.createDataset() })),
                h("div.sm-card-rule"),
                h("span.sm-section-label", null, "Recent"),
                recent);
            this.root.append(this.header("none"), h("div.sm-state-page", null, card));
            try {
                const places = await API.places();
                this.places = places;
                recent.innerHTML = "";
                const seen = new Set();
                const rows = [];
                places.notebooks.forEach((nb) => {
                    const key = nb.context.kind + ":" + nb.context.id;
                    if (seen.has(key)) return;
                    seen.add(key);
                    const count = places.notebooks.filter((x) => x.context.id === nb.context.id).length;
                    rows.push(h("button", { type: "button", onclick: () => this.goNotebook(nb.id) },
                        icon(nb.context.kind === "dataset" ? "layers" : "image"),
                        h("span", null, h("strong", null, nb.context.name),
                            h("span", null, (nb.context.kind === "dataset" ? "Dataset" : "Project") + " · " +
                                count + (count === 1 ? " notebook" : " notebooks")))));
                });
                if (!rows.length) rows.push(h("span.sm-form-help", null, "No notebooks yet."));
                recent.append(...rows.slice(0, 6));
            } catch (err) {
                recent.textContent = err.message;
            }
        }

        renderLicence() {
            this.clear();
            const lic = this.status.licence;
            const limits = lic.limits || {};
            const card = h("div.sm-card", null,
                h("div", { style: { display: "flex", alignItems: "center", gap: "12px" } }, window.ScimapIcons.mark(),
                    h("h1", null, "SCIMAP Pro")),
                h("p", null, "Single-cell and spatial analysis of multiplexed imaging data."),
                h("p.sm-lede", null, lic.tier === "expired" ? "Your Scimap Pro license has ended. A valid Scimap Pro license is required to use this plugin."
                    : lic.tier === "invalid" ? "The Scimap Pro license on this computer could not be verified. A valid Scimap Pro license is required to use this plugin."
                        : "A valid Scimap Pro license is required to use this plugin."),
                h("div.sm-card-actions", null,
                    button("Add license", { kind: "primary", icon: "key", onclick: () => this.licenceDialog() }),
                    h("a.sm-btn", { href: lic.purchase_url, target: "_blank", rel: "noopener" }, icon("external"), h("span", null, "Purchase license"))),
                h("p.sm-muted", { style: { fontSize: "12px" } }, "Already licensed in Scimap Pro on this computer? Plexora finds that license on its own."),
                h("div.sm-card-rule"),
                h("div", { style: { display: "flex", alignItems: "center", gap: "12px" } },
                    h("div", { style: { flex: "1", display: "flex", flexDirection: "column", gap: "2px" } },
                        h("strong", { style: { fontSize: "13px", color: "var(--sm-ink)" } }, "Or continue with Starter"),
                        h("span.sm-muted", { style: { fontSize: "12px" } }, "Free: up to " + n(limits.max_cells || 100000) + " cells, one worker, watermarked plots.")),
                    button("Continue with Starter", { onclick: () => { store(STARTER_KEY, "1"); this.openNotebook(); } })));
            this.root.append(this.header("project"), h("div.sm-state-page", null, card));
        }

        // -- the notebook ------------------------------------------------------

        renderShell() {
            this.clear();
            const nb = this.view.notebook;
            this.root.append(this.header("project", { notebook: true, actions: true }));
            const lic = this.status.licence;
            if (!lic.licensed && store("plexora.scimap.starterBanner") !== "dismissed") {
                const limits = lic.limits || {};
                this.root.append(h("div.sm-banner", { role: "note" }, icon("info"),
                    h("span.sm-banner-text", null, h("strong", null, "Starter: "),
                        "up to " + n(limits.max_cells || 100000) + " cells, one worker, watermarked plots. A trial or a license lifts these limits."),
                    button("Add license", { onclick: () => this.licenceDialog() }),
                    button("Start a 30-day trial", { kind: "quiet", onclick: () => this.startTrial() }),
                    iconButton("x", "Dismiss", () => { store("plexora.scimap.starterBanner", "dismissed"); this.renderShell(); })));
            }
            this.sidebar = h("aside.sm-sidebar", { "aria-label": "Analysis tools" });
            this.main = h("main.sm-main");
            this.column = h("div.sm-column");
            this.column.addEventListener("mousedown", (e) => {
                const node = e.target.closest && e.target.closest("[data-cell]");
                if (node) {
                    this.pressTarget = e.target;
                    this.select(node.dataset.cell);
                    this.pressTarget = null;
                }
            });
            this.main.append(this.column);
            this.bodyNode = h("div.sm-body", null, this.sidebar, this.main);
            this.root.append(this.bodyNode);
            this.paintSidebar();
            this.paintColumn();
            if (this.inspectorOpen) {
                window.ScimapViews.setPicking(true);
                this.mountInspector();
            }
        }

        // -- the plot sidebar (scimapInspector.js) -----------------------------

        mountInspector() {
            if (!this.bodyNode) return;
            if (!this.inspector) this.inspector = new window.ScimapInspector.Inspector(this.ctx());
            this.inspector.ctx = this.ctx();
            this.inspector.paint();
            if (!this.scrim) {
                this.scrim = h("div.sm-insp-scrim", { "aria-hidden": "true", onclick: () => this.closeInspector() });
            }
            this.bodyNode.append(this.scrim, this.inspector.root);
            this.bodyNode.classList.add("has-inspector");
            this.reflow();
            if (!this.onResize) {
                this.onResize = () => this.layoutInspector();
                window.addEventListener("resize", this.onResize);
            }
            this.layoutInspector();
        }

        // Under 1180 px the sidebar opens over the steps, as a sheet.
        layoutInspector() {
            if (!this.bodyNode) return;
            this.bodyNode.classList.toggle("is-narrow", window.innerWidth < 1180);
        }

        // The steps' column changed width (a sidebar opened or closed): plotly
        // sizes its plots on the window's resize, so tell it.
        reflow() {
            if (this.reflowing) return;
            this.reflowing = true;
            requestAnimationFrame(() => requestAnimationFrame(() => {
                this.reflowing = false;
                document.querySelectorAll("#sm_workspace .sm-plotly").forEach((plot) => {
                    if (window.Plotly && plot.isConnected && plot.offsetParent) window.Plotly.Plots.resize(plot);
                });
            }));
        }

        openInspector(section) {
            this.inspectorOpen = true;
            store(INSPECTOR_KEY, "open");
            window.ScimapViews.setPicking(true);
            this.mountInspector();
            this.paintInspectorButton();
            if (section && this.inspector) this.inspector.openSection(section);
        }

        closeInspector() {
            this.inspectorOpen = false;
            store(INSPECTOR_KEY, "closed");
            window.ScimapViews.setPicking(false);
            if (this.inspector) {
                this.inspector.destroy();
                this.inspector = null;
            }
            if (this.scrim) this.scrim.remove();
            if (this.bodyNode) this.bodyNode.classList.remove("has-inspector");
            this.paintInspectorButton();
            this.reflow();
        }

        toggleInspector() {
            if (this.inspectorOpen) {
                this.closeInspector();
                return;
            }
            // Opened from the header with nothing chosen: the selected step's
            // first plot, else the empty state.
            const views = window.ScimapViews;
            if (!views.current() && this.selectedId && views.firstPanel(this.selectedId)) {
                views.select(this.selectedId, views.firstPanel(this.selectedId));
            }
            this.openInspector();
        }

        inspect(cellId, panelId, section) {
            if (cellId && panelId) window.ScimapViews.select(cellId, panelId);
            if (this.inspectorOpen && !section) {
                // Customize on the plot the sidebar already shows closes it.
                const cur = window.ScimapViews.selected();
                if (cur && cur.cellId === cellId && cur.panelId === panelId && this.lastInspected === cellId + "/" + panelId) {
                    this.lastInspected = null;
                    this.closeInspector();
                    return;
                }
            }
            this.lastInspected = cellId + "/" + panelId;
            this.openInspector(section);
        }

        paintInspectorButton() {
            if (!this.inspectorButton) return;
            this.inspectorButton.classList.toggle("is-on", Boolean(this.inspectorOpen));
            this.inspectorButton.setAttribute("aria-pressed", String(Boolean(this.inspectorOpen)));
        }

        paintSaved() {
            if (!this.savedNode) return;
            this.savedNode.innerHTML = "";
            this.savedNode.classList.toggle("is-failed", this.saveState === "failed");
            if (this.saveState === "saving") this.savedNode.append("Saving…");
            else if (this.saveState === "failed") this.savedNode.append("Not saved");
            else this.savedNode.append(icon("check", "xs"), "Saved");
        }

        paintSidebar() {
            const side = this.sidebar;
            side.innerHTML = "";
            side.classList.toggle("is-collapsed", this.collapsed);
            const avail = this.view.availability;
            if (this.collapsed) {
                side.append(h("button.sm-rail-btn.is-toggle", { type: "button", "aria-label": "Show the tool list",
                    title: "Show the tool list", onclick: () => this.toggleSidebar() }, icon("panelOpen")));
                this.catalog.forEach((cat, ci) => {
                    if (ci) side.append(h("span.sm-rail-rule"));
                    cat.tools.forEach((t) => {
                        const a = avail[t.id] || {};
                        side.append(h("button.sm-rail-btn", { type: "button", "aria-label": t.name,
                            title: t.name + (a.reason ? " — " + a.reason : ""), "aria-disabled": String(!a.enabled),
                            onclick: () => this.pickTool(t.id) }, icon(t.icon)));
                    });
                });
                return;
            }
            const searchInput = h("input", { type: "search", placeholder: "Search tools", "aria-label": "Search tools",
                value: this.search });
            searchInput.addEventListener("input", () => { this.search = searchInput.value; this.paintToolList(list); });
            searchInput.addEventListener("keydown", (e) => {
                if (e.key === "Enter") {
                    const first = list.querySelector('.sm-tool:not([aria-disabled="true"])');
                    if (first) first.click();
                } else if (e.key === "Escape") {
                    searchInput.value = "";
                    this.search = "";
                    this.paintToolList(list);
                    searchInput.blur();
                }
            });
            this.searchInput = searchInput;
            const list = h("div.sm-tools");
            side.append(
                h("div.sm-side-head", null, h("span.sm-side-title", null, "Analysis tools"),
                    iconButton("panelClose", "Collapse the tool list", () => this.toggleSidebar())),
                h("label.sm-search", null, icon("search"), searchInput),
                list,
                h("div.sm-side-foot", null,
                    h("div.sm-brand", null, window.ScimapIcons.mark(), h("span.sm-brand-name", null, "SCIMAP Pro"))));
            this.paintToolList(list);
        }

        paintToolList(list) {
            list.innerHTML = "";
            const q = this.search.trim().toLowerCase();
            const avail = this.view.availability;
            this.catalog.forEach((cat) => {
                const tools = cat.tools.filter((t) => !q || t.name.toLowerCase().includes(q) || (t.summary || "").toLowerCase().includes(q));
                if (!tools.length) return;
                list.append(h("div.sm-tools-group", null, cat.title));
                tools.forEach((t) => {
                    const a = avail[t.id] || {};
                    const tail = [];
                    if (t.id === "prepare" && a.done) tail.push(h("span.sm-tool-tail.is-done", { title: "Done" }, icon("check", "sm")));
                    if (t.id === "prepare" && a.start) tail.push(h("span.sm-tag", null, "Start here"));
                    if (t.viewer) tail.push(h("span.sm-tool-tail", null, "Viewer", icon("external", "xs")));
                    if (!a.enabled && a.link && this.tools[a.link] && avail[a.link] && avail[a.link].enabled) {
                        tail.push(h("span.sm-tool-link", null, "Add it"));
                    }
                    list.append(h("button.sm-tool" + (t.id === "prepare" && a.start ? ".is-start" : ""), {
                        type: "button", "aria-disabled": String(!a.enabled), title: t.summary,
                        onclick: () => this.pickTool(t.id),
                    }, icon(t.icon),
                    h("span.sm-tool-text", null, h("span.sm-tool-name", null, t.name),
                        !a.enabled && a.reason ? h("span.sm-tool-reason", null, a.reason) : null),
                    tail));
                });
            });
            if (!list.children.length) list.append(h("div.sm-tools-group", null, "No tool matches “" + this.search + "”"));
        }

        toggleSidebar() {
            this.collapsed = !this.collapsed;
            store(SIDEBAR_KEY, this.collapsed ? "collapsed" : "open");
            this.paintSidebar();
            // The rail animates its width; reflow when it has settled.
            setTimeout(() => this.reflow(), 220);
        }

        pickTool(toolId) {
            const a = this.view.availability[toolId] || {};
            if (toolId === "gating") {
                this.openViewer({ tool: "gating" });
                return;
            }
            if (!a.enabled) {
                if (a.link && this.view.availability[a.link] && this.view.availability[a.link].enabled) {
                    this.addTool(a.link, {});
                } else if (a.reason) {
                    this.toast(a.reason);
                }
                return;
            }
            if (toolId === "prepare") {
                const existing = this.view.notebook.cells.find((c) => c.tool === "prepare");
                if (existing) {
                    this.focusCell(existing.id, true);
                    return;
                }
            }
            this.addTool(toolId, {});
        }

        paintColumn() {
            const nb = this.view.notebook;
            const column = this.column;
            if (!this.titleRow) {
                this.titleInput = h("input.sm-title-input", { "aria-label": "Notebook title", value: nb.title, maxlength: "120", spellcheck: "false" });
                this.titleInput.addEventListener("change", () => this.op({ op: "set_title", title: this.titleInput.value }));
                this.titleInput.addEventListener("keydown", (e) => { if (e.key === "Enter") this.titleInput.blur(); });
                this.titleMeta = h("span.sm-title-meta");
                this.titleActions = h("div.sm-title-actions");
                this.titleRow = h("div.sm-titlebar", null,
                    h("div.sm-titlebar-text", null, this.titleInput, this.titleMeta), this.titleActions);
            }
            if (document.activeElement !== this.titleInput) this.titleInput.value = nb.title;
            const w = this.view.working || {};
            const folder = nb.results_dir || (w.results_dir || "");
            this.titleMeta.innerHTML = "";
            this.titleMeta.append((nb.cells.length ? nb.cells.length + (nb.cells.length === 1 ? " step" : " steps") : "No steps yet") +
                " · results in ", h("span.sm-mono", null, tildePath(folder)));
            this.titleActions.innerHTML = "";
            const runnable = nb.cells.some((c) => ["idle", "failed", "outdated", "cancelled"].includes(c.state));
            if (runnable && nb.cells.length > 1) {
                this.titleActions.append(button("Run all", { kind: "quiet", icon: "playAll", onclick: () => this.runAll() }));
            }
            if (nb.cells.length > 1) {
                const allCollapsed = nb.cells.every((c) => c.collapsed);
                this.titleActions.append(button(allCollapsed ? "Expand all" : "Collapse all", { kind: "quiet",
                    onclick: () => this.op({ op: "set_collapsed", collapsed: !allCollapsed }) }));
            }
            if (!column.contains(this.titleRow)) column.prepend(this.titleRow);

            // A note above a lone Prepare step, as the design draws it.
            const prepare = nb.cells.find((c) => c.tool === "prepare");
            const onlyPrepare = nb.cells.length === 1 && prepare && prepare.state !== "done";
            this.paintIntro(onlyPrepare);

            const wanted = new Set(nb.cells.map((c) => c.id));
            for (const [id, node] of this.cellNodes) {
                if (!wanted.has(id)) {
                    node.remove();
                    this.cellNodes.delete(id);
                    this.signatures.delete(id);
                    this.stopTicker(id);
                }
            }
            let anchor = this.introNode && column.contains(this.introNode) ? this.introNode : this.titleRow;
            nb.cells.forEach((cell) => {
                const signature = this.signature(cell);
                let node = this.cellNodes.get(cell.id);
                if (!node || this.signatures.get(cell.id) !== signature) {
                    const fresh = this.renderCell(cell);
                    if (!node && this.painted) fresh.classList.add("is-new");
                    // A fold, an unfold, Settings or History opening: the step
                    // slides from its old height to its new one.
                    const from = node && this.sliding.has(cell.id) ? node.getBoundingClientRect().height : null;
                    this.sliding.delete(cell.id);
                    if (node) node.replaceWith(fresh);
                    if (from !== null) window.ScimapDom.slide(fresh, from);
                    node = fresh;
                    this.cellNodes.set(cell.id, node);
                    this.signatures.set(cell.id, signature);
                }
                if (anchor.nextSibling !== node) anchor.after(node);
                node.classList.toggle("is-selected", cell.id === this.selectedId);
                anchor = node;
            });
            this.paintEmpty(nb, anchor);
            this.painted = true;
        }

        paintIntro(show) {
            if (!show) {
                if (this.introNode) this.introNode.remove();
                if (this.afterNode) this.afterNode.remove();
                return;
            }
            const context = this.view.notebook.context;
            const csv = context.kind === "project";
            if (!this.introNode) {
                this.introNode = h("div.sm-note", null, icon("info"), h("p", null,
                    context.kind === "dataset"
                        ? "Scimap Pro works on one AnnData file. The first step reads each project's table and joins them on their shared markers, once. The other tools unlock when it finishes."
                        : "Scimap Pro works on an AnnData file. The first step makes one from this project's cell table, once. The other tools unlock when it finishes."));
                this.afterNode = h("div.sm-after", null, icon("chevR", "sm"),
                    "After this step: Clustering, UMAP, the summaries and the spatial tools.");
            }
            // Round 2: Prepare explains itself; only the "after this step" line stays.
            if (this.introNode.isConnected) this.introNode.remove();
            void csv;
        }

        paintEmpty(nb, anchor) {
            const showEmpty = nb.cells.length === 0 || (nb.cells.length === 1 && nb.cells[0].tool === "prepare" && nb.cells[0].state === "done");
            if (this.afterNode) {
                const only = nb.cells.length === 1 && nb.cells[0].tool === "prepare" && nb.cells[0].state !== "done";
                if (only) anchor.after(this.afterNode);
                else this.afterNode.remove();
            }
            if (!showEmpty) {
                if (this.emptyNode) this.emptyNode.remove();
                return;
            }
            const a = this.view.availability;
            const card = (id, iconName, title, text) => h("button", { type: "button", disabled: !(a[id] && a[id].enabled) || null,
                title: a[id] && a[id].reason ? a[id].reason : null, onclick: () => this.addTool(id, {}) },
            h("strong", null, icon(iconName), title), h("span", null, text));
            const w = this.view.working;
            const empty = h("div.sm-empty", null,
                h("div", null, h("h2", null, "Start an analysis"),
                    h("p", null, "Pick a tool on the left. Each one adds a step you can set up and run. Nothing runs until you press Run, and every step keeps its settings so you can rerun or export it.")),
                h("div.sm-suggest", null,
                    card("quality", "shield", "Check quality", "Flag cells whose position looks wrong before anything else."),
                    card("celltypes", "tag", "Name cell types", "From your gates and a few rules, such as CD3+ is a T cell."),
                    card("cluster", "cluster", "Cluster cells", "Group cells by marker expression, then name the groups.")),
                w && w.exists ? h("div.sm-after", { style: { padding: 0 } }, icon("check", "sm"),
                    "Data is ready: " + n(w.n_cells) + " cells and " + (w.markers || []).length + " markers in " + w.file + ".") : null);
            if (this.emptyNode) this.emptyNode.replaceWith(empty);
            else anchor.after(empty);
            this.emptyNode = empty;
        }

        signature(cell) {
            const job = cell.job || {};
            return JSON.stringify([cell.state, cell.collapsed, cell.run && cell.run.run_id, cell.run && cell.run.status,
                cell.stale, job.status, job.stage, job.ahead, cell.chips, cell.choices,
                this.view.working && this.view.working.mtime, this.view.notebook.cells.length,
                this.locals.get(cell.id) && this.locals.get(cell.id).version,
                cell.tool === "celltypes" ? [cell.params.rules, cell.gated] : null, cell.params.source,
                Cells().shownRun(cell) ? null : cell.params.method, cell.params.graph,
                cell.tool === "prepare" ? [cell.params, this.view.notebook.results_dir, this.view.notebook.context,
                    this.view.notebook.memory, (this.view.working || {}).reading] : null, cell.version, cell.versions]);
        }

        renderCell(cell) {
            this.stopTicker(cell.id);
            try {
                return Cells().render(cell, this.ctx());
            } catch (err) {
                console.error(err); // eslint-disable-line no-console
                return h("section.sm-cell", null, h("div.sm-cell-head", null, "This step could not be drawn: " + err.message));
            }
        }

        rerender(cellId, animate) {
            const local = this.local(cellId);
            local.version = (local.version || 0) + 1;
            if (animate) this.sliding.add(cellId);
            this.paintColumn();
        }

        focusCell(cellId, flash) {
            const node = this.cellNodes.get(cellId);
            if (!node) return;
            node.scrollIntoView({ behavior: "smooth", block: "nearest" });
            if (flash) {
                node.classList.add("is-flash");
                setTimeout(() => node.classList.remove("is-flash"), 900);
            }
        }

        // -- the context steps render with ------------------------------------

        ctx() {
            const self = this;
            return {
                nb: this.view.notebook,
                view: this.view,
                work: this.view.working || {},
                status: this.status,
                tools: this.tools,
                index: (id) => this.view.notebook.cells.findIndex((c) => c.id === id) + 1,
                local: (id) => this.local(id),
                rerender: (id, animate) => this.rerender(id, animate),
                op: (op, o) => this.op(op, o),
                setParams: (id, params, immediate) => this.setParams(id, params, immediate),
                setDraft: (id, text) => {
                    const node = this.cellNodes.get(id);
                    const pill = node && node.querySelector(".sm-cell-head .sm-state");
                    if (pill) pill.textContent = "Draft · " + text;
                    this.local(id).draft = "Draft · " + text;
                },
                run: (id, body) => this.run(id, body),
                cancel: (id) => this.cancel(id),
                addTool: (tool, params, after) => this.addTool(tool, params, after),
                result: (id, runId) => this.result(id, runId),
                colors: (id, column) => API.colors(this.notebookId, id, column).then((a) => a.colors),
                figures: (id, runId) => this.figures(id, runId),
                recolor: (id, column) => this.recolor(id, column),
                // The embedding's Color by: kept with the step (View in tissue
                // colours by it) without redrawing the step.
                setColor: (id, column) => {
                    const cell = this.view.notebook.cells.find((c) => c.id === id);
                    if (cell) cell.params = Object.assign({}, cell.params, { color: column });
                    return this.op({ op: "set_display", id, display: { color: column } }, { quiet: true });
                },
                openEmbedding: (id, handoff) => this.openEmbedding(id, handoff),
                refreshView: () => this.loadView().catch(() => null),
                figureMenu: (anchor, cell, entry) => this.figureMenu(anchor, cell, entry),
                figureFile: (cell, entry, fmt) => this.figureFile(cell, entry, fmt),
                drawView: (id, view, opts, compute) => API.drawView(this.notebookId, id, view, opts, compute).then((a) => a.figure),
                inspect: (id, panelId, section) => this.inspect(id, panelId, section),
                closeInspector: () => this.closeInspector(),
                reflow: () => this.reflow(),
                selectedId: () => this.selectedId,
                goStep: (id) => { this.select(id); this.focusCell(id, true); },
                runAll: () => this.runAll(),
                exportNotebook: () => this.exportDialog(),
                notebook: () => this.view && this.view.notebook,
                working: () => (this.view && this.view.working) || {},
                closeFloating: () => this.closeFloating(),
                lasso: (id, polygon, color) => API.lasso(this.notebookId, id, polygon, color).then((a) => a.selection),
                column: (name) => this.columnSummary(name),
                plan: (folder) => API.plan(this.notebookId, folder).then((a) => a.plan),
                viewInViewer: (id, body) => this.viewInViewer(id, body),
                viewColumn: (column, focus) => this.viewColumn(column, focus),
                menu: (anchor, items) => this.menu(anchor, items),
                toast: (text) => this.toast(text),
                colorOf: (column) => this.colorOf(column),
                defaultLabel: (cell) => this.defaultLabel(cell),
                chooseMarkers: (anchor, all, chosen, done) => this.chooseMarkers(anchor, all, chosen, done),
                browseFolder: (id, start) => this.browseFolder(id, start),
                setResultsDir: (id, path) => this.setResultsDir(id, path),
                deleteStep: (id) => this.deleteStep(id),
                sendToFigure: (anchor, cell, entry) => this.sendToFigure(anchor, cell, entry),
                csvFor: (cell) => this.csvFor(cell),
                openLicence: () => this.licenceDialog(),
                startTrial: () => this.startTrial(),
                focusPrepare: () => {
                    const p = this.view.notebook.cells.find((c) => c.tool === "prepare");
                    if (p) this.focusCell(p.id, true);
                },
                stamp: () => this.recordsStamp,
                adopt: (view) => this.apply(view),
                popover: (anchor, node) => {
                    this.closeFloating();
                    this.place(node, anchor);
                    return () => this.closeFloating();
                },
                viewerUrl: (project) => this.viewerHref(project),
                makeCurrent: async (id, version) => {
                    await this.flush();
                    const answer = await API.makeCurrent(this.notebookId, id, version);
                    this.forgetResults(id);
                    this.apply(answer.view);
                    this.rerender(id);
                },
                ticker: (id, fn) => {
                    self.stopTicker(id);
                    self.tickers.set(id, setInterval(fn, 1000));
                },
            };
        }

        local(id) {
            if (!this.locals.has(id)) this.locals.set(id, {});
            return this.locals.get(id);
        }

        stopTicker(id) {
            const t = this.tickers.get(id);
            if (t) clearInterval(t);
            this.tickers.delete(id);
        }

        // A category's colour as Scimap Pro's figures draw it (server palette.py).
        colorOf(column) {
            const info = ((this.view.working || {}).columns || []).find((c) => c.name === column);
            const colors = (info && info.colors) || {};
            const cats = info && info.categories ? info.categories : null;
            return (category, index) => {
                if (colors[String(category)]) return colors[String(category)];
                const i = cats ? cats.indexOf(String(category)) : -1;
                return window.ScimapPlots.color(i >= 0 ? i : index);
            };
        }

        defaultLabel(cell) {
            const p = Object.assign({}, cell.params || {});
            if (cell.tool === "cluster") {
                return (p.method || "leiden") === "kmeans" ? "kmeans_k" + (p.k || 10)
                    : "leiden_r" + String(Number(p.resolution || 1).toFixed(2)).replace(/0+$/, "").replace(/\.$/, "").replace(".", "p");
            }
            return { umap: "umap", distance: "spatialDistance", interaction: "spatialCooccurrence",
                     neighborhoods: "neighborhoods" }[cell.tool] || "";
        }

        // Results are cached by run, so a version made current draws from its
        // own; this drops a step's cached ones all the same.
        forgetResults(cellId) {
            if (window.ScimapViews) window.ScimapViews.forget(cellId);
            for (const key of Array.from(this.results.keys())) if (key.indexOf(cellId + ":") === 0) this.results.delete(key);
            if (this.figureSets) for (const key of Array.from(this.figureSets.keys())) if (key.indexOf(cellId + ":") === 0) this.figureSets.delete(key);
        }

        async result(cellId, runId) {
            const key = cellId + ":" + runId;
            if (!this.results.has(key)) {
                this.results.set(key, API.result(this.notebookId, cellId).then((a) => a.result));
                this.results.get(key).catch(() => this.results.delete(key));
            }
            return this.results.get(key);
        }

        displayKey(cellId) {
            const cell = this.view.notebook.cells.find((c) => c.id === cellId);
            if (!cell || cell.tool !== "umap") return "";
            const run = Cells().shownRun(cell) || {};
            return (cell.params && cell.params.color) || ((run.effective || {}).display || {}).color || "";
        }

        async figures(cellId, runId) {
            const key = cellId + ":" + runId + ":" + this.displayKey(cellId);
            if (!this.figureSets) this.figureSets = new Map();
            if (!this.figureSets.has(key)) {
                this.figureSets.set(key, API.figures(this.notebookId, cellId).then((a) => a.figures));
                this.figureSets.get(key).catch(() => this.figureSets.delete(key));
            }
            return this.figureSets.get(key);
        }

        async recolor(cellId, column) {
            const cell = this.view.notebook.cells.find((c) => c.id === cellId);
            if (!cell) return;
            cell.params = Object.assign({}, cell.params, { color: column });
            const local = this.local(cellId);
            local.selection = null;
            await this.op({ op: "set_display", id: cellId, display: { color: column } }, { quiet: true });
            const run = Cells().shownRun(cell);
            if (run) this.results.delete(cellId + ":" + run.run_id);
            this.rerender(cellId);
        }

        // A figure for a paper: Scimap Pro draws it again through matplotlib.
        figureMenu(anchor, cell, entry) {
            const items = [{ label: entry.title + ", drawn by Scimap Pro", heading: true },
                { label: "PNG, 300 dpi", icon: "download", onSelect: () => this.figureFile(cell, entry, "png") },
                { label: "PDF", icon: "download", onSelect: () => this.figureFile(cell, entry, "pdf") },
                { label: "SVG", icon: "download", onSelect: () => this.figureFile(cell, entry, "svg") },
                { sep: true },
                { label: "Send to Figure Builder…", icon: "figure", onSelect: () => this.sendToFigure(anchor, cell, entry) }];
            this.menu(anchor, items);
        }

        async figureBlob(cell, entry, fmt) {
            await this.flush();
            const response = await fetch(API.figureUrl(this.notebookId, cell.id, entry.name, fmt, entry.view, entry.opts),
                { credentials: "same-origin" });
            if (!response.ok) {
                let message = "The figure could not be drawn.";
                try { message = (await response.json()).message || message; } catch (_) { /* not JSON */ }
                throw new Error(message);
            }
            const disposition = response.headers.get("Content-Disposition") || "";
            const match = /filename="([^"]+)"/.exec(disposition);
            return { blob: await response.blob(), name: match ? match[1] : entry.name + "." + fmt };
        }

        async figureFile(cell, entry, fmt) {
            this.toast("Drawing " + entry.title + " as " + fmt.toUpperCase() + "…");
            try {
                const { blob, name } = await this.figureBlob(cell, entry, fmt);
                window.ScimapPlots.download(name, blob);
            } catch (err) {
                this.toast(err.message);
            }
        }

        async columnSummary(name) {
            const stamp = (this.view.working || {}).mtime;
            const key = name + ":" + stamp;
            if (!this.columns.has(key)) {
                this.columns.set(key, API.column(this.notebookId, name).then((a) => a.summary));
                this.columns.get(key).catch(() => this.columns.delete(key));
            }
            return this.columns.get(key);
        }

        // -- edits ---------------------------------------------------------------

        apply(view) {
            if (!view || this.destroyed) return;
            this.view = view;
            if (!this.sidebar) return;
            this.paintSidebar();
            this.paintColumn();
            this.paintOverview();
            const headerNode = this.root.querySelector(".sm-header");
            if (headerNode) headerNode.replaceWith(this.header("project", { notebook: true, actions: true }));
            this.schedulePoll();
        }

        async op(op, options) {
            if (op.op === "set_collapsed") this.collapseNow(op);
            this.setSaving("saving");
            try {
                const answer = await API.patch(this.notebookId, [op]);
                this.setSaving("saved");
                if (!(options && options.quiet)) this.apply(answer.view);
                else this.view = answer.view;
                return answer;
            } catch (err) {
                this.setSaving("failed");
                this.toast(err.message);
                return null;
            }
        }

        // Fold or unfold at once, before the server answers, keeping the step's
        // head where it is on screen so the page does not jump under the pointer.
        collapseNow(op) {
            const cells = this.view.notebook.cells.filter((c) => !op.id || c.id === op.id);
            cells.forEach((c) => { if (Boolean(c.collapsed) !== Boolean(op.collapsed)) this.sliding.add(c.id); });
            const pinned = op.id ? this.cellNodes.get(op.id) : null;
            const scroller = this.column && this.column.closest(".sm-main") || this.column;
            const before = pinned ? pinned.getBoundingClientRect().top : null;
            cells.forEach((c) => { c.collapsed = Boolean(op.collapsed); });
            this.paintColumn();
            if (pinned && scroller) {
                const node = this.cellNodes.get(op.id);
                const after = node ? node.getBoundingClientRect().top : before;
                if (after !== before) scroller.scrollTop += after - before;
            }
        }

        setParams(cellId, params, immediate) {
            const merged = Object.assign(this.pending.get(cellId) || {}, params);
            this.pending.set(cellId, merged);
            const cell = this.view.notebook.cells.find((c) => c.id === cellId);
            if (cell) Object.assign(cell.params, params);
            this.setSaving("saving");
            clearTimeout(this.flushTimer);
            this.flushTimer = setTimeout(() => this.flush(), immediate ? 0 : 600);
        }

        async flush() {
            clearTimeout(this.flushTimer);
            if (!this.pending.size) return;
            const ops = Array.from(this.pending.entries()).map(([id, params]) => ({ op: "set_params", id, params }));
            this.pending.clear();
            try {
                const answer = await API.patch(this.notebookId, ops);
                this.view = answer.view;
                this.setSaving("saved");
                // Settings changes alter what a form offers (a method's fields,
                // a column's choices) but never what a step shows, so only the
                // sidebar is repainted; the form already shows what was typed.
                if (this.sidebar) this.paintSidebar();
            } catch (err) {
                this.setSaving("failed");
                this.toast(err.message);
            }
        }

        setSaving(state) {
            this.saveState = state;
            this.paintSaved();
        }

        // A tool picked from the list (or a suggestion) takes the stage: every
        // other step folds to its one-line head, and the new one opens at the
        // top of the column, ready to set up.
        async addTool(toolId, params, after) {
            const answer = await this.op({ op: "add_cell", tool: toolId, params: params || {}, after: after || null });
            const id = answer && answer.created && answer.created[0];
            if (!id) return;
            const others = this.view.notebook.cells.filter((c) => c.id !== id && !c.collapsed &&
                c.state !== "running" && c.state !== "queued");
            if (others.length) {
                others.forEach((c) => { c.collapsed = true; });
                this.paintColumn();
                API.patch(this.notebookId, others.map((c) => ({ op: "set_collapsed", id: c.id, collapsed: true })))
                    .then((a) => { this.view = a.view; }).catch(() => {});
            }
            requestAnimationFrame(() => this.stage(id));
        }

        stage(cellId) {
            const node = this.cellNodes.get(cellId);
            const scroller = this.column && this.column.closest(".sm-main");
            if (!node || !scroller) return;
            const top = node.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop - 16;
            scroller.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
            node.classList.add("is-flash");
            setTimeout(() => node.classList.remove("is-flash"), 900);
        }

        async run(cellId, body) {
            await this.flush();
            const local = this.local(cellId);
            local.editing = false;
            try {
                const answer = await API.run(this.notebookId, cellId, body || {});
                this.apply(answer.view);
            } catch (err) {
                if (err.code === "license_required" || err.status === 402) {
                    const cell = this.view.notebook.cells.find((c) => c.id === cellId);
                    if (cell) {
                        cell.run = Object.assign({}, cell.run || {}, { status: "failed", error: { code: err.code, message: err.message, detail: err.detail }, previous: Cells().shownRun(cell) });
                        cell.state = "failed";
                        this.signatures.delete(cellId);
                        this.paintColumn();
                        return;
                    }
                }
                this.toast(err.message);
            }
        }

        async runAll() {
            await this.flush();
            try {
                const answer = await API.runAll(this.notebookId);
                this.apply(answer.view);
                const refused = (answer.queued || []).find((q) => q.error);
                if (refused) this.toast(refused.error.message);
            } catch (err) {
                this.toast(err.message);
            }
        }

        async cancel(cellId) {
            try {
                const answer = await API.cancel(this.notebookId, cellId);
                this.apply(answer.view);
            } catch (err) {
                this.toast(err.message);
            }
        }

        // -- polling ---------------------------------------------------------------

        busy() {
            return this.view && (this.view.running.length ||
                this.view.notebook.cells.some((c) => c.state === "queued" || c.state === "running"));
        }

        // Plexora is the one source of truth for projects and datasets: when a
        // record changes anywhere (the viewer, the home page, another tab), the
        // notebook re-reads its data and Prepare redraws.
        watchRecords() {
            clearTimeout(this.recordsTimer);
            if (this.destroyed) return;
            this.recordsTimer = setTimeout(async () => {
                if (!document.hidden) {
                    try {
                        const answer = await API.stamp();
                        if (this.recordsStamp && answer.stamp !== this.recordsStamp) {
                            this.recordsStamp = answer.stamp;
                            const fresh = await API.view(this.notebookId);
                            this.apply(fresh.view);
                            const prepare = this.view.notebook.cells.find((c) => c.tool === "prepare");
                            if (prepare) {
                                const local = this.local(prepare.id);
                                local.data = null;
                                // An edit open under a project's row is kept.
                                if (!local.editing) this.rerender(prepare.id);
                            }
                        } else {
                            this.recordsStamp = answer.stamp;
                        }
                    } catch (err) { /* the next tick tries again */ }
                }
                this.watchRecords();
            }, 4000);
        }

        schedulePoll() {
            clearTimeout(this.pollTimer);
            if (this.destroyed || !this.busy()) return;
            const delay = document.hidden ? 3000 : 1000;
            this.pollTimer = setTimeout(async () => {
                try {
                    const answer = await API.view(this.notebookId);
                    this.apply(answer.view);
                } catch (err) {
                    this.schedulePoll();
                }
            }, delay);
        }

        // -- the viewer ------------------------------------------------------------

        viewerHref(project, query) {
            const root = this.base.replace(/\/plugins\/scimap$/, "");
            return root + "/" + encodeURIComponent(project) + (query ? "?" + query : "");
        }

        leaveReturn() {
            try {
                window.sessionStorage.setItem(RETURN_KEY, JSON.stringify({
                    notebook_id: this.notebookId, href: window.location.pathname,
                    scroll: this.main ? this.main.scrollTop : 0,
                    title: this.view.notebook.title,
                }));
            } catch (err) { /* the trip still works */ }
        }

        restoreReturn() {
            try {
                const note = JSON.parse(window.sessionStorage.getItem(RETURN_KEY) || "null");
                if (note && note.notebook_id === this.notebookId && this.main && note.scroll) {
                    requestAnimationFrame(() => requestAnimationFrame(() => { this.main.scrollTop = note.scroll; }));
                }
            } catch (err) { /* nothing to restore */ }
        }

        go(href) {
            this.flush();
            if (window.PlexoraRouter && typeof window.PlexoraRouter.go === "function") window.PlexoraRouter.go(href);
            else window.location.href = href;
        }

        async viewInViewer(cellId, body) {
            await this.flush();
            try {
                const answer = await API.show(this.notebookId, cellId, body || {});
                this.leaveReturn();
                const query = answer.annotation ? "tool=cell_explorer" : "";
                this.go(this.viewerHref(answer.project, query));
            } catch (err) {
                this.toast(err.message);
            }
        }

        // The same embedding in a window over the tissue: the viewer script
        // (scimapEmbedWindow.js) reads this note and opens it there.
        openEmbedding(cellId, handoff) {
            try {
                window.sessionStorage.setItem("plexora:scimap-embed", JSON.stringify(Object.assign({ open: true }, handoff)));
            } catch (err) { /* the viewer offers it from Cell Explorer too */ }
            this.viewInViewer(cellId, {});
        }

        viewColumn(column, focus) {
            const producer = this.view.notebook.cells.find((c) => {
                const run = Cells().shownRun(c);
                return run && run.effective && run.effective.label === column;
            });
            if (producer) {
                this.viewInViewer(producer.id, { focus });
                return;
            }
            this.toast("That column came with the data; open it in Cell Explorer from the viewer.");
        }

        openViewer(options) {
            const context = this.view.notebook.context;
            const project = context.kind === "dataset" ? (this.lastProject || context.projects[0]) : context.projects[0];
            this.leaveReturn();
            this.go(this.viewerHref(project, options && options.tool ? "tool=" + options.tool : ""));
        }

        async projectPicker(anchor) {
            const context = this.view.notebook.context;
            const items = [{ label: "Open a project in the viewer", heading: true }];
            context.projects.forEach((p) => items.push({ label: p, icon: "image", onSelect: () => {
                this.lastProject = p;
                this.leaveReturn();
                this.go(this.viewerHref(p, ""));
            } }));
            items.push({ sep: true });
            items.push({ label: "Manage dataset", icon: "layers", onSelect: () => this.manageDataset() });
            items.push({ label: "Create dataset", icon: "plus", onSelect: () => this.createDataset() });
            this.menu(anchor, items);
        }

        createDataset() {
            if (window.PlexoraDatasetBuilder && typeof window.PlexoraDatasetBuilder.open === "function") {
                window.PlexoraDatasetBuilder.open();
            } else {
                this.go(this.base.replace(/\/plugins\/scimap$/, "") + "/?tab=datasets");
            }
        }

        manageDataset() {
            const context = this.view.notebook.context;
            if (window.PlexoraOpenProject && typeof window.PlexoraOpenProject.open === "function") {
                window.PlexoraOpenProject.open(context.id);
            } else {
                this.go(this.base.replace(/\/plugins\/scimap$/, "") + "/");
            }
        }

        async pickPlace(anchor, kind) {
            const places = this.places || await API.places();
            this.places = places;
            const items = [];
            if (kind === "project") {
                const withTable = places.projects.filter((p) => p.has_table);
                if (!withTable.length) items.push({ label: "No project has a cell table yet", disabled: true });
                withTable.forEach((p) => items.push({ label: p.name, icon: "image", onSelect: () => this.openContext("project", p.name) }));
            } else {
                if (!places.datasets.length) items.push({ label: "No datasets yet", disabled: true });
                places.datasets.forEach((d) => items.push({ label: d.name, hint: d.projects.length + " projects", icon: "layers",
                    onSelect: () => this.openContext("dataset", d.id) }));
            }
            this.menu(anchor, items, { scroll: true });
        }

        openContext(kind, ref) {
            window.location.href = this.base + "/notebook?" + kind + "=" + encodeURIComponent(ref);
        }

        goNotebook(id) {
            window.location.href = this.base + "/notebook/" + encodeURIComponent(id);
        }

        // -- menus, popovers, dialogs ------------------------------------------------

        closeFloating() {
            if (window.ScimapUi) window.ScimapUi.close();
            this.closeOwnFloating();
        }

        // A menu of the page's own; the shared popovers (scimapUi.js) close it
        // as they open, by the "scimap:float" event.
        closeOwnFloating() {
            if (this.floating) {
                this.floating.remove();
                this.floating = null;
            }
            if (this.floatingAnchor) this.floatingAnchor.classList.remove("is-open");
            this.floatingAnchor = null;
            document.removeEventListener("pointerdown", this.outside, true);
        }

        place(node, anchor) {
            this.root.append(node);
            const r = anchor.getBoundingClientRect();
            const w = node.offsetWidth;
            const hgt = node.offsetHeight;
            let left = Math.min(r.left, window.innerWidth - w - 12);
            if (r.right - w > 12 && r.left + w > window.innerWidth - 12) left = r.right - w;
            let top = r.bottom + 6;
            if (top + hgt > window.innerHeight - 12) top = Math.max(12, r.top - hgt - 6);
            node.style.left = Math.max(12, left) + "px";
            node.style.top = top + "px";
            this.floating = node;
            this.floatingAnchor = anchor;
            anchor.classList.add("is-open");
            this.outside = (e) => {
                if (!node.contains(e.target) && !anchor.contains(e.target)) this.closeFloating();
            };
            setTimeout(() => document.addEventListener("pointerdown", this.outside, true), 0);
        }

        menu(anchor, items, options) {
            if (this.floatingAnchor === anchor) {
                this.closeFloating();
                return;
            }
            this.closeFloating();
            const node = h("div.sm-menu", { role: "menu" });
            if (options && options.scroll) {
                node.style.maxHeight = "360px";
                node.style.overflow = "auto";
            }
            items.forEach((item) => {
                if (item.sep) node.append(h("div.sm-menu-sep"));
                else if (item.heading) node.append(h("div.sm-menu-label", null, item.label));
                else {
                    node.append(h("button.sm-menu-item" + (item.danger ? ".is-danger" : "") + (item.current ? ".is-current" : ""), {
                        type: "button", role: "menuitem", disabled: item.disabled || null,
                        onclick: () => { this.closeFloating(); if (item.onSelect) item.onSelect(); },
                    }, item.icon ? icon(item.icon) : h("span.sm-ic"), h("span", null, item.label),
                    item.hint ? h("span.sm-menu-hint", null, item.hint) : null));
                }
            });
            this.place(node, anchor);
            const first = node.querySelector(".sm-menu-item:not(:disabled)");
            if (first) first.focus({ preventScroll: true });
            node.addEventListener("keydown", (e) => {
                const buttons = Array.from(node.querySelectorAll(".sm-menu-item:not(:disabled)"));
                const i = buttons.indexOf(document.activeElement);
                if (e.key === "ArrowDown") { e.preventDefault(); (buttons[i + 1] || buttons[0]).focus(); }
                if (e.key === "ArrowUp") { e.preventDefault(); (buttons[i - 1] || buttons[buttons.length - 1]).focus(); }
                if (e.key === "Escape") { this.closeFloating(); anchor.focus(); }
            });
        }

        chooseMarkers(anchor, all, chosen, done) {
            this.closeFloating();
            const picked = new Set(chosen);
            const filter = h("input.sm-input", { placeholder: "Filter markers", style: { width: "100%", marginBottom: "8px" } });
            const list = h("div.sm-checklist");
            const paint = () => {
                list.innerHTML = "";
                const q = filter.value.trim().toLowerCase();
                all.filter((m) => !q || m.toLowerCase().includes(q)).forEach((m) => {
                    const box = h("input.sm-check", { type: "checkbox", checked: picked.has(m) || null });
                    box.addEventListener("change", () => { box.checked ? picked.add(m) : picked.delete(m); count.textContent = picked.size + " of " + all.length; });
                    list.append(h("label", null, box, m));
                });
            };
            const count = h("span.sm-form-help", { style: { marginRight: "auto" } }, picked.size + " of " + all.length);
            filter.addEventListener("input", paint);
            paint();
            const node = h("div.sm-popover", { role: "dialog", "aria-label": "Choose markers" }, filter, list,
                h("div.sm-pop-actions", null, count,
                    button("All", { kind: "quiet", onclick: () => { all.forEach((m) => picked.add(m)); paint(); count.textContent = picked.size + " of " + all.length; } }),
                    button("None", { kind: "quiet", onclick: () => { picked.clear(); paint(); count.textContent = "0 of " + all.length; } }),
                    button("Done", { kind: "primary", onclick: () => {
                        this.closeFloating();
                        done(all.filter((m) => picked.has(m)));
                    } })));
            this.place(node, anchor);
            filter.focus();
        }

        dialog(content, options) {
            const o = options || {};
            const scrim = h("div.sm-scrim");
            const box = h("div.sm-dialog", { role: o.alert ? "alertdialog" : "dialog", "aria-modal": "true",
                style: o.width ? { width: o.width + "px" } : null });
            const close = () => {
                scrim.remove();
                document.removeEventListener("keydown", onKey, true);
                if (o.onClose) o.onClose();
            };
            const onKey = (e) => {
                if (e.key === "Escape") { e.stopPropagation(); close(); }
            };
            document.addEventListener("keydown", onKey, true);
            scrim.addEventListener("pointerdown", (e) => { if (e.target === scrim) close(); });
            box.append(...content(close));
            scrim.append(box);
            this.root.append(scrim);
            const focus = box.querySelector("[data-autofocus]") || box.querySelector("button, input");
            if (focus) focus.focus();
            return close;
        }

        // Notebook-style delete: at once, no question, and undoable (Z, or the
        // toast's Undo) for as long as the page is open. The result stays in
        // the working file, so an undone step comes back with its result.
        async deleteStep(cellId) {
            const cells = this.view.notebook.cells;
            const index = cells.findIndex((c) => c.id === cellId);
            const cell = cells[index];
            if (!cell || !Cells().canDelete(cell, this.ctx())) return;
            const kept = JSON.parse(JSON.stringify(cell));
            const answer = await this.op({ op: "remove_cell", id: cellId });
            if (!answer) return;
            this.undoStack = (this.undoStack || []).concat([{ cell: kept, index }]).slice(-20);
            if (this.selectedId === cellId) {
                const next = this.view.notebook.cells[Math.min(index, this.view.notebook.cells.length - 1)];
                this.select(next ? next.id : null);
            }
            this.toast(this.tools[cell.tool].name + " deleted.", { label: "Undo", run: () => this.undoDelete() });
        }

        async undoDelete() {
            const last = (this.undoStack || []).pop();
            if (!last) return;
            const answer = await this.op({ op: "restore_cell", cell: last.cell, index: last.index });
            if (answer) {
                this.select(last.cell.id);
                this.toast(this.tools[last.cell.tool].name + " restored.");
            }
        }

        // The step keyboard commands act on: the last one clicked.
        select(cellId) {
            if (this.selectedId && this.cellNodes.get(this.selectedId)) {
                this.cellNodes.get(this.selectedId).classList.remove("is-selected");
            }
            this.selectedId = cellId;
            const node = cellId ? this.cellNodes.get(cellId) : null;
            if (node) node.classList.add("is-selected");
            this.followStep(cellId, this.pressTarget);
        }

        // The sidebar's notebook overview (no plot chosen) shows the steps as
        // they are now.
        paintOverview() {
            if (this.inspector && window.ScimapViews && !window.ScimapViews.current()) {
                this.inspector.ctx = this.ctx();
                this.inspector.paint();
            }
        }

        // The open sidebar follows the step chosen: its first plot, or the
        // notebook's overview for a step without one.
        followStep(cellId, target) {
            const views = window.ScimapViews;
            if (!this.inspectorOpen || !views) return;
            const cur = views.selected();
            if (cur && cur.cellId === cellId) return;
            // A press on a plot picks that plot itself (scimapViews.js).
            if (target && target.closest && target.closest(".sm-view[data-panel]")) return;
            const first = cellId ? views.firstPanel(cellId) : null;
            views.select(first ? cellId : null, first || null);
            if (!first) this.paintOverview();
        }

        notebookMenu(anchor) {
            const nb = this.view.notebook;
            const items = [{ label: "Notebooks for " + nb.context.name, heading: true }];
            API.notebooks(nb.context.kind, nb.context.id).then((answer) => {
                answer.notebooks.forEach((other) => items.push({ label: other.title, icon: "book", current: other.id === nb.id,
                    hint: other.steps + (other.steps === 1 ? " step" : " steps"), onSelect: () => other.id !== nb.id && this.goNotebook(other.id) }));
                items.push({ sep: true },
                    { label: "New notebook", icon: "plus", onSelect: () => { window.location.href = this.base + "/notebook?" + nb.context.kind + "=" + encodeURIComponent(nb.context.id) + "&new=1"; } },
                    { label: "Rename", icon: "rename", onSelect: () => { this.titleInput.focus(); this.titleInput.select(); } },
                    { label: "Duplicate", icon: "copy", onSelect: async () => {
                        const made = await API.duplicate(nb.id);
                        this.goNotebook(made.notebook_id);
                    } },
                    { label: "Import a .scimap file…", icon: "upload", onSelect: () => this.importFile() },
                    { label: "Export…", icon: "download", onSelect: () => this.exportDialog() },
                    { label: "Change results folder…", icon: "folder", onSelect: () => this.browseFolder(null, nb.results_dir) },
                    { sep: true },
                    { label: "Delete notebook", icon: "trash", danger: true, onSelect: () => this.confirmDeleteNotebook() });
                this.menu(anchor, items, { scroll: true });
            }).catch((err) => this.toast(err.message));
        }

        confirmDeleteNotebook() {
            const nb = this.view.notebook;
            this.dialog((close) => [
                h("div.sm-dialog-head", null, h("div", null, h("h1", null, "Delete “" + nb.title + "”?"))),
                h("div.sm-dialog-body", null, h("p", null, "The notebook's steps and their records go. The working file and the results in it stay in the results folder.")),
                h("div.sm-dialog-foot", null, button("Cancel", { onclick: close }),
                    button("Delete notebook", { kind: "danger", onclick: async () => {
                        close();
                        await API.remove(nb.id);
                        window.location.href = this.base + "/notebook";
                    } })),
            ], { alert: true, width: 460 });
        }

        importFile() {
            const input = h("input", { type: "file", accept: ".scimap,application/zip" });
            input.addEventListener("change", async () => {
                if (!input.files[0]) return;
                try {
                    const ctx = this.view.notebook.context;
                    const made = await API.importFile(input.files[0], ctx.kind, ctx.id);
                    this.goNotebook(made.notebook_id);
                } catch (err) {
                    this.toast(err.message);
                }
            });
            input.click();
        }

        async exportDialog() {
            await this.flush();
            const nb = this.view.notebook;
            let format = "ipynb";
            let include = "workflow";
            let sizes = null;
            API.estimate(nb.id).then((a) => { sizes = a.sizes; paintChoices(); }).catch(() => null);
            const formats = [["ipynb", "Jupyter notebook", ".ipynb", "Rerun it in Jupyter with Scimap Pro."],
                ["py", "Python script", ".py", "Run it anywhere, a cluster included."],
                ["html", "Report", ".html", "Plots' numbers, tables and settings to read. Not a workflow."],
                ["scimap", "Plexora notebook", ".scimap", "Reopen it in Plexora, here or on another computer."]];
            const grid = h("div.sm-formats", { role: "radiogroup", "aria-label": "Format" });
            const choices = h("div");
            const paintFormats = () => {
                grid.innerHTML = "";
                formats.forEach(([id, name, ext, text]) => grid.append(h("button.sm-format", { type: "button", role: "radio",
                    "aria-checked": String(format === id), onclick: () => { format = id; paintFormats(); paintChoices(); } },
                h("strong", null, name + " ", h("code", null, ext)), h("span", null, text))));
            };
            const paintChoices = () => {
                choices.innerHTML = "";
                if (format !== "scimap") return;
                const options = [["workflow", "Workflow only", "Steps and settings. The person opening it needs the same data."],
                    ["results", "Workflow and results", "Adds the result columns. Contains per-cell labels."],
                    ["data", "Workflow, results and cell data", "Adds the working AnnData. Contains every cell's intensities and position."]];
                choices.append(h("span.sm-section-label", { style: { display: "block", marginBottom: "6px" } }, "What to include"),
                    h("div.sm-choices", { role: "radiogroup", "aria-label": "What to include" }, options.map(([id, title, text]) => {
                        const radio = h("input", { type: "radio", name: "sm_include", checked: include === id || null });
                        radio.addEventListener("change", () => { include = id; paintChoices(); });
                        return h("label" + (include === id ? ".is-on" : ""), null, radio,
                            h("span.sm-option-text", null, h("strong", null, title), h("span", null, text)),
                            h("span.sm-size", null, sizes ? bytes(sizes[id]) : ""));
                    })));
            };
            paintFormats();
            paintChoices();
            const done = nb.cells.filter((c) => Cells().shownRun(c)).length;
            this.dialog((close) => [
                h("div.sm-dialog-head", null,
                    h("div", null, h("h1", null, "Export “" + nb.title + "”"),
                        h("p", null, done + (done === 1 ? " completed step" : " completed steps") + " · every setting and seed goes with it")),
                    iconButton("x", "Close", close)),
                h("div.sm-dialog-body", null, grid, choices,
                    h("div.sm-two", null,
                        h("div", null, h("strong", null, "Always included"),
                            h("span", null, "Settings and seeds, annotation mappings, and the Scimap Pro version each step ran with.")),
                        h("div", null, h("strong", null, "Never included"),
                            h("span", null, "Images and masks, license keys, server addresses and passwords, paths on this computer.")))),
                h("div.sm-dialog-foot", null,
                    h("span.sm-foot-note", null, "Scripts read the cell table from a data folder beside them."),
                    button("Cancel", { onclick: close }),
                    button("Export", { kind: "primary", icon: "download", onclick: () => {
                        close();
                        const a = document.createElement("a");
                        a.href = API.exportUrl(nb.id, format, include);
                        a.download = "";
                        document.body.appendChild(a);
                        a.click();
                        a.remove();
                    } })),
            ], { width: 600 });
        }

        licenceDialog() {
            const lic = this.status.licence;
            const key = h("input.sm-input.is-mono", { placeholder: "BIOC-XXXX-XXXX-XXXX-XXXX", style: { flex: "1" }, "data-autofocus": "" });
            const message = h("p", { style: { color: "var(--sm-fail-ink)", fontSize: "12px" }, hidden: true });
            const fail = (err) => { message.textContent = err.message; message.hidden = false; };
            const succeed = (close, state) => {
                close();
                this.status.licence = state;
                store(STARTER_KEY, null);
                this.toast(state.licensed ? "Scimap Pro is licensed on this computer." : "License added.");
                this.openNotebook();
            };
            const file = h("input", { type: "file", accept: ".bioc,.txt", hidden: true });
            this.dialog((close) => {
                file.addEventListener("change", async () => {
                    if (!file.files[0]) return;
                    try { succeed(close, (await API.installLicence(file.files[0])).licence); } catch (err) { fail(err); }
                });
                const connectBox = h("div.sm-option", null,
                    h("span.sm-option-text", null, h("strong", null, "Sign in with your BioCognia account"),
                        h("span", null, "Opens your browser; finds the seat your account holds.")),
                    button("Connect this computer", { kind: "primary", onclick: async () => {
                        try {
                            const started = await API.connect();
                            window.open(started.verify_url, "_blank", "noopener");
                            connectBox.replaceChildren(h("span.sm-option-text", null, h("strong", null, "Approve this code in your browser"),
                                h("span", null, "Waiting for the approval…")), h("span.sm-code-box", null, started.code));
                            const poll = async () => {
                                try {
                                    const answer = await API.connectPoll(started.code);
                                    if (answer.pending) setTimeout(poll, (answer.retry_after || started.interval) * 1000);
                                    else succeed(close, answer.license);
                                } catch (err) { fail(err); }
                            };
                            setTimeout(poll, started.interval * 1000);
                        } catch (err) { fail(err); }
                    } }));
                return [
                    h("div.sm-dialog-head", null,
                        h("div", null, h("h1", null, "Add a Scimap Pro license"),
                            h("p", null, "Any one of these works. SCIMAP unlocks as soon as the license checks out; no restart.")),
                        iconButton("x", "Close", close)),
                    h("div.sm-dialog-body", null, connectBox,
                        h("div.sm-option", { style: { flexDirection: "column", alignItems: "stretch", gap: "6px" } },
                            h("strong", { style: { fontSize: "13px", color: "var(--sm-ink)" } }, "Paste a license key"),
                            h("div", { style: { display: "flex", gap: "8px" } }, key,
                                button("Activate", { onclick: async () => {
                                    try { succeed(close, (await API.activate(key.value)).licence); } catch (err) { fail(err); }
                                } })),
                            h("span.sm-form-help", null, "The key from your account's Licenses page or the email that came with your seat.")),
                        h("div.sm-option.is-dashed", null, icon("file"),
                            h("span.sm-option-text", null, h("strong", null, "Use a license file"),
                                h("span", null, "For a computer without internet: choose the .bioc file.")),
                            button("Choose file…", { onclick: () => file.click() }), file),
                        message),
                    h("div.sm-dialog-foot", null, icon("lock", "sm"),
                        h("span.sm-foot-note", null, "Scimap Pro keeps the license on this computer. It never goes into notebooks, exports or logs."),
                        h("a.sm-btn-link", { href: lic.purchase_url, target: "_blank", rel: "noopener" }, "Purchase license ↗")),
                ];
            }, { width: 520 });
        }

        startTrial() {
            window.open(this.status.licence.trial_url, "_blank", "noopener");
            this.licenceDialog();
        }

        async browseFolder(cellId, start) {
            const picker = window.PlexoraPathPicker || window.PlexoraFileExplorer;
            if (!picker || typeof picker.pick !== "function") {
                this.toast("Type the folder's path into the field.");
                return;
            }
            const chosen = await picker.pick({ mode: "directory", title: "Where should the results go?", start: start || "" });
            if (!chosen) return;
            const path = typeof chosen === "string" ? chosen : (chosen.path || chosen[0]);
            if (path) this.setResultsDir(cellId, path);
        }

        async setResultsDir(cellId, path) {
            const answer = await this.op({ op: "set_results_dir", path });
            if (answer && cellId) this.rerender(cellId);
        }

        async sendToFigure(anchor, cell, entry) {
            const root = this.base.replace(/\/plugins\/scimap$/, "") + "/plugins/figure_builder";
            let figures = [];
            try {
                const response = await fetch(root + "/api/figures");
                if (!response.ok) throw new Error("Figure Builder is not available.");
                figures = (await response.json()).figures || [];
            } catch (err) {
                this.toast("Figure Builder is not available here.");
                return;
            }
            const send = async (figureId, title) => {
                let picked = entry;
                if (!picked) {
                    const run = Cells().shownRun(cell);
                    const figs = run ? await this.figures(cell.id, run.run_id).catch(() => null) : null;
                    picked = ((figs && figs.figures) || []).find((f) => !f.error);
                }
                if (!picked) {
                    this.toast("This step has no figure to send.");
                    return;
                }
                let blob, name;
                try {
                    ({ blob, name } = await this.figureBlob(cell, picked, "png"));
                } catch (err) {
                    this.toast(err.message);
                    return;
                }
                const response = await fetch(root + "/api/figures/" + encodeURIComponent(figureId) + "/assets?filename=" + encodeURIComponent(name),
                    { method: "POST", body: blob });
                if (!response.ok) {
                    this.toast("Figure Builder refused the picture.");
                    return;
                }
                this.toast("Sent to " + title + ". It is in the figure's Assets.");
            };
            const items = [{ label: "Send the picture to a figure", heading: true }];
            figures.filter((f) => f.readable !== false).slice(0, 12).forEach((f) => items.push({ label: f.title, icon: "figure",
                onSelect: () => send(f.figure_id, f.title) }));
            items.push({ sep: true }, { label: "New figure", icon: "plus", onSelect: async () => {
                const response = await fetch(root + "/api/figures", { method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ title: this.view.notebook.title }) });
                const made = await response.json();
                if (made.figure_id) send(made.figure_id, this.view.notebook.title);
            } });
            this.menu(anchor, items, { scroll: true });
        }


        toast(text, action) {
            if (this.toastNode) this.toastNode.remove();
            const node = h("div.sm-toast", { role: "status" }, h("span", null, text),
                action ? h("button.sm-toast-action", { type: "button", onclick: () => {
                    node.remove();
                    action.run();
                } }, action.label) : null);
            this.root.append(node);
            this.toastNode = node;
            clearTimeout(this.toastTimer);
            this.toastTimer = setTimeout(() => node.remove(), 4200);
        }

        key(e) {
            if (!this.view || !this.sidebar) return;
            // A popover's keys are its own (scimapUi.js: arrows, Space, Undo).
            if (e.target && e.target.closest && e.target.closest(".sm-ui-pop")) return;
            const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target && e.target.tagName) || "") ||
                Boolean(e.target && e.target.isContentEditable);
            const plain = !typing && !e.metaKey && !e.ctrlKey && !e.altKey;
            // ] the plot sidebar, [ the tools, 1-4 the selected plot's views.
            // Inside the embedding [ and ] size its brush, so they stay there.
            const inEmbed = Boolean(e.target && e.target.closest && e.target.closest(".sm-emb"));
            if (plain && !inEmbed && e.key === "]") { e.preventDefault(); this.toggleInspector(); return; }
            if (plain && !inEmbed && e.key === "[") { e.preventDefault(); this.toggleSidebar(); return; }
            if (plain && /^[1-4]$/.test(e.key) && window.ScimapViews && window.ScimapViews.nth(Number(e.key) - 1)) {
                e.preventDefault();
                return;
            }
            if (e.key === "Escape" && this.inspectorOpen && this.bodyNode && this.bodyNode.classList.contains("is-narrow") &&
                !document.querySelector(".sm-menu, .sm-vpick, .sm-ui-pop, .sm-dialog")) {
                this.closeInspector();
                return;
            }
            if (e.key === "/" && !typing && !e.metaKey && !e.ctrlKey) {
                e.preventDefault();
                if (this.collapsed) this.toggleSidebar();
                if (this.searchInput) this.searchInput.focus();
                return;
            }
            // Jupyter's command mode: D D deletes the selected step, Z undoes.
            if (!typing && !e.metaKey && !e.ctrlKey && !e.altKey && (e.key === "d" || e.key === "D")) {
                const now = Date.now();
                if (this.selectedId && this.lastD && now - this.lastD < 700) {
                    e.preventDefault();
                    this.lastD = 0;
                    this.deleteStep(this.selectedId);
                } else {
                    this.lastD = now;
                }
                return;
            }
            if (!typing && !e.metaKey && !e.ctrlKey && !e.altKey && (e.key === "z" || e.key === "Z") && (this.undoStack || []).length) {
                e.preventDefault();
                this.undoDelete();
                return;
            }
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                const node = e.target && e.target.closest && e.target.closest("[data-cell]");
                const cells = this.view.notebook.cells;
                const target = node ? cells.find((c) => c.id === node.dataset.cell)
                    : cells.slice().reverse().find((c) => c.state === "idle");
                if (target) {
                    e.preventDefault();
                    this.run(target.id);
                }
            }
        }
    }

    function boot() {
        const root = document.getElementById("sm_workspace");
        if (!root || root.dataset.booted === "1") return null;
        root.dataset.booted = "1";
        const workspace = new Workspace(root);
        workspace.boot();
        window.__scimap = workspace;
        return workspace;
    }

    return { boot, Workspace };
})();

if (typeof PlexoraPage !== "undefined") {
    PlexoraPage.register(() => {
        const workspace = window.ScimapWorkspace.boot();
        return workspace ? () => workspace.destroy() : null;
    });
} else if (typeof document !== "undefined") {
    document.addEventListener("DOMContentLoaded", () => window.ScimapWorkspace.boot());
}
