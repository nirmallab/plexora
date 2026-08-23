/**
 * FigureWorkspace - the chrome around the canvas.
 *
 * Pages, zoom, the tray, the inspector, the alignment bar and the save status.
 * The canvas itself is FigureCanvas; this file is what surrounds it, and the
 * split is where it is because the two appear in two places with different
 * amounts of room and the canvas is the half that is identical in both.
 *
 * ## One controller, two homes
 *
 * The same markup is rendered on a figure's own page and into the viewer's
 * split slot, and they never coexist -- so the ids are the same and this drives
 * either. What differs is where the document comes from:
 *
 *   standalone   this creates the FigureDocumentState and owns it
 *   split        the sidebar controller already has one, and passes it in
 *
 * That second case is not a convenience. Two FigureDocumentStates on one figure
 * in one tab would hold two revisions of it and conflict with EACH OTHER --
 * every save from one making the other stale, in the same window, with the user
 * doing nothing wrong. One document, one revision, one save queue.
 */
class FigureWorkspace {

    /** CSS pixels per millimetre at 100%. 96 dpi / 25.4 mm per inch. */
    static get PX_PER_MM() { return 96 / 25.4; }

    constructor(options) {
        this.api = options.api || new FigureBuilderApi();
        this.figureId = options.figureId;
        //: Passed in when something else already owns the document -- see the
        //: class comment on why sharing it is a correctness matter.
        this.state = options.state
            || new FigureDocumentState({ api: this.api, figureId: this.figureId });
        this.ownsState = !options.state;
        this.onEditPanel = options.onEditPanel || ((panelId) => this.editPanel(panelId));

        this.root = document.getElementById("fb_workspace");
        this.canvas = null;
    }

    static boot() {
        const root = document.getElementById("fb_workspace");
        // An empty data-figure-id is the split panel, whose figure is chosen in
        // the sidebar. Booting it here would open "" and show an error for a
        // panel that is working exactly as intended.
        if (!root || !root.dataset.figureId) return null;
        const workspace = new FigureWorkspace({ figureId: root.dataset.figureId });
        workspace.setup();
        workspace.state.load();
        return workspace;
    }

    el(id) {
        return document.getElementById(id);
    }

    setup() {
        if (!this.root) return;
        this.root.dataset.figureId = this.figureId;

        this.canvas = new FigureCanvas({
            state: this.state,
            api: this.api,
            figureId: this.figureId,
            pageEl: this.el("fb_page"),
            surfaceEl: this.el("fb_page_surface"),
            guideEl: this.el("fb_page_guides"),
            onEditPanel: (panelId) => this.onEditPanel(panelId),
            onSelectionChange: (ids) => this.renderInspector(ids),
        });
        this.canvas.setup();

        this.state.on("change", () => this.render());
        this.state.on("status", (payload) => this.renderStatus(payload));

        this.el("fb_title")?.addEventListener("change", () => this.commitTitle());
        this.el("fb_title")?.addEventListener("keydown", (event) => {
            // Enter commits and gets out of the way; blur commits too, so a
            // title typed and then clicked away from is not silently discarded.
            if (event.key === "Enter") event.target.blur();
            if (event.key === "Escape") {
                event.target.value = this.state.title;
                event.target.blur();
            }
        });

        this.el("fb_page_select")?.addEventListener("change", (event) => {
            this.canvas.setPage(event.target.value);
        });
        this.el("fb_page_add")?.addEventListener("click", () => this.addPage());
        this.el("fb_page_remove")?.addEventListener("click", () => this.removePage());

        this.el("fb_zoom_in")?.addEventListener("click", () => this.canvas.setScale(this.canvas.scale * 1.25));
        this.el("fb_zoom_out")?.addEventListener("click", () => this.canvas.setScale(this.canvas.scale * 0.8));
        this.el("fb_zoom_fit")?.addEventListener("click", () => this.zoomToFit());

        this.el("fb_conflict_reload")?.addEventListener("click", () => window.location.reload());

        this.el("fb_tray_collapse")?.addEventListener("click", (event) => {
            const button = event.currentTarget;
            const open = button.getAttribute("aria-expanded") !== "true";
            button.setAttribute("aria-expanded", String(open));
            this.root.classList.toggle("fb-tray-collapsed", !open);
        });

        this.el("fb_arrange_bar")?.addEventListener("click", (event) => {
            const command = event.target.closest("[data-arrange]")?.dataset.arrange;
            if (command) this.canvas.arrange(command);
        });

        // Delegated: the tray is re-rendered on every document change, so
        // handlers bound to its items would be rebound continuously and leak
        // the ones that were replaced.
        this.el("fb_tray_strip")?.addEventListener("dragstart", (event) => {
            const item = event.target.closest(".fb-tray-item");
            if (!item) return;
            event.dataTransfer.setData("text/x-plexora-panel", item.dataset.panelId);
            event.dataTransfer.effectAllowed = "move";
        });
        this.el("fb_tray_strip")?.addEventListener("dblclick", (event) => {
            const item = event.target.closest(".fb-tray-item");
            if (item) this.placeFromTray(item.dataset.panelId);
        });

        this.el("fb_inspector")?.addEventListener("input", (event) => this.inspectorChanged(event));
        this.el("fb_inspector")?.addEventListener("change", (event) => this.inspectorChanged(event));
        this.el("fb_inspector")?.addEventListener("click", (event) => {
            if (event.target.closest("#fb_edit_view")) {
                this.onEditPanel(Array.from(this.canvas.selection)[0]);
                return;
            }
            const refresh = event.target.closest("#fb_refresh_source");
            if (refresh) this.acceptChangedSource(refresh.dataset.sourceId);
        });

        this.el("fb_split_with")?.addEventListener("click", () => this.split("with_composite"));
        this.el("fb_split_only")?.addEventListener("click", () => this.split("channels_only"));

        // Dropping an image onto the page imports it into THIS FIGURE and
        // nowhere else. Making the user create a project to put a schematic in
        // a figure is exactly the setup step this plugin exists to remove.
        const scroll = this.el("fb_canvas_scroll");
        scroll?.addEventListener("dragover", (event) => {
            if (event.dataTransfer?.types.includes("Files")) event.preventDefault();
        });
        scroll?.addEventListener("drop", (event) => this.dropFiles(event));

        this.exportUi = new FigureExportUi({
            api: this.api, figureId: this.figureId, state: this.state,
        });
        this.exportUi.setup();

        // Undo and redo are the application's, not this panel's: there is one
        // Undo, and a second that only worked here would be a second answer to
        // the same keystroke.
        this._onKey = (event) => this.keyDown(event);
        window.addEventListener("keydown", this._onKey);
    }

    destroy() {
        window.removeEventListener("keydown", this._onKey);
        this.canvas?.destroy();
    }

    keyDown(event) {
        if (!(event.metaKey || event.ctrlKey)) return;
        const typing = document.activeElement
            && ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName);
        if (typing) return;
        if (event.key === "z" && !event.shiftKey) {
            event.preventDefault();
            this.state.undo();
        } else if ((event.key === "z" && event.shiftKey) || event.key === "y") {
            event.preventDefault();
            this.state.redo();
        }
    }

    // -- editing ---------------------------------------------------------

    commitTitle() {
        const input = this.el("fb_title");
        const title = (input.value || "").trim();
        if (!title || title === this.state.title) {
            input.value = this.state.title;
            return;
        }
        this.state.commit([{ op: "set_meta", changes: { title: title } }],
            (draft) => { draft.title = title; });
    }

    addPage() {
        const template = this.state.pages[this.state.pages.length - 1];
        // A new page copies the last one's size rather than defaulting to A4: a
        // figure whose pages are not all the same size exports as a document
        // nobody meant to make.
        const page = {
            page_id: FigureSchema.newPageId(),
            name: "Page " + (this.state.pages.length + 1),
            preset: template ? template.preset : "a4",
            orientation: template ? template.orientation : "portrait",
            size_mm: template ? { ...template.size_mm } : { w: 210, h: 297 },
            margins_mm: template ? { ...template.margins_mm } : undefined,
            background: template ? template.background : "#ffffff",
        };
        this.state.commit([{ op: "add_page", page: page }],
            (draft) => { draft.pages.push(page); });
        this.canvas.pageId = page.page_id;
    }

    removePage() {
        if (this.state.pages.length <= 1) {
            window.alert("A figure needs at least one page.");
            return;
        }
        const pageId = this.canvas.pageId;
        const panels = FigureSchema.panelsOnPage(this.state.document, pageId);
        // Never silently orphaned. The safe answer -- keep them, unplaced -- is
        // what Cancel does, because a captured scene may be the only record of a
        // view somebody spent an hour finding.
        const destroy = panels.length
            ? window.confirm(
                `This page holds ${FigureSchema.countPhrase(panels.length, "panel")}.\n\n`
                + "OK deletes them with the page. Cancel keeps them in the tray.")
            : false;

        this.state.commit(
            [{ op: "remove_page", page_id: pageId, panels: destroy ? "delete" : "tray" }],
            (draft) => {
                draft.pages = draft.pages.filter((page) => page.page_id !== pageId);
                panels.forEach((panel) => {
                    if (destroy) delete draft.panels[panel.panel_id];
                    else draft.panels[panel.panel_id].placement = null;
                });
                Object.keys(draft.annotations).forEach((id) => {
                    if (draft.annotations[id].page_id === pageId) delete draft.annotations[id];
                });
            });
        this.canvas.pageId = null;
    }

    /** Drop a tray panel onto the page without dragging it, for the keyboard
     *  and for a tray that is too narrow to drag out of comfortably. */
    placeFromTray(panelId) {
        const panel = this.state.panel(panelId);
        const page = this.canvas.page;
        if (!panel || !page) return;
        const aspect = panel.scene.viewport.h / panel.scene.viewport.w || 1;
        const width = Math.min(60, page.size_mm.w / 3);
        const placement = {
            page_id: page.page_id,
            x_mm: page.margins_mm.left,
            y_mm: page.margins_mm.top,
            w_mm: width,
            h_mm: width * aspect,
            z: this.canvas.nextZ(),
        };
        this.state.commit(
            [{ op: "move_panels", moves: [{ panel_id: panelId, placement: placement }] }],
            (draft) => { draft.panels[panelId].placement = placement; });
    }

    inspectorChanged(event) {
        const field = event.target.dataset?.field;
        const panelId = Array.from(this.canvas.selection)[0];
        if (!field || !panelId) return;
        const panel = this.state.panel(panelId);
        if (!panel) return;

        const changes = {};
        if (field === "title") changes.title = event.target.value;
        else if (field === "label") {
            // Typing a label makes it the user's; it stops renumbering when the
            // page is rearranged, which is the whole difference between the two.
            changes.label = { ...panel.label, text: event.target.value, auto: false };
        } else if (field === "label_auto") {
            changes.label = { ...panel.label, auto: event.target.checked };
        } else if (field === "scalebar") {
            changes.scalebar = { ...panel.scalebar, visible: event.target.checked };
        } else if (field === "legend_channels") {
            changes.legend = { ...panel.legend, channels: event.target.checked };
        } else if (field === "legend_plugins") {
            changes.legend = { ...panel.legend, plugins: event.target.checked };
        } else return;

        this.state.commit(
            [{ op: "update_panel", panel_id: panelId, changes: changes }],
            (draft) => { Object.assign(draft.panels[panelId], changes); });
    }

    /**
     * Reopen the view a panel was captured from.
     *
     * Overridden in the split panel, where the viewer is already on screen. On
     * the standalone page there is no viewer at all, so this navigates -- with
     * the request left in sessionStorage for the page that lands to pick up.
     */
    editPanel(panelId) {
        const panel = this.state.panel(panelId);
        const source = panel && this.state.source(panel.source_id);
        if (!source || source.kind !== "plexora_project" || !source.datasource) {
            window.alert("This panel has no project image to reopen.");
            return;
        }
        try {
            window.sessionStorage.setItem("plexora:figure-builder-pending",
                JSON.stringify({ figure_id: this.figureId, panel_id: panelId }));
        } catch (error) {
            /* Private-browsing modes throw; the navigation is still worth doing. */
        }
        window.location.href = this.api.url(encodeURIComponent(source.datasource))
            + "?tool=figure_builder";
    }

    /**
     * Record that the user has looked at a changed source and accepted it.
     *
     * Only the fingerprint moves. No panel is re-rendered and no captured scene
     * is touched -- accepting the new image is a statement about what the
     * warning should say from now on, not a decision to redraw a figure from
     * data that has changed underneath it. Re-rendering a panel is done by
     * reopening it, deliberately, one panel at a time.
     */
    async acceptChangedSource(sourceId) {
        const source = this.state.source(sourceId);
        if (!source || !source.datasource) return;
        const described = await this.api.describeSource(source.datasource);
        if (!described.ok) return;

        const changes = {
            image: described.data.source.image,
            channels: described.data.source.channels,
            fingerprint: described.data.source.fingerprint,
            status: "ok",
        };
        await this.state.commit(
            [{ op: "update_source", source_id: sourceId, changes: changes }],
            (draft) => { Object.assign(draft.sources[sourceId], changes); });
        // The status the inspector shows is computed by the server on read, so
        // the local copy has to be told too or the badge stays until a reload.
        this.state.sourceStatus[sourceId] = { status: "ok", reasons: [] };
        this.render();
    }

    split(mode) {
        const panelId = Array.from(this.canvas.selection)[0];
        if (panelId) this.canvas.splitComposite(panelId, mode);
    }

    /**
     * Files dropped onto the page.
     *
     * Figure-only by design: a schematic or a supporting RGB image is not a
     * project, and the bytes land in this figure's own directory. The panel
     * arrives at the image's own aspect ratio -- landing everything square and
     * making the user fix it afterwards is squashed content waiting to be
     * exported.
     */
    async dropFiles(event) {
        const files = Array.from(event.dataTransfer?.files || []);
        if (!files.length) return;
        event.preventDefault();

        const page = this.canvas.page;
        if (!page) return;
        const task = window.PlexoraStatus?.begin("Importing");
        for (const file of files) {
            const uploaded = await this.api.addAsset(this.figureId, file.name, file);
            if (!uploaded.ok) {
                task?.fail(uploaded.data.error || "That file could not be imported");
                return;
            }
            await this.placeAsset(uploaded.data, page, file);
        }
        task?.done();
    }

    async placeAsset(asset, page, file) {
        const dimensions = await this.imageSize(file);
        const sourceId = FigureSchema.newSourceId();
        const panelId = FigureSchema.newPanelId();
        const width = Math.min(60, page.size_mm.w / 3);
        const aspect = dimensions.height / dimensions.width || 1;

        const source = {
            source_id: sourceId, kind: "imported_asset", asset_id: asset.asset_id,
            display_name: asset.filename,
            image: { width: dimensions.width, height: dimensions.height },
            // No calibration, and none invented: an imported PNG has no
            // physical scale, so its panels have no scale bar until somebody
            // types one in.
            pixel_size: null, channels: [], status: "ok",
        };
        const panel = {
            panel_id: panelId, source_id: sourceId,
            scene: { ...FigureSchema.emptyScene(sourceId),
                     viewport: { x: 0, y: 0, w: dimensions.width, h: dimensions.height } },
            placement: { page_id: page.page_id, x_mm: page.margins_mm.left,
                         y_mm: page.margins_mm.top, w_mm: width, h_mm: width * aspect,
                         z: this.canvas.nextZ() },
            title: asset.filename, label: { text: "", auto: true, visible: true },
            scalebar: { visible: false, target_um: null },
            legend: { channels: false, plugins: false }, render_revision: 1,
        };
        await this.state.commit(
            [{ op: "add_source", source: source }, { op: "add_panel", panel: panel }],
            (draft) => {
                draft.sources[sourceId] = source;
                draft.panels[panelId] = panel;
            });
    }

    imageSize(file) {
        return new Promise((resolve) => {
            const url = URL.createObjectURL(file);
            const image = new Image();
            image.onload = () => {
                resolve({ width: image.naturalWidth || 1, height: image.naturalHeight || 1 });
                URL.revokeObjectURL(url);
            };
            // A TIFF the browser cannot decode still imports; it simply lands
            // square until the user resizes it, which is better than refusing
            // a file the export renderer can read perfectly well.
            image.onerror = () => {
                resolve({ width: 1, height: 1 });
                URL.revokeObjectURL(url);
            };
            image.src = url;
        });
    }

    zoomToFit() {
        this.canvas.zoomToFit(this.el("fb_canvas_scroll"));
        this.renderZoom();
    }

    // -- rendering -------------------------------------------------------

    render() {
        if (!this.state.document || !this.root) return;
        const title = this.el("fb_title");
        if (title && title !== document.activeElement) title.value = this.state.title;

        this.renderPageList();
        this.canvas.render();
        this.renderZoom();
        this.renderTray();
        this.renderInspector(Array.from(this.canvas.selection));
    }

    renderPageList() {
        const select = this.el("fb_page_select");
        const pages = this.state.pages;
        if (!pages.some((page) => page.page_id === this.canvas.pageId)) {
            this.canvas.pageId = pages.length ? pages[0].page_id : null;
        }
        if (!select) return;
        select.innerHTML = pages.map((page) =>
            `<option value="${FigureSchema.escapeHtml(page.page_id)}">${FigureSchema.escapeHtml(page.name)}</option>`
        ).join("");
        select.value = this.canvas.pageId || "";
    }

    renderZoom() {
        const readout = this.el("fb_zoom_readout");
        if (readout) {
            readout.textContent =
                Math.round((this.canvas.scale / FigureWorkspace.PX_PER_MM) * 100) + "%";
        }
    }

    renderTray() {
        const panels = FigureSchema.panelsInTray(this.state.document);
        const count = this.el("fb_tray_count");
        if (count) {
            count.textContent = panels.length
                ? FigureSchema.countPhrase(panels.length, "panel") + " waiting — drag one onto the page"
                : "Captured panels land here.";
        }
        const strip = this.el("fb_tray_strip");
        if (!strip) return;
        strip.innerHTML = panels.map((panel) => {
            const source = this.state.source(panel.source_id);
            const span = FigureSchema.physicalWidthUm(source, panel.scene.viewport);
            const caption = span ? FigureSchema.formatMicrons(span) + " wide" : "";
            return `<div class="fb-tray-item" draggable="true"
                         data-panel-id="${FigureSchema.escapeHtml(panel.panel_id)}"
                         title="${FigureSchema.escapeHtml(caption)}">
                <img src="${this.api.previewUrl(this.figureId, panel.panel_id, panel.render_revision)}"
                     alt="" draggable="false" loading="lazy">
            </div>`;
        }).join("");
    }

    renderInspector(ids) {
        const empty = this.el("fb_inspector_empty");
        const body = this.el("fb_inspector_body");
        const bar = this.el("fb_arrange_bar");
        const count = this.el("fb_selection_count");

        if (bar) bar.hidden = ids.length < 2;
        if (count) count.textContent = FigureSchema.countPhrase(ids.length, "panel") + " selected";
        if (!body || !empty) return;

        const splitControls = this.el("fb_split_controls");
        if (ids.length !== 1) {
            empty.hidden = false;
            body.hidden = true;
            if (splitControls) splitControls.hidden = true;
            return;
        }
        const panel = this.state.panel(ids[0]);
        if (!panel) {
            empty.hidden = false;
            body.hidden = true;
            if (splitControls) splitControls.hidden = true;
            return;
        }
        empty.hidden = true;
        body.hidden = false;

        const source = this.state.source(panel.source_id);
        const status = this.state.sourceStatus[panel.source_id]?.status || "ok";
        const span = FigureSchema.physicalWidthUm(source, panel.scene.viewport);
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);

        // Only offered where it means something. A split control on a
        // single-channel panel is a button with nothing to do, which reads as
        // broken rather than as not applicable.
        const split = this.el("fb_split_controls");
        if (split) split.hidden = (panel.scene.channels || []).length < 2;

        body.innerHTML = `
            <label class="control-label" for="fb_field_title">Title</label>
            <input id="fb_field_title" class="fb-input" type="text" data-field="title"
                   value="${escape(panel.title)}" maxlength="200">

            <label class="control-label" for="fb_field_label">Label</label>
            <div class="fb-figure-row">
                <input id="fb_field_label" class="fb-input" type="text" data-field="label"
                       value="${escape(panel.label.auto ? "" : panel.label.text)}"
                       placeholder="automatic" maxlength="8">
                <label class="fb-check" title="Renumber this label when panels are rearranged">
                    <input type="checkbox" data-field="label_auto" ${panel.label.auto ? "checked" : ""}>
                    Auto
                </label>
            </div>

            <label class="fb-check">
                <input type="checkbox" data-field="scalebar" ${panel.scalebar.visible ? "checked" : ""}
                       ${span ? "" : "disabled"}>
                Scale bar
            </label>
            ${span
                ? `<div class="fb-muted">${escape(FigureSchema.formatMicrons(span))} across</div>`
                : `<div class="fb-muted">Scale information unavailable for this image.</div>`}

            <label class="fb-check">
                <input type="checkbox" data-field="legend_channels" ${panel.legend.channels ? "checked" : ""}>
                Channel legend
            </label>
            <label class="fb-check">
                <input type="checkbox" data-field="legend_plugins" ${panel.legend.plugins ? "checked" : ""}>
                Overlay legend
            </label>

            <div class="fb-inspector-source">
                <div class="fb-muted">${escape(source ? (source.display_name || source.datasource) : "no source")}</div>
                ${status !== "ok"
                    ? `<div class="fb-banner-detail">This panel's source has
                           ${status === "missing" ? "gone" : "changed"} since it was captured.
                           The panel still shows what was captured; nothing has been
                           re-rendered.</div>
                       ${status === "changed"
                           ? `<button type="button" id="fb_refresh_source"
                                      class="sidebar-action secondary"
                                      data-source-id="${escape(panel.source_id)}">
                                  Accept the new image
                              </button>` : ""}`
                    : ""}
                <button type="button" id="fb_edit_view" class="sidebar-action secondary">
                    <span class="fas fa-arrow-up-right-from-square"></span> Edit view
                </button>
            </div>`;
    }

    renderStatus(payload) {
        const status = this.el("fb_save_status");
        if (status) {
            status.textContent = {
                loading: "Opening…",
                saving: "Saving…",
                saved: "Saved",
                unsaved: "Unsaved changes",
                failed: payload.detail || "Save failed",
                conflict: "Changed elsewhere",
                unreadable: "Cannot be opened",
            }[payload.status] || "";
            status.className = "fb-save-status fb-status-" + payload.status;
        }
        const conflict = this.el("fb_conflict_banner");
        if (conflict) conflict.hidden = payload.status !== "conflict";
        const unreadable = this.el("fb_unreadable_banner");
        if (unreadable) {
            unreadable.hidden = payload.status !== "unreadable";
            const detail = this.el("fb_unreadable_detail");
            if (detail) detail.textContent = payload.detail || "";
        }
    }
}

if (typeof document !== "undefined" && document.addEventListener) {
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", () => FigureWorkspace.boot());
    } else {
        FigureWorkspace.boot();
    }
}
