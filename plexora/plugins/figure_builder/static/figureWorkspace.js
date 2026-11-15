/**
 * FigureWorkspace - the chrome around the canvas.
 *
 * The topbar, the tool rail, the panel tray, the pages and the save status. The
 * canvas itself is FigureCanvas; this file is what surrounds it.
 *
 * ## The canvas is the workspace
 *
 * This used to be rendered in two places -- a figure's own page, and a split
 * pane beside the live viewer -- and the split pane has gone. Composing a
 * figure and looking down a microscope are different activities, and half a
 * window was not enough room for either. So there is one home now, and it gets
 * the whole page.
 *
 * The `options.state` seam is kept even though nothing shares a document today,
 * because the rule it protects has not changed: two FigureDocumentStates on one
 * figure in one tab would hold two revisions of it and conflict with EACH OTHER
 * -- every save from one making the other stale, in the same window, with the
 * user doing nothing wrong. Anything that wants to edit this figure alongside
 * the canvas (Quick Edit) is handed THIS state rather than opening its own.
 *
 * ## Nothing permanent that acts on a selection
 *
 * There is no properties column. Every control that acts on what is selected
 * lives in FigureContextBar, which appears beside the selection and nowhere
 * else; every control that is rarely wanted lives in a menu. What is permanent
 * is only what is always true: which figure this is, which page, how big, and
 * whether it is saved.
 */
class FigureWorkspace {

    /** CSS pixels per millimetre at 100%. 96 dpi / 25.4 mm per inch. */
    static get PX_PER_MM() { return 96 / 25.4; }

    /** How long the tray has to be before finding something in it is work.
     *  Lower than it was: the tiles are two across and their names are behind a
     *  hover, so scanning for one by eye stops working sooner. */
    static get SEARCH_THRESHOLD() { return 8; }

    /** Where the viewer says it sent the user here from. Written by
     *  figureSidebarController, read by the back arrow, and per-tab: two tabs
     *  can be on two figures reached two different ways. */
    static get ORIGIN_KEY() { return "plexora:figure-builder-origin"; }

    /** How long a toast stays. Long enough to read and act on, short enough not
     *  to sit over the figure. */
    static get TOAST_MS() { return 9000; }

    /** How long after a save settles before the figure's thumbnail is redrawn.
     *  Dragging a panel is a run of saves, and rasterising the page on each of
     *  them would be work thrown away on the way to the one that matters. */
    static get THUMBNAIL_MS() { return 1500; }

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
        this.contextBar = null;
        this.tool = "select";

        //: What the tray has selected, which is NOT what the canvas has
        //: selected: a tray panel is not on the page, so the two selections
        //: cannot be one. Insertion-ordered, which is the order a batch is
        //: placed in.
        this.traySelection = new Set();
        //: The last tray item clicked without a modifier, so Shift has an
        //: anchor to range from.
        this.trayAnchor = null;
        //: `{sourceId, channels}` copied from one panel with "Copy rendering",
        //: waiting to be put on others. Separate from FigureClipboard, which
        //: holds OBJECTS -- copying a rendering must not make Ctrl+V paste
        //: something other than what the user copied.
        this.renderClipboard = null;
        this.traySearch = "";
        //: The menu currently open under a topbar button, if any.
        this.menu = null;
        //: The rich text editor, while a text annotation is being typed into.
        this.textEditor = null;
        //: Fit the page the first time it is drawn. An A4 at 100% is taller
        //: than most windows, so opening at 100% shows the top third of an
        //: empty page and reads as a figure that failed to load.
        this._fitted = false;
        //: Which secondary sidebar has the strip beside the rail -- see
        //: `showSidebar`. "panels" to match the markup, which ships with the
        //: tray open and the text panel hidden; set here rather than applied,
        //: because applying it reads the document and the document has not
        //: arrived yet.
        this.sidebar = null;
        //: Which tab the left column shows: "layers", "assets" or "insert".
        this.tab = "assets";
        //: Whether each side column is open. Remembered per browser, like the
        //: View options: how wide somebody likes their canvas is not a fact
        //: about the figure.
        this.columns = { left: true, right: true, ...FigureWorkspace.readColumns() };
        // A narrow window floats the columns over the canvas, one at a time;
        // it opens on the inspector, which is the one that follows the work.
        if (this.columns.left && this.columns.right && this.narrow()) this.columns.left = false;
        //: Preview: the figure drawn as it exports, with every piece of chrome
        //: gone. What it saved, so leaving puts the user back where they were.
        this.preview = null;
        //: The debounce behind the figure's thumbnail, and the revision the
        //: last one was drawn at -- so a save that changed nothing visible
        //: does not re-upload the same picture.
        this._thumbnailTimer = null;
        this._thumbnailRevision = -1;
    }

    static boot() {
        const root = document.getElementById("fb_workspace");
        if (!root || !root.dataset.figureId) return null;
        const workspace = new FigureWorkspace({ figureId: root.dataset.figureId });
        workspace.setup();
        // Adoption waits for the document, because the captures become panels
        // OF it. Chained here rather than inside `load` so that a workspace
        // handed a document somebody else owns (Quick Edit's seam) does not
        // also try to adopt into it.
        workspace.state.load().then(async (opened) => {
            if (!opened) return;
            // Asked BEFORE anything is adopted, because adopting bumps the
            // revision and the question is about the figure the user arrived
            // at, not the one they are leaving with.
            const untouched = (workspace.state.document || {}).revision === 0;
            await workspace.adoptFromBin();
            if (untouched) workspace.inviteRename();
        });
        return workspace;
    }

    /** What the server calls a figure nobody has named: "Untitled Figure", or
     *  "Untitled Figure 4". Matched rather than compared, because the number
     *  is assigned at creation and this side does not know which one it got. */
    static get UNTITLED() { return /^untitled figure(\s+\d+)?$/i; }

    static get COLUMNS_KEY() { return "plexora.figure_builder.columns"; }

    static readColumns() {
        try {
            const stored = JSON.parse(window.localStorage.getItem(FigureWorkspace.COLUMNS_KEY));
            return stored && typeof stored === "object"
                ? { left: stored.left !== false, right: stored.right !== false } : {};
        } catch (error) {
            return {};   // private-browsing modes throw rather than answering null
        }
    }

    el(id) {
        return document.getElementById(id);
    }

    setup() {
        if (!this.root) return;
        this.root.dataset.figureId = this.figureId;
        FigureIcons.hydrate(this.root);

        this.canvas = new FigureCanvas({
            state: this.state,
            api: this.api,
            figureId: this.figureId,
            pageEl: this.el("fb_page"),
            surfaceEl: this.el("fb_page_surface"),
            guideEl: this.el("fb_page_guides"),
            // Double-clicking a panel reaches for the CHEAP edit now that
            // there is one: Quick Edit opens beside the canvas, where the
            // expensive route is a page load into the viewer.
            onEditPanel: (panelId) => this.quickEditPanel(panelId),
            onSelectionChange: (ids) => this.selectionChanged(ids),
            // Every kind of drag but one hides the bar: it would either follow
            // the object under the pointer or sit pointing at where the object
            // used to be. A ROTATION is the exception -- the box turns about a
            // centre that does not move, so the bar is not in the way, and the
            // Transform panel's angle counting up as the object turns is the
            // whole point of being able to see it.
            onGesture: (active, kind) => {
                this.contextBar?.suppress(active && kind !== "rotate");
                this.root?.classList.toggle("is-gesturing", Boolean(active));
            },
            onRotatePreview: (degrees) => this.contextBar?.previewRotation(degrees),
            onToolFinished: () => this.setTool("select"),
            onNotice: (message) => this.toast(message),
            // Tray panels have landed. Placing them no longer selects them --
            // see FigureCanvas.placePanels -- so this is what tells the tray
            // that what it was holding has gone. It used to happen by accident:
            // a canvas selection cleared the tray's selection through
            // `selectionChanged`, and the tray was redrawn on the document
            // change.
            onPlaced: (ids) => {
                ids.forEach((id) => this.traySelection.delete(id));
                this.renderTray();
            },
            onRemoved: (ids) => this.showRemoved(ids),
            onEditText: (annotationId, options) => this.editText(annotationId, options),
            onEditPoints: (annotationId) => this.editPoints(annotationId),
            onResolution: (panelId) => this.showResolution(panelId),
            // The node editor has entered, left, or changed which nodes are
            // selected inside the shape. The panel's Points section reads all
            // of that off the editor, so it has to be told: selecting a node
            // changes no document, and nothing else would ever redraw it.
            onPointEditChange: (annotationId) => {
                this.shapePanel?.update(annotationId ? [annotationId]
                    : Array.from(this.canvas.selection));
                this.contextSidebar();
            },
        });
        this.canvas.setup();

        this.textEditor = new FigureTextEditor({
            overlayEl: this.el("fb_overlay_layer"),
            canvas: this.canvas,
            state: this.state,
            onCommit: (id, rich) => this.updateAnnotation(id, { rich: rich }),
        });
        this.textPanel = new FigureTextPanel({
            root: this.el("fb_text_panel"),
            canvas: this.canvas,
            state: this.state,
            editor: this.textEditor,
            onStyle: (id, changes) => this.updateAnnotation(id, changes),
            onClose: () => this.contextSidebar(),
        });
        this.textPanel.setup();

        this.shapePanel = new FigureShapePanel({
            root: this.el("fb_shape_panel"),
            canvas: this.canvas,
            state: this.state,
            onStyle: (id, changes) => this.updateAnnotation(id, changes),
            onClose: () => this.contextSidebar(),
        });
        this.shapePanel.setup();

        this.linePanel = new FigureLinePanel({
            root: this.el("fb_line_panel"),
            canvas: this.canvas,
            state: this.state,
            onStyle: (id, changes) => this.updateAnnotation(id, changes),
            onClose: () => this.contextSidebar(),
        });
        this.linePanel.setup();

        this.imagePanel = new FigureImagePanel({
            root: this.el("fb_image_panel"),
            layout: "inspector",
            canvas: this.canvas,
            state: this.state,
            handlers: {
                onQuickEdit: (panelId) => this.quickEditPanel(panelId),
                onEditPanel: (panelId) => this.onEditPanel(panelId),
                onSplit: () => this.split(),
                onPanelChange: (panelId, changes) => this.updatePanel(panelId, changes),
                onPanelsChange: (updates) => this.updatePanels(updates),
                onSetPixelSize: (sourceIds, value, panelIds) =>
                    this.setPixelSize(sourceIds, value, panelIds),
                onCopyRendering: (panelId) => this.copyRendering(panelId),
                onApplyRendering: (panelIds) => this.applyRendering(panelIds),
                hasRenderClipboard: () => this.hasRenderClipboard(),
                // Null means "every image on the page", as the Insert tab's
                // Panel letters does.
                onRelabel: (panelIds) => this.relabel(panelIds
                    || FigureSchema.panelsOnPage(this.state.document, this.canvas.pageId)
                        .map((panel) => panel.panel_id)),
            },
            onClose: () => this.contextSidebar(),
        });
        this.imagePanel.setup();

        //: The left column's first tab, and the top and foot of the right one.
        this.layers = new FigureLayers({
            root: this.el("fb_layers"), state: this.state, canvas: this.canvas,
            api: this.api, figureId: this.figureId, workspace: this,
        });
        this.layers.setup();
        this.inspector = new FigureInspector({
            root: this.el("fb_inspector"), tailRoot: this.el("fb_inspector_tail"),
            titleEl: this.el("fb_inspector_title"), subEl: this.el("fb_inspector_sub"),
            state: this.state, canvas: this.canvas, workspace: this,
        });
        this.inspector.setup();

        // Number fields apply as they are typed, one undo step per sitting.
        if (typeof FigureLiveFields !== "undefined" && this.root
                && typeof this.root.addEventListener === "function") {
            this.liveFields = new FigureLiveFields(this.root, this.state,
                () => Array.from(this.canvas.selection || []).sort().join(",")
                    || `page:${this.canvas.pageId || ""}`);
        }

        this.contextBar = new FigureContextBar({
            overlayEl: this.el("fb_overlay_layer"),
            canvas: this.canvas,
            state: this.state,
            compact: true,
            handlers: {
                onReplace: (panelId) => this.openReplace(panelId, this.contextBar?.el),
                onLayouts: () => this.openLayouts(),
                onEditPanel: (panelId) => this.onEditPanel(panelId),
                onQuickEdit: (panelId) => this.quickEditPanel(panelId),
                onArrange: (command) => this.arrange(command),
                onRemoveFromPage: () => this.canvas.removeSelection(),
                onDeleteFromFigure: (ids) => this.deleteFromFigure(ids),
                // Still here for the Transform popover, which types a width and
                // a height onto whichever kind of object is selected. Everything
                // else a PANEL has moved to the image sidebar.
                onPanelChange: (panelId, changes) => this.updatePanel(panelId, changes),
                onCopyRendering: (panelId) => this.copyRendering(panelId),
                onApplyRendering: (panelIds) => this.applyRendering(panelIds),
                hasRenderClipboard: () => this.hasRenderClipboard(),
                onAnnotationChange: (id, changes) => this.updateAnnotation(id, changes),
                onEditText: (annotationId, options) => this.editText(annotationId, options),
                onEditPoints: (annotationId) => this.editPoints(annotationId),
                onAcceptSource: (sourceId) => this.acceptChangedSource(sourceId),
                onInsertSymbol: (id, glyph) => this.insertSymbol(id, glyph),
                // The figure's unit lives in the View menu, and the Transform
                // popover reads and writes THAT one rather than keeping a
                // second copy of the same setting.
                units: () => this.viewOptions?.prefs.units,
                onUnits: (unit) => this.setUnits(unit),
            },
        });

        this.contextMenu = new FigureContextMenu({
            state: this.state,
            canvas: this.canvas,
            handlers: {
                // Quick Edit where it can help, the viewer where it cannot: a
                // panel from an imported PNG has no microscopy view to adjust.
                onQuickEdit: (panelId) => this.quickEditPanel(panelId),
                onEditPanel: (panelId) => this.onEditPanel(panelId),
                onEditText: (annotationId, options) => this.editText(annotationId, options),
                onEditPoints: (annotationId) => this.editPoints(annotationId),
                onDeleteFromFigure: (ids) => this.deleteFromFigure(ids),
                onReplace: (panelId) => this.openReplace(panelId),
                // Both surfaces run the same registry, so the right-click menu
                // needs every handler the floating bar's actions name.
                onRemoveFromPage: () => this.canvas.removeSelection(),
                onCopyRendering: (panelId) => this.copyRendering(panelId),
                onApplyRendering: (panelIds) => this.applyRendering(panelIds),
                hasRenderClipboard: () => this.hasRenderClipboard(),
                onPageBackground: () => this.openPageBackground(),
                onDuplicatePage: () => this.duplicatePage(),
                onPlaceFromTray: () => this.placeFromTray(Array.from(this.traySelection)),
                onDuplicateTray: () => this.duplicateTray(),
                onDeleteTray: () => this.deleteFromFigure(Array.from(this.traySelection)),
                onTrayContext: (panelId) => this.trayContext(panelId),
                traySelection: () => Array.from(this.traySelection),
            },
        });
        this.contextMenu.setup(this.el("fb_page_surface"), this.el("fb_tray_strip"));

        //: Rulers, grid, margins, snapping and the unit -- all of them
        //: statements about how the page is drawn for this person on this
        //: machine, none of them in the document.
        this.viewOptions = new FigureViewOptions({ workspace: this, canvas: this.canvas });
        this.viewOptions.setup();
        this.syncCanvasUnit();

        //: The slide-over that edits a panel's microscopy view in place. Handed
        //: THIS document state -- never a second one; see the class comment.
        this.quickEdit = new FigureQuickEdit({
            workspace: this,
            api: this.api,
            state: this.state,
            figureId: this.figureId,
            // The datasource the viewer has loaded: the only one Quick Edit
            // may draw live in 3-D (routes._loaded_datasource says why).
            loadedDatasource: this.root?.dataset.loadedDatasource || "",
            onOpenInViewer: (panelId) => this.onEditPanel(panelId),
            // Opening the slide-over shuts the strip; closing it gives the strip
            // back to whatever the selection asks for. See `contextSidebar`.
            onSessionChange: () => this.contextSidebar(),
        });
        this.quickEdit.setup();

        this.contextBar.setup();

        this.state.on("change", () => this.render());
        this.state.on("status", (payload) => this.renderStatus(payload));
        // The library and the destination picker are grids of pictures, and
        // until this existed every one of those pictures was the same
        // placeholder icon. Drawn once a save has settled, from the previews
        // already on screen -- see figureThumbnail.js.
        this.state.on("status", (payload) => {
            if (payload.status === "saved") this.scheduleThumbnail();
        });

        this.applyBackLink();
        this.bindTopbar();
        this.bindEditbar();
        this.bindColumns();
        this.bindTray();
        this.applyColumns();
        this.restoreView();
        this.bindCanvasHost();

        this.exportUi = new FigureExportUi({
            api: this.api, figureId: this.figureId, state: this.state,
        });
        this.exportUi.setup();

        // Undo and redo are the application's, not this panel's: there is one
        // Undo, and a second that only worked here would be a second answer to
        // the same keystroke.
        this._onKey = (event) => this.keyDown(event);
        window.addEventListener("keydown", this._onKey);
        // A window made narrower squeezed the canvas, not the columns: their
        // widths were only clamped on load and while dragging. Re-fitted
        // here, once per frame, from the widths the user chose (so widening
        // the window gives them back), and a window that crosses into the
        // narrow layout keeps one floating column, as it would have opened.
        this._onResize = () => {
            this.contextBar?.position();
            if (this._resizeFrame) return;
            this._resizeFrame = requestAnimationFrame(() => {
                this._resizeFrame = 0;
                if (this.narrow() && this.columns.left && this.columns.right) {
                    this.columns.left = false;
                    this.applyColumns();
                }
                for (const side of ["left", "right"]) {
                    this._applyColumnWidth?.(side, this._chosenWidths?.[side], false);
                }
            });
        };
        window.addEventListener("resize", this._onResize);
        // A reload, or a full navigation away, keeps the place too.
        this._onLeave = () => this.saveReturn();
        window.addEventListener("pagehide", this._onLeave);
    }

    destroy() {
        this.saveReturn();
        window.removeEventListener("pagehide", this._onLeave);
        window.removeEventListener("keydown", this._onKey);
        window.removeEventListener("resize", this._onResize);
        // The canvas is a routed page and the window outlives it: a space-pan
        // listener left here would keep cancelling Space app-wide and pin this
        // whole workspace, one more set per visit.
        window.removeEventListener("keydown", this._onSpaceDown);
        window.removeEventListener("keyup", this._onSpaceUp);
        window.removeEventListener("blur", this._releaseSpace);
        this._detachDrop?.();
        this._detachDrop = null;
        this.liveFields?.destroy();
        this.closeMenu();
        this.closeResolution();
        this.contextMenu?.destroy();
        this.viewOptions?.destroy();
        this.quickEdit?.destroy();
        this.contextBar?.destroy();
        this.canvas?.destroy();
        this.inspector?.destroy?.();
        this.imagePanel?.destroy?.();
        window.clearTimeout(this._thumbnailTimer);
        if (this._resizeFrame) cancelAnimationFrame(this._resizeFrame);
        if (this._pendingZoom?.frame) cancelAnimationFrame(this._pendingZoom.frame);
        this._pendingZoom = null;
    }

    // -- wiring ------------------------------------------------------------

    bindTopbar() {
        this.el("fb_title")?.addEventListener("change", () => this.commitTitle());
        this.el("fb_title")?.addEventListener("input", () => this.fitTitle());
        this.el("fb_title")?.addEventListener("keydown", (event) => {
            // Enter commits and gets out of the way; blur commits too, so a
            // title typed and then clicked away from is not silently discarded.
            if (event.key === "Enter") event.target.blur();
            if (event.key === "Escape") {
                event.target.value = this.state.title;
                event.target.blur();
            }
        });

        this.el("fb_undo")?.addEventListener("click", () => this.undo());
        this.el("fb_redo")?.addEventListener("click", () => this.redo());

        this.el("fb_page_select")?.addEventListener("change", (event) => {
            this.goToPage(event.target.value);
        });
        this.el("fb_page_menu")?.addEventListener("click", (event) => {
            this.openMenu(event.currentTarget, this.pageMenuEntries(),
                          (act) => this.pageAction(act));
        });
        // The same page menu, from the "···" in the inspector's head.
        this.el("fb_inspector_more")?.addEventListener("click", (event) => {
            this.openMenu(event.currentTarget, this.pageMenuEntries(),
                          (act) => this.pageAction(act));
        });

        this.el("fb_zoom_in")?.addEventListener("click",
            () => this.setScale(this.canvas.scale * 1.25));
        this.el("fb_zoom_out")?.addEventListener("click",
            () => this.setScale(this.canvas.scale * 0.8));
        this.el("fb_zoom_readout")?.addEventListener("click", (event) => {
            this.openMenu(event.currentTarget, this.zoomMenuEntries(),
                          (act) => this.zoomAction(act));
        });
        this.el("fb_zoom_fit")?.addEventListener("click", (event) => {
            this.openMenu(event.currentTarget, this.zoomMenuEntries(), (act) => this.zoomAction(act));
        });

        // Under the page rather than in a menu: adding one is the commonest
        // thing anybody does to a figure that has run out of room, and it is
        // the only page action worth a permanent button.
        this.el("fb_page_add")?.addEventListener("click", () => this.addPage());

        this.el("fb_view_menu")?.addEventListener("click", (event) => {
            this.openMenu(event.currentTarget, this.viewOptions.menuEntries(),
                          (act) => this.viewOptions.pick(act));
        });

        // The same trip as the tray's, from the other end of the page.
        this.el("fb_add_image")?.addEventListener("click", () => this.addPanels());

        this.el("fb_conflict_reload")?.addEventListener("click", () => window.location.reload());
    }

    /**
     * Where the back arrow goes.
     *
     * Two ways in to this page, and they want different ways out. Arriving
     * from a project's viewer, back is that viewer with the tool still open --
     * the commonest trip there is is capture a few fields, look at the figure,
     * go back to the slide for one more, and an arrow that always went to the
     * library made the way back a search. Arriving from the library, back is
     * the library, which is what the markup already says.
     *
     * The note is left by the viewer at the moment it sends the user here. It
     * carries the figure id, so one left over from a different figure is
     * ignored rather than followed back to a slide nobody asked for, and it is
     * NOT consumed: reloading this page should not change where back goes.
     */
    applyBackLink() {
        const link = this.el("fb_back");
        if (!link) return;
        let note = null;
        try {
            note = JSON.parse(window.sessionStorage.getItem(
                FigureWorkspace.ORIGIN_KEY) || "null");
        } catch (error) {
            return;   // private-browsing modes throw rather than answering null
        }
        // A path on this server and nothing else: this value ends up in an
        // href, and sessionStorage is writable by anything else on the origin.
        if (!note || note.figure_id !== this.figureId) return;
        if (typeof note.href !== "string" || !note.href.startsWith("/")
            || note.href.startsWith("//")) return;
        link.href = note.href;
        const label = note.label ? `Back to ${note.label}` : "Back to the viewer";
        link.title = label;
        link.setAttribute("aria-label", label);
    }

    /**
     * The editing row: Add, Layouts, Arrange, Annotate, and the page's size.
     *
     * One insertion path with three ways in. Add here, the Insert tab and the
     * Assets tab all end in the same calls -- `addPanels`, `importFiles`,
     * `openCaptureBin`, `openBookmarks`, `setTool` -- so there is nothing one of
     * them can do that the others do differently.
     */
    bindEditbar() {
        this.el("fb_menu_add")?.addEventListener("click", (event) => {
            this.openMenu(event.currentTarget, this.addMenuEntries(), (act) => this.insert(act));
        });
        this.el("fb_menu_layouts")?.addEventListener("click", () => this.openLayouts());
        this.el("fb_menu_arrange")?.addEventListener("click", (event) => {
            this.openMenu(event.currentTarget, this.arrangeMenuEntries(),
                          (act) => this.arrangeAction(act));
        });
        this.el("fb_menu_annotate")?.addEventListener("click", (event) => {
            this.openAnnotateCard(event.currentTarget);
        });
        // The page's size, which is a property of the paper: pressing it lets
        // go of the selection, and the inspector then describes the page.
        this.el("fb_page_setup")?.addEventListener("click", () => {
            this.canvas.select([], false);
            this.setColumn("right", true);
            this.el("fb_inspector_scroll")?.scrollTo?.({ top: 0 });
        });
        this.el("fb_preview")?.addEventListener("click", () => this.togglePreview());

        // The empty page's two buttons. "Add image" goes to the figure's own
        // assets, which is where captures and imported files are; the other is
        // the trip to a viewer (`fb_add_image`, bound in bindTopbar).
        this.el("fb_page_empty")?.addEventListener("click", (event) => {
            // Assets is already open beside the page, so this one uploads.
            if (event.target.closest("[data-empty='assets']")) this.el("fb_add_image_input")?.click();
            const starter = event.target.closest("[data-starter]");
            if (starter) this.chooseStarter(starter.dataset.starter);
        });
        this.renderStarters();

        const insert = this.el("fb_insert_view");
        if (insert) {
            insert.innerHTML = this.insertMarkup();
            insert.addEventListener("click", (event) => {
                const tile = event.target.closest("[data-insert]");
                if (tile) this.insert(tile.dataset.insert, tile);
            });
        }
    }

    /** The two side columns: the switches at either end of the editing row,
     *  and the tabs of the left one. */
    bindColumns() {
        this.el("fb_toggle_left")?.addEventListener("click",
            () => this.setColumn("left", !this.columns.left));
        this.el("fb_toggle_right")?.addEventListener("click",
            () => this.setColumn("right", !this.columns.right));
        const tabs = this.el("fb_left")?.querySelector(".fb-tabs");
        tabs?.addEventListener("click", (event) => {
            const tab = event.target.closest("[data-tab]");
            if (tab) this.showTab(tab.dataset.tab);
        });
        // Arrow keys move between the tabs, the tablist pattern.
        tabs?.addEventListener("keydown", (event) => {
            const order = ["assets", "layers", "insert"];
            const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
            if (!step) return;
            event.preventDefault();
            const next = order[(order.indexOf(this.tab) + step + order.length) % order.length];
            this.showTab(next);
            this.el(`fb_tab_${next}`)?.focus();
        });
        this.bindColumnResize();
    }

    /** Each side column's width: its default, and how far a drag may take it. */
    static get COLUMN_WIDTHS() {
        return {
            left: { def: 320, min: 296, max: 640, prop: "--fb-left-w", id: "fb_left" },
            right: { def: 340, min: 304, max: 520, prop: "--fb-right-w", id: "fb_right" },
        };
    }

    static get COLUMN_WIDTH_KEY() { return "plexora.figure_builder.column_widths"; }

    /**
     * A handle on the inner edge of each side column: drag to resize, double
     * click to go back to the default, arrow keys when it has focus. Widths
     * are kept in localStorage, for every figure. The canvas takes up what is
     * left by layout alone; only the rulers and the floating bar are told.
     */
    bindColumnResize() {
        const root = this.el("fb_workspace");
        if (!root) return;
        const specs = FigureWorkspace.COLUMN_WIDTHS;
        let saved = {};
        try {
            saved = JSON.parse(window.localStorage.getItem(FigureWorkspace.COLUMN_WIDTH_KEY) || "{}") || {};
        } catch (error) { saved = {}; }
        this.columnWidths = {};

        const apply = (side, width, persist) => {
            const spec = specs[side];
            // The canvas keeps at least 360px between the two columns.
            const other = side === "left" ? "right" : "left";
            const otherWidth = this.columns[other] ? (this.columnWidths[other] || specs[other].def) : 0;
            const room = Math.max(spec.min, window.innerWidth - otherWidth - 360);
            const clamped = Math.round(Math.max(spec.min, Math.min(spec.max, room, Number(width) || spec.def)));
            this.columnWidths[side] = clamped;
            root.style.setProperty(spec.prop, `${clamped}px`);
            if (persist) {
                this._chosenWidths[side] = clamped;
                try {
                    window.localStorage.setItem(FigureWorkspace.COLUMN_WIDTH_KEY, JSON.stringify(this.columnWidths));
                } catch (error) { /* private window: the width lasts this visit */ }
            }
            this.viewOptions?.drawRulers();
            this.contextBar?.position();
        };

        //: What the user dragged each column to, as opposed to what the
        //: window currently has room for (`this.columnWidths`).
        this._chosenWidths = { left: saved.left || specs.left.def, right: saved.right || specs.right.def };
        this._applyColumnWidth = apply;
        for (const side of ["left", "right"]) {
            const spec = specs[side];
            apply(side, saved[side] || spec.def, false);
            const column = this.el(spec.id);
            if (!column || column.querySelector(":scope > .fb-col-resize")) continue;
            const handle = document.createElement("div");
            handle.className = `fb-col-resize fb-col-resize-${side}`;
            handle.tabIndex = 0;
            handle.setAttribute("role", "separator");
            handle.setAttribute("aria-orientation", "vertical");
            handle.setAttribute("aria-label", side === "left" ? "Resize the left sidebar" : "Resize the right sidebar");
            handle.title = "Drag to resize · double-click to reset";
            column.appendChild(handle);

            handle.addEventListener("pointerdown", (event) => {
                if (event.button !== 0) return;
                event.preventDefault();
                handle.setPointerCapture(event.pointerId);
                const startX = event.clientX;
                const startWidth = this.columnWidths[side];
                root.classList.add("is-resizing-columns");
                handle.classList.add("is-dragging");
                const move = (moveEvent) => {
                    const dx = moveEvent.clientX - startX;
                    apply(side, startWidth + (side === "left" ? dx : -dx), false);
                };
                const up = () => {
                    handle.removeEventListener("pointermove", move);
                    handle.removeEventListener("pointerup", up);
                    handle.removeEventListener("pointercancel", up);
                    root.classList.remove("is-resizing-columns");
                    handle.classList.remove("is-dragging");
                    apply(side, this.columnWidths[side], true);
                };
                handle.addEventListener("pointermove", move);
                handle.addEventListener("pointerup", up);
                handle.addEventListener("pointercancel", up);
            });
            handle.addEventListener("dblclick", () => apply(side, spec.def, true));
            handle.addEventListener("keydown", (event) => {
                const step = { ArrowLeft: -16, ArrowRight: 16 }[event.key];
                if (!step) return;
                event.preventDefault();
                apply(side, this.columnWidths[side] + (side === "left" ? step : -step), true);
            });
        }
    }

    /** Open or close one side column. The canvas takes the width back by
     *  layout alone -- nothing is redrawn but the rulers, which measure it. */
    setColumn(side, open) {
        this.columns[side] = Boolean(open);
        try {
            window.localStorage.setItem(FigureWorkspace.COLUMNS_KEY, JSON.stringify(this.columns));
        } catch (error) { /* see readColumns */ }
        // Narrow windows show one column at a time, over the canvas.
        if (open && this.narrow()) this.columns[side === "left" ? "right" : "left"] = false;
        this.applyColumns();
    }

    /** The narrow layout: the same breakpoint as the stylesheet's
     *  `max-width: 1099px`, asked the same way so the two never disagree. */
    narrow() {
        return typeof window !== "undefined"
            && (window.matchMedia ? window.matchMedia("(max-width: 1099px)").matches : window.innerWidth < 1100);
    }

    applyColumns() {
        const root = this.el("fb_workspace");
        root?.classList.toggle("has-left", this.columns.left);
        root?.classList.toggle("has-right", this.columns.right);
        for (const [side, id] of [["left", "fb_toggle_left"], ["right", "fb_toggle_right"]]) {
            const button = this.el(id);
            if (!button) continue;
            button.classList.toggle("is-on", this.columns[side]);
            button.setAttribute("aria-pressed", String(this.columns[side]));
        }
        const left = this.el("fb_left");
        if (left) left.hidden = !this.columns.left;
        this.applyInspector();
        this.viewOptions?.drawRulers();
        this.contextBar?.position();
    }

    /**
     * Which tab, which Assets list and whether "Previously used" is on, kept
     * per figure for this browser tab, so a reload or Back lands where the
     * user was rather than on Layers. sessionStorage: a new tab starts fresh.
     */
    viewKey() {
        return `plexora:figure-builder-view:${this.figureId}`;
    }

    /**
     * Where the user was on the canvas -- the page, what was selected, the
     * zoom, and where each scrolling part was -- kept for this browser tab
     * when the figure is left (for the viewer, by Back, by a reload), and put
     * back when it is opened again. The tab, the Assets list and the
     * inspector's open sections are kept elsewhere (saveView, localStorage).
     */
    returnKey() {
        return `plexora:figure-builder-return:${this.figureId}`;
    }

    saveReturn() {
        const scroll = this.el("fb_canvas_scroll");
        if (!this.canvas || !scroll || !scroll.isConnected) return;
        const top = (id) => this.el(id)?.scrollTop || 0;
        try {
            window.sessionStorage.setItem(this.returnKey(), JSON.stringify({
                page: this.canvas.pageId,
                selection: Array.from(this.canvas.selection),
                scale: this.canvas.scale,
                left: scroll.scrollLeft, top: scroll.scrollTop,
                assets: top("fb_assets_pane"), layers: top("fb_layers_view"),
                inspector: top("fb_inspector_scroll"),
                at: Date.now(),
            }));
        } catch (error) {
            /* Private-browsing modes throw; the figure opens fitted. */
        }
    }

    readReturn() {
        try {
            const saved = JSON.parse(window.sessionStorage.getItem(this.returnKey()) || "null");
            return saved && typeof saved === "object" ? saved : null;
        } catch (error) {
            return null;
        }
    }

    applyReturn(saved) {
        if (!saved || !this.canvas) return;
        if (saved.scale > 0) {
            this.canvas.setScale(saved.scale);
            this.afterZoom();
        }
        const scroll = this.el("fb_canvas_scroll");
        if (scroll) {
            scroll.scrollLeft = saved.left || 0;
            scroll.scrollTop = saved.top || 0;
        }
        const ids = (saved.selection || []).filter((id) => this.canvas.objectById(id));
        if (ids.length) this.canvas.select(ids, false, true);
        // The sidebars are drawn by the selection above; their scroll waits
        // one frame for that.
        window.requestAnimationFrame(() => {
            for (const [id, value] of [["fb_assets_pane", saved.assets], ["fb_layers_view", saved.layers],
                                       ["fb_inspector_scroll", saved.inspector]]) {
                const el = this.el(id);
                if (el && value) el.scrollTop = value;
            }
            this.viewOptions?.drawRulers();
            this.contextBar?.position();
        });
    }

    saveView() {
        try {
            window.sessionStorage.setItem(this.viewKey(), JSON.stringify({
                tab: this.tab, pane: this.assetsPane || "captures", used: this.usedPanes || {},
            }));
        } catch (error) {
            /* Private-browsing modes throw; the page opens on Layers. */
        }
    }

    restoreView() {
        let saved = null;
        try {
            saved = JSON.parse(window.sessionStorage.getItem(this.viewKey()) || "null");
        } catch (error) {
            saved = null;
        }
        if (saved && typeof saved === "object") {
            this.usedPanes = (saved.used && typeof saved.used === "object") ? saved.used : {};
            if (["captures", "bookmarks", "uploads"].includes(saved.pane)) this.assetsPane = saved.pane;
            if (["layers", "assets", "insert"].includes(saved.tab)) this.tab = saved.tab;
        }
        // Shown through showTab even for the default (Assets), so its list is
        // mounted and read. A shut column stays shut.
        if (this.columns.left) this.showTab(this.tab);
    }

    /** Show one of the left column's tabs, opening the column if it was shut. */
    /** How long a fetched Assets collection counts as current. */
    static get ASSETS_FRESH_MS() { return 15000; }

    showTab(name) {
        this.tab = name;
        this.saveView();
        if (!this.columns.left) this.setColumn("left", true);
        const views = { layers: "fb_layers_view", assets: "fb_tray_panel", insert: "fb_insert_view" };
        for (const [tab, id] of Object.entries(views)) {
            const view = this.el(id);
            if (view) view.hidden = tab !== name;
            const button = this.el(`fb_tab_${tab}`);
            if (!button) continue;
            button.classList.toggle("is-active", tab === name);
            button.setAttribute("aria-selected", String(tab === name));
            button.tabIndex = tab === name ? 0 : -1;
        }
        if (name === "assets") this.showAssets(this.assetsPane || "captures");
        if (name === "layers") this.layers?.render();
    }

    /**
     * One of the Assets tab's three: Captures, Bookmarks or Uploads.
     *
     * Each is one grid (FigureCaptureBinGrid). In it, first, this figure's
     * images of that kind that are off the page, tagged "In figure" -- an
     * image taken off the page goes back under the tab it was added from --
     * then, for Captures and Bookmarks, the collection outside the figure:
     * the captures not in any figure yet (bookmarks left out: they have
     * their own tab), and the views bookmarked in the viewer. Every card can
     * be dragged onto the page. One search box and one filter narrow it all.
     */
    showAssets(name) {
        if (!["captures", "bookmarks", "uploads"].includes(name)) name = "captures";
        this.assetsPane = name;
        this.saveView();
        const parts = { fb_assets_bin: "captures", fb_assets_viewer: "captures",
                        fb_assets_bookmarks: "bookmarks", fb_assets_bookmark_go: "bookmarks",
                        fb_assets_uploads: "uploads", fb_assets_upload: "uploads" };
        for (const [id, pane] of Object.entries(parts)) {
            const el = this.el(id);
            if (el) el.hidden = pane !== name;
        }
        this.el("fb_tray_panel")?.querySelectorAll("[data-assets]").forEach((button) => {
            const on = button.dataset.assets === name;
            button.classList.toggle("is-on", on);
            button.setAttribute("aria-pressed", String(on));
        });
        const search = this.el("fb_tray_search");
        if (search) {
            search.placeholder = `Search ${name}`;
            search.title = name === "uploads" ? "Search by name or file"
                : "Search by name, project, dataset or channel";
        }
        const grids = this.assetGrids || (this.assetGrids = {});
        if (grids[name]) {
            grids[name].setQuery(this.traySearch);
            // The collection can change from the viewer, so it is asked for
            // again -- but not on every flick between the three tabs.
            const now = Date.now();
            if (!(now - (grids[name]._askedAt || 0) < FigureWorkspace.ASSETS_FRESH_MS)) {
                grids[name]._askedAt = now;
                grids[name].refresh();
            }
            this.renderTray();
            return;
        }
        const host = this.el({ captures: "fb_assets_bin", bookmarks: "fb_assets_bookmarks",
                               uploads: "fb_assets_uploads" }[name]);
        if (!host || typeof FigureCaptureBinGrid === "undefined") return;
        const shared = {
            api: this.api, search: false, quietWhenEmpty: true, draggable: true, selectable: true,
            addLabel: "Add to page",
            searchText: (capture) => this.captureHaystack(capture),
            onLoaded: (grid) => {
                // "Previously used" on or off, remembered per list.
                if (grid && grid.usedToggle) {
                    this.usedPanes = { ...(this.usedPanes || {}), [name]: grid.showUsed };
                    this.saveView();
                }
                this.updateFilterOptions();
                this.fitSourceCard();
            },
            onAddPanels: (ids) => this.placeFromTray(ids),
            onRemovePanels: (ids) => this.deleteFromFigure(ids),
            onGoto: (id) => this.gotoPanel(id),
            onAdd: async (ids) => {
                await this.adoptCaptures(ids, { place: true });
                grids[name].refresh();
            },
        };
        grids[name] = name === "captures"
            ? new FigureCaptureBinGrid(host, { ...shared, excludeBookmarked: true })
            : name === "bookmarks"
                ? new FigureCaptureBinGrid(host, {
                    ...shared, bookmarks: true,
                    onGo: (capture) => this.api.openViewAt(capture.datasource, capture.capture_id),
                    onAddOne: (capture) => this.adoptCaptures([capture.capture_id], { place: true }),
                })
                : new FigureCaptureBinGrid(host, {
                    ...shared, collection: false, noun: "upload",
                    excludeBookmarked: true, byPlacement: true });
        grids[name].setQuery(this.traySearch);
        grids[name].setPredicate(this.assetPredicate());
        if (grids[name].usedToggle && (this.usedPanes || {})[name]) grids[name].showUsed = true;
        grids[name].setup();
        this.renderTray();
    }

    /**
     * Images just taken off the page are in Assets now: show them there.
     * The tab they came from (Captures, Bookmarks, Uploads), on the list that
     * holds them -- "Previously used" for a capture or a bookmark, which has
     * been in a figure; the off-page list for an upload. The side column's
     * own tab (Assets or Layers) is left as it is.
     */
    showRemoved(panelIds) {
        const panel = panelIds.map((id) => this.state.panel(id)).find(Boolean);
        if (!panel) return;
        const origin = FigureSchema.panelOrigin(panel, this.state.source(panel.source_id));
        const pane = { capture: "captures", bookmark: "bookmarks", upload: "uploads" }[origin];
        if (!pane) return;
        const showUsed = pane !== "uploads";
        this.usedPanes = { ...(this.usedPanes || {}), [pane]: showUsed };
        const grid = (this.assetGrids || {})[pane];
        if (grid && grid.usedToggle && grid.showUsed !== showUsed) {
            grid.showUsed = showUsed;
            grid.checked.clear();
        }
        if (this.assetsPane !== pane) this.showAssets(pane);
        else {
            this.saveView();
            this.renderTray();
        }
    }

    /** Select one of the figure's images where it is on the page. */
    gotoPanel(panelId) {
        const panel = this.state.panel(panelId);
        if (!panel || !panel.placement) return;
        if (panel.placement.page_id !== this.canvas.pageId) this.goToPage(panel.placement.page_id);
        this.canvas.select([panel.panel_id], false, true);
    }

    // -- the Assets filter ------------------------------------------------

    /** The filter beside the search, and the project list it names images
     *  by (core's /projects: display name, dataset, location per key). */
    setupAssetFilter() {
        if (typeof FigureAssetFilter === "undefined") return;
        this.assetFilter = new FigureAssetFilter({
            button: this.el("fb_assets_filter"),
            chips: this.el("fb_assets_chips"),
            root: this.root,
            onChange: () => this.applyAssetFilter(),
        });
        this.projectCards = new Map();
        this.api.listProjects?.().then((cards) => {
            if (!cards) return;
            this.projectCards = new Map(cards.map((card) => [card.name, card]));
            this.applyAssetFilter();
        });
    }

    capturesFacets(capture) {
        return FigureAssetFilter.facetsOf(capture, this.projectCards);
    }

    panelFacets(panel) {
        return FigureAssetFilter.facetsOf({
            source: this.state.source(panel.source_id), scene: panel.scene,
            created_at: panel.created_at,
        }, this.projectCards);
    }

    /** The filter as a test on one bin capture, or null when it is off. */
    assetPredicate() {
        const filter = this.assetFilter;
        if (!filter || !filter.active) return null;
        return (capture) => filter.matches(this.capturesFacets(capture));
    }

    /** The search reads a capture's project and dataset names too. */
    captureHaystack(capture) {
        const facets = typeof FigureAssetFilter === "undefined" ? null : this.capturesFacets(capture);
        return [FigureSchema.captureSearchText(capture),
                facets && facets.project, facets && facets.dataset].filter(Boolean).join(" ");
    }

    applyAssetFilter() {
        const predicate = this.assetPredicate();
        for (const grid of Object.values(this.assetGrids || {})) grid.setPredicate(predicate);
        this.renderTray();
    }

    /** The filter's options: what the tab now showing could list -- this
     *  figure's images of its kind, on the page or off, and its collection. */
    updateFilterOptions() {
        if (!this.assetFilter) return;
        const pane = this.assetsPane || "captures";
        const panels = Object.values(this.state.document?.panels || {})
            .filter((panel) => this.inAssetsPane(panel));
        const grid = (this.assetGrids || {})[pane];
        const captures = grid ? grid.listed() : [];
        this.assetFilter.setItems(panels.map((panel) => this.panelFacets(panel))
            .concat(captures.map((capture) => this.capturesFacets(capture))));
    }

    // -- inserting ---------------------------------------------------------

    addMenuEntries() {
        return [
            { heading: "Images" },
            { act: "viewer", icon: "camera", label: "From a viewer…" },
            { act: "captures", icon: "inbox", label: "Captures…" },
            { act: "bookmarks", icon: "bookmark", label: "Bookmarks…" },
            { act: "file", icon: "file-image", label: "Upload image…" },
            { separator: true },
            { heading: "Page" },
            { act: "page", icon: "plus", label: "New page" },
            { act: "duplicate_page", icon: "clone", label: "Duplicate this page" },
        ];
    }

    /** Every insertion, by name -- the Add menu, the Insert tab and the empty
     *  page all land here. `anchor` is the control that asked, for the cards
     *  that open beside it. */
    insert(act, anchor) {
        const run = {
            assets: () => this.showTab("assets"),
            viewer: () => this.addPanels(),
            captures: () => this.openCaptureBin(),
            bookmarks: () => this.openBookmarks(),
            file: () => this.el("fb_add_image_input")?.click(),
            page: () => this.addPage(),
            duplicate_page: () => this.duplicatePage(),
            text: () => this.openTextCard(anchor || this.el("fb_menu_annotate")),
            shapes: () => this.openShapesCard(anchor || this.el("fb_menu_annotate")),
            lines: () => this.openLinesCard(anchor || this.el("fb_menu_annotate")),
            rect: () => this.setTool("shape:rect"),
            ellipse: () => this.setTool("shape:ellipse"),
            line: () => this.setTool("line:line"),
            arrow: () => this.setTool("line:arrow"),
            letters: () => this.relabelSelectionOrPage(),
            scalebar: () => this.scaleBarsOn(),
            channels: () => this.channelCaptions(),
            colorbar: () => this.colorBarsOn(),
            borders: () => this.bordersOn(),
            split: () => this.splitSelected(),
            duplicate: () => (this.selectedImageIds().length
                ? this.canvas.duplicateSelection() : this.toast("Select the images to duplicate.")),
            replace: () => (this.selectedImageIds().length === 1
                ? this.openReplace(this.selectedImageIds()[0], anchor)
                : this.toast("Select one image to replace.")),
        }[act];
        run?.();
        this.revealFor(act);
    }

    /**
     * After an insertion, the settings for what was inserted, in the right
     * column: its section opened and brought into view. One image or several
     * have different sections; Panel letters with nothing selected are the
     * page's (Document properties). The drawing tools only open the column --
     * what they make is selected once placed, and its own panel shows there.
     */
    revealFor(act) {
        const several = this.selectedImageIds().length > 1;
        const any = this.selectedImageIds().length > 0;
        const sections = {
            channels: ["io_labels", "io_m_labels"],
            scalebar: ["io_bar", "io_m_bars"],
            colorbar: ["io_look", "io_m_look"],
            borders: ["io_look", "io_m_look"],
            letters: ["io_letter", "io_m_letters"],
        }[act];
        if (sections && any) {
            this.revealSection(sections[several ? 1 : 0]);
        } else if (act === "letters") {
            this.revealSection("style");
        } else if (["text", "shapes", "lines", "rect", "ellipse", "line", "arrow"].includes(act)) {
            this.setColumn("right", true);
        }
    }

    /** Open the right column on one fold (an image section or a page
     *  section) and scroll it into view. */
    revealSection(id) {
        this.setColumn("right", true);
        if (typeof FigureInspector !== "undefined" && !FigureInspector.foldOpen(id)) {
            FigureInspector.foldFlip(id);
        }
        // After both halves of the column have redrawn on the fold change.
        window.setTimeout(() => {
            const head = this.root?.querySelector(
                `.fb-right [data-ifold="${id}"], .fb-right [data-style="${id}"]`);
            head?.scrollIntoView?.({ block: "start", behavior: "smooth" });
        }, 120);
    }

    /** The Insert tab: everything that can be put on the page, by kind. */
    insertMarkup() {
        const tile = (act, icon, label, title) => `<button type="button" class="fb-insert-tile"
                data-insert="${act}" title="${FigureSchema.escapeHtml(title || label)}">
                ${FigureIcons.fa(icon)}
                <span>${FigureSchema.escapeHtml(label)}</span></button>`;
        // Three groups of six, a full 3 x 2 grid each, ruled apart.
        const group = (title, tiles) => `<section class="fb-insert-group">
                <h3 class="fb-insert-title">${title}</h3>
                <div class="fb-insert-grid">${tiles.join("")}</div></section>`;
        return group("Images", [
            tile("viewer", "camera", "From a viewer", "Open a project and capture fields into this figure"),
            tile("captures", "inbox", "Captures", "Captures not yet in any figure"),
            tile("bookmarks", "bookmark", "Bookmarks", "Views saved in the viewer"),
            tile("file", "file-image", "Image file", "A PNG, JPEG or TIFF from your computer"),
            tile("duplicate", "clone", "Duplicate", "Duplicate the selected images (⌘D)"),
            tile("replace", "arrow-right-arrow-left", "Replace", "Put another of the figure's images in the selected one's place"),
        ]) + group("Annotations", [
            tile("text", "font", "Text"),
            tile("rect", "square", "Rectangle"),
            tile("ellipse", "circle", "Ellipse"),
            tile("line", "minus", "Line"),
            tile("arrow", "arrow-right-long", "Arrow"),
            tile("shapes", "shapes", "More shapes"),
        ]) + group("Scientific", [
            tile("letters", "heading", "Panel letters", "Letter the selected images A, B, C… in reading order"),
            tile("scalebar", "ruler-horizontal", "Scale bars", "Turn on scale bars on the selected images"),
            tile("channels", "palette", "Channel labels", "Label the selected images with their channels, in their colors"),
            tile("colorbar", "swatchbook", "Color bars", "An intensity ramp per channel on the selected images"),
            tile("split", "table-columns", "Split channels", "The selected composite and one image per channel, in a row"),
            tile("borders", "vector-square", "Borders", "A border round each selected image"),
        ]);
    }

    /** Borders on for the selected images, at their own (or default) width
     *  and color. */
    bordersOn() {
        const ids = this.selectedImageIds();
        if (!ids.length) {
            this.toast("Select the images to put borders on.");
            return;
        }
        this.updatePanels(ids.map((id) => this.state.panel(id))
            .map((panel) => ({ panel_id: panel.panel_id,
                               changes: { border: { ...(panel.border || {}), visible: true } } })));
    }

    /** Insert's Split channels: one composite image with two channels or more. */
    splitSelected() {
        const ids = this.selectedImageIds();
        const panel = ids.length === 1 ? this.state.panel(ids[0]) : null;
        const channels = (panel?.scene?.channels || []).filter((channel) => channel.visible !== false);
        if (!panel || channels.length < 2) {
            this.toast("Select one image with two or more channels to split.");
            return;
        }
        this.split();
    }

    /**
     * The Annotate card: the text styles, the shapes and the lines in one
     * card, then the scientific furniture. The same three cards the rail used
     * to open one at a time, stacked -- each part is generated from the same
     * definitions, so this cannot offer something the others do not.
     */
    openAnnotateCard(anchor) {
        const text = FigureRichText.PRESETS.map((preset) => ({
            act: `text:${preset.id}`, icon: "font", label: preset.label, hint: `${preset.size_pt} pt` }));
        this.openMenu(anchor, [
            { heading: "Text" },
            ...text,
            { separator: true },
            { heading: "Shapes and lines" },
            { act: "shape:rect", icon: "square", label: "Rectangle" },
            { act: "shape:ellipse", icon: "circle", label: "Ellipse" },
            { act: "line:line", icon: "minus", label: "Line" },
            { act: "line:arrow", icon: "arrow-right-long", label: "Arrow" },
            { act: "insert:shapes", icon: "shapes", label: "More shapes…" },
            { act: "insert:lines", icon: "ellipsis", label: "More lines…" },
            { separator: true },
            { heading: "Scientific" },
            { act: "insert:letters", icon: "font", label: "Panel labels A, B, C" },
            { act: "insert:scalebar", icon: "ruler-horizontal", label: "Scale bars" },
            { act: "insert:channels", icon: "palette", label: "Channel labels" },
            { act: "insert:colorbar", icon: "swatchbook", label: "Color bars" },
        ], (act) => {
            if (act.startsWith("insert:")) this.insert(act.slice(7), anchor);
            else this.setTool(act);
        });
    }

    arrangeMenuEntries() {
        const sel = FigureSelection.describe(Array.from(this.canvas.selection), this.state, this.canvas);
        const two = sel.arrangeable > 1;
        const any = sel.count > 0;
        return [
            { heading: "Align" },
            { act: "left", icon: "align-left", label: "Left", disabled: !two },
            { act: "center", icon: "align-center", label: "Centers", disabled: !two },
            { act: "right", icon: "align-right", label: "Right", disabled: !two },
            { act: "top", icon: "arrow-up", label: "Top", disabled: !two },
            { act: "middle", icon: "arrows-up-down", label: "Middles", disabled: !two },
            { act: "bottom", icon: "arrow-down", label: "Bottom", disabled: !two },
            { separator: true },
            { act: "distribute_h", icon: "arrows-left-right", label: "Space evenly across", disabled: sel.arrangeable < 3 },
            { act: "distribute_v", icon: "arrows-up-down", label: "Space evenly down", disabled: sel.arrangeable < 3 },
            { act: "same_size", icon: "expand", label: "Match size to the first", disabled: !two },
            { separator: true },
            { act: "front", icon: "angles-up", label: "Bring to front", hint: "⌘⇧]", disabled: !any },
            { act: "forward", icon: "angle-up", label: "Bring forward", hint: "⌘]", disabled: !any },
            { act: "backward", icon: "angle-down", label: "Send backward", hint: "⌘[", disabled: !any },
            { act: "back", icon: "angles-down", label: "Send to back", hint: "⌘⇧[", disabled: !any },
            { separator: true },
            sel.grouped
                ? { act: "group", icon: "object-ungroup", label: "Ungroup", hint: "⇧⌘G" }
                : { act: "group", icon: "object-group", label: "Group", hint: "⌘G", disabled: sel.count < 2 },
            { separator: true },
            { act: "fit_content", icon: "expand", label: "Fit content to page" },
            { act: "center_content", icon: "arrows-left-right", label: "Center content on page" },
        ];
    }

    arrangeAction(act) {
        if (["front", "forward", "backward", "back"].includes(act)) this.canvas.reorderZ(act);
        else if (act === "group") {
            const sel = FigureSelection.describe(Array.from(this.canvas.selection), this.state, this.canvas);
            if (sel.grouped) this.canvas.ungroupSelection();
            else this.canvas.groupSelection();
        } else if (act === "fit_content") this.canvas.fitContentToPage();
        else if (act === "center_content") this.canvas.centerContent();
        else this.arrange(act);
    }

    // -- layouts -----------------------------------------------------------

    /** The placed images in the selection, in selection order. */
    selectedImageIds() {
        return Array.from(this.canvas.selection)
            .filter((id) => this.state.panel(id)?.placement && this.canvas.isEditable(id));
    }

    /**
     * The Layouts popover: every preset as a picture, the two gaps, letters
     * and fit. Presets the selection cannot fill are shown and dimmed, with the
     * reason in the tooltip, so the list does not change shape under the
     * pointer as the selection changes.
     */
    openLayouts() {
        const anchor = this.el("fb_menu_layouts");
        if (!anchor) return;
        const ids = this.selectedImageIds();
        const count = ids.length;
        const gutter = this.state.document.settings.style.gutter_mm;
        const prefs = this.layoutPrefs || (this.layoutPrefs = { label: false, fit: "fit" });
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const group = this.canvas.groupOfExactly(ids);
        const tiles = FigureLayouts.PRESETS.map((preset) => {
            const ok = FigureLayouts.fits(preset, count);
            const on = group?.layout?.preset === preset.id;
            const tip = ok ? `${preset.label} layout`
                : (count < 2 ? "Select two or more images first"
                    : `Holds ${preset.n} images; ${count} are selected`);
            return `<button type="button" class="fb-layout-tile${on ? " is-on" : ""}"
                        data-act="layout:${preset.id}" ${ok ? "" : "disabled"}
                        title="${escape(tip)}">${FigureLayouts.diagram(preset)}
                        <span>${escape(preset.label)}</span></button>`;
        }).join("");
        const g = group?.layout;
        this.mountMenu(anchor, `
            <div class="fb-insp-section-head"><span>Layouts</span>
                <span class="fb-insp-aside">${count >= 2 ? `${count} images selected` : "Select two or more images"}</span></div>
            <div class="fb-layout-tiles fb-layout-tiles-4">${tiles}</div>
            <div class="fb-insp-grid2">
                <label class="fb-insp-field"><span class="fb-insp-field-label">Gap across</span>
                    <input type="text" inputmode="decimal" class="fb-insp-input" data-layout-gap="x"
                           value="${escape(String(Number((g ? g.gap_x_mm : gutter).toFixed(2))))}"
                           aria-label="Horizontal gap"><span class="fb-insp-suffix">mm</span></label>
                <label class="fb-insp-field"><span class="fb-insp-field-label">Gap down</span>
                    <input type="text" inputmode="decimal" class="fb-insp-input" data-layout-gap="y"
                           value="${escape(String(Number((g ? g.gap_y_mm : gutter).toFixed(2))))}"
                           aria-label="Vertical gap"><span class="fb-insp-suffix">mm</span></label>
            </div>`,
            (act) => {
                if (!act.startsWith("layout:")) return;
                const menu = this._lastLayoutsMenu;
                const gap = (axis) => {
                    const value = parseFloat(menu?.querySelector(`[data-layout-gap="${axis}"]`)?.value);
                    return Number.isFinite(value) && value >= 0 ? Math.min(50, value) : gutter;
                };
                // Lettering and fit are no longer asked here: panel labels are
                // switched in the Page view, and a layout keeps the fit its
                // group already has (fit, never crop, for a new one).
                prefs.fit = g ? g.fit : "fit";
                this.layoutSelection(act.slice(7), { gap_x_mm: gap("x"), gap_y_mm: gap("y") });
            }, "fb-layouts-card");
        this._lastLayoutsMenu = this.menu;
        // The switches and fields are not actions: a press on them must not
        // close the card, which every [data-act] row does.
        this.menu?.querySelectorAll("input").forEach((input) => {
            input.addEventListener("click", (event) => event.stopPropagation());
            // On a layout that exists, a gap typed here applies at once.
            input.addEventListener("change", () => {
                const value = parseFloat(String(input.value).replace(",", "."));
                if (!group?.layout || !(value >= 0)) return;
                // The server keeps a gap within 0-50 mm (normalize_group_layout).
                this.canvas.relayout(group.group_id,
                    { [input.dataset.layoutGap === "x" ? "gap_x_mm" : "gap_y_mm"]: Math.min(50, value) });
            });
        });
    }

    /**
     * "Start from a layout": remember the layout chosen on an empty page, and
     * lay the page's images out in it as soon as there are enough of them to
     * fill it -- however they arrive, from Assets, a file or a viewer.
     * Pressing the chosen one again lets go of it.
     */
    chooseStarter(presetId) {
        this.pendingLayout = this.pendingLayout === presetId ? null : presetId;
        this.renderStarters();
        const preset = FigureLayouts.byId(presetId);
        if (this.pendingLayout && preset) {
            this.toast(`Add ${preset.n} images and they will be laid out ${preset.label}.`,
                       { label: "Add images", act: () => this.showTab("assets") });
        }
    }

    renderStarters() {
        const host = this.el("fb_page_starters");
        if (!host) return;
        host.innerHTML = ["2x2", "3x2", "top3", "left2"].map((id) => {
            const preset = FigureLayouts.byId(id);
            const on = this.pendingLayout === id;
            return `<button type="button" class="fb-layout-tile${on ? " is-on" : ""}" data-starter="${id}"
                        aria-pressed="${on}" title="${FigureSchema.escapeHtml(preset.label)}: fills as images are added">
                    ${FigureLayouts.diagram(preset)}<span>${FigureSchema.escapeHtml(preset.label)}</span></button>`;
        }).join("");
    }

    /** Apply a remembered starter layout once the page can fill it. */
    fillStarter() {
        const preset = this.pendingLayout && FigureLayouts.byId(this.pendingLayout);
        if (!preset || this._fillingStarter) return;
        const ids = FigureSchema.panelsOnPage(this.state.document, this.canvas.pageId)
            .filter((panel) => this.canvas.isEditable(panel.panel_id)).map((panel) => panel.panel_id);
        if (ids.length < preset.n) return;
        this.pendingLayout = null;
        this.renderStarters();
        this._fillingStarter = true;
        // After this render, not inside it: the layout is a commit of its own.
        window.setTimeout(() => {
            this._fillingStarter = false;
            this.canvas.select(ids.slice(0, preset.n), false, true);
            this.layoutSelection(preset.id);
        }, 0);
    }

    /** Lay the selected images out in `presetId`. */
    layoutSelection(presetId, gaps) {
        const ids = this.selectedImageIds();
        const prefs = this.layoutPrefs || (this.layoutPrefs = { label: false, fit: "fit" });
        const gutter = this.state.document.settings.style.gutter_mm;
        const groupId = this.canvas.applyLayout(ids, {
            preset: presetId,
            gap_x_mm: gaps ? gaps.gap_x_mm : gutter,
            gap_y_mm: gaps ? gaps.gap_y_mm : gutter,
            fit: prefs.fit,
        }, { label: prefs.label });
        if (groupId) this.canvas.select(ids, false, true);
    }

    /** "Lay out all images": select every image on the page, then offer the
     *  layouts for them. */
    layoutAllImages() {
        const ids = FigureSchema.panelsOnPage(this.state.document, this.canvas.pageId)
            .filter((panel) => this.canvas.isEditable(panel.panel_id))
            .map((panel) => panel.panel_id);
        if (ids.length < 2) {
            this.toast("A layout needs two or more images on this page.");
            return;
        }
        this.canvas.select(ids, false, true);
        this.openLayouts();
    }

    /** The tray's "Add as layout": place the selected assets, then lay them
     *  out in the first preset that holds them all. */
    placeTrayAsLayout() {
        const ids = Array.from(this.traySelection).filter((id) => this.state.panel(id));
        if (ids.length < 2) return;
        this.placeFromTray(ids);
        const preset = FigureLayouts.suggested(ids.length)[0]
            || FigureLayouts.PRESETS.find((each) => FigureLayouts.fits(each, ids.length));
        if (!preset) return;
        this.canvas.select(ids, false, true);
        this.layoutSelection(preset.id);
    }

    // -- the scientific furniture ------------------------------------------

    /** Letter panels A, B, C… in reading order: the selected images, or every
     *  image on the page when none is selected. Typed letters become automatic
     *  again, which is the point of the command. */
    relabelSelectionOrPage() {
        const ids = this.selectedImageIds();
        this.relabel(ids.length ? ids : FigureSchema.panelsOnPage(this.state.document, this.canvas.pageId)
            .map((panel) => panel.panel_id));
    }

    relabel(panelIds) {
        this.updatePanels(panelIds.map((id) => this.state.panel(id)).filter(Boolean)
            .map((panel) => ({ panel_id: panel.panel_id,
                               changes: { label: { ...panel.label, visible: true, auto: true, text: "" } } })));
    }

    /** Scale bars on for the selected images. Nothing about the length is
     *  decided here: a calibrated image measures in microns, an uncalibrated
     *  one in pixels (`compose.scale_bar`). */
    scaleBarsOn() {
        const ids = this.selectedImageIds();
        if (!ids.length) {
            this.toast("Select the images to put scale bars on.");
            return;
        }
        this.updatePanels(ids.map((id) => this.state.panel(id))
            .map((panel) => ({ panel_id: panel.panel_id,
                               changes: { scalebar: { ...panel.scalebar, visible: true } } })));
    }

    /** Colour bars on for the selected images: one intensity ramp per
     *  visible channel, in raw units, drawn from the panel's own windows. */
    colorBarsOn() {
        const ids = this.selectedImageIds();
        if (!ids.length) {
            this.toast("Select the images to put color bars on.");
            return;
        }
        this.updatePanels(ids.map((id) => this.state.panel(id))
            .map((panel) => ({ panel_id: panel.panel_id,
                               changes: { colorbar: { ...panel.colorbar, visible: true } } })));
    }

    /** One caption per visible channel, in the channel's colour -- the image
     *  panel's "Channels" preset, applied from here. */
    channelCaptions() {
        const ids = this.selectedImageIds();
        if (!ids.length) {
            this.toast("Select the images to name the channels on.");
            return;
        }
        // The image panel's own preset, so the words and colours are exactly
        // the ones its "Channels" row would write.
        const updates = ids.map((id) => this.state.panel(id)).map((panel) => ({
            panel_id: panel.panel_id,
            added: this.imagePanel.presetEntries(panel, { preset: "channels" }),
            labels: panel.labels || [],
        })).filter((entry) => entry.added.length)
            .map((entry) => ({ panel_id: entry.panel_id,
                               changes: { labels: [...entry.labels, ...entry.added] } }));
        if (!updates.length) {
            this.toast("The selected images have no channels to name.");
            return;
        }
        this.updatePanels(updates);
    }

    // -- preview -----------------------------------------------------------

    /**
     * The figure as it exports: no columns, no handles, no guides, no badges.
     *
     * The selection, the zoom and the scroll are kept and given back, so
     * leaving is going back to work rather than starting over. Drawn by the
     * same renderer as editing -- everything editing adds is chrome the
     * stylesheet hides under `.is-preview`, so the two cannot drift apart.
     */
    togglePreview(on) {
        const want = on === undefined ? !this.preview : Boolean(on);
        if (want === Boolean(this.preview)) return;
        const root = this.el("fb_workspace");
        const scroll = this.el("fb_canvas_scroll");
        if (want) {
            this.closeMenu();
            this.contextBar?.closePopover();
            this.closeResolution();
            if (this.tool !== "select") this.setTool("select");
            this.preview = {
                selection: Array.from(this.canvas.selection),
                scale: this.canvas.scale,
                left: scroll?.scrollLeft || 0, top: scroll?.scrollTop || 0,
            };
            this.canvas.select([], false);
            root?.classList.add("is-preview");
            const spec = this.el("fb_preview_spec");
            if (spec) {
                const pages = this.state.pages;
                const at = pages.findIndex((page) => page.page_id === this.canvas.pageId);
                const format = (this.el("fb_export_format")?.value || "pdf").toUpperCase();
                spec.textContent = `${format} · ${this.state.document.settings.dpi_default} DPI`
                    + ` · page ${at + 1} of ${pages.length}`;
            }
            this.zoomToFit();
        } else {
            const saved = this.preview;
            this.preview = null;
            root?.classList.remove("is-preview");
            this.canvas.setScale(saved.scale);
            this.afterZoom();
            if (scroll) {
                scroll.scrollLeft = saved.left;
                scroll.scrollTop = saved.top;
            }
            this.canvas.select(saved.selection.filter((id) => this.canvas.objectById(id)), false, true);
        }
        const button = this.el("fb_preview");
        if (button) {
            button.setAttribute("aria-pressed", String(want));
            button.classList.toggle("is-on", want);
            const word = button.querySelector(".fb-preview-word");
            if (word) word.textContent = want ? "Back to editing" : "Preview";
        }
    }

    // -- the resolution warning --------------------------------------------

    /**
     * What a panel's low-resolution badge means, and what can be done.
     *
     * Numbers first -- the source's pixels, the printed size, the result in
     * pixels per inch against the export setting -- because "low resolution"
     * on its own is a verdict nobody can check. Then the three remedies that
     * actually add detail or stop asking for it; never "upsample", which
     * cannot recover anything the source does not hold.
     */
    showResolution(panelId) {
        const panel = this.state.panel(panelId);
        const quality = panel && FigureCanvas.panelQuality(panel, this.state.document);
        const host = this.el("fb_overlay_layer");
        if (!quality || !host) return;
        this.closeResolution();
        if (!this.canvas.selection.has(panelId) || this.canvas.selection.size !== 1) {
            this.canvas.select([panelId], false, true);
        }
        const unit = this.inspector?.unit || { per: 1, label: "mm" };
        const show = (mm) => String(Number((mm / unit.per).toFixed(unit.label === "in" ? 2 : 1)));
        const place = panel.placement;
        const card = document.createElement("div");
        card.className = "fb-resolution";
        card.setAttribute("role", "dialog");
        card.setAttribute("aria-label", "Low effective resolution");
        const shrinkTo = Math.min(quality.fit_w_mm, place.w_mm);
        card.innerHTML = `
            <div class="fb-resolution-head">
                ${FigureIcons.fa("triangle-exclamation")}
                <strong>Low effective resolution</strong>
                <button type="button" class="fb-icon-button" data-res="close" aria-label="Close">
                    ${FigureIcons.fa("xmark")}</button>
            </div>
            <dl class="fb-resolution-facts">
                <dt>Source</dt><dd>${quality.source_w} × ${quality.source_h} px${quality.cropped
                    ? ` <span>(of ${quality.frame_w} × ${quality.frame_h})</span>` : ""}</dd>
                <dt>Size on page</dt><dd>${show(place.w_mm)} × ${show(place.h_mm)} ${unit.label}</dd>
                <dt>Effective resolution</dt><dd class="is-strong">${quality.ppi} ppi</dd>
                <dt>Export setting</dt><dd>${quality.dpi} DPI</dd>
            </dl>
            <p>At this size the panel holds fewer pixels than the export prints. It will export,
                but enlarging cannot add detail the source does not have.</p>
            <div class="fb-resolution-actions">
                <button type="button" class="fb-button" data-res="shrink">
                    Shrink to ${show(shrinkTo)} ${unit.label} wide (${quality.dpi} ppi)</button>
                <button type="button" class="fb-button" data-res="replace">Replace with another image…</button>
                <button type="button" class="fb-button" data-res="source"
                    ${this.state.source(panel.source_id)?.kind === "imported_asset" ? "disabled" : ""}>
                    Open the source to capture it larger</button>
            </div>
            <p class="fb-resolution-note">The threshold is three quarters of the export DPI, which
                is set under Figure style with nothing selected.</p>`;
        host.appendChild(card);
        this.resolutionCard = card;
        // The floating bar is about the same panel and would sit on top of
        // the card that explains it.
        this.contextBar?.suppress(true);
        // Beside the panel, kept inside the canvas.
        const el = this.canvas.surfaceEl.querySelector(`[data-panel-id="${FigureSchema.cssEscape(panelId)}"]`);
        const box = el?.getBoundingClientRect();
        const frame = host.getBoundingClientRect();
        if (box) {
            const width = card.offsetWidth || 300;
            const height = card.offsetHeight || 280;
            let left = box.right - frame.left + 8;
            if (left + width > frame.width - 8) left = Math.max(8, box.left - frame.left - width - 8);
            const top = Math.max(8, Math.min(frame.height - height - 8, box.top - frame.top));
            card.style.left = `${left}px`;
            card.style.top = `${top}px`;
        }
        card.addEventListener("click", (event) => {
            const act = event.target.closest("[data-res]")?.dataset.res;
            if (!act) return;
            if (act === "shrink") {
                const k = shrinkTo / place.w_mm;
                this.canvas.setBox(panelId, { w_mm: place.w_mm * k, h_mm: place.h_mm * k });
            } else if (act === "replace") {
                this.openReplace(panelId, event.target.closest("[data-res]"));
                return;
            } else if (act === "source") {
                this.onEditPanel(panelId);
            }
            this.closeResolution();
        });
        this._onResolutionDismiss = (event) => {
            if (card.contains(event.target) || event.target.closest?.("[data-lowres]")) return;
            this.closeResolution();
        };
        document.addEventListener("pointerdown", this._onResolutionDismiss, true);
        card.querySelector("[data-res='shrink']")?.focus();
    }

    closeResolution() {
        if (this._onResolutionDismiss) {
            document.removeEventListener("pointerdown", this._onResolutionDismiss, true);
            this._onResolutionDismiss = null;
        }
        if (this.resolutionCard) this.contextBar?.suppress(false);
        this.resolutionCard?.remove();
        this.resolutionCard = null;
    }

    // -- replacing a panel's image -----------------------------------------

    /**
     * Put another of the figure's images in this panel's place.
     *
     * The PLACE is kept -- position, size, stacking, letter, scale-bar and
     * caption settings, layout and group membership -- and the IMAGE changes.
     * The panel that was there goes back to the figure's assets rather than
     * being deleted: it may be the only record of a view. One commit.
     *
     * The choice is from the images in this figure that are not on a page;
     * a capture from a viewer or a file arrives there first.
     */
    openReplace(panelId, anchor) {
        const candidates = FigureSchema.panelsInTray(this.state.document);
        const at = anchor || this.el("fb_menu_add");
        if (!candidates.length) {
            this.toast("There is no other image in this figure to use. Capture one or add an image file first.",
                       { label: "Show assets", act: () => this.showTab("assets") });
            return;
        }
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const tiles = candidates.slice(0, 24).map((panel) => {
            const source = this.state.source(panel.source_id);
            const name = FigureSchema.panelCaption(panel)
                || (source && (source.display_name || source.datasource)) || "Image";
            return `<button type="button" class="fb-replace-tile" data-act="${escape(panel.panel_id)}"
                        title="${escape(name)}">
                <img alt="" src="${escape(this.canvas.panelImageUrl(panel, source))}">
                <span>${escape(name)}</span></button>`;
        }).join("");
        this.mountMenu(at, `<div class="fb-insp-section-head"><span>Replace with</span>
                <span class="fb-insp-aside">Images in this figure, not on a page</span></div>
            <div class="fb-replace-grid">${tiles}</div>`,
            (replacement) => this.replacePanel(panelId, replacement), "fb-replace-card");
    }

    replacePanel(oldId, newId) {
        const old = this.state.panel(oldId);
        const next = this.state.panel(newId);
        if (!old || !next || !old.placement || next.placement) return;
        this.closeResolution();
        const placement = { ...old.placement };
        const furniture = {
            label: { ...old.label },
            scalebar: { ...next.scalebar, ...FigureWorkspace.barLook(old.scalebar) },
        };
        const operations = [
            { op: "move_panels", moves: [{ panel_id: oldId, placement: null },
                                         { panel_id: newId, placement: placement }] },
            { op: "update_panel", panel_id: newId, changes: furniture },
        ];
        const group = this.canvas.groupFor(oldId);
        let newGroup = null;
        if (group) {
            newGroup = { ...group, member_ids: group.member_ids.map((id) => (id === oldId ? newId : id)) };
            operations.push({ op: "ungroup_items", group_id: group.group_id },
                            { op: "group_items", group: newGroup });
        }
        this.state.commit(operations, (draft) => {
            draft.panels[oldId].placement = null;
            draft.panels[newId].placement = placement;
            Object.assign(draft.panels[newId], furniture);
            if (newGroup) draft.groups[newGroup.group_id] = newGroup;
        });
        this.canvas.select([newId], false, true);
    }

    /** How a scale bar LOOKS -- what carries over to the image that replaces
     *  a panel's. The length and unit stay the new image's own, because they
     *  depend on its calibration. */
    static barLook(bar) {
        const out = {};
        for (const key of ["visible", "position", "color", "thickness_mm", "margin_mm",
                           "label", "label_size_pt"]) {
            if (bar && key in bar) out[key] = bar[key];
        }
        return out;
    }

    /** Show another page. The selection belongs to the page it was made on,
     *  so it is let go; everything that describes "this page" is redrawn. */
    goToPage(pageId) {
        this.closeResolution();
        this.canvas.selection.clear();
        this.canvas.setPage(pageId);
        this.selectionChanged([]);
        this.render();
    }

    /** A change to the current page's size, orientation or margins. */
    updatePage(changes) {
        const pageId = this.canvas.pageId;
        if (!pageId) return;
        this.state.commit([{ op: "update_page", page_id: pageId, changes: changes }],
            (draft) => {
                const page = draft.pages.find((entry) => entry.page_id === pageId);
                if (page) Object.assign(page, changes);
            });
        // A page of a new size is fitted again, or the sheet is left half off
        // the window (or a postage stamp in the middle of it).
        window.requestAnimationFrame?.(() => this.zoomToFit());
        // Nothing is moved to suit the new size -- that would rearrange a
        // figure behind the user's back -- but anything left hanging off the
        // sheet is pointed out, with the one-press remedy.
        const page = this.canvas.page;
        const off = page && this.canvas.pageItems().some((item) => {
            const box = FigureCanvas.unionBox([item.box]);
            return box.x_mm < -0.01 || box.y_mm < -0.01
                || box.x_mm + box.w_mm > page.size_mm.w + 0.01
                || box.y_mm + box.h_mm > page.size_mm.h + 0.01;
        });
        if (off) {
            this.toast("Some of the figure is now off the page.",
                       { label: "Fit content to page", act: () => this.canvas.fitContentToPage() });
        }
    }

    /**
     * The tray, entirely delegated.
     *
     * It is re-rendered on every document change, so handlers bound to its
     * items would be rebound continuously and leak the ones that were replaced.
     */
    bindTray() {
        const strip = this.el("fb_tray_strip");
        if (!strip) return;

        strip.addEventListener("pointerdown", (event) => {
            const item = event.target.closest(".fb-tray-item");
            if (item && !item.dataset.gotoPanel) this.trayPointerDown(item.dataset.panelId, event);
        });
        strip.addEventListener("click", (event) => {
            const item = event.target.closest("[data-goto-panel]");
            const panel = item && this.state.panel(item.dataset.gotoPanel);
            if (!panel || !panel.placement) return;
            if (panel.placement.page_id !== this.canvas.pageId) this.goToPage(panel.placement.page_id);
            this.canvas.select([panel.panel_id], false, true);
        });
        strip.addEventListener("dragstart", (event) => {
            const item = event.target.closest(".fb-tray-item");
            if (!item) return;
            // The whole tray selection travels if the dragged item is part of
            // it -- picking four panels and then dragging one of them plainly
            // means all four.
            const ids = this.traySelection.has(item.dataset.panelId)
                ? Array.from(this.traySelection)
                : [item.dataset.panelId];
            event.dataTransfer.setData("text/x-plexora-panel", JSON.stringify(ids));
            event.dataTransfer.effectAllowed = "move";
        });
        strip.addEventListener("dblclick", (event) => {
            const item = event.target.closest(".fb-tray-item");
            if (item) this.placeFromTray([item.dataset.panelId]);
        });
        // The desktop app's window cannot land an HTML5 drag (see
        // services/pointerDrag.js); the same drag onto the page, by pointer.
        if (window.PlexoraDesktop && window.PlexoraPointerDrag) {
            window.PlexoraPointerDrag.attach(strip, {
                source: ".fb-tray-item",
                payload: (item) => (this.traySelection.has(item.dataset.panelId)
                    ? Array.from(this.traySelection) : [item.dataset.panelId]),
                label: (ids) => (ids.length === 1 ? "1 panel" : `${ids.length} panels`),
                targetAt: (under) => {
                    const surface = this.canvas?.surfaceEl;
                    return surface && surface.contains(under) ? surface : null;
                },
                onOver: (target, over) => target.classList.toggle("is-drop-target", over),
                onDrop: (_target, ids, point) => this.canvas?.placeTrayPanelsAt(ids, point),
            });
            // The Assets grids: the figure's own images and captures alike.
            const pane = this.el("fb_assets_pane");
            if (pane) {
                window.PlexoraPointerDrag.attach(pane, {
                    source: "[data-drag]",
                    payload: (card) => (this.assetGrids || {})[this.assetsPane || "captures"]
                        ?.payloadFor(card) || null,
                    label: (data) => {
                        const n = data.panels.length + data.captures.length;
                        return n === 1 ? "1 image" : `${n} images`;
                    },
                    targetAt: (under) => {
                        const surface = this.canvas?.surfaceEl;
                        return surface && surface.contains(under) ? surface : null;
                    },
                    onOver: (target, over) => target.classList.toggle("is-drop-target", over),
                    onDrop: (_target, data, point) => {
                        if (data.panels.length) this.canvas?.placeTrayPanelsAt(data.panels, point);
                        if (data.captures.length) this.placeCapturesAt(data.captures, point);
                    },
                });
            }
        }

        // Filters as the characters arrive; nothing is submitted. Esc and the ×
        // are the way back out -- a filter still on from five minutes ago looks
        // exactly like a tray that has lost panels, and the names it would be
        // recognised by are behind a hover now.
        const search = this.el("fb_tray_search");
        search?.addEventListener("input", (event) => {
            this.setTraySearch(event.target.value || "");
        });
        this.setupAssetFilter();
        search?.addEventListener("keydown", (event) => {
            if (event.key !== "Escape" || !this.traySearch) return;
            // Kept off the window: Escape out here also clears the canvas
            // selection, and one key should not do two things at once.
            event.stopPropagation();
            this.clearTraySearch();
        });
        this.el("fb_tray_search_clear")?.addEventListener("click", () => {
            this.clearTraySearch();
            search?.focus();
        });

        // Which source the tab shows. The captures bin and the bookmarks are
        // mounted the first time they are asked for and refreshed every time
        // after, so a capture taken in another tab shows up on the next look.
        this.el("fb_tray_panel")?.addEventListener("click", (event) => {
            const pick = event.target.closest("[data-assets]");
            if (pick) this.showAssets(pick.dataset.assets);
        });

        // The batch row, shown while two or more assets are selected.
        this.el("fb_tray_place")?.addEventListener("click",
            () => this.placeFromTray(Array.from(this.traySelection)));
        this.el("fb_tray_layout")?.addEventListener("click", () => this.placeTrayAsLayout());

        // Where the next panels come from: one press, one page load, no list.
        this.el("fb_tray_add")?.addEventListener("click", () => this.addPanels());
        this.el("fb_tray_bookmark_go")?.addEventListener("click", () => this.bookmarkInViewer());
        this.el("fb_tray_file")?.addEventListener("click",
            () => this.el("fb_add_image_input")?.click());
        // The third way, and the only one that needs no trip: captures already
        // taken and not yet in any figure. Before the bin existed there was
        // nothing to offer here -- an unassigned capture lived in the viewer
        // page's memory and died with it.
        this.el("fb_tray_bin")?.addEventListener("click", () => this.openCaptureBin());
        // And the views saved in the viewer (M): to go back to, or straight
        // onto the page.
        this.el("fb_tray_bookmarks")?.addEventListener("click", () => this.openBookmarks());

        this.el("fb_add_image_input")?.addEventListener("change", (event) => {
            const files = Array.from(event.target.files || []);
            // Cleared straight away, or choosing the same file twice in a row
            // fires nothing the second time and reads as a broken button.
            event.target.value = "";
            if (files.length) this.importFiles(files);
        });
    }

    // -- the inspector column -------------------------------------------------

    /**
     * Kept for the four object panels' `onClose` and for Quick Edit, which
     * call it when what they show may have changed. There is no strip to
     * arbitrate any more: the inspector shows every panel that has something
     * to say, under the block that describes the selection.
     */
    showSidebar(name) {
        this.sidebar = name || null;
        this.applyInspector();
    }

    contextSidebar() {
        this.applyInspector();
    }

    /**
     * Which of the four object panels are in the inspector, and whether the
     * inspector is there at all.
     *
     * Each panel says whether it has anything to show (`wants`); the panels'
     * own close buttons are hidden in this column, so `reveal()` first undoes
     * a dismissal a previous version of the page could have stored. Quick Edit
     * takes the column while it is open: it is a dark slide-over about one
     * panel's image, and the inspector beside it would be a second description
     * of the same panel in another colour scheme.
     */
    applyInspector() {
        const quick = Boolean(this.quickEdit?.session);
        const root = this.el("fb_workspace");
        root?.classList.toggle("is-quickediting", quick);
        const right = this.el("fb_right");
        // NOT hidden while Quick Edit is open: the card floats over this
        // column, and taking the column out of the row widened the canvas
        // under it, which re-centred the page -- the figure moved every time
        // Quick Edit opened or closed. `is-quickediting` only makes it inert.
        if (right) {
            right.hidden = !this.columns.right;
            right.inert = quick;
        }
        for (const [name, id] of Object.entries(FigureWorkspace.SIDEBARS)) {
            const el = this.el(id);
            const panel = { text: this.textPanel, shape: this.shapePanel,
                            line: this.linePanel, image: this.imagePanel }[name];
            if (!el || !panel) continue;
            panel.reveal?.();
            el.hidden = !panel.wants;
        }
        this.renderRail();
    }

    /** The four object panels, by name, and the element each one draws into. */
    static get SIDEBARS() {
        return { text: "fb_text_panel", shape: "fb_shape_panel",
                 line: "fb_line_panel", image: "fb_image_panel" };
    }

    /** The unit the page is measured in, changed from wherever names it. */
    setUnits(unit) {
        this.viewOptions?.setUnit(unit);
        this.syncCanvasUnit();
        this.canvas.render();
        this.inspector?.render();
    }

    /** The canvas writes sizes (under a selection) in the figure's unit. */
    syncCanvasUnit() {
        const name = this.viewOptions?.prefs.units || "mm";
        const spec = FigureViewOptions.UNITS[name] || FigureViewOptions.UNITS.mm;
        this.canvas.unit = { per: spec.per, label: spec.label };
    }

    /** Open the left column on the figure's assets. `open === false` goes back
     *  to Layers, which is what "closing the tray" means now. */
    showTray(open) {
        this.showTab(open === false ? "layers" : "assets");
    }

    /**
     * Where the next panels come from: the project page.
     *
     * A page load, with THIS figure remembered as the capture destination
     * first -- the same localStorage key the dock over there reads, because
     * "which figure am I capturing into" is one setting and this is plainly the
     * figure the user is looking at. Nothing is written to the figure by going:
     * a slide somebody looks at and leaves, leaves no trace.
     *
     * This was a menu of the server's eight most recent projects with "All
     * projects…" under them, which is the project page's whole job done twice
     * and done worse -- in a card 312px wide, with the names clipped and the
     * entry that opens the page that does it properly at the bottom. It is also
     * the state this page opens in: a new figure is empty, so the first thing
     * anybody does here is this, and the first thing anybody did was read a
     * list.
     *
     * Two ways in, because the trip is worth a permanent button in both places
     * it is wanted from: the tray it fills, and beside the name of the figure it
     * fills -- which is where somebody who has just made one is looking.
     */
    addPanels() {
        // The viewer it leads to opens Figure Builder (figureArrival.js), and
        // not the bookmarks tray, whatever an earlier trip left on.
        this.leaveArrival({ figureBuilder: true, bookmarks: false });
        this.rememberDestination();
        PlexoraRouter.go(this.api.url("open_project"));
    }

    /** What the next viewer opens: Figure Builder's capture tool (a note
     *  figureArrival.js takes once), and core's bookmarks tray (its own key,
     *  client/src/js/views/bookmarkTray.js, read when a viewer starts). */
    leaveArrival({ figureBuilder, bookmarks }) {
        try {
            if (figureBuilder) window.sessionStorage.setItem("plexora.figure_builder.openOnArrival", "1");
            else window.sessionStorage.removeItem("plexora.figure_builder.openOnArrival");
            window.sessionStorage.setItem("plexora.bookmarkTray.open", bookmarks ? "1" : "0");
        } catch (error) {
            /* Private-browsing modes throw; the trip is still worth making. */
        }
    }

    /**
     * The same trip, to bookmark views instead: the viewer's bookmarks tray
     * opens by itself when the image does. The key is core's
     * (client/src/js/views/bookmarkTray.js, OPEN_KEY): the tray reads it,
     * per tab, when a viewer page starts.
     */
    bookmarkInViewer() {
        // The bookmarks tray only. The tray keeps its own key open after
        // this; the next "Capture from a viewer" turns it back off.
        this.leaveArrival({ figureBuilder: false, bookmarks: true });
        this.rememberDestination();
        PlexoraRouter.go(this.api.url("open_project"));
    }

    /**
     * Point the viewer's capture dock at THIS figure before leaving for it.
     *
     * The key is read off the sidebar controller rather than spelled again
     * here: two copies of a storage key is how a rename turns into captures
     * quietly landing in the figure somebody was working on last week.
     */
    rememberDestination() {
        try {
            window.localStorage.setItem(
                FigureBuilderSidebarController.STORAGE_KEY, this.figureId);
        } catch (error) {
            /* Private-browsing modes throw. The trip is still worth making --
               the picker over there puts this figure first when it asks. */
        }
    }

    // -- captures arriving from the bin ----------------------------------

    /**
     * Take up the note the destination picker left, if it names this figure.
     *
     * This is where adoption happens -- after the navigation rather than
     * before it. The viewer used to have to write every waiting capture into a
     * figure BEFORE it could leave, because the strip over there was the only
     * copy of one; a write that failed had to cancel the trip. Captures are in
     * the bin now, so the picker's answer is just a note, and the work is done
     * here with the document already open in front of the user.
     *
     * Read once and cleared (see FigureCaptureBin.takeAdoptNote), so reloading
     * this page cannot add the same captures twice.
     */
    async adoptFromBin() {
        const note = FigureCaptureBin.takeAdoptNote();
        if (!note || note.figure_id !== this.figureId || !note.capture_ids.length) return;
        await this.adoptCaptures(note.capture_ids);
    }

    /**
     * Turn bin captures into panels of this figure.
     *
     * The ids and not the rows, because both callers have only ids: the note
     * carries them across a navigation, and the bin dialog has them from a
     * selection the user made in it. The rows are re-read here, which is also
     * the check that the captures still exist -- a second tab may have adopted
     * or deleted them in between, and a silent no-op is the right answer to
     * "add these" when there is nothing left to add.
     */
    async adoptCaptures(captureIds, { place = false, at = null } = {}) {
        const bin = new FigureCaptureBin({ api: this.api });
        const rows = await bin.list();
        if (!rows) {
            this.toast("Your captures bin could not be read, so nothing was added.");
            return false;
        }
        const wanted = new Set(captureIds);
        const entries = rows.filter((row) => wanted.has(row.capture_id));
        if (!entries.length) return false;

        const onPage = place && this.canvas?.page;
        const result = await FigureCaptureBin.adoptInto(this.state, this.api, entries,
            onPage ? { place: (panels) => this.placementsFor(panels, at) } : {});
        // Added, but the server would not take them out of the bin: say so,
        // or the same captures are added again from there.
        const kept = result.binKept
            ? " They are still in your captures bin too — remove them there"
              + " so they are not added twice."
            : "";
        if (onPage) {
            if (result.panelIds.length) {
                this.toast(`${FigureSchema.countPhrase(result.panelIds.length, "view")} added to the figure.${kept}`);
            } else if (result.failed) {
                this.toast("That view could not be added to the figure.");
            }
            return result.panelIds.length > 0;
        }
        if (result.panelIds.length) {
            // Into the tray, where they can be seen: composition is a separate
            // decision from capture, so nothing is placed on the page. The
            // strip is opened because a panel that arrived into a closed drawer
            // is a panel the user has no reason to believe arrived.
            this.assetsPane = entries.every((row) => row.bookmarked) ? "bookmarks" : "captures";
            this.showTab("assets");
            this.renderTray();
        }
        // ONE toast, because `toast` shows one at a time -- a second call would
        // replace the first and the user would only ever read the bad news or
        // only ever the good.
        const added = result.panelIds.length
            ? `${FigureSchema.countPhrase(result.panelIds.length, "panel")} added from your captures.${kept}`
            : "";
        const lost = result.failed
            ? `${FigureSchema.countPhrase(result.failed, "capture")} could not be added — `
              + "still in your captures bin."
            : "";
        if (added || lost) this.toast([added, lost].filter(Boolean).join(" "));
        return result.panelIds.length > 0;
    }

    /** Where new panels go on the page: free places, the size a tray panel
     *  is placed at (FigureCanvas.traySizes), above everything there. */
    placementsFor(panels, atPoint = null) {
        const page = this.canvas?.page;
        if (!page) return null;
        const width = Math.min(60, page.size_mm.w / 3);
        const sizes = panels.map((panel) => {
            const viewport = panel.scene && panel.scene.viewport;
            const frame = viewport && FigureSchema.frameSize(viewport);
            const aspect = (frame && frame.w) ? frame.h / frame.w : 1;
            return { w_mm: width, h_mm: width * (aspect || 1) };
        });
        // Dropped: the first lands centred under the pointer, the rest flow
        // from there, as a drop of the figure's own images does.
        const origin = atPoint && sizes.length ? {
            x_mm: Math.max(page.margins_mm.left, atPoint.x - width / 2),
            y_mm: Math.max(page.margins_mm.top, atPoint.y - sizes[0].h_mm / 2),
        } : null;
        const boxes = FigureCanvas.freePlacements(
            sizes, page, this.canvas.occupiedBoxes(),
            this.state.document.settings.style.gutter_mm, origin);
        let z = this.canvas.nextZ();
        return boxes.map((box) => ({ page_id: page.page_id, ...box, z: z++ }));
    }

    /**
     * The views saved in the viewer (M), without going through the captures:
     * each one can be gone back to -- the viewer opens at it -- or put
     * straight onto this figure's page. The dialog stays open, so several can
     * be added in a row; each says it was.
     */
    openBookmarks() {
        const dialog = document.createElement("dialog");
        dialog.className = "fb-dialog fb-bin-dialog";
        dialog.innerHTML = `
            <div class="fb-bin-head">
                <h2>Bookmarks</h2>
                <button class="fb-picker-close" type="button" data-role="close"
                        title="Close" aria-label="Close">
                    ${FigureIcons.fa("xmark")}
                </button>
            </div>
            <div data-role="host"></div>`;
        (this.root || document.body).appendChild(dialog);

        const grid = new FigureCaptureBinGrid(dialog.querySelector('[data-role="host"]'), {
            api: this.api,
            bookmarks: true,
            onGo: (capture) => {
                dialog.close();
                this.api.openViewAt(capture.datasource, capture.capture_id);
            },
            onAddOne: (capture) => this.adoptCaptures([capture.capture_id], { place: true }),
        });
        grid.setup();

        dialog.addEventListener("click", (event) => {
            if (event.target.closest?.('[data-role="close"]')) dialog.close();
        });
        dialog.addEventListener("close", () => dialog.remove());
        if (typeof dialog.showModal !== "function") {
            dialog.remove();
            return;
        }
        dialog.showModal();
    }

    /**
     * Browse the whole bin and pick from it, without leaving the figure.
     *
     * The other two ways to more panels are trips: "Add panels" opens a
     * project to capture in, and the file button imports an image. This one
     * needs neither, because the captures are already taken -- which is the
     * state anybody who pressed "Figure Canvas" with captures unticked is in.
     *
     * Built per opening rather than kept in the markup, for FigureConfirm's
     * reason: one element per question cannot be confused with another, and
     * there is nothing to reset between openings.
     */
    openCaptureBin() {
        const dialog = document.createElement("dialog");
        dialog.className = "fb-dialog fb-bin-dialog";
        dialog.innerHTML = `
            <div class="fb-bin-head">
                <h2>Captures</h2>
                <button class="fb-picker-close" type="button" data-role="close"
                        title="Close" aria-label="Close">
                    ${FigureIcons.fa("xmark")}
                </button>
            </div>
            <div data-role="host"></div>`;
        // Inside the workspace, where the light palette is declared. A dialog
        // in the top layer is positioned by the viewport wherever it sits in
        // the tree, so there is nothing to pay for putting it where the tokens
        // are.
        (this.root || document.body).appendChild(dialog);

        const grid = new FigureCaptureBinGrid(dialog.querySelector('[data-role="host"]'), {
            api: this.api,
            addLabel: "Add to this figure",
            // The bookmarks have their own dialog and their own tab.
            excludeBookmarked: true,
            onAdd: (ids) => {
                // Closed first: the adoption takes a moment and the panels
                // appear in the tray behind, which a modal would be covering.
                dialog.close();
                this.adoptCaptures(ids);
            },
        });
        grid.setup();

        dialog.addEventListener("click", (event) => {
            if (event.target.closest?.('[data-role="close"]')) dialog.close();
        });
        dialog.addEventListener("close", () => dialog.remove());
        if (typeof dialog.showModal !== "function") {
            dialog.remove();
            return;
        }
        dialog.showModal();
    }

    // -- what this figure looks like, small -------------------------------

    /**
     * Redraw the thumbnail, once the saving has stopped.
     *
     * Debounced because a drag is a run of saves and only the last one is the
     * layout the user meant. Skipped when this workspace does not own the
     * document: there is one owner per figure (see the class comment), and two
     * of them racing to upload two pictures of the same page would be a
     * thumbnail that flickers between them.
     */
    scheduleThumbnail() {
        if (!this.ownsState) return;
        window.clearTimeout(this._thumbnailTimer);
        this._thumbnailTimer = window.setTimeout(
            () => this.storeThumbnail(), FigureWorkspace.THUMBNAIL_MS);
    }

    async storeThumbnail() {
        const document_ = this.state.document;
        if (!document_) return;
        // Once per revision. Autosave settles more than once on the same
        // document -- a preview upload, a status flap -- and re-rasterising the
        // page to send identical bytes is work for nothing.
        if (this._thumbnailRevision === document_.revision) return;
        this._thumbnailRevision = document_.revision;
        const blob = await FigureThumbnail.render(
            document_, this.canvas?.pageId,
            (panelId, renderRevision) =>
                this.api.previewUrl(this.figureId, panelId, renderRevision),
            { imageFor: (panelId) => this.shownImage(panelId) });
        // No toast and no status either way. A thumbnail is a convenience and
        // the figure is already saved; an error beside work that is safely
        // stored would be reporting a failure the user cannot act on.
        if (blob) await this.api.putThumbnail(this.figureId, blob);
    }

    /**
     * The picture of a panel this page is showing, decoded -- on the sheet, or
     * for a panel not placed yet, in the tray -- or null.
     */
    shownImage(panelId) {
        const id = FigureSchema.cssEscape(panelId);
        const candidates = [
            this.canvas?.surfaceEl?.querySelector?.(`.fb-panel[data-panel-id="${id}"] .fb-panel-image`),
            this.el("fb_tray_strip")?.querySelector?.(`[data-panel-id="${id}"] img`),
        ];
        return candidates.find((image) => image && image.complete && image.naturalWidth > 0) || null;
    }

    bindCanvasHost() {
        // Dropping an image onto the page imports it into THIS FIGURE and
        // nowhere else. Making the user create a project to put a schematic in
        // a figure is exactly the setup step this plugin exists to remove.
        const scroll = this.el("fb_canvas_scroll");
        if (!scroll) return;
        scroll.addEventListener("dragover", (event) => {
            const types = event.dataTransfer?.types || [];
            if (types.includes("Files") || types.includes("text/x-plexora-capture")) event.preventDefault();
        });
        scroll.addEventListener("drop", (event) => {
            this.dropCaptures(event);
            this.dropFiles(event);
        });
        // In the desktop app a file dropped from Explorer or Finder arrives as
        // a PATH from the shell rather than as a DOM drop (desktopBridge.js).
        // The page is a drop zone in core's registry (browsePicker.js), so it
        // lights and labels itself like every other target, a drop over a
        // dialog on top of it is not taken, and it is registered once per
        // bind rather than adding a document listener each time.
        this._detachDrop?.();
        if (typeof attachDropTarget === "function") {
            this._detachDrop = attachDropTarget(scroll, {
                filter: "image", mode: "file", count: "many",
                hint: (paths) => (paths.length > 1
                    ? `Add ${paths.length} images to this figure` : "Add to this figure"),
                onDrop: (paths, { x, y }) => {
                    this.importNativePaths(paths, { clientX: x, clientY: y });
                },
            });
        }
        // The floating bar is positioned from where the selection is on SCREEN,
        // so scrolling the page moves it.
        scroll.addEventListener("scroll", () => this.contextBar?.position(), { passive: true });

        // Clicking off the page puts the pointer back to selecting. ON the
        // page an armed tool is USED by the click and hands itself back the
        // moment it has placed something; the desk around the sheet is where a
        // tool armed by mistake is abandoned, and with no Select button in the
        // rail this is the way out that does not need the keyboard.
        //
        // Not while space is held: that is a pan, and the pointer is out here
        // because that is where the hand happened to be.
        scroll.addEventListener("pointerdown", (event) => {
            if (this.spaceHeld || this.tool === "select") return;
            if (event.target.closest("#fb_page")) return;
            this.setTool("select");
        });

        // Ctrl/Cmd + wheel zooms about the pointer. A trackpad pinch arrives as
        // exactly this event with ctrlKey set, which is why one handler serves
        // both and neither needs a gesture library.
        scroll.addEventListener("wheel", (event) => {
            if (!event.ctrlKey && !event.metaKey) return;
            event.preventDefault();
            // Exponential, so a fast scroll and several slow ones of the same
            // total distance land in the same place -- which is also what lets
            // the factors of one frame's events be multiplied together and
            // applied once. A trackpad sends well over 60 of these a second,
            // and each zoom re-renders the sheet and the rulers.
            const pending = this._pendingZoom || (this._pendingZoom = { factor: 1 });
            pending.factor *= Math.exp(-event.deltaY * 0.0035);
            pending.x = event.clientX;
            pending.y = event.clientY;
            if (!pending.frame) {
                pending.frame = requestAnimationFrame(() => {
                    const zoom = this._pendingZoom;
                    this._pendingZoom = null;
                    if (zoom && this.canvas) this.zoomAt(zoom.x, zoom.y, zoom.factor);
                });
            }
        }, { passive: false });

        this.bindSpacePan(scroll);
    }

    /**
     * Hold space to pan.
     *
     * The listener is on the SCROLL container in the capture phase, so it runs
     * before the canvas surface's own pointerdown and can stop it: without
     * that, a space-drag would start a marquee under the hand that is trying to
     * move the page.
     */
    bindSpacePan(scroll) {
        this._onSpaceDown = (event) => {
            if (event.code !== "Space" || this.spaceHeld) return;
            const typing = document.activeElement
                && ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName);
            if (typing) return;
            event.preventDefault();
            this.spaceHeld = true;
            scroll.classList.add("is-pannable");
        };
        this._releaseSpace = () => {
            this.spaceHeld = false;
            this.panning = null;
            scroll.classList.remove("is-pannable", "is-panning");
        };
        this._onSpaceUp = (event) => {
            if (event.code === "Space") this._releaseSpace();
        };
        window.addEventListener("keydown", this._onSpaceDown);
        window.addEventListener("keyup", this._onSpaceUp);
        // A window that loses focus mid-pan never sees the keyup, and would
        // come back still in pan mode with no key held down to explain it.
        window.addEventListener("blur", this._releaseSpace);

        scroll.addEventListener("pointerdown", (event) => {
            if (!this.spaceHeld || event.button !== 0) return;
            event.preventDefault();
            event.stopPropagation();
            scroll.classList.add("is-panning");
            this.panning = {
                x: event.clientX, y: event.clientY,
                left: scroll.scrollLeft, top: scroll.scrollTop,
            };
            scroll.setPointerCapture?.(event.pointerId);
        }, true);

        scroll.addEventListener("pointermove", (event) => {
            if (!this.panning) return;
            scroll.scrollLeft = this.panning.left - (event.clientX - this.panning.x);
            scroll.scrollTop = this.panning.top - (event.clientY - this.panning.y);
        });
        const stop = () => {
            if (!this.panning) return;
            this.panning = null;
            scroll.classList.remove("is-panning");
        };
        scroll.addEventListener("pointerup", stop);
        scroll.addEventListener("pointercancel", stop);
    }

    // -- tools -------------------------------------------------------------

    /**
     * Arm a rail tool, or put the pointer back to selecting.
     *
     * Tools are one-shot everywhere they place something: the rail disarms
     * itself afterwards, so a lit button always means "the next click on the
     * page does this" rather than a mode somebody has to remember to leave.
     * That is why there is no Select button to go back to -- "select" is the
     * state this page is in unless it has just been told otherwise, and the
     * ways out of a tool are placing something with it, clicking the desk, and
     * Escape.
     */
    setTool(name) {
        this.tool = name || "select";
        this.canvas.setTool(this.tool);
        this.renderRail();
    }

    /** Light Annotate while one of its drawing tools is armed: the button is
     *  the way back to the card, and the lit state says the next press on the
     *  page draws rather than selects. */
    renderRail() {
        const tool = this.tool || "select";
        const armed = tool !== "select";
        const button = this.el("fb_menu_annotate");
        if (button) {
            button.classList.toggle("is-active", armed);
            button.setAttribute("aria-pressed", String(armed));
        }
    }

    /**
     * Open Edit Points on a shape.
     *
     * The shape equivalent of `editText`, and deliberately the same shape of
     * method: put the canvas into a mode, then reveal the sidebar the mode's
     * controls live in, because entering the mode is exactly when they are
     * wanted -- including for a panel the user shut earlier for this object.
     *
     * Entering CONVERTS a preset to a custom path (`FigurePointEditor.enter`),
     * which is one undoable operation. That is why the two are not separated
     * into "convert" and "edit": there is no state in between for anyone to be
     * in, and a dialog asking about it would be a dialog on every use.
     */
    editPoints(annotationId) {
        if (!this.canvas?.pointEditor) return;
        this.canvas.pointEditor.enter(annotationId);
        this.shapePanel?.reveal();
        this.shapePanel?.update([annotationId]);
        this.contextSidebar();
    }

    // -- editing text in place -----------------------------------------------

    /**
     * Type into a text annotation where it sits.
     *
     * The editor itself is `FigureTextEditor` -- a contenteditable in the
     * overlay layer, which is where it has to be: `FigureCanvas.render()`
     * replaces the page surface wholesale on every change, and an editor
     * mounted inside it would be destroyed by the autosave triggered by the
     * last thing the user typed.
     *
     * This used to be a <textarea>, and the reason it no longer is, is the
     * whole feature: a textarea holds one style for its entire contents, so an
     * italic gene name inside a roman sentence was unreachable.
     */
    editText(annotationId, options) {
        this.textEditor?.open(annotationId, options);
        // Typing into a caption is exactly when its formatting is wanted, so
        // this overrides a panel the user shut earlier for the same object.
        this.textPanel?.reveal();
        this.textPanel?.update([annotationId]);
        this.contextSidebar();
    }

    /**
     * Put a character in at the caret.
     *
     * Opens the editor if it is not open, with the caret at the END rather than
     * over everything: the editor's own "select all on open" is right for
     * double-click-and-retype, and wrong for a palette whose whole purpose is
     * to ADD one character to what is already there.
     */
    insertSymbol(annotationId, glyph) {
        if (!this.textEditor) return;
        if (!this.textEditor.active || this.textEditor.annotationId !== annotationId) {
            this.textEditor.open(annotationId);
            const end = this.textEditor.plainLength();
            this.textEditor.setOffsets({ start: end, end: end });
        }
        this.textEditor.replaceSelection(glyph);
    }

    closeTextEditor() {
        this.textEditor?.close(true);
    }

    /** One annotation's style or text, from the context bar. */
    updateAnnotation(annotationId, changes) {
        const current = this.state.document.annotations[annotationId];
        if (!current) return;
        changes = this.withAutofit(current, changes);
        this.state.commit(
            [{ op: "update_annotation", annotation_id: annotationId, changes: changes }],
            (draft) => {
                const annotation = draft.annotations[annotationId];
                if (changes.style) Object.assign(annotation.style, changes.style);
                if (changes.geometry) Object.assign(annotation.geometry, changes.geometry);
                if ("text" in changes) annotation.text = changes.text;
                // `rich` is replaced whole, never merged -- half a line list
                // means nothing -- and `text` is re-derived from it here so the
                // optimistic draft matches what the server will send back.
                if ("rich" in changes) {
                    annotation.rich = changes.rich;
                    annotation.text = FigureRichText.plainText(changes.rich);
                }
            });
    }

    /**
     * Grow or shrink a text box to the height of what is in it.
     *
     * Computed HERE and sent as part of the same operation, rather than left to
     * the optimistic draft: a height that only ever existed in the browser is a
     * box that is the right size until the page is reloaded. Dragging the top or
     * bottom handle is what turns this off -- the gesture is the opt-out, so
     * there is no checkbox to find first.
     */
    withAutofit(annotation, changes) {
        const style = { ...annotation.style, ...(changes.style || {}) };
        if (annotation.type !== "text" || !style.autofit) return changes;
        const merged = {
            ...annotation, style: style,
            geometry: { ...annotation.geometry, ...(changes.geometry || {}) },
            rich: "rich" in changes ? changes.rich : annotation.rich,
        };
        const height = FigureCanvas.textLayout(merged).block_h_mm;
        if (Math.abs(height - merged.geometry.h_mm) < 1e-6) return changes;
        return { ...changes,
                 geometry: { ...(changes.geometry || {}), h_mm: height } };
    }

    // -- menus ---------------------------------------------------------------

    /**
     * A small menu under a topbar button.
     *
     * Appended to <body> with fixed positioning rather than inside the topbar:
     * the topbar is a flex row whose children are clipped, and a menu inside it
     * would be cut off by the button it hangs from.
     */
    openMenu(anchor, entries, onPick) {
        // A checkable entry keeps the tick's WIDTH whether it is ticked or not,
        // so the labels line up and the menu does not shuffle sideways as
        // things are turned on.
        // Entries may also carry an `icon` (a Font Awesome name, drawn in the
        // menus' icon gutter), a `hint` (ranged right and muted, like a key),
        // or be a `heading` that names the rows under it.
        const iconned = entries.some((entry) => entry.icon);
        this.mountMenu(anchor, entries.map((entry) => {
            if (entry.separator) return '<span class="fb-menu-separator"></span>';
            if (entry.heading) {
                return `<div class="fb-menu-heading">${FigureSchema.escapeHtml(entry.heading)}</div>`;
            }
            return `<button type="button" class="fb-menu-item" data-act="${entry.act}"
                       ${entry.disabled ? "disabled" : ""}>
                   ${"checked" in entry
                       ? `<span class="fb-menu-tick">${entry.checked ? "✓" : ""}</span>`
                       : ""}${iconned ? `<span class="fb-menu-icon" aria-hidden="true">${entry.icon
                           ? FigureIcons.fa(entry.icon) : ""}</span>` : ""}<span
                       class="fb-menu-text">${FigureSchema.escapeHtml(entry.label)}</span>${entry.hint
                       ? `<span class="fb-menu-key">${FigureSchema.escapeHtml(String(entry.hint))}</span>` : ""}</button>`;
        }).join(""), onPick);
    }

    /**
     * Mount a body-level popup under `anchor` and wire it up.
     *
     * Shared by the plain menus and by the shapes card, because everything here
     * is about being a popup rather than about being a list. Three things in it
     * are load-bearing:
     *
     *   * the `.fb-menu` class, which carries the light-token copy. <body> is
     *     outside `.fb-workspace`, so a popup mounted here inherits none of the
     *     page's colours and comes out unreadable without it;
     *
     *   * `offsetWidth`/`offsetHeight` rather than `getBoundingClientRect`. The
     *     card animates in with a `scale()`, and the rect is the TRANSFORMED
     *     box -- measuring it puts a freshly opened menu in the wrong place and
     *     a reopened one in the right place;
     *
     *   * a capture-phase dismiss, so a press anywhere closes this before that
     *     press reaches whatever it landed on.
     */
    mountMenu(anchor, html, onPick, className) {
        const reopening = this.menu && this._menuAnchor === anchor;
        this.closeMenu();
        if (reopening) return;

        const menu = document.createElement("div");
        menu.className = className ? `fb-menu ${className}` : "fb-menu";
        menu.innerHTML = html;
        document.body.appendChild(menu);

        const box = anchor.getBoundingClientRect();
        const size = { width: menu.offsetWidth, height: menu.offsetHeight };
        // Below the anchor unless there is no room, in which case above it --
        // the rail runs down the left edge, so its lower buttons open a tall
        // card that would otherwise hang off the bottom of the window.
        const below = box.bottom + 6;
        menu.style.top = Math.round(below + size.height > window.innerHeight - 8
            ? Math.max(8, window.innerHeight - size.height - 8)
            : below) + "px";
        menu.style.left = Math.round(Math.max(8,
            Math.min(box.left, window.innerWidth - size.width - 8))) + "px";

        menu.addEventListener("click", (event) => {
            const item = event.target.closest("[data-act]");
            if (!item || item.disabled) return;
            this.closeMenu();
            onPick(item.dataset.act);
        });
        this._onMenuDismiss = (event) => {
            if (menu.contains(event.target) || anchor.contains(event.target)) return;
            this.closeMenu();
        };
        document.addEventListener("pointerdown", this._onMenuDismiss, true);
        anchor.setAttribute("aria-expanded", "true");
        // Every other control on this page that opens something looks open while
        // it is: the tool rail lights, the context buttons tint, the colour
        // wells ring. The zoom readout said nothing, so the only way to tell an
        // open zoom menu from a stray click was to look for the menu.
        anchor.classList.add("is-open");
        this.menu = menu;
        this._menuAnchor = anchor;
    }

    /**
     * The Shapes picker: every preset in a grid, then the four drawing tools.
     *
     * ONE card rather than a menu whose entries open submenus. A submenu is a
     * second thing to keep open while reaching for the first, and the whole
     * content here is seventeen icons and four tools -- it fits, so it is shown.
     *
     * The icons are generated from the definitions (`FigureShapeDefs.icon`) and
     * are inline SVG, not Font Awesome spans: FontAwesome walks the document
     * once at boot, so a span injected into a card opened afterwards is never
     * replaced and draws nothing at all.
     *
     * Picking ARMS a tool rather than dropping a shape on the page. It is the
     * grammar the Text and Line tools already use -- press, then draw where you
     * meant -- and it is what makes the drag that follows decide the size.
     */
    openShapesCard(anchor) {
        const cells = FigureShapeDefs.GRID.map((id) => {
            const label = FigureSchema.escapeHtml(FigureShapeDefs.byId(id).label);
            return `<button type="button" class="fb-shape-cell" data-act="shape:${id}"
                            title="${label}" aria-label="${label}"
                    >${FigureShapeDefs.icon(id)}</button>`;
        }).join("");
        // Two to a row, and the hint moved into the tooltip. As four full-width
        // rows this section stood taller than the seventeen presets above it,
        // which put the weight of the card on its smaller half; and a hint is
        // read once, by whoever has not used the tool before, while the height
        // it costs is paid every time the card opens.
        const tools = FigureShapeDefs.CUSTOM_TOOLS.map((tool) => {
            const label = FigureSchema.escapeHtml(tool.label);
            return `<button type="button" class="fb-shape-tool" data-act="shape:${tool.id}"
                            title="${label} — ${FigureSchema.escapeHtml(tool.hint)}">
                        ${FigureShapeDefs.customIcon(tool.id)}
                        <span class="fb-shape-tool-label">${label}</span>
                    </button>`;
        }).join("");
        this.mountMenu(anchor,
            `<div class="fb-shapes-grid">${cells}</div>`
            + '<span class="fb-menu-separator"></span>'
            + '<div class="fb-shapes-title">Custom</div>'
            + `<div class="fb-shape-tools">${tools}</div>`,
            (act) => this.setTool(act), "fb-shapes-card");
    }

    /**
     * The Text picker: a heading, a subheading and a paragraph.
     *
     * Text was the one rail button that armed something the moment it was
     * pressed, while both of its neighbours opened a card -- so the button that
     * looked the most like the other two behaved the least like them, and the
     * three sizes people reach for first were reachable only by placing a box
     * and then going to find the stepper in the sidebar.
     *
     * Each row is DRAWN in the style it inserts, at `SAMPLE_SCALE` of the real
     * size, for the same reason the shapes card generates its icons from the
     * definitions: a picker that describes what it does in words can drift from
     * what it does, and one that shows it cannot.
     *
     * Picking ARMS the tool rather than dropping a box on the page -- the
     * grammar every other tool here uses, and the thing that lets the drag that
     * follows decide where the words go and how wide they run.
     */
    openTextCard(anchor) {
        const rows = FigureRichText.PRESETS.map((preset) => {
            const label = FigureSchema.escapeHtml(preset.label);
            const weight = preset.marks && preset.marks.bold ? 700 : 400;
            return `<button type="button" class="fb-text-style" data-act="text:${preset.id}"
                            style="font-size:${
                                Math.round(preset.size_pt * FigureRichText.SAMPLE_SCALE)
                            }px;font-weight:${weight}"
                            title="${label} \u2014 ${preset.size_pt} pt"
                    >${label}</button>`;
        }).join("");
        this.mountMenu(anchor, `<div class="fb-text-styles">${rows}</div>`,
                       (act) => this.setTool(act), "fb-text-card");
    }

    /**
     * The Lines picker: five variants of the one line object.
     *
     * The same card as Shapes and for the same reasons -- one panel rather than
     * a menu whose entries open submenus, icons generated from the definitions
     * (`FigureLineDefs.icon`) so the picker cannot lie about what it inserts,
     * and inline SVG rather than Font Awesome spans, which are replaced once at
     * boot and would draw nothing in a card opened afterwards.
     *
     * Five cells rather than a matrix of every head against every dash. What a
     * user picks here is a starting point; the sidebar has both heads, the head
     * size, the dash and the edge, and every one of them can be changed on a
     * line already drawn. A grid of forty cells would be forty ways to reach a
     * state that is four clicks away in either case.
     */
    openLinesCard(anchor) {
        const cells = FigureLineDefs.GRID.map((id) => {
            const label = FigureSchema.escapeHtml(FigureLineDefs.byId(id).label);
            return `<button type="button" class="fb-line-cell" data-act="line:${id}"
                            title="${label}" aria-label="${label}"
                    >${FigureLineDefs.icon(id)}</button>`;
        }).join("");
        this.mountMenu(anchor, `<div class="fb-lines-grid">${cells}</div>`,
                       (act) => this.setTool(act), "fb-lines-card");
    }

    closeMenu() {
        if (this._onMenuDismiss) {
            document.removeEventListener("pointerdown", this._onMenuDismiss, true);
            this._onMenuDismiss = null;
        }
        this._menuAnchor?.setAttribute("aria-expanded", "false");
        this._menuAnchor?.classList.remove("is-open");
        this._menuAnchor = null;
        this.menu?.remove();
        this.menu = null;
    }

    pageMenuEntries() {
        const pages = this.state.pages.map((page, index) => ({
            act: `go:${page.page_id}`, label: `${page.name || `Page ${index + 1}`}`,
            checked: page.page_id === this.canvas.pageId,
        }));
        return [
            ...pages,
            { separator: true },
            { act: "add", label: "Add a page" },
            { act: "duplicate", label: "Duplicate this page" },
            { act: "remove", label: "Delete this page",
              disabled: this.state.pages.length <= 1 },
            { separator: true },
            { act: "background", label: "Page background…" },
            // Numbering is the whole FIGURE's -- A, B, C run in reading order
            // across every page of it -- and it was a row in the image sidebar,
            // inside the section for one selected panel's own title. So a
            // document-wide setting was reachable only by selecting an image,
            // and it looked like a property of that image. It belongs where the
            // rest of the document's settings are.
            { act: "numbering", label: "Panel numbering…" },
        ];
    }

    pageAction(act) {
        if (act.startsWith("go:")) this.goToPage(act.slice(3));
        else if (act === "add") this.addPage();
        else if (act === "duplicate") this.duplicatePage();
        else if (act === "remove") this.removePage();
        else if (act === "background") this.openPageBackground();
        else if (act === "numbering") this.openNumbering();
    }

    /** The three numbering schemes, ticked, as a second level of the page menu
     *  -- the same shape `openPageBackground` uses, and anchored to the same
     *  button whichever route got here. */
    openNumbering() {
        const anchor = this.el("fb_page_menu");
        if (!anchor) return;
        const now = this.state.document.settings.label_style;
        this.openMenu(anchor, FigureWorkspace.LABEL_STYLES.map(([style, label]) => ({
            act: style, label: label, checked: now === style,
        })), (act) => this.updateSettings({ label_style: act }));
    }

    /** The numbering schemes a figure can use, in the order they are offered. */
    static get LABEL_STYLES() {
        return [["A", "A, B, C"], ["a", "a, b, c"], ["A1", "A1, A2, A3"]];
    }

    /** The figure's unit, named rather than cycled -- see
     *  FigureViewOptions.menuEntries. Anchored to the View button, whichever
     *  route got here. */
    openUnits() {
        const anchor = this.el("fb_view_menu");
        if (!anchor) return;
        const now = this.viewOptions.prefs.units;
        this.openMenu(anchor, Object.entries(FigureViewOptions.UNITS).map(
            ([name, spec]) => ({ act: name, label: spec.label, checked: now === name })),
            (act) => this.viewOptions.setUnit(act));
    }

    /**
     * The page background, as a second level of the page menu.
     *
     * Anchored to the same button whichever route got here, including the
     * canvas right-click: a popover that appeared where the pointer happened to
     * be would be a third place this figure's page settings live.
     */
    openPageBackground() {
        const anchor = this.el("fb_page_menu");
        if (!anchor) return;
        this.openMenu(anchor, [
            { act: "#ffffff", label: "White" },
            { act: "#000000", label: "Black" },
            { act: "custom", label: "Custom color…" },
            { separator: true },
            // Only PNG carries it through: the export dialog says so, and the
            // canvas draws the conventional checkerboard so nobody discovers it
            // at the file.
            { act: FigureCanvas.TRANSPARENT, label: "Transparent" },
        ], (act) => this.setPageBackground(act));
    }

    setPageBackground(act) {
        if (act !== "custom") {
            this.commitPageBackground(act);
            return;
        }
        this.openPageColor(document.querySelector('[data-swatch="page_background"]'));
    }

    /**
     * The app's own colour palette (FigureColorField, the channel list's
     * picker) for the page background, against the swatch in the Page view,
     * or against the page menu when the right column is shut.
     */
    openPageColor(button) {
        const anchor = (button && button.isConnected ? button : null) || this.el("fb_page_menu");
        if (!anchor) return;
        if (!anchor.dataset.swatch) anchor.dataset.swatch = "page_background";
        const current = this.canvas.page?.background;
        FigureColorField.open(anchor, {
            value: current && current !== FigureCanvas.TRANSPARENT ? current : "#ffffff",
            onPick: (hex) => this.commitPageBackground(hex),
        });
    }

    commitPageBackground(background) {
        const pageId = this.canvas.pageId;
        if (!pageId) return;
        this.state.commit(
            [{ op: "update_page", page_id: pageId, changes: { background: background } }],
            (draft) => {
                const page = draft.pages.find((entry) => entry.page_id === pageId);
                if (page) page.background = background;
            });
    }

    /**
     * A copy of this page, with everything on it, as ONE undo step.
     *
     * The commonest reason to want it is a second version of a figure that is
     * nearly right -- which means the copy has to carry the panels, not just
     * the page size, and has to arrive complete enough to compare the two.
     */
    duplicatePage() {
        const current = this.canvas.page;
        if (!current) return;
        const page = {
            ...JSON.parse(JSON.stringify(current)),
            page_id: FigureSchema.newPageId(),
            name: current.name + " copy",
        };
        const panels = FigureSchema.panelsOnPage(this.state.document, current.page_id);
        const annotations = Object.values(this.state.document.annotations)
            .filter((annotation) => annotation.page_id === current.page_id);
        const made = this.canvas.copiesOf(panels, annotations, 0, 0, page.page_id);

        // The page has to exist before anything can be placed on it, so it
        // leads the batch -- and the whole batch is one commit, or duplicating
        // a six-panel page would be seven undo steps.
        const done = this.canvas.commitCopies(made, {
            select: false,
            operations: [{ op: "add_page", page: page }],
            mutate: (draft) => { draft.pages.push(page); },
        });
        (done || Promise.resolve(false)).then((stored) => {
            if (stored !== false) this.goToPage(page.page_id);
        });
    }

    zoomMenuEntries() {
        return [
            { act: "fit", label: "Fit page  ⌘0" },
            { act: "width", label: "Fit width" },
            { act: "selection", label: "Fit selection",
              disabled: !this.canvas.selection.size },
            { act: "100", label: "100%  ⌘1" },
        ];
    }

    zoomAction(act) {
        if (act === "fit") this.zoomToFit();
        else if (act === "width") this.zoomToWidth();
        else if (act === "selection") this.zoomToSelection();
        else if (act === "100") this.setScale(FigureWorkspace.PX_PER_MM);
    }

    // -- keyboard ------------------------------------------------------------

    keyDown(event) {
        // A <dialog> traps focus, not keystrokes. With the delete confirmation
        // up, Escape reached this handler as well as the dialog and disarmed
        // the drawing tool behind it.
        if (FigureConfirm.modalOpen) return;
        // A slider, checkbox or button that has focus is not being typed in:
        // after dragging Quick Edit's contrast slider the focus is on its range
        // input, and Ctrl+Z has to reach the undo below.
        const active = document.activeElement;
        const typing = active && (["TEXTAREA", "SELECT"].includes(active.tagName)
            || (active.tagName === "INPUT"
                && !["range", "checkbox", "radio", "button", "color"].includes(active.type)));
        if (typing) return;

        // While Quick Edit is open its own changes are what undo steps
        // through (FigureQuickEdit.undo); the figure's history waits until it
        // closes, so Ctrl+Z never changes something hidden behind the card.
        if (this.quickEdit?.session && (event.metaKey || event.ctrlKey)
                && (event.key === "z" || event.key === "Z" || event.key === "y" || event.key === "Y")) {
            event.preventDefault();
            if (event.key === "y" || event.key === "Y" || event.shiftKey) this.quickEdit.redo();
            else this.quickEdit.undo();
            return;
        }

        if (event.key === "Escape" && this.preview) {
            this.togglePreview(false);
            return;
        }
        // Escape closes Quick Edit -- after anything open inside it (its
        // colour picker, its channel menu, the histogram) has had the key.
        if (event.key === "Escape" && this.quickEdit?.session && !FigureConfirm.modalOpen) {
            const popover = Array.from(document.querySelectorAll(".fb-qe-pop, .fb-qe-histogram"))
                .some((element) => element.isConnected && element.offsetParent !== null
                      && getComputedStyle(element).visibility !== "hidden");
            if (!popover) {
                event.preventDefault();
                this.quickEdit.requestClose();
                return;
            }
        }
        // A bare P, and only when nothing is being typed: the caption editor is
        // a contenteditable DIV, which the tag guard above does not catch.
        if ((event.key === "p" || event.key === "P") && !event.metaKey && !event.ctrlKey
                && !event.altKey && !document.activeElement?.isContentEditable
                && !this.canvas?.pointEditor?.active && !this.quickEdit?.session) {
            event.preventDefault();
            this.togglePreview();
            return;
        }
        if (event.key === "Escape") {
            this.closeMenu();
            this.closeResolution();
            this.contextBar?.closePopover();
            // Both keydown listeners are on the window, so Escape reaches this
            // one AND the canvas's. Edit Points and a half-drawn polygon are
            // modes the canvas owns and ends for itself; stopping here leaves
            // one meaning per press instead of leaving the mode and disarming
            // the tool that would be needed to get back into it.
            if (this.canvas?.pointEditor?.active) return;
            if (this.canvas?.shapeDrawing?.active) return;
            // Escape also disarms a drawing tool, which is the other thing on
            // this page that is waiting for a click nobody wants to make.
            if (this.tool !== "select") this.setTool("select");
            return;
        }
        if (!(event.metaKey || event.ctrlKey)) return;

        // The zoom chords. Bound here rather than on the canvas because they
        // are about the window, not about the selection.
        const zoom = {
            "0": () => this.zoomToFit(),
            "1": () => this.setScale(FigureWorkspace.PX_PER_MM),
            "=": () => this.setScale(this.canvas.scale * 1.25),
            "+": () => this.setScale(this.canvas.scale * 1.25),
            "-": () => this.setScale(this.canvas.scale * 0.8),
        }[event.key];
        if (zoom) {
            event.preventDefault();
            zoom();
            return;
        }
        // Lower-cased: with Shift held most platforms report "Z", which used
        // to make Ctrl+Shift+Z do nothing outside macOS.
        const key = String(event.key).toLowerCase();
        if (key === "z" && !event.shiftKey) {
            event.preventDefault();
            this.undo();
        } else if ((key === "z" && event.shiftKey) || key === "y") {
            event.preventDefault();
            this.redo();
        }
    }

    /** The figure's undo, from the keyboard or the toolbar. Not while an
     *  object is being dragged -- the release would write the drag over the
     *  undone state, losing the undo and the redo with it -- and not while
     *  Quick Edit is open, whose own changes are what undo steps through. */
    undo() {
        if (this.canvas?.gesture) return;
        if (this.quickEdit?.session) { this.quickEdit.undo(); return; }
        this.state.undo();
    }

    redo() {
        if (this.canvas?.gesture) return;
        if (this.quickEdit?.session) { this.quickEdit.redo(); return; }
        this.state.redo();
    }

    // -- editing ---------------------------------------------------------

    /**
     * A figure nobody has named yet opens with its name selected.
     *
     * "Untitled Figure 3" is a placeholder the server had to invent to keep
     * two new figures apart; it is not a name anybody chose, and the moment to
     * fix that is the moment the figure opens -- not three panels later when
     * the export dialog puts the placeholder in a filename. Selected rather
     * than merely focused, so the first keystroke replaces it: a caret at the
     * end of a placeholder invites "Untitled Figure 3 blah".
     *
     * Two guards, and each has a case behind it. Only at revision 0, so
     * reopening a figure that HAS work in it never steals the keyboard from
     * the canvas; and only while the name still matches the pattern the server
     * generates, so a figure the user deliberately called "Untitled Figure"
     * is left alone after the first time.
     */
    inviteRename() {
        const input = this.el("fb_title");
        if (!input || !FigureWorkspace.UNTITLED.test((input.value || "").trim())) return;
        input.focus();
        // `select` before `setSelectionRange` because a plain text input has
        // the first and not always the second, and the stub DOM the probes run
        // has neither reliably.
        if (typeof input.select === "function") input.select();
    }

    /**
     * The title field as wide as the name in it, so the save status sits
     * right after it. CSS `field-sizing: content` does this where the browser
     * has it; elsewhere (Safari) the text is measured here. Capped by the
     * stylesheet's max-width, past which the name ellipsises.
     */
    fitTitle() {
        const input = this.el("fb_title");
        if (!input || typeof getComputedStyle !== "function"
                || CSS?.supports?.("field-sizing", "content")) return;
        const style = getComputedStyle(input);
        const canvas = this._titleRuler || (this._titleRuler = document.createElement("canvas"));
        const context = canvas.getContext?.("2d");
        if (!context) return;
        context.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
        const text = input.value || input.placeholder || "";
        const pad = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight)
            + parseFloat(style.borderLeftWidth) + parseFloat(style.borderRightWidth);
        input.style.width = `${Math.ceil(context.measureText(text).width + pad + 2)}px`;
    }

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
    /**
     * The same change to several panels, as ONE undo step.
     *
     * Turning scale bars on across a row of six is one thing the user did. Six
     * commits would be six saves and six presses of Cmd+Z to take back, and the
     * fourth press would leave the row half-changed.
     */
    updatePanels(updates) {
        const real = updates.filter((entry) => this.state.panel(entry.panel_id));
        if (!real.length) return Promise.resolve(false);
        return this.state.commit(
            real.map((entry) => ({
                op: "update_panel", panel_id: entry.panel_id, changes: entry.changes })),
            (draft) => {
                for (const entry of real) {
                    Object.assign(draft.panels[entry.panel_id], entry.changes);
                }
            });
    }

    /**
     * The figure's panel-letter size, from Document properties. The latest
     * size set wins wherever it was set: an image or a selection given its
     * own size keeps it until this is changed, and changing this hands every
     * image back to the figure's size, in the same commit (one undo step).
     */
    setLetterSize(size) {
        const settings = this.state.document.settings;
        const style = { ...settings.style, label_size_pt: size };
        const own = Object.values(this.state.document.panels)
            .filter((panel) => panel.label && panel.label.size_pt);
        this.state.commit(
            [{ op: "set_meta", changes: { settings: { style: style } } }]
                .concat(own.map((panel) => ({ op: "update_panel", panel_id: panel.panel_id,
                                              changes: { label: { ...panel.label, size_pt: null } } }))),
            (draft) => {
                draft.settings.style = style;
                for (const panel of own) draft.panels[panel.panel_id].label.size_pt = null;
            });
    }

    /** A figure-wide setting -- the label style, the gutter, the default DPI. */
    updateSettings(settings) {
        this.state.commit(
            [{ op: "set_meta", changes: { settings: settings } }],
            (draft) => { Object.assign(draft.settings, settings); });
    }

    /**
     * Record a pixel size the user typed against the images it describes.
     *
     * On the SOURCE, because it is a fact about the image and every panel of it
     * is entitled to the same answer -- and marked `manual`, which the
     * provenance page prints. A number somebody typed is not the same evidence
     * as one the file stated, and a figure that could not tell the difference
     * would have scale bars nobody could check.
     *
     * `panelIds` is what makes calibrating an image LATER work. An uncalibrated
     * import measures its bar in image pixels, because "500 px" is true and
     * silence is not; the moment a real pixel size arrives, that bar is
     * measuring in the wrong thing. Those panels are switched back to microns
     * on automatic in the SAME commit -- one thing the user did, one press of
     * Ctrl+Z to take back, and no figure left half-converted.
     */
    setPixelSize(sourceIds, value, panelIds) {
        const real = sourceIds.filter((id) => this.state.source(id));
        if (!real.length || !(value > 0)) return;
        const pixelSize = { value: value, unit: "µm", source: "manual" };

        const calibrated = new Set(real);
        const converted = (panelIds || []).map((id) => this.state.panel(id))
            .filter((panel) => panel && calibrated.has(panel.source_id)
                && panel.scalebar.unit === "px")
            .map((panel) => ({
                panel_id: panel.panel_id,
                changes: { scalebar: { ...panel.scalebar, unit: "um",
                                       target_um: null, target_px: null } },
            }));

        const operations = real.map((sourceId) => ({
            op: "update_source", source_id: sourceId,
            changes: { pixel_size: pixelSize } }));
        for (const entry of converted) {
            operations.push({ op: "update_panel", panel_id: entry.panel_id,
                              changes: entry.changes });
        }

        this.state.commit(operations, (draft) => {
            for (const sourceId of real) draft.sources[sourceId].pixel_size = pixelSize;
            for (const entry of converted) {
                Object.assign(draft.panels[entry.panel_id], entry.changes);
            }
        });
    }

    // -- copying one panel's rendering onto others ---------------------------

    /**
     * Remember how this panel is rendered, to put on others.
     *
     * CHANNELS only -- their colours, their windows, which of them are on.
     * Never the viewport, the placement, the captions or the label: those are
     * what makes each panel a different panel, and a "copy rendering" that
     * moved them would be a duplicate wearing the target's id.
     *
     * A clipboard of its own rather than the object clipboard `FigureClipboard`
     * holds. Ctrl+V after this must still paste the objects the user copied.
     */
    copyRendering(panelId) {
        const panel = this.state.panel(panelId);
        const channels = (panel && panel.scene.channels) || [];
        if (!channels.length) {
            this.toast("That panel has no channel settings to copy.");
            return;
        }
        this.renderClipboard = {
            sourceId: panel.source_id,
            channels: JSON.parse(JSON.stringify(channels)),
        };
        this.toast(`Rendering copied. Select the panels to apply it to and `
            + `choose "Apply rendering".`);
        // The Apply action's `enabled` reads this; nothing else would tell the
        // bar or the sidebar that it has just become live.
        this.selectionChanged(Array.from(this.canvas.selection));
    }

    hasRenderClipboard() {
        return Boolean(this.renderClipboard && this.renderClipboard.channels.length);
    }

    /**
     * Put the copied rendering on every selected panel.
     *
     * One commit for the lot of them, because it is one thing the user did and
     * has to be one thing they can undo -- the same rule dragging five panels
     * follows. Each panel's render revision moves, so its cached preview is
     * refetched and the export re-renders it.
     *
     * Then the previews are actually REDRAWN, here, from the new settings. A
     * version of this that only bumped the revision would leave every panel
     * showing the old colours until the user reopened them one at a time,
     * which for the case this exists for -- eight panels of one slide -- is the
     * work it was meant to save.
     */
    async applyRendering(panelIds) {
        if (!this.hasRenderClipboard()) return;
        const updates = [];
        const missed = new Set();
        for (const panelId of panelIds) {
            const panel = this.state.panel(panelId);
            const source = panel && this.state.source(panel.source_id);
            if (!panel || !source || source.kind !== "plexora_project") continue;
            const mapped = FigureSchema.mapRenderingChannels(
                this.renderClipboard.channels, source.channels);
            for (const name of mapped.skipped) missed.add(name);
            if (!mapped.channels.length) continue;
            updates.push({
                panel_id: panelId,
                changes: {
                    scene: { ...panel.scene, channels: mapped.channels },
                    render_revision: panel.render_revision + 1,
                },
            });
        }
        if (!updates.length) {
            this.toast("None of the selected panels could take those channels.");
            return;
        }
        const stored = await this.updatePanels(updates);
        if (stored === false) return;
        if (missed.size) {
            this.toast(`Applied. Not in every image: ${Array.from(missed).join(", ")}.`);
        }
        await this.refreshPreviews(updates.map((entry) => entry.panel_id));
    }

    /**
     * Redraw and re-upload the cached raster for these panels.
     *
     * Two at a time. One is slower than it needs to be for eight panels and
     * unlimited is a browser holding eight uint16 planes per panel at once
     * while the server does eight simultaneous pyramid reads.
     *
     * Each panel's new picture goes into `canvas.previewOverrides` the moment
     * it exists and comes out once the upload has landed -- so the page fills
     * in panel by panel instead of sitting on the old rasters until the last
     * upload finishes.
     */
    async refreshPreviews(panelIds) {
        const canvas = this.canvas;
        const queue = panelIds.slice();
        const worker = async () => {
            while (queue.length) {
                const panelId = queue.shift();
                const panel = this.state.panel(panelId);
                const source = panel && this.state.source(panel.source_id);
                if (!panel || !source) continue;
                let made = null;
                try {
                    made = await FigurePanelCompositor.renderPreview(
                        this.api, this.figureId, panel, source, {});
                } catch (error) {
                    made = null;
                }
                if (!made) continue;
                canvas.previewOverrides.set(panelId, made.dataURL);
                const image = canvas.surfaceEl?.querySelector(
                    `.fb-panel[data-panel-id="${FigureSchema.cssEscape(panelId)}"] .fb-panel-image`);
                if (image) image.src = made.dataURL;
                await this.api.putPreview(this.figureId, panelId,
                                          panel.render_revision, made.blob,
                                          { width: made.width, height: made.height });
                canvas.previewOverrides.delete(panelId);
            }
        };
        await Promise.all([worker(), worker()]);
        canvas.render();
    }



    /** One panel's properties, from the context bar. */
    updatePanel(panelId, changes) {
        if (!this.state.panel(panelId)) return;
        this.state.commit(
            [{ op: "update_panel", panel_id: panelId, changes: changes }],
            (draft) => { Object.assign(draft.panels[panelId], changes); });
    }
    /**
     * Rearrange the selection, then check the labels still make sense.
     *
     * An arrange moves panels into a new reading order, and automatic labels
     * follow it -- but a label the user typed does not. So a row that was
     * A B C and is now C A B keeps its hand-written letters in the wrong
     * places, silently, in a figure whose whole job is to be referred to by
     * those letters. The offer is a toast rather than a prompt: it is a
     * suggestion about presentation, and interrupting a layout gesture with a
     * dialog would be worse than the problem.
     */
    arrange(command) {
        this.canvas.arrange(command);
        const manual = this.canvas.selectedPanels()
            .filter((panel) => panel.placement && panel.label.visible && !panel.label.auto);
        if (manual.length < 2) return;
        this.toast("Panel order changed. Some labels were typed in, so they have not"
            + " moved.", { label: "Reset them to automatic",
                          act: () => this.resetLabels(manual.map((p) => p.panel_id)) });
    }

    resetLabels(panelIds) {
        this.updatePanels(panelIds
            .map((panelId) => ({ panel: this.state.panel(panelId), panel_id: panelId }))
            .filter((entry) => entry.panel)
            .map((entry) => ({
                panel_id: entry.panel_id,
                changes: { label: { ...entry.panel.label, auto: true, text: "" } },
            })));
    }

    /**
     * Something worth saying that is not worth stopping for.
     *
     * In the overlay layer, bottom centre, gone after a while. Everything it
     * carries is a suggestion -- reset these labels, reopen a panel to redraw
     * it -- and a modal for any of them would interrupt a gesture to report on
     * that same gesture.
     */
    toast(message, action) {
        const host = this.el("fb_overlay_layer");
        if (!host) return;
        host.querySelectorAll(".fb-toast").forEach((old) => old.remove());

        const toast = document.createElement("div");
        toast.className = "fb-toast";
        toast.innerHTML = `<span>${FigureSchema.escapeHtml(message)}</span>`;
        if (action) {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "fb-toast-action";
            button.textContent = action.label;
            button.addEventListener("click", () => {
                toast.remove();
                action.act();
            });
            toast.appendChild(button);
        }
        const dismiss = document.createElement("button");
        dismiss.type = "button";
        dismiss.className = "fb-toast-close";
        dismiss.innerHTML = FigureIcons.svg("x", 14);
        dismiss.addEventListener("click", () => toast.remove());
        toast.appendChild(dismiss);

        host.appendChild(toast);
        window.setTimeout(() => toast.remove(), FigureWorkspace.TOAST_MS);
    }



    /**
     * Split a composite into a row of single-channel panels, and DRAW them.
     *
     * The drawing is the half this was missing. `splitComposite` writes N new
     * panels whose scenes are right, but a panel's picture on the canvas is a
     * cached raster fetched from `/previews/<panel_id>` -- and nothing had ever
     * stored one for a derived panel, so every split produced a row of empty
     * frames. It looked like a rendering bug in the split; it was the absence
     * of a render.
     *
     * `refreshPreviews` is the same machinery Apply Rendering uses: read each
     * visible channel over the panel's own viewport, composite in the browser
     * with the export's arithmetic, show it immediately through
     * `previewOverrides`, and upload. Which means a split row is drawn from the
     * SOURCE at the windows the user set, rather than from a crop of the
     * composite -- so a channel that was faint under three others comes out
     * looking the way it does on its own.
     */
    async split() {
        const panelId = Array.from(this.canvas.selection)[0];
        if (!panelId) return;
        const groupId = this.canvas.splitComposite(panelId);
        if (!groupId) return;
        const group = this.state.document.link_groups[groupId];
        const derived = (group ? group.panel_ids : []).filter((id) => {
            const panel = this.state.panel(id);
            return panel && panel.derived_from
                && panel.derived_from.operation === "split_channel";
        });
        if (derived.length) await this.refreshPreviews(derived);
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
        // After the commit, through the one page switch: the commit redraws
        // first, and a page id set behind it left every side column describing
        // the page that was just left.
        this.goToPage(page.page_id);
    }

    async removePage() {
        if (this.state.pages.length <= 1) {
            FigureConfirm.tell({
                title: "A figure needs at least one page.",
                body: "Add another page first, or delete what is on this one.",
            });
            return;
        }
        const pageId = this.canvas.pageId;
        const index = this.state.pages.findIndex((page) => page.page_id === pageId);
        const panels = FigureSchema.panelsOnPage(this.state.document, pageId);

        // Three answers, and it used to be asked with two.
        //
        // A page holding panels can go three ways -- take the panels with it,
        // leave them in the tray, or not go at all -- and `window.confirm` has
        // room for the first two. So Cancel was spent on "keep them in the
        // tray", the page went either way, and the one thing a user pressing
        // Escape on a delete confirmation means was the one thing they could
        // not say. Now Escape means what it says and the two real answers are
        // two buttons that name themselves.
        //
        // Never silently orphaned, and keeping them is the safe answer, because
        // a captured scene may be the only record of a view somebody spent an
        // hour finding.
        let destroy = false;
        if (panels.length) {
            const answer = await FigureConfirm.choose({
                title: "Delete this page?",
                body: [`It holds ${FigureSchema.countPhrase(panels.length, "panel")}.`,
                       "Panels kept in the tray can be placed on another page. "
                       + "Deleted, they go with their captured scenes."],
                choices: [
                    { value: null, label: "Cancel", focus: true },
                    { value: false, label: "Keep the panels", kind: "primary" },
                    { value: true, label: "Delete them too", kind: "danger" },
                ],
            });
            if (answer === null) return;
            destroy = answer;
        }

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
        // To the page before the one deleted (the first, if that was it),
        // through the one page switch. This used to set the canvas to no page
        // and leave the page list to pick one on its next draw -- but the
        // commit's draw had already happened, so the canvas was left on no
        // page, and the next click on the sheet drew Document properties for
        // a page that did not exist: an empty column.
        const pages = this.state.pages;
        const next = pages[Math.max(0, index - 1)] || pages[0];
        if (next) this.goToPage(next.page_id);
    }

    // -- the tray ------------------------------------------------------------

    /**
     * A click in the tray.
     *
     * On pointerdown rather than click so that a drag which begins on an
     * unselected item selects it first -- otherwise dragging a panel out of a
     * four-panel selection would carry four panels the user could no longer see
     * highlighted.
     */
    trayPointerDown(panelId, event) {
        const ids = this.trayVisibleIds();
        if (event.shiftKey && this.trayAnchor && ids.includes(this.trayAnchor)) {
            const from = ids.indexOf(this.trayAnchor);
            const to = ids.indexOf(panelId);
            const [low, high] = from < to ? [from, to] : [to, from];
            this.traySelection = new Set(ids.slice(low, high + 1));
        } else if (event.metaKey || event.ctrlKey) {
            if (this.traySelection.has(panelId)) this.traySelection.delete(panelId);
            else this.traySelection.add(panelId);
            this.trayAnchor = panelId;
        } else {
            this.traySelection = new Set([panelId]);
            this.trayAnchor = panelId;
        }
        this.renderTray();
    }

    /** The tray panels currently on screen, in the order they are shown. */
    trayVisibleIds() {
        return this.trayPanels().map((panel) => panel.panel_id);
    }

    /** Drop the filter and show the whole tray again. */
    clearTraySearch() {
        const search = this.el("fb_tray_search");
        if (search) search.value = "";
        this.setTraySearch("");
    }

    /** One search for the whole Assets tab: this figure's images and the
     *  captures and bookmarks grids under them. */
    setTraySearch(query) {
        this.traySearch = query;
        for (const grid of Object.values(this.assetGrids || {})) grid.setQuery(query);
        this.renderTray();
    }

    /** The figure's images that are off the page, under the Assets tab now
     *  showing (each goes back under the tab it was added from), and
     *  matching the search. */
    trayPanels() {
        return FigureSchema.panelsInTray(this.state.document)
            .filter((panel) => this.inAssetsPane(panel))
            .filter((panel) => this.passesAssetFilter(panel));
    }

    /** The search and the filter, for one of this figure's images. */
    passesAssetFilter(panel) {
        if (!FigureSchema.matchesSearch(this.trayHaystack(panel), this.traySearch)) return false;
        return !this.assetFilter || !this.assetFilter.active
            || this.assetFilter.matches(this.panelFacets(panel));
    }

    /** Is the tab narrowed at all, by the search or the filter? */
    get assetsNarrowed() {
        return Boolean(this.traySearch.trim()) || Boolean(this.assetFilter && this.assetFilter.active);
    }

    /** Is this image of the kind the Assets tab now showing lists? */
    inAssetsPane(panel) {
        const origin = { captures: "capture", bookmarks: "bookmark", uploads: "upload" }[
            this.assetsPane || "captures"];
        return FigureSchema.panelOrigin(panel, this.state.source(panel.source_id)) === origin;
    }

    /** While a search is typed, the images ON the page that answer it too:
     *  a search for a channel or a file has to find it wherever it is. */
    placedMatches() {
        if (!this.assetsNarrowed) return [];
        return Object.values(this.state.document?.panels || {})
            .filter((panel) => panel.placement && this.inAssetsPane(panel))
            .filter((panel) => this.passesAssetFilter(panel));
    }

    /** Everything an image of this figure can be found by in the Assets
     *  search: its names and captions, the image it came from, and what the
     *  scene showed (channels, cell layers). */
    trayHaystack(panel) {
        const source = this.state.source(panel.source_id);
        const facets = this.projectCards && typeof FigureAssetFilter !== "undefined"
            ? this.panelFacets(panel) : null;
        return [panel.name, FigureSchema.panelCaption(panel), panel.label && panel.label.text,
                ...(panel.labels || []).map((entry) => entry.text),
                source && source.display_name, source && source.datasource,
                facets && facets.project, facets && facets.dataset,
                FigureSchema.sceneSearchText(panel.scene)]
            .filter(Boolean).join(" ");
    }

    /**
     * Put tray panels onto the page without dragging them.
     *
     * The same path a drag takes, so double-clicking four selected panels and
     * dragging four selected panels produce the same layout -- and neither
     * stacks them.
     */
    placeFromTray(panelIds) {
        const ids = panelIds.filter((id) => this.state.panel(id));
        const page = this.canvas.page;
        if (!ids.length || !page) return;
        this.canvas.placePanels(ids, this.canvas.traySizes(ids, page), null);
        ids.forEach((id) => this.traySelection.delete(id));
    }

    /** Right-clicking a tray item that is not in the tray selection selects it
     *  first, so the menu never acts on something else. */
    trayContext(panelId) {
        if (this.traySelection.has(panelId)) return;
        this.traySelection = new Set([panelId]);
        this.trayAnchor = panelId;
        this.renderTray();
    }

    duplicateTray() {
        const panels = Array.from(this.traySelection)
            .map((id) => this.state.panel(id)).filter(Boolean);
        if (!panels.length) return;
        // No offset and no selection: these have no placement, so there is
        // nothing to offset and nothing on the page to select.
        this.canvas.commitCopies(this.canvas.copiesOf(panels, [], 0, 0), { select: false });
    }

    /**
     * Destroy panels, and the sources nothing references any more.
     *
     * The OTHER delete, and the reason the two are worded differently. Removing
     * a panel from a page is a statement about the page and is one keystroke
     * away; this destroys the captured scene, which may be the only record of a
     * view somebody spent an hour finding. So it asks, it says how many, and it
     * says what else goes with them.
     *
     * A source left behind with no panels is not harmless: it is what the
     * provenance page lists and what "this source has changed" is checked
     * against, so a figure that accumulated one per deleted panel would warn
     * about images it no longer draws. They go in the SAME commit, so the whole
     * thing is one undo step -- and undoing it brings the source back too.
     */
    async deleteFromFigure(ids) {
        const panels = ids.map((id) => this.state.panel(id)).filter(Boolean);
        const annotationIds = ids.filter((id) => this.state.document.annotations[id]);
        if (!panels.length && !annotationIds.length) return;

        const doomed = new Set(panels.map((panel) => panel.panel_id));
        const orphaned = this.sourcesLeftUnreferenced(doomed);

        const parts = [];
        if (panels.length) parts.push(FigureSchema.countPhrase(panels.length, "panel"));
        if (annotationIds.length) {
            parts.push(FigureSchema.countPhrase(annotationIds.length, "annotation"));
        }
        const body = ["This cannot be undone by closing the page — the captured "
                      + "scenes go with them."];
        if (orphaned.length) {
            body.push("The figure will also stop referencing "
                + `${FigureSchema.countPhrase(orphaned.length, "image")}, `
                + "which nothing else uses.");
        }
        const go = await FigureConfirm.ask({
            title: `Delete ${parts.join(" and ")} from this figure?`,
            body: body,
            confirm: "Delete",
        });
        if (!go) return;

        const operations = [];
        if (panels.length) {
            operations.push({ op: "remove_panels", panel_ids: Array.from(doomed) });
        }
        if (annotationIds.length) {
            operations.push({ op: "remove_annotations", annotation_ids: annotationIds });
        }
        // "keep" rather than "delete": the panels are already going in the
        // operation above, and asking the server to delete them twice is how a
        // batch fails halfway.
        for (const sourceId of orphaned) {
            operations.push({ op: "remove_source", source_id: sourceId, panels: "keep" });
        }

        for (const id of doomed) this.canvas.selection.delete(id);
        for (const id of annotationIds) this.canvas.selection.delete(id);
        for (const id of ids) this.traySelection.delete(id);

        this.state.commit(operations, (draft) => {
            for (const id of doomed) delete draft.panels[id];
            for (const id of annotationIds) delete draft.annotations[id];
            for (const id of orphaned) delete draft.sources[id];
            for (const [groupId, group] of Object.entries(draft.groups || {})) {
                group.member_ids = group.member_ids.filter(
                    (member) => !doomed.has(member) && !annotationIds.includes(member));
                if (group.member_ids.length < 2) delete draft.groups[groupId];
            }
        });
        this.canvas.onSelectionChange(Array.from(this.canvas.selection));
    }

    /** The sources that would have no panels left once `doomed` has gone. */
    sourcesLeftUnreferenced(doomed) {
        const survivors = new Set();
        for (const panel of Object.values(this.state.document.panels)) {
            if (!doomed.has(panel.panel_id)) survivors.add(panel.source_id);
        }
        return Object.keys(this.state.document.sources)
            .filter((sourceId) => !survivors.has(sourceId));
    }



    // -- importing files -----------------------------------------------------

    /** Captures and bookmarks dragged in from the Assets tab: into the
     *  figure, onto the page where they were dropped. (The figure's own
     *  images in the same drag are the canvas's: FigureCanvas.dropFromTray.) */
    dropCaptures(event) {
        const raw = event.dataTransfer?.getData("text/x-plexora-capture");
        if (!raw) return;
        let ids = [];
        try {
            ids = JSON.parse(raw);
        } catch (error) {
            return;
        }
        if (!Array.isArray(ids) || !ids.length) return;
        event.preventDefault();
        this.placeCapturesAt(ids, event);
    }

    async placeCapturesAt(ids, point) {
        await this.adoptCaptures(ids, { place: true, at: this.canvas.surfacePoint(point) });
        for (const grid of Object.values(this.assetGrids || {})) grid.refresh();
    }

    dropFiles(event) {
        const files = Array.from(event.dataTransfer?.files || []);
        if (!files.length) return;
        event.preventDefault();
        // The desktop app delivers the same drop natively (see
        // bindCanvasHost); taking both would import every file twice.
        if (window.PlexoraDesktop) return;
        this.importFiles(files, this.canvas.surfacePoint(event));
    }

    /** Paths the desktop app's window was handed by a native drop. */
    async importNativePaths(paths, point) {
        let files;
        try {
            files = await Promise.all((paths || []).map(
                (path) => window.PlexoraDesktop.fileFromPath(path)));
        } catch (error) {
            window.PlexoraStatus?.begin("Importing")?.fail(String(error));
            return;
        }
        if (files.length) this.importFiles(files, this.canvas.surfacePoint(point));
    }

    /**
     * Files brought in from outside the project.
     *
     * Figure-only by design: a schematic or a supporting RGB image is not a
     * project, and the bytes land in this figure's own directory. Each panel
     * arrives at the image's own aspect ratio, and a batch is laid out rather
     * than stacked -- landing everything square, or all at one coordinate, is
     * work the user then has to undo.
     */
    async importFiles(files, atPoint) {
        const page = this.canvas.page;
        if (!page) return;
        const task = window.PlexoraStatus?.begin("Importing");

        const added = [];
        for (const file of files) {
            const uploaded = await this.api.addAsset(this.figureId, file.name, file);
            if (!uploaded.ok) {
                task?.fail(uploaded.data.error || "That file could not be imported");
                return;
            }
            added.push({ asset: uploaded.data, dimensions: await this.imageSize(file) });
        }

        const width = Math.min(60, page.size_mm.w / 3);
        const sizes = added.map((entry) => ({
            w_mm: width,
            h_mm: width * ((entry.dimensions.height / entry.dimensions.width) || 1),
        }));
        const origin = atPoint ? {
            x_mm: Math.max(page.margins_mm.left, atPoint.x - width / 2),
            y_mm: Math.max(page.margins_mm.top, atPoint.y - sizes[0].h_mm / 2),
        } : null;
        const boxes = FigureCanvas.freePlacements(
            sizes, page, this.canvas.occupiedBoxes(),
            this.state.document.settings.style.gutter_mm, origin);

        let z = this.canvas.nextZ();
        const operations = [];
        const records = [];
        added.forEach((entry, index) => {
            const sourceId = FigureSchema.newSourceId();
            const panelId = FigureSchema.newPanelId();
            const source = {
                source_id: sourceId, kind: "imported_asset", asset_id: entry.asset.asset_id,
                display_name: entry.asset.filename,
                image: { width: entry.dimensions.width, height: entry.dimensions.height },
                // No calibration, and none invented: an imported PNG has no
                // physical scale. Its bar is measured in image PIXELS instead,
                // which is a true statement about the picture -- the file's name
                // is on the source, so the tray still knows what it is.
                pixel_size: null, channels: [], status: "ok",
            };
            const panel = {
                panel_id: panelId, source_id: sourceId,
                scene: { ...FigureSchema.emptyScene(sourceId),
                         viewport: { x: 0, y: 0,
                                     w: entry.dimensions.width, h: entry.dimensions.height } },
                placement: { page_id: page.page_id, ...boxes[index], z: z++ },
                origin: "upload",
                label: { text: "", auto: true, visible: true },
                ...FigureSchema.defaultFurniture({
                    scalebar: { ...FigureSchema.defaultFurniture().scalebar,
                                unit: "px" },
                }),
                render_revision: 1,
            };
            operations.push({ op: "add_source", source: source },
                            { op: "add_panel", panel: panel });
            records.push({ source: source, panel: panel });
        });

        // One commit: dropping four files is one thing the user did, and four
        // would be four undo steps for it.
        await this.state.commit(operations, (draft) => {
            for (const record of records) {
                draft.sources[record.source.source_id] = record.source;
                draft.panels[record.panel.panel_id] = record.panel;
            }
        });
        // They land on the page, which in the Uploads list is "Previously
        // used": switched to, so what was just added is there to see.
        this.usedPanes = { ...(this.usedPanes || {}), uploads: true };
        const uploads = this.assetGrids?.uploads;
        if (uploads && !uploads.showUsed) {
            uploads.showUsed = true;
            uploads.checked.clear();
        }
        this.saveView();
        this.renderTray();
        task?.done();
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

    // -- reopening a panel ---------------------------------------------------

    /**
     * The cheap edit, where it is possible.
     *
     * Quick Edit opens beside the canvas and costs nothing but a few small
     * reads; the main viewer is a page load away from the figure. So this is
     * what a double-click and the bar's first button reach for, and the
     * expensive route is what "Open in Main Viewer" says on it.
     *
     * A panel with no project image behind it -- an imported PNG, a source
     * whose project has gone -- has nothing to quick-edit, and falls through to
     * the route that can at least explain itself.
     */
    quickEditPanel(panelId) {
        const panel = this.state.panel(panelId);
        if (this.quickEdit?.canEdit(panel)) {
            this.quickEdit.open(panelId);
            return;
        }
        this.onEditPanel(panelId);
    }

    /**
     * Open a panel's view in the main viewer.
     *
     * There is no viewer on this page, so this navigates -- with the request
     * left in sessionStorage for the page that lands to pick up.
     *
     * The note carries three things the viewer cannot work out for itself: WHICH
     * panel, what SHAPE it is now (a square capture the user has since dragged
     * into a wide strip must be reframed as a wide strip), and that the user
     * expects to come back here when they are done. Without the last one the
     * round trip is a one-way trip and the user has to find the figure again.
     */
    editPanel(panelId) {
        const panel = this.state.panel(panelId);
        const source = panel && this.state.source(panel.source_id);
        if (!source || source.kind !== "plexora_project" || !source.datasource) {
            FigureConfirm.tell({
                title: "This panel has no project image to reopen.",
                body: "It was captured from an image the figure no longer references.",
            });
            return;
        }
        const place = panel.placement;
        const aspect = place && place.h_mm > 0
            ? place.w_mm / place.h_mm
            : FigureSchema.frameAspect(panel.scene.viewport);
        try {
            window.sessionStorage.setItem("plexora:figure-builder-pending",
                JSON.stringify({
                    figure_id: this.figureId,
                    panel_id: panelId,
                    mode: "update",
                    aspect: aspect,
                    return_to: "canvas",
                }));
        } catch (error) {
            /* Private-browsing modes throw; the navigation is still worth doing. */
        }
        this.saveReturn();
        PlexoraRouter.go(this.api.url(encodeURIComponent(source.datasource))
            + "?tool=figure_builder");
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
        // The status the context bar shows is computed by the server on read,
        // so the local copy has to be told too or the badge stays until a
        // reload.
        this.state.sourceStatus[sourceId] = { status: "ok", reasons: [] };
        this.render();
    }

    // -- zoom and pan ----------------------------------------------------------

    setScale(scale) {
        this.canvas.setScale(scale);
        this.afterZoom();
    }

    zoomToFit() {
        this.canvas.zoomToFit(this.el("fb_canvas_scroll"));
        this.afterZoom();
    }

    /** The page as wide as the canvas allows, scrolled to its top. */
    zoomToWidth() {
        const scroll = this.el("fb_canvas_scroll");
        const page = this.canvas.page;
        if (!scroll || !page) return;
        const box = window.getComputedStyle(scroll);
        const width = scroll.clientWidth - parseFloat(box.paddingLeft) - parseFloat(box.paddingRight) - 48;
        this.canvas.setScale(width / page.size_mm.w);
        scroll.scrollTop = 0;
        this.afterZoom();
    }

    afterZoom() {
        this.renderZoom();
        this.viewOptions?.apply();
        this.contextBar?.position();
    }

    /**
     * Zoom about a point on screen, keeping what is under it still.
     *
     * Measured before and after rather than computed from the scroll offset,
     * because the page is centred with `margin: auto` whenever it is narrower
     * than the viewport -- so its left edge is not a function of scrollLeft and
     * the arithmetic that assumes it is drifts by half the slack, which is most
     * of the window when a page is zoomed out.
     */
    zoomAt(clientX, clientY, factor) {
        const scroll = this.el("fb_canvas_scroll");
        const pageEl = this.el("fb_page");
        if (!scroll || !pageEl) return;

        const before = pageEl.getBoundingClientRect();
        const mmX = (clientX - before.left) / this.canvas.scale;
        const mmY = (clientY - before.top) / this.canvas.scale;

        const wanted = this.canvas.scale * factor;
        this.canvas.setScale(wanted);
        // setScale clamps, so the point is recomputed from where it actually
        // landed rather than from where it was asked to.
        const after = pageEl.getBoundingClientRect();
        scroll.scrollLeft += (after.left + mmX * this.canvas.scale) - clientX;
        scroll.scrollTop += (after.top + mmY * this.canvas.scale) - clientY;
        this.afterZoom();
    }

    /**
     * Fill the window with what is selected.
     *
     * Padded rather than edge to edge: a selection that fills the viewport
     * exactly hides what is next to it, and "is this panel the same size as the
     * one beside it" is most of what anybody zooms in to check.
     */
    zoomToSelection() {
        const boxes = this.canvas.selectedPanels()
            .filter((panel) => panel.placement).map((panel) => panel.placement)
            .concat(this.canvas.selectedAnnotations().map((annotation) => ({
                x_mm: annotation.geometry.x_mm + Math.min(0, annotation.geometry.w_mm),
                y_mm: annotation.geometry.y_mm + Math.min(0, annotation.geometry.h_mm),
                w_mm: Math.abs(annotation.geometry.w_mm),
                h_mm: Math.abs(annotation.geometry.h_mm),
            })));
        const scroll = this.el("fb_canvas_scroll");
        if (!boxes.length || !scroll) {
            this.zoomToFit();
            return;
        }
        const left = Math.min(...boxes.map((box) => box.x_mm));
        const top = Math.min(...boxes.map((box) => box.y_mm));
        const right = Math.max(...boxes.map((box) => box.x_mm + box.w_mm));
        const bottom = Math.max(...boxes.map((box) => box.y_mm + box.h_mm));

        const padding = 64;
        this.canvas.setScale(Math.min(
            (scroll.clientWidth - padding) / Math.max(1, right - left),
            (scroll.clientHeight - padding) / Math.max(1, bottom - top)));

        const pageEl = this.el("fb_page");
        const paper = pageEl.getBoundingClientRect();
        const view = scroll.getBoundingClientRect();
        scroll.scrollLeft += (paper.left + this.canvas.toPx((left + right) / 2))
            - (view.left + view.width / 2);
        scroll.scrollTop += (paper.top + this.canvas.toPx((top + bottom) / 2))
            - (view.top + view.height / 2);
        this.afterZoom();
    }

    // -- rendering -------------------------------------------------------

    render() {
        if (!this.state.document || !this.root) return;
        const title = this.el("fb_title");
        if (title && title !== document.activeElement) title.value = this.state.title;
        this.fitTitle();

        if (!this._fitted) {
            // Coming back (from the viewer, Back, a reload): the page that
            // was showing, before anything is drawn.
            this._return = this.readReturn();
            const page = this._return && this._return.page;
            if (page && page !== this.canvas.pageId && this.state.pages.some((each) => each.page_id === page)) {
                this.canvas.setPage(page);
            }
        }
        this.renderPageList();
        // An undo, a redo or a refused save can take away objects that were
        // selected (Cmd+D, then Cmd+Z: the copies go, the selection held them).
        // Left in, every later command would send ids the document no longer
        // has, and the server refuses the whole batch.
        for (const id of Array.from(this.canvas.selection)) {
            const object = this.canvas.objectById(id);
            const page = object && (object.placement ? object.placement.page_id : object.page_id);
            if (!object || (page && page !== this.canvas.pageId)) this.canvas.selection.delete(id);
        }
        this.canvas.render();
        if (!this._fitted) {
            this._fitted = true;
            this.canvas.zoomToFit(this.el("fb_canvas_scroll"));
            if (this._return) {
                const snapshot = this._return;
                this._return = null;
                window.requestAnimationFrame(() => this.applyReturn(snapshot));
            }
        }
        // Margins move with the page, the grid and the rulers move with the
        // zoom, and both can have changed by the time this runs.
        this.viewOptions?.apply();
        // The annotation being typed into is hidden behind its editor, and the
        // render above has just put it back. Re-hidden here rather than in the
        // canvas, which does not know an editor exists.
        if (this.textEditor?.active) {
            const element = this.canvas.surfaceEl.querySelector(
                `[data-annotation-id="${FigureSchema.cssEscape(this.textEditor.annotationId)}"]`);
            // Measured before it is hidden. `visibility: hidden` keeps the
            // layout box, so the order does not strictly matter -- but the
            // editor follows the annotation's new geometry, leading and
            // alignment here, and reading them off it first is the honest way
            // round.
            this.textEditor.reposition(element);
            if (element) element.style.visibility = "hidden";
        }
        this.renderZoom();
        this.renderTray();
        this.renderHistory();
        this.renderRail();
        this.renderPageMeta();
        const ids = Array.from(this.canvas.selection);
        this.contextBar?.update(ids);
        this.layers?.render();
        this.inspector?.update(ids);
        this.textPanel?.update(ids);
        this.shapePanel?.update(ids);
        this.linePanel?.update(ids);
        this.imagePanel?.update(ids);
        this.contextSidebar();
        this.fillStarter();
    }

    /**
     * The two captions that describe the page rather than change it.
     *
     * The size sits on the sheet, above its top-left corner, because it is a
     * property of the paper; the object count sits in the status bar, because
     * it is a property of the window's contents. Both are read-only -- the size
     * is changed from the page menu, and there is nothing to click on a count.
     */
    renderPageMeta() {
        const page = this.canvas.page;
        const label = this.el("fb_page_setup_label");
        if (label) {
            label.textContent = page ? `${(FigureSchema.PAGE_PRESETS[page.preset]
                || { label: "Custom" }).label} · ${page.orientation === "landscape"
                ? "Landscape" : "Portrait"}` : "";
        }
        const caption = this.el("fb_sheet_caption");
        if (caption) {
            // Rounded to a tenth and stripped of a trailing zero: A4 is 210 mm,
            // not 210.0 mm, and a custom page of 148.5 keeps its half.
            const mm = (value) => String(Math.round(value * 10) / 10);
            caption.textContent = page
                ? `${mm(page.size_mm.w)} × ${mm(page.size_mm.h)} mm`
                : "";
        }

        const count = this.el("fb_object_count");
        if (!count) return;
        if (!page) {
            count.textContent = "";
            const empty = this.el("fb_page_empty");
            if (empty) empty.hidden = true;
            return;
        }
        const placed = FigureSchema.panelsOnPage(this.state.document, page.page_id).length
            + Object.values(this.state.document.annotations)
                .filter((annotation) => annotation.page_id === page.page_id).length;
        const index = this.state.pages.findIndex((entry) => entry.page_id === page.page_id);
        const selected = this.canvas.selection.size;
        count.textContent = `Page ${index + 1} of ${this.state.pages.length} · `
            + (placed === 1 ? "1 object" : `${placed} objects`)
            + (selected ? ` · ${selected} selected` : "");
        // The empty page's invitation, on the sheet, only while there is
        // nothing at all on it.
        const empty = this.el("fb_page_empty");
        if (empty) empty.hidden = placed > 0;
    }

    selectionChanged(ids) {
        // Selecting on the page is a statement about the page, so the tray's
        // own selection lets go: two highlighted sets meaning two different
        // things is what makes "which of these does Delete act on?"
        // unanswerable.
        if (ids.length && this.traySelection.size) {
            this.traySelection.clear();
            this.renderTray();
        }
        this.renderRail();
        this.renderPageMeta();
        this.contextBar?.update(ids);
        this.layers?.syncSelection();
        this.layers?.reveal();
        this.inspector?.update(ids);
        this.textPanel?.update(ids);
        this.shapePanel?.update(ids);
        this.linePanel?.update(ids);
        this.imagePanel?.update(ids);
        // Here and NOT in the render pump: following the selection is a
        // response to a selection, and the pump runs on every document change
        // -- including the one the follow itself makes when it saves the panel
        // being left, which would re-enter this mid-switch.
        this.quickEdit?.update(ids);
        this.contextSidebar();
    }

    renderPageList() {
        const select = this.el("fb_page_select");
        const pages = this.state.pages;
        if (!pages.some((page) => page.page_id === this.canvas.pageId)) {
            this.canvas.pageId = pages.length ? pages[0].page_id : null;
        }
        const label = this.el("fb_page_label");
        if (label) {
            const index = pages.findIndex((page) => page.page_id === this.canvas.pageId);
            label.textContent = `Page ${index + 1} of ${pages.length}`;
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

    renderHistory() {
        const undo = this.el("fb_undo");
        const redo = this.el("fb_redo");
        if (undo) undo.disabled = !this.state.canUndo;
        if (redo) redo.disabled = !this.state.canRedo;
    }

    renderTray() {
        const all = FigureSchema.panelsInTray(this.state.document);
        const count = this.el("fb_tray_count");
        if (count) count.textContent = all.length ? String(all.length) : "";
        // On the Assets tab, so a capture arriving while Layers is showing is
        // not a capture that seems to have been lost.
        const badge = this.el("fb_tray_badge");
        if (badge) badge.textContent = all.length ? String(all.length) : "";
        // The grid itself waits while nobody can see it: this runs on every
        // document change (each drag, nudge and undo), and rebuilding the
        // filter facets and every card behind the Layers tab was work for
        // nobody. `showTab` brings it up to date when Assets is shown.
        if (this.tab && this.tab !== "assets") { this._trayStale = true; return; }
        this._trayStale = false;
        const shown = this.trayPanels();
        this.updateFilterOptions();

        const clear = this.el("fb_tray_search_clear");
        if (clear) clear.hidden = !this.traySearch;

        // This figure's images go into the tab's own grid, as cards like the
        // rest, tagged "In figure": those off the page, then a search's or
        // filter's matches on the page ("On page"). What is on the page is
        // otherwise in Layers.
        const grid = (this.assetGrids || {})[this.assetsPane || "captures"];
        if (!grid) return;
        const total = all.filter((panel) => this.inAssetsPane(panel)).length;
        if (grid.byPlacement) {
            // Uploads: every one of the figure's, off the page and on it;
            // the grid's "Previously used" switch picks which half shows.
            const placed = Object.values(this.state.document?.panels || {})
                .filter((panel) => panel.placement && this.inAssetsPane(panel));
            const onPage = placed.filter((panel) => this.passesAssetFilter(panel));
            grid.setExtras(shown.concat(onPage).map((panel) => this.assetCard(panel)),
                           { off: total, on: placed.length });
        } else {
            grid.setExtras(shown.concat(this.placedMatches()).map((panel) => this.assetCard(panel)), total);
        }
        this.fitSourceCard();
    }

    /**
     * The tab's "how to get some" card: whole while the tab is empty; once
     * there is something, just its button, pinned to the bottom of the
     * column (.fb-assets-source.is-compact).
     */
    fitSourceCard() {
        const pane = this.assetsPane || "captures";
        const grid = (this.assetGrids || {})[pane];
        const card = this.el({ captures: "fb_assets_viewer", bookmarks: "fb_assets_bookmark_go",
                               uploads: "fb_assets_upload" }[pane]);
        if (!card) return;
        card.classList.toggle("is-compact", Boolean(grid && grid.hasAny()));
    }

    /** One of this figure's images as the Assets grid draws it. */
    assetCard(panel) {
        const source = this.state.source(panel.source_id);
        const span = FigureSchema.physicalWidthUm(source, panel.scene.viewport);
        const frame = panel.scene && panel.scene.viewport
            ? FigureSchema.frameSize(panel.scene.viewport) : null;
        const pixels = frame ? `${Math.round(frame.w)} × ${Math.round(frame.h)} px` : "";
        return {
            id: panel.panel_id,
            name: panel.name || FigureSchema.panelCaption(panel)
                || (source && (source.display_name || source.datasource)) || "Untitled image",
            meta: [span ? FigureSchema.formatMicrons(span) + " wide" : "", pixels]
                .filter(Boolean).join(" · "),
            url: this.canvas.panelImageUrl(panel, source),
            placed: Boolean(panel.placement),
        };
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
            // A tick for saved, the one state that is good news; the rest are
            // words alone, in their own colour (see .fb-status-*).
            if (payload.status === "saved") {
                status.innerHTML = FigureIcons.svg("check", 12) + " Saved";
            }
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

// Mounted through core's page registry rather than DOMContentLoaded: the canvas
// is one of the pages appRouter.js can render without a document load, and that
// event fires once per document. boot() already returns null when this is not
// the figure page, which is what makes running it on every page safe.
//
// The cleanup matters more here than anywhere else in the app. This workspace
// binds keydown and resize on `window` (see setup) and owns a canvas, a context
// menu and a Quick Edit sidebar that each bind their own -- so leaving it
// mounted after the user walks back to the slide would give the viewer a second
// listener for every key it handles. destroy() already existed for exactly this
// shape of teardown; it simply had nothing to call it before.
//
// Guarded because this file is also parsed outside a browser by the probes.
if (typeof PlexoraPage !== "undefined") {
    PlexoraPage.register(() => {
        const workspace = FigureWorkspace.boot();
        return workspace ? () => workspace.destroy() : null;
    });
}
