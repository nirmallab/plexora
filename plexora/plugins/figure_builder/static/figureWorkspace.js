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

    /** How many projects the Add menu lists before it hands over to the page
     *  built for choosing between many. */
    static get ADD_MENU_PROJECTS() { return 8; }

    /** Where the viewer says it sent the user here from. Written by
     *  figureSidebarController, read by the back arrow, and per-tab: two tabs
     *  can be on two figures reached two different ways. */
    static get ORIGIN_KEY() { return "plexora:figure-builder-origin"; }

    /** How long a toast stays. Long enough to read and act on, short enough not
     *  to sit over the figure. */
    static get TOAST_MS() { return 9000; }

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
        this.sidebar = "panels";
        //: ...and what to put back when a contextual panel has finished with it.
        this.pinnedSidebar = "panels";
    }

    static boot() {
        const root = document.getElementById("fb_workspace");
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
            // Double-clicking a panel reaches for the CHEAP edit now that
            // there is one: Quick Edit opens beside the canvas, where the
            // expensive route is a page load into the viewer.
            onEditPanel: (panelId) => this.quickEditPanel(panelId),
            onSelectionChange: (ids) => this.selectionChanged(ids),
            onGesture: (active) => this.contextBar?.suppress(active),
            onToolFinished: () => this.setTool("select"),
            onEditText: (annotationId) => this.editText(annotationId),
            onEditPoints: (annotationId) => this.editPoints(annotationId),
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
            canvas: this.canvas,
            state: this.state,
            handlers: {
                onQuickEdit: (panelId) => this.quickEditPanel(panelId),
                onEditPanel: (panelId) => this.onEditPanel(panelId),
                onSplit: (mode) => this.split(mode),
                onPanelChange: (panelId, changes) => this.updatePanel(panelId, changes),
                onPanelsChange: (updates) => this.updatePanels(updates),
                onSetPixelSize: (sourceIds, value) => this.setPixelSize(sourceIds, value),
                onShareLegendColours: (panelIds, colours) =>
                    this.shareLegendColours(panelIds, colours),
                onCopyRendering: (panelId) => this.copyRendering(panelId),
                onApplyRendering: (panelIds) => this.applyRendering(panelIds),
                hasRenderClipboard: () => this.hasRenderClipboard(),
            },
            onClose: () => this.contextSidebar(),
        });
        this.imagePanel.setup();

        this.contextBar = new FigureContextBar({
            overlayEl: this.el("fb_overlay_layer"),
            canvas: this.canvas,
            state: this.state,
            handlers: {
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
                onEditText: (annotationId) => this.editText(annotationId),
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
                onEditText: (annotationId) => this.editText(annotationId),
                onEditPoints: (annotationId) => this.editPoints(annotationId),
                onDeleteFromFigure: (ids) => this.deleteFromFigure(ids),
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

        //: The slide-over that edits a panel's microscopy view in place. Handed
        //: THIS document state -- never a second one; see the class comment.
        this.quickEdit = new FigureQuickEdit({
            workspace: this,
            api: this.api,
            state: this.state,
            figureId: this.figureId,
            onOpenInViewer: (panelId) => this.onEditPanel(panelId),
            // Opening the slide-over shuts the strip; closing it gives the strip
            // back to whatever the selection asks for. See `contextSidebar`.
            onSessionChange: () => this.contextSidebar(),
        });
        this.quickEdit.setup();

        this.contextBar.setup();

        this.state.on("change", () => this.render());
        this.state.on("status", (payload) => this.renderStatus(payload));

        this.applyBackLink();
        this.bindTopbar();
        this.bindToolRail();
        this.bindTray();
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
        this._onResize = () => this.contextBar?.position();
        window.addEventListener("resize", this._onResize);
    }

    destroy() {
        window.removeEventListener("keydown", this._onKey);
        window.removeEventListener("resize", this._onResize);
        this.closeMenu();
        this.contextMenu?.destroy();
        this.viewOptions?.destroy();
        this.quickEdit?.destroy();
        this.contextBar?.destroy();
        this.canvas?.destroy();
    }

    // -- wiring ------------------------------------------------------------

    bindTopbar() {
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

        this.el("fb_undo")?.addEventListener("click", () => this.state.undo());
        this.el("fb_redo")?.addEventListener("click", () => this.state.redo());

        this.el("fb_page_select")?.addEventListener("change", (event) => {
            this.canvas.setPage(event.target.value);
            this.contextBar?.update([]);
        });
        this.el("fb_page_menu")?.addEventListener("click", (event) => {
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
        this.el("fb_zoom_fit")?.addEventListener("click", () => this.zoomToFit());

        // Under the page rather than in a menu: adding one is the commonest
        // thing anybody does to a figure that has run out of room, and it is
        // the only page action worth a permanent button.
        this.el("fb_page_add")?.addEventListener("click", () => this.addPage());

        this.el("fb_view_menu")?.addEventListener("click", (event) => {
            this.openMenu(event.currentTarget, this.viewOptions.menuEntries(),
                          (act) => this.viewOptions.pick(act));
        });

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

    bindToolRail() {
        this.el("fb_tool_rail")?.addEventListener("click", (event) => {
            const tool = event.target.closest("[data-tool]");
            if (tool) this.setTool(tool.dataset.tool);
        });
        // Split where the controls differ rather than by how they are drawn: a
        // shape has a fill and is dragged out as a box, a line and an arrow
        // have two ends and are dragged out along one.
        this.el("fb_tool_shapes")?.addEventListener("click", (event) => {
            this.openShapesCard(event.currentTarget);
        });
        this.el("fb_tool_lines")?.addEventListener("click", (event) => {
            this.openLinesCard(event.currentTarget);
        });
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
            if (item) this.trayPointerDown(item.dataset.panelId, event);
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

        // Filters as the characters arrive; nothing is submitted. Esc and the ×
        // are the way back out -- a filter still on from five minutes ago looks
        // exactly like a tray that has lost panels, and the names it would be
        // recognised by are behind a hover now.
        const search = this.el("fb_tray_search");
        search?.addEventListener("input", (event) => {
            this.traySearch = event.target.value || "";
            this.renderTray();
        });
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

        // Two ways in and out of the same card: the rail item that opens it,
        // and the card's own X. Both go through one method so the rail button
        // and the aria state cannot disagree.
        this.el("fb_tool_panels")?.addEventListener("click", () => this.showTray());
        this.el("fb_tray_close")?.addEventListener("click", () => this.showTray(false));

        // Where the next panels come from. Built when it is opened rather than
        // at boot: the project list is a fetch, and a figure page that never
        // adds a panel should never ask the server for one.
        this.el("fb_tray_add")?.addEventListener("click", (event) => {
            this.openAddMenu(event.currentTarget);
        });

        // The file input moved here from the rail with the button that opens
        // it: importing a picture is a way of adding a panel, and this is where
        // adding panels lives now.
        this.el("fb_add_image_input")?.addEventListener("change", (event) => {
            const files = Array.from(event.target.files || []);
            // Cleared straight away, or choosing the same file twice in a row
            // fires nothing the second time and reads as a broken button.
            event.target.value = "";
            if (files.length) this.importFiles(files);
        });
    }

    // -- the strip beside the rail ------------------------------------------

    /**
     * One secondary sidebar at a time.
     *
     * The tray and the text panel occupy the SAME strip beside the rail, and
     * they used to hide and show themselves independently: selecting a caption
     * while the tray was open drew the text panel underneath it, where it was
     * invisible and still took every click that landed on the overlap. Two
     * panels each answering "am I open?" for themselves is two answers to a
     * question that has room for one.
     *
     * So the strip has an owner, and it is here. `name` is the panel that
     * should be in it, or null for none.
     *
     * The tray is opened BY HAND and the text panel appears with the selection,
     * which is why the tray is remembered as `pinned` and the text panel is
     * not: a contextual panel borrows the strip while it has something to say
     * and gives it back afterwards, rather than leaving the user to go and
     * reopen the drawer they never closed.
     */
    showSidebar(name) {
        this.sidebar = name || null;
        // A CONTEXTUAL panel never becomes the pinned one: the strip goes back
        // to whatever the user last chose for themselves once the selection
        // that summoned it has gone.
        if (name !== "text" && name !== "shape" && name !== "line"
                && name !== "image") {
            this.pinnedSidebar = this.sidebar;
        }
        this.applySidebar();
    }

    /**
     * Settle which contextual panel has the strip, or hand it back.
     *
     * ARBITRATION, not a switch. Each panel says only whether it has anything
     * to show (`wants`); this decides, because there is one strip and only one
     * of them can be in it -- two panels each showing and hiding themselves is
     * how they ended up stacked on top of each other. Text, then shape, then
     * line, then image: the first three are mutually exclusive in practice (a
     * selection is one kind or another) and a fixed order is one fewer thing to
     * reason about than a most-recently-asked rule.
     *
     * Image comes LAST because it is the only one that overlaps with the
     * others: selecting a caption and a panel together is a real thing to do,
     * and the caption's panel has controls that apply to exactly what was
     * selected while this one mostly does not.
     */
    contextSidebar() {
        // Quick Edit takes the strip with it. It is a 420px dark slide-over
        // whose subject is one panel's image, and a contextual sidebar is a
        // 300px light card describing the same panel's furniture -- open
        // together they were two panels about one object, in two colour schemes,
        // one of them lying over the artwork the other is for.
        //
        // The guard is here rather than at the open, because the selection pump
        // calls this on every change: without it, clicking another panel while
        // a session is live would reopen the strip underneath the slide-over.
        if (this.quickEdit?.session) return this.showSidebar(null);
        if (this.textPanel?.wants) return this.showSidebar("text");
        if (this.shapePanel?.wants) return this.showSidebar("shape");
        if (this.linePanel?.wants) return this.showSidebar("line");
        if (this.imagePanel?.wants) return this.showSidebar("image");
        return this.showSidebar(this.pinnedSidebar);
    }

    /**
     * Draw the strip, and tell the canvas how much room it has taken.
     *
     * `--fb-sidebar-w` is the second half of a promise this file's own top
     * comment has been making since the workspace was built: "the canvas is
     * padded rather than overlapped... the padding is a variable, so opening the
     * tray slides the page clear of it". The padding cleared the RAIL and
     * nothing else, so the tray and every contextual panel lay over three
     * hundred pixels of the sheet -- including, in the image panel's case, over
     * the very panel it was describing.
     *
     * The cost is that the page moves when a drawer opens, which is what the old
     * code said it was avoiding. That cost is real and it is smaller: a sheet
     * that slides 300px in 180ms is a movement the eye follows, where a card
     * lying over the thing being judged is a card that has to be closed before
     * any judgement can be made.
     */
    applySidebar() {
        for (const [name, id] of Object.entries(FigureWorkspace.SIDEBARS)) {
            const el = this.el(id);
            if (el) el.hidden = this.sidebar !== name;
        }
        const root = this.el("fb_workspace");
        if (root) {
            const width = FigureWorkspace.SIDEBAR_WIDTHS[this.sidebar] || 0;
            root.style.setProperty("--fb-sidebar-w", `${width}px`);
        }
        // The rulers are drawn into fixed-size canvases from the scroll
        // surface's own geometry, so a padding change moves every tick on them.
        this.viewOptions?.drawRulers();
        // The tray's badge only shows while its card is shut, so it depends on
        // what was just decided.
        this.renderTray();
        this.renderRail();
    }

    /** Which element each secondary sidebar is. The single place they are named
     *  together, so adding another is one line rather than a search. */
    static get SIDEBARS() {
        return { panels: "fb_tray_panel", text: "fb_text_panel",
                 shape: "fb_shape_panel", line: "fb_line_panel",
                 image: "fb_image_panel" };
    }

    /** How much of the desk each of them covers, in the same place they are
     *  named -- the stylesheet knows the widths too, and two copies of a number
     *  the page slides by is a number that drifts. */
    static get SIDEBAR_WIDTHS() {
        return { panels: 312, text: 300, shape: 300, line: 300, image: 300 };
    }

    /** The unit the page is measured in, changed from wherever names it. */
    setUnits(unit) {
        this.viewOptions?.setUnit(unit);
    }

    /**
     * Open or close the panel tray.
     *
     * Not remembered between sessions. The tray is where captured panels
     * arrive, and a figure page that opened with it shut would look, on the day
     * it is reopened, like a figure that had lost them.
     *
     * With no argument this toggles, so the rail button is a switch rather than
     * a one-way door -- clicking the lit item is how anybody expects to put it
     * out.
     *
     * Nothing else moves. The card lies over the desk beside the sheet and the
     * canvas keeps its position: the tray used to push the page sideways, which
     * meant looking at your panels moved the thing you were composing.
     */
    showTray(open) {
        const want = open === undefined ? this.sidebar !== "panels" : !!open;
        this.showSidebar(want ? "panels" : null);
    }

    /**
     * Where the next panels come from.
     *
     * Two routes and one menu. A project is a page load into that project's
     * viewer, with THIS figure remembered as the capture destination first --
     * the same localStorage key the dock over there reads, because "which
     * figure am I capturing into" is one setting and this is plainly the
     * figure the user is looking at. Nothing is written to the figure by
     * opening the menu or by opening a project: a slide somebody looks at and
     * leaves, leaves no trace.
     *
     * An image file is the other kind of thing a figure is made of -- the
     * schematic, the plot that came out of R -- and it goes into this figure
     * without a project at all.
     */
    async openAddMenu(anchor) {
        const projects = await this.projectList();
        const shown = projects.slice(0, FigureWorkspace.ADD_MENU_PROJECTS);
        const entries = shown.map((project) => ({
            act: "open:" + project.name,
            label: project.name,
        }));
        if (!shown.length) {
            entries.push({ act: "none", label: "No projects on this server",
                           disabled: true });
        }
        entries.push({ separator: true });
        // The rest of them on the page built for choosing between many. That
        // page opens a project WITHOUT the tool, so the trip is two clicks
        // longer at the far end -- the figure is remembered either way, so the
        // captures still land here.
        if (projects.length > shown.length) {
            entries.push({ act: "browse", label: "All projects…" });
        }
        entries.push({ act: "file", label: "Image file…" });
        this.openMenu(anchor, entries, (act) => this.addFrom(act));
    }

    /**
     * The projects on this server, most recently opened first.
     *
     * Fetched once: the list changes when somebody creates a project, which is
     * not something that happens while a figure page sits open, and a menu
     * that re-fetched on every click would be a request per glance.
     */
    async projectList() {
        if (this._projects) return this._projects;
        this._projects = [];
        try {
            const response = await fetch(this.api.url("projects"));
            const data = response.ok ? await response.json() : [];
            if (Array.isArray(data)) {
                this._projects = data
                    .filter((project) => project && project.name)
                    .sort((a, b) => String(b.lastOpenedAt || "")
                        .localeCompare(String(a.lastOpenedAt || "")));
            }
        } catch (error) {
            // The menu still has the image-file route, which needs no server.
        }
        return this._projects;
    }

    addFrom(act) {
        if (act === "file") {
            this.el("fb_add_image_input")?.click();
            return;
        }
        if (act === "browse") {
            this.rememberDestination();
            PlexoraRouter.go(this.api.url("open_project"));
            return;
        }
        if (!act.startsWith("open:")) return;
        this.rememberDestination();
        // ?tool= so the capture dock is on the image when the page lands. The
        // user asked to add panels; arriving at a viewer with no way to
        // capture from it would be most of the way to nothing.
        PlexoraRouter.go(this.api.url(encodeURIComponent(act.slice(5)))
            + "?tool=figure_builder");
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
               the dock asks which figure when it gets there. */
        }
    }

    bindCanvasHost() {
        // Dropping an image onto the page imports it into THIS FIGURE and
        // nowhere else. Making the user create a project to put a schematic in
        // a figure is exactly the setup step this plugin exists to remove.
        const scroll = this.el("fb_canvas_scroll");
        if (!scroll) return;
        scroll.addEventListener("dragover", (event) => {
            if (event.dataTransfer?.types.includes("Files")) event.preventDefault();
        });
        scroll.addEventListener("drop", (event) => this.dropFiles(event));
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
            // total distance land in the same place.
            this.zoomAt(event.clientX, event.clientY, Math.exp(-event.deltaY * 0.0035));
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

    renderRail() {
        const rail = this.el("fb_tool_rail");
        rail?.querySelectorAll("[data-tool]").forEach((button) => {
            const on = button.dataset.tool === this.tool;
            button.classList.toggle("is-active", on);
            button.setAttribute("aria-pressed", String(on));
        });
        // The two menu buttons have no data-tool of their own -- each stands
        // for a pair -- so each is lit whenever either of its pair is armed.
        for (const [id, armed] of [
            ["fb_tool_shapes", (tool) => tool.startsWith("shape:")],
            ["fb_tool_lines", (tool) => FigureCanvas.lineTool(tool) !== null]]) {
            const button = this.el(id);
            if (!button) continue;
            const on = armed(this.tool || "");
            button.classList.toggle("is-active", on);
            button.setAttribute("aria-pressed", String(on));
        }
        // The tray's rail item is a switch, not a tool: it is lit while its
        // card is open, and the card is the authority on that -- keeping a
        // second copy of "is the tray open" in a field is how the button and
        // the panel end up disagreeing after an undo or a reload.
        const tray = this.el("fb_tray_panel");
        const trayButton = this.el("fb_tool_panels");
        if (tray && trayButton) {
            trayButton.classList.toggle("is-active", !tray.hidden);
            trayButton.setAttribute("aria-expanded", String(!tray.hidden));
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
    editText(annotationId) {
        this.textEditor?.open(annotationId);
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
        this.mountMenu(anchor, entries.map((entry) => entry.separator
            ? '<span class="fb-menu-separator"></span>'
            : `<button type="button" class="fb-menu-item" data-act="${entry.act}"
                       ${entry.disabled ? "disabled" : ""}>
                   ${"checked" in entry
                       ? `<span class="fb-menu-tick">${entry.checked ? "✓" : ""}</span>`
                       : ""}${FigureSchema.escapeHtml(entry.label)}</button>`).join(""),
            onPick);
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
        return [
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
        if (act === "add") this.addPage();
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
        // The OS colour picker, through an input nothing ever sees. Building a
        // colour wheel here would be a worse one that also has to be
        // maintained.
        const picker = document.createElement("input");
        picker.type = "color";
        picker.value = this.canvas.page?.background || "#ffffff";
        picker.style.position = "fixed";
        picker.style.opacity = "0";
        picker.style.pointerEvents = "none";
        document.body.appendChild(picker);
        picker.addEventListener("change", () => {
            this.commitPageBackground(picker.value);
            picker.remove();
        });
        picker.addEventListener("blur", () => picker.remove());
        picker.click();
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
            if (stored !== false) this.canvas.setPage(page.page_id);
        });
    }

    zoomMenuEntries() {
        return [
            { act: "fit", label: "Fit page  ⌘0" },
            { act: "selection", label: "Fit selection",
              disabled: !this.canvas.selection.size },
            { act: "100", label: "100%  ⌘1" },
        ];
    }

    zoomAction(act) {
        if (act === "fit") this.zoomToFit();
        else if (act === "selection") this.zoomToSelection();
        else if (act === "100") this.setScale(FigureWorkspace.PX_PER_MM);
    }

    // -- keyboard ------------------------------------------------------------

    keyDown(event) {
        // A <dialog> traps focus, not keystrokes. With the delete confirmation
        // up, Escape reached this handler as well as the dialog and disarmed
        // the drawing tool behind it.
        if (FigureConfirm.modalOpen) return;
        const typing = document.activeElement
            && ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName);
        if (typing) return;

        if (event.key === "Escape") {
            this.closeMenu();
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

    /** A figure-wide setting -- the label style, the gutter, the default DPI. */
    updateSettings(settings) {
        this.state.commit(
            [{ op: "set_meta", changes: { settings: settings } }],
            (draft) => { Object.assign(draft.settings, settings); });
    }

    /**
     * Record a pixel size the user typed for images that never had one.
     *
     * On the SOURCE, because it is a fact about the image and every panel of it
     * is entitled to the same answer -- and marked `manual`, which the
     * provenance page prints. A number somebody typed is not the same evidence
     * as one the file stated, and a figure that could not tell the difference
     * would have scale bars nobody could check.
     */
    setPixelSize(sourceIds, value) {
        const real = sourceIds.filter((id) => this.state.source(id));
        if (!real.length || !(value > 0)) return;
        const pixelSize = { value: value, unit: "µm", source: "manual" };
        this.state.commit(
            real.map((sourceId) => ({
                op: "update_source", source_id: sourceId,
                changes: { pixel_size: pixelSize } })),
            (draft) => {
                for (const sourceId of real) draft.sources[sourceId].pixel_size = pixelSize;
            });
    }

    /**
     * Make a set of panels draw each marker the same colour.
     *
     * Asked for explicitly, never inferred: two panels showing CD8 in different
     * colours may be deliberate, and quietly repainting one of them would
     * change what a figure asserts. The legend popover puts this behind a
     * button that says what it does, with "keep them separate" first.
     *
     * Every recoloured panel gets a new render revision, so its cached preview
     * is refetched and the export re-renders it -- a recolour that left the old
     * raster in place would be a panel whose picture and whose legend disagree.
     */
    shareLegendColours(panelIds, canonical) {
        const updates = [];
        for (const panelId of panelIds) {
            const panel = this.state.panel(panelId);
            if (!panel) continue;
            let touched = false;
            const channels = (panel.scene.channels || []).map((channel) => {
                const name = channel.fullname_at_capture || channel.key;
                const colour = canonical.get(name);
                if (!colour) return channel;
                if (channel.color.r === colour.r && channel.color.g === colour.g
                        && channel.color.b === colour.b) {
                    return channel;
                }
                touched = true;
                return { ...channel, color: { ...colour } };
            });
            if (!touched) continue;
            updates.push({
                panel_id: panelId,
                changes: {
                    scene: { ...panel.scene, channels: channels },
                    render_revision: panel.render_revision + 1,
                },
            });
        }
        if (!updates.length) return;
        this.updatePanels(updates);
        // The previews are now pictures of the old colours. Saying so beats
        // showing a row that looks unchanged until it is exported.
        this.toast("Colors matched. Reopen a panel to redraw its preview — "
            + "the export renders the new colors either way.");
    }

    // -- copying one panel's rendering onto others ---------------------------

    /**
     * Remember how this panel is rendered, to put on others.
     *
     * CHANNELS only -- their colours, their windows, which of them are on.
     * Never the viewport, the placement, the title or the label: those are what
     * makes each panel a different panel, and a "copy rendering" that moved
     * them would be a duplicate wearing the target's id.
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
                    `.fb-panel[data-panel-id="${panelId}"] .fb-panel-image`);
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
        dismiss.innerHTML = '<span class="fas fa-xmark"></span>';
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
    async split(mode) {
        const panelId = Array.from(this.canvas.selection)[0];
        if (!panelId) return;
        const groupId = this.canvas.splitComposite(panelId, mode);
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
        this.canvas.pageId = page.page_id;
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
        this.canvas.pageId = null;
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
        this.traySearch = "";
        const search = this.el("fb_tray_search");
        if (search) search.value = "";
        this.renderTray();
    }

    trayPanels() {
        const panels = FigureSchema.panelsInTray(this.state.document);
        const needle = this.traySearch.trim().toLowerCase();
        if (!needle) return panels;
        return panels.filter((panel) => this.trayHaystack(panel).includes(needle));
    }

    trayHaystack(panel) {
        const source = this.state.source(panel.source_id);
        return [panel.title, panel.label && panel.label.text,
                source && (source.display_name || source.datasource)]
            .filter(Boolean).join(" ").toLowerCase();
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

    dropFiles(event) {
        const files = Array.from(event.dataTransfer?.files || []);
        if (!files.length) return;
        event.preventDefault();
        this.importFiles(files, this.canvas.surfacePoint(event));
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
                // physical scale, so its panels have no scale bar until
                // somebody types one in.
                pixel_size: null, channels: [], status: "ok",
            };
            const panel = {
                panel_id: panelId, source_id: sourceId,
                scene: { ...FigureSchema.emptyScene(sourceId),
                         viewport: { x: 0, y: 0,
                                     w: entry.dimensions.width, h: entry.dimensions.height } },
                placement: { page_id: page.page_id, ...boxes[index], z: z++ },
                title: entry.asset.filename, label: { text: "", auto: true, visible: true },
                ...FigureSchema.defaultFurniture(), render_revision: 1,
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
            : (panel.scene.viewport.h > 0
                ? panel.scene.viewport.w / panel.scene.viewport.h : 1);
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

        this.renderPageList();
        this.canvas.render();
        if (!this._fitted) {
            this._fitted = true;
            this.canvas.zoomToFit(this.el("fb_canvas_scroll"));
        }
        // Margins move with the page, the grid and the rulers move with the
        // zoom, and both can have changed by the time this runs.
        this.viewOptions?.apply();
        // The annotation being typed into is hidden behind its editor, and the
        // render above has just put it back. Re-hidden here rather than in the
        // canvas, which does not know an editor exists.
        if (this.textEditor?.active) {
            const element = this.canvas.surfaceEl.querySelector(
                `[data-annotation-id="${this.textEditor.annotationId}"]`);
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
        this.contextBar?.update(Array.from(this.canvas.selection));
        this.textPanel?.update(Array.from(this.canvas.selection));
        this.shapePanel?.update(Array.from(this.canvas.selection));
        this.linePanel?.update(Array.from(this.canvas.selection));
        this.imagePanel?.update(Array.from(this.canvas.selection));
        this.contextSidebar();
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
            return;
        }
        const placed = FigureSchema.panelsOnPage(this.state.document, page.page_id).length
            + Object.values(this.state.document.annotations)
                .filter((annotation) => annotation.page_id === page.page_id).length;
        count.textContent = placed === 1 ? "1 object" : `${placed} objects`;
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
        this.contextBar?.update(ids);
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
        const shown = this.trayPanels();

        const wrap = this.el("fb_tray_search_wrap");
        if (wrap) wrap.hidden = all.length < FigureWorkspace.SEARCH_THRESHOLD;
        const clear = this.el("fb_tray_search_clear");
        if (clear) clear.hidden = !this.traySearch;
        const count = this.el("fb_tray_count");
        if (count) {
            count.textContent = all.length
                ? (shown.length === all.length
                    ? String(all.length)
                    : `${shown.length} / ${all.length}`)
                : "";
        }
        const empty = this.el("fb_tray_empty");
        if (empty) empty.hidden = all.length > 0;

        // Only while the card is shut. Open, the count is already in its
        // heading, and two copies of the same number a hundred pixels apart
        // read as two different numbers.
        const badge = this.el("fb_tray_badge");
        const panel = this.el("fb_tray_panel");
        if (badge) {
            badge.textContent = (all.length && panel?.hidden) ? String(all.length) : "";
        }

        const strip = this.el("fb_tray_strip");
        if (!strip) return;
        // Anything filtered out of view is out of the selection too: a batch
        // placed from a search box has to be the batch that was visible.
        const visible = new Set(shown.map((panel) => panel.panel_id));
        for (const id of Array.from(this.traySelection)) {
            if (!visible.has(id)) this.traySelection.delete(id);
        }

        if (all.length && !shown.length) {
            strip.innerHTML = '<p class="fb-muted fb-tray-nomatch">No panel matches that.</p>';
            return;
        }
        strip.innerHTML = shown.map((panel) => {
            const source = this.state.source(panel.source_id);
            const span = FigureSchema.physicalWidthUm(source, panel.scene.viewport);
            const caption = panel.title
                || (source && (source.display_name || source.datasource))
                || "Untitled panel";
            const detail = span ? FigureSchema.formatMicrons(span) + " wide" : "";
            const selected = this.traySelection.has(panel.panel_id);
            // The tiles are two across and mostly picture now, so the caption
            // is behind a hover -- which makes this the accessible name as well
            // as the tooltip, and it has to carry both lines.
            const tip = detail ? `${caption} — ${detail}` : caption;
            // And the tile is the SHAPE of the region it shows, before the
            // picture arrives: the preview is this viewport, so the ratio is
            // known without waiting for the file. A grid that resized itself as
            // twelve lazy images loaded would move the tile out from under the
            // pointer that was reaching for it.
            const view = panel.scene && panel.scene.viewport;
            const ratio = (view && view.w > 0 && view.h > 0)
                ? ` style="aspect-ratio:${Number(view.w)} / ${Number(view.h)}"`
                : "";
            return `<div class="fb-tray-item${selected ? " is-selected" : ""}" draggable="true"
                         role="option" aria-selected="${selected}"
                         data-panel-id="${FigureSchema.escapeHtml(panel.panel_id)}"
                         aria-label="${FigureSchema.escapeHtml(tip)}"
                         title="${FigureSchema.escapeHtml(tip)}">
                <img src="${this.api.previewUrl(this.figureId, panel.panel_id, panel.render_revision)}"
                     alt="" draggable="false" loading="lazy"${ratio}>
                <span class="fb-tray-item-text">
                    <span class="fb-tray-item-name">${FigureSchema.escapeHtml(caption)}</span>
                    <span class="fb-tray-item-meta">${FigureSchema.escapeHtml(detail)}</span>
                </span>
            </div>`;
        }).join("");
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
