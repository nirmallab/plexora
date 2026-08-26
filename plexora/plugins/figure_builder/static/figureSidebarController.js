/**
 * FigureBuilderSidebarController - Figure Builder inside the viewer.
 *
 * The name is core's: this is whatever `createSidebarController` returns, and
 * core calls that hook for every plugin. Figure Builder no longer HAS a sidebar
 * panel. It declares no panels at all, so it gets no card in the tool column,
 * and everything it shows the user is on the image itself
 * (figureCaptureDock, figureCaptureBoxes, figureCaptureTool).
 *
 * That is not tidiness. The controls were split between the two -- capture on
 * the image, "which figure?" in the sidebar -- and the two halves of one
 * decision in two places is worse than either place on its own. It also means
 * the sidebar no longer offers a way to close this tool, so the dock carries
 * one, and it takes the plugin all the way out.
 *
 * The figure canvas is NOT here either. It used to be rendered beside the image
 * in a second workspace slot; composing a figure and looking down a microscope
 * are different activities and half a window was not enough for either, so the
 * canvas has its own page and its own URL (figureWorkspace, on
 * `/plugins/figure_builder/figure/<id>`) and everything in this file that
 * wanted it navigates instead.
 *
 * ## Captures do not need a figure
 *
 * A capture goes into a session strip first and into a figure second. Making the
 * user answer "which figure?" before they have decided which regions are worth
 * keeping is a management decision demanded at the worst possible moment -- and
 * the honest answer, early on, is usually "I do not know yet". So:
 *
 *   figure already open   the capture is committed to it immediately, exactly
 *                         as before, and survives a reload
 *   no figure open        the capture is held in this page's memory and the
 *                         strip marks it as not yet in a figure. It is written
 *                         out the moment one is chosen, in ONE batch.
 *
 * The second case is the one with a cost, and it is stated rather than hidden:
 * the strip marks those captures, and leaving the page while any are unattached
 * asks first. They are memory, and memory does not survive a navigation.
 *
 * ## Every capture leaves a mark
 *
 * The region a capture came from stays outlined on the image for the rest of the
 * session (figureCaptureBoxes). Selecting one -- from the strip or by clicking
 * its outline -- highlights both and puts the viewer back over that field. It
 * restores NOTHING about the rendering, on purpose: going back to a region,
 * changing the channels and capturing it again is how two panels of one field
 * under two renderings are made, and it has to be the easy thing to do.
 * Reopening a panel's whole captured scene is a different action, is reached
 * from the canvas, and says so on screen while it is running.
 *
 * Which figure is "current" is remembered in localStorage rather than on the
 * server. It is a property of this browser and this person's train of thought,
 * not of the figure or of the project: two people (or two windows) working on
 * different figures from the same image is ordinary, and a server-side "current
 * figure" would make each of them keep switching the other's back.
 */
class FigureBuilderSidebarController {

    static get STORAGE_KEY() { return "plexora.figure_builder.currentFigure"; }

    /** Core's name for this plugin, which is also the handle the tool loader
     *  answers to. */
    static get TOOL() { return "figure_builder"; }

    /**
     * The panel a capture becomes.
     *
     * Static and pure so the shape is checked without a browser -- one place
     * for the defaults means a field added to the format has one place to be
     * remembered in, whichever path built the panel.
     */
    static panelFor(capture, source) {
        return {
            panel_id: FigureSchema.newPanelId(),
            source_id: source.source_id,
            // The scene was taken before the source existed -- see onCaptured --
            // so this is where the two are joined.
            scene: { ...capture.scene, source_id: source.source_id },
            // Straight to the tray: composition is a different sitting from
            // exploration, and forcing a layout decision at the moment of
            // capture is what makes people stop capturing.
            placement: null,
            label: { text: "", auto: true, visible: true },
            // No calibration, no scale bar. A bar drawn from an assumed pixel
            // size looks exactly like one that is right.
            ...FigureSchema.defaultFurniture({
                scalebar: {
                    ...FigureSchema.defaultFurniture().scalebar,
                    visible: Boolean(source.pixel_size),
                },
            }),
            render_revision: 1,
        };
    }

    constructor(ctx) {
        this.ctx = ctx;
        this.datasource = ctx.datasource;
        this.api = new FigureBuilderApi({ url: ctx.url });

        this.figures = [];
        this.figureId = null;
        this.state = null;

        //: This session's captures, newest first. Each is
        //: {id, scene, preview, url, panelId} -- panelId null until it is in a
        //: figure. The list dies with the page, which is also why every entry in
        //: it is from THIS datasource: navigating to another image reloads.
        this.captures = [];
        //: The one the user is looking at, in the strip and on the image at the
        //: same time. One id, not a flag per capture, because "selected" is a
        //: property of the session and two things render it.
        this.selected = null;
        //: Attachment runs in a chain rather than in parallel: two captures a
        //: moment apart would otherwise both read "nothing attached yet" and
        //: write the same panel twice.
        this._attaching = null;

        //: {panelId, stash, report} while a panel's view is loaded into the
        //: live viewer. Null the rest of the time -- which is what every
        //: handler below tests, rather than a set of booleans.
        this.editing = null;
        //: A panel the figure page asked for before it navigated here. Held
        //: until the document is open, because the panel being asked for is
        //: inside it.
        this.pendingPanelId = null;

        //: What the dock says about itself. Both are transient: nothing here is
        //: worth persisting, and a stale "Saved" is worse than none.
        this.statusText = "";
        this.failure = "";
        //: The "where do these go?" dialog, built here rather than rendered
        //: into a panel, because there is no panel. Appended to <body>: a
        //: <dialog> inside a hidden ancestor is one showModal() opens onto
        //: nothing.
        this.chooser = null;

        this.capture = new FigureCaptureTool(ctx, {
            toolName: FigureBuilderSidebarController.TOOL,
            onCapture: (rect, screenRect, preview) => this.onCaptured(rect, preview),
            onStateChange: () => this.renderDock(),
            // The lock is the selection: when the frame stops being on a
            // capture, the strip and the boxes stop saying it is.
            onUnpin: () => this.deselect(),

        });
        // The boxes share the capture tool's coordinate frame rather than
        // carrying a second copy of the arithmetic: selecting a capture aims
        // the frame at its box, and two copies would agree right up until the
        // day one of them was fixed.
        this.boxes = new FigureCaptureBoxes(ctx, {
            tool: this.capture,
            onSelect: (id) => this.selectCapture(id),
        });
        this.dock = new FigureCaptureDock({
            onToggleCapture: () => this.toggleCapture(),
            onSelectCapture: (id) => this.selectCapture(id),
            onRemoveCapture: (id) => this.removeCapture(id),
            onNewFigure: () => this.createFigure(),
            onChooseFigure: () => this.askWhereToPut(),
            onOpenCanvas: () => this.goToCanvas(),
            onUpdatePanel: () => this.updatePanel(),
            onCancelEdit: () => this.cancelEdit(),
            onClose: () => this.close(),
        });

        //: Unattached captures are memory. Leaving with some still in the strip
        //: is the one way to lose work here, so it is the one thing that asks.
        this._onBeforeUnload = (event) => {
            if (!this.unattached()) return;
            event.preventDefault();
            event.returnValue = "";
        };

        // Through the plugin's own cleanup list so the viewer, document and
        // window listeners this tool installs go when the plugin does. Left
        // behind, a drag meant for another tool would move a viewfinder over an
        // image nobody is capturing from.
        ctx.onCleanup?.(() => this.destroy());
    }

    /** How many captures are not in a figure yet. */
    unattached() {
        return this.captures.filter((capture) => !capture.panelId).length;
    }

    // -- lifecycle -------------------------------------------------------

    setup() {
        this.buildChooser();
        window.addEventListener("beforeunload", this._onBeforeUnload);
        this.mount();
    }

    destroy() {
        window.removeEventListener("beforeunload", this._onBeforeUnload);
        this.capture.destroy();
        this.boxes.destroy();
        this.dock.destroy();
        this.chooser?.remove();
        this.chooser = null;
        for (const capture of this.captures) {
            if (capture.url) URL.revokeObjectURL(capture.url);
        }
        this.captures = [];
        this.selected = null;
    }

    /**
     * Take Figure Builder off the page altogether.
     *
     * Through the loader rather than by tearing down in place: the loader owns
     * the record of which tools are loaded, and a controller that dismantled
     * itself behind its back would leave an entry pointing at a dead object --
     * and re-opening from the Tools menu would then do nothing at all.
     */
    async close() {
        const waiting = this.unattached();
        // FigureConfirm and not `window.confirm`, for the reasons in its
        // docstring. On this page it lands on <body> rather than in the
        // workspace, which is where it should be: the viewer is dark, and
        // core's tokens are the right ones to inherit here.
        if (waiting && !await FigureConfirm.ask({
            title: "Close Figure Builder?",
            body: FigureSchema.countPhrase(waiting, "capture")
                  + " not yet in a figure will be lost.",
            confirm: "Close and discard",
        })) {
            return;
        }
        // Belt and braces: the loader's removeTool reaches destroy() through
        // deactivatePlugin, and on a page where the loader is somehow absent
        // this is still the honest thing to do.
        if (window.PlexoraToolLoader?.removeTool) {
            window.PlexoraToolLoader.removeTool(FigureBuilderSidebarController.TOOL);
        } else {
            this.destroy();
        }
    }

    onShow() {
        // The library may have changed on another page since this tool was
        // last looked at -- a figure created there, or deleted.
        this.mount();
        this.fetchSaved().then(() => {
            // A request the canvas left behind on its way here, taken up now.
            //
            // applyOrDefault reads the same note, but only when the tool BOOTS
            // -- which used to be the only way back into the viewer, because
            // the canvas set window.location and the page was rebuilt around
            // the note. appRouter.js keeps the viewer alive across that trip
            // now, so coming back can find this controller already running,
            // already holding a figure, and with nothing to make it look. That
            // is indistinguishable from double-clicking a panel doing nothing.
            //
            // After the list, not before: the canvas is a place figures get
            // created, and a note naming one this session has never heard of
            // would be discarded as naming a figure that does not exist.
            const pending = this.takePendingEdit();
            if (pending) this.adopt(pending);
            else this.render();
        });
    }

    /**
     * Another tool was opened over this one.
     *
     * The FRAME stands down, because it listens on the viewer and on the
     * document, neither of which goes away here -- and a drag meant for the tool
     * the user just opened must not also redraw a viewfinder.
     *
     * The DOCK does not. It is the session: the captures in it, and the regions
     * they came from, are the work, and a tool whose work vanished because the
     * user glanced at ROI would be a tool nobody trusted with an hour of it.
     * Figure Builder leaves the page when its own Close is pressed, and that is
     * the only time.
     */
    onHide() {
        this.capture.disarm();
        this.renderDock();
    }

    /**
     * Everything this tool opens with: the library, and which figure was last
     * being worked on.
     *
     * One request, made in parallel with core's channel restore by the sidebar
     * -- which is why this is a fetch that returns rather than a fetch that
     * renders.
     */
    async fetchSaved() {
        const result = await this.api.listFigures();
        if (!result.ok) {
            this.fail("The figure library could not be read.");
            return null;
        }
        this.figures = result.data.figures.filter((figure) => figure.readable);
        return this.figures;
    }

    applyOrDefault() {
        // A request left by the figure page before it navigated here outranks
        // the remembered figure: the user asked for this panel by
        // double-clicking it a moment ago, and landing on a different figure
        // would be the tool ignoring the thing they just did.
        this.adopt(this.takePendingEdit());
    }

    /** Open what `pending` asks for, or fall back to the remembered figure. */
    adopt(pending) {
        const remembered = pending ? pending.figure_id : this.readRemembered();
        const exists = this.figures.some((figure) => figure.figure_id === remembered);
        this.figureId = exists ? remembered : null;
        this.pendingPanelId = (pending && exists) ? pending.panel_id : null;
        //: The whole request, not just the panel: it also says which SHAPE the
        //: panel is now and where the user expects to end up. Kept rather than
        //: reduced to an id, because both of those are decisions made on the
        //: canvas that this page has no other way to learn.
        this.pendingEdit = (pending && exists) ? pending : null;
        this.render();
        if (this.figureId) this.openFigure(this.figureId);
    }


    // -- capturing -------------------------------------------------------

    mount() {
        if (this.dock.mount()) this.renderDock();
        if (this.boxes.mount()) this.renderBoxes();
    }

    /** Arm or stand down the viewfinder. */
    toggleCapture() {
        // Not while the viewer is showing a panel's borrowed scene: a capture
        // taken then would be a panel of somebody else's view, and nothing on
        // screen would say so.
        if (this.editing) return;
        if (this.capture.active) this.capture.disarm();
        else this.capture.arm();
    }

    /**
     * The shutter fired. Keep what was taken.
     *
     * The scene is read SYNCHRONOUSLY, before anything is awaited: it comes
     * from the live viewer, and an await first would let a pan, a channel
     * change or a contrast tweak land in a panel whose crop was decided before
     * it. The preview and the figure can both wait; the scene cannot.
     */
    async onCaptured(rect, previewPromise) {
        const capture = {
            id: FigureSchema.newId("cap"),
            // No source id yet -- there may be no figure to own a source. It is
            // filled in by panelFor() when the capture reaches one.
            scene: FigureScene.capture(this.ctx, "", rect),
            preview: null,
            url: null,
            panelId: null,
        };
        this.captures.unshift(capture);
        // Selected, but NOT centred on: the viewer is already exactly there,
        // and flying it to where it is would be a jolt with no cause. Only an
        // explicit click on a strip item or a box moves the viewer.
        //
        // Locking is free here for the same reason -- the frame is already on
        // what it just took, so pinTo() moves nothing -- and it makes the next
        // press of the shutter re-take this exact region after a channel
        // change. Panning or redrawing the frame lets go of it again, which is
        // what keeps "one frame, four places" working.
        this.selected = this.capture.pinTo(
            capture.scene.viewport, this.labelFor(capture.id)) ? capture.id : null;
        this.renderDock();
        this.renderBoxes();

        const preview = await previewPromise;
        if (preview) {
            capture.preview = preview;
            // An object URL, not the preview route: the thumbnail is then there
            // the moment the shutter closes, and it is the only way an
            // unattached capture -- which the server has never seen -- can show
            // one at all.
            capture.url = URL.createObjectURL(preview.blob);
        }
        this.renderDock();
        await this.attachCaptures();
    }

    /**
     * Go back to a capture, arm the shutter, and lock onto it.
     *
     * The FIELD, and nothing else. The channels, the windows, the colours and
     * the overlays stay exactly as the user has them, so the obvious next move
     * -- change the rendering, capture the same region again -- produces a
     * second panel in pixel-level concordance with the first. Restoring the
     * captured scene here would take that away, and a "go back" that silently
     * rewrote the viewer's colours would be the more surprising of the two.
     *
     * Locked, not merely aimed: while a capture is selected the shutter takes
     * THAT region, so the second panel is the same region and not a freehand
     * rectangle over roughly the same tissue. The lock is the selection -- see
     * deselect() for the other end of it.
     */
    selectCapture(id) {
        const capture = this.captures.find((entry) => entry.id === id);
        if (!capture) return;
        // Clicking a capture is asking to work on that region, and everything
        // that follows -- the frame landing on it, the shutter locking onto it
        // -- is invisible with the mode off. Arming here rather than making the
        // user find the orb first is also what stops the click reading as "that
        // did nothing": with no frame on screen, going back to a region moves
        // the viewer and leaves nothing behind saying why.
        //
        // Not while a panel's view is on loan, for the reason toggleCapture()
        // gives: a shot taken then is a panel of somebody else's scene.
        if (!this.editing) this.capture.arm();
        // Quietly: the flight below moves the viewer, and a lock that noticed
        // would let go of the very selection being made.
        this.capture.unpin(true);
        this.selected = id;
        this.renderDock();
        this.renderBoxes();

        const rect = capture.scene.viewport;
        const label = this.labelFor(id);
        this.boxes.centerOn(rect, () => {
            // A second click while this one was in the air wins; landing the
            // frame now would put it on the region the user just left.
            if (this.selected !== id) return;
            // One call, not aimAt-then-pinTo: those took two readings of where
            // the region is, a moment apart, and the viewer is still settling
            // for a while after it says it has stopped -- so the two disagreed
            // by a few pixels and the lock was refused on the capture the user
            // had just clicked. lockOn() takes one reading and uses it for both.
            if (!this.capture.lockOn(rect, label)) this.deselect();
        });
    }

    /**
     * The frame let go of the region it was locked to.
     *
     * Called by the tool when the user pans, zooms, redraws the frame or drags
     * it somewhere else. The highlight on the image and the active item in the
     * strip both mean "the shutter will take this one", so when that stops
     * being true they have to stop saying it.
     */
    deselect() {
        if (!this.selected) return;
        this.selected = null;
        this.renderDock();
        this.renderBoxes();
    }

    /** What to call a capture on the frame: the same number its box and its
     *  thumbnail carry, so all three are obviously the one thing. */
    labelFor(id) {
        const index = this.captures.findIndex((capture) => capture.id === id);
        return index < 0 ? "" : "Capture " + (index + 1);
    }

    /**
     * Write every unattached capture into the open figure, as one batch.
     *
     * One call is one undo step, so a burst of six captures that reach a figure
     * together undo as the one thing the user did. Nothing happens at all when
     * no figure is open -- that is the whole point of the strip.
     */
    attachCaptures() {
        this._attaching = (this._attaching || Promise.resolve())
            .then(() => this.attachOnce())
            .catch((error) => {
                console.error("figure_builder: captures could not be attached", error);
                return false;
            });
        return this._attaching;
    }

    async attachOnce() {
        if (!this.figureId || !this.state || !this.state.document) return false;
        const pending = this.captures.filter((capture) => !capture.panelId);
        if (!pending.length) return true;

        const source = await this.ensureSource();
        if (!source) {
            this.fail("This image could not be registered as a source.");
            return false;
        }

        // Oldest first, so the tray reads in the order the captures were taken
        // -- the strip shows them newest first because that is where the eye
        // goes, but the figure is a record and records run forwards.
        const ordered = pending.slice().reverse();
        const panels = ordered.map((capture) =>
            FigureBuilderSidebarController.panelFor(capture, source));

        this.setStatus("Saving captures…");
        const stored = await this.state.commit(
            panels.map((panel) => ({ op: "add_panel", panel: panel })),
            (draft) => { for (const panel of panels) draft.panels[panel.panel_id] = panel; });
        if (!stored) return false;

        // The panel is committed first and the preview uploaded after. The scene
        // is the master and the raster is a convenience -- so an upload that
        // fails leaves a panel that still re-renders correctly at export,
        // whereas the other order would leave an orphaned raster and no record
        // of what it was.
        for (let index = 0; index < ordered.length; index += 1) {
            const capture = ordered[index];
            const panel = panels[index];
            capture.panelId = panel.panel_id;
            if (capture.preview) {
                await this.api.putPreview(this.figureId, panel.panel_id, 1,
                    capture.preview.blob,
                    { width: capture.preview.width, height: capture.preview.height });
            }
        }
        this.render();
        return true;
    }

    /**
     * Drop a capture from the strip.
     *
     * If it already reached the figure it is a panel, and it goes from there
     * too: a strip and a canvas that disagree about what was kept is worse than
     * either answer on its own. Its box goes with it -- an outline on the image
     * pointing at a capture nobody can open is a mark with nothing behind it.
     */
    async removeCapture(id) {
        // Anything in flight finishes first: a capture removed while its panel
        // is halfway to the server would leave that panel in the figure with
        // nothing in the strip pointing at it. Waiting costs a moment and makes
        // the two paths agree -- after this, the capture either has a panel to
        // remove or never got one.
        if (this._attaching) await this._attaching;

        const index = this.captures.findIndex((capture) => capture.id === id);
        if (index < 0) return;
        const [capture] = this.captures.splice(index, 1);
        if (capture.url) URL.revokeObjectURL(capture.url);
        // The lock and the highlight are one state, so a capture that is gone
        // must not leave the shutter still aimed at where it used to be.
        if (this.selected === id) {
            this.selected = null;
            this.capture.unpin(true);
        }
        this.renderDock();
        this.renderBoxes();

        if (!capture.panelId || !this.state || !this.state.document) return;
        await this.state.commit(
            [{ op: "remove_panels", panel_ids: [capture.panelId] }],
            (draft) => { delete draft.panels[capture.panelId]; });
    }

    // -- choosing where captures go --------------------------------------

    /**
     * The "where do these go?" dialog.
     *
     * A native <dialog> is modal, focus-trapped and Esc-dismissible without a
     * line of script, and it cannot end up behind the canvas the way a
     * positioned div can. Built here rather than rendered by the server for the
     * same reason the dock is: this plugin has no panel to render it into, and
     * core has no slot over the image.
     */
    buildChooser() {
        if (this.chooser || !document.body) return;
        const dialog = document.createElement("dialog");
        dialog.id = "fb_destination_dialog";
        dialog.className = "fb-dialog";
        dialog.innerHTML = `
            <h2>Where would you like to add these captures?</h2>
            <p class="fb-muted" data-role="summary"></p>

            <div class="fb-dialog-actions">
                <button class="sidebar-action" type="button" data-role="new">
                    <span class="fas fa-plus"></span> Create new figure
                </button>
                <button class="sidebar-action secondary" type="button" data-role="existing">
                    <span class="fas fa-folder-open"></span> Open existing figure
                </button>
            </div>

            <div data-role="pick" hidden>
                <label class="control-label" for="fb_destination_select">Figure</label>
                <select id="fb_destination_select" class="fb-select" aria-label="Figure"></select>
                <div class="fb-dialog-actions">
                    <button class="sidebar-action" type="button" data-role="open">Open</button>
                </div>
            </div>

            <div class="fb-dialog-actions fb-dialog-footer">
                <button class="sidebar-action secondary" type="button" data-role="cancel">Cancel</button>
            </div>`;
        dialog.addEventListener("click", (event) => {
            const role = event.target.closest("[data-role]")?.dataset.role;
            if (role === "new") this.chooseNew();
            else if (role === "existing") this.offerExisting();
            else if (role === "open") this.chooseExisting();
            else if (role === "cancel") this.closeChooser();
        });
        document.body.appendChild(dialog);
        this.chooser = dialog;
    }

    part(role) {
        return this.chooser?.querySelector(`[data-role="${role}"]`) || null;
    }

    /**
     * Ask, once, at the moment the answer is needed.
     *
     * Which is when the user goes to compose -- not when they opened the tool,
     * and not when they took their first capture.
     */
    askWhereToPut() {
        if (!this.chooser) return;
        const summary = this.part("summary");
        if (summary) {
            const pending = this.unattached();
            summary.textContent = pending
                ? FigureSchema.countPhrase(pending, "capture") + " waiting."
                : "Nothing is waiting — this only chooses where the next ones go.";
        }
        const pick = this.part("pick");
        if (pick) pick.hidden = true;
        const existing = this.part("existing");
        if (existing) existing.disabled = this.figures.length === 0;
        this.chooser.showModal?.();
    }

    closeChooser() {
        this.chooser?.close?.();
    }

    /**
     * "A new figure" from the destination dialog: make it, then go and look at it.
     *
     * A navigation, not a pane. `goToCanvas` is what runs, so the waiting
     * captures are written into the new figure BEFORE the page changes and a
     * failed write cancels the trip -- unattached captures are memory, and this
     * navigation ends the memory.
     */
    async chooseNew() {
        this.closeChooser();
        // Only if one was actually created: opening the canvas onto a figure
        // that failed to be made is an empty page and no explanation.
        if (await this.createFigure()) await this.goToCanvas();
    }

    offerExisting() {
        const select = this.chooser?.querySelector("#fb_destination_select");
        if (select) {
            select.innerHTML = this.figures.map((figure) =>
                `<option value="${FigureSchema.escapeHtml(figure.figure_id)}">`
                + `${FigureSchema.escapeHtml(figure.title || "Untitled figure")}</option>`).join("");
        }
        const pick = this.part("pick");
        if (pick) pick.hidden = false;
    }

    async chooseExisting() {
        const select = this.chooser?.querySelector("#fb_destination_select");
        const figureId = select && select.value;
        if (!figureId) return;
        this.closeChooser();
        // Chooses where the next captures go, and nothing else. It used to open
        // the canvas beside the image as well, which answered a question the
        // user had not asked and took half the viewer to do it.
        await this.selectFigure(figureId);
    }

    /**
     * Leave for the Figure Canvas, once there is somewhere for the captures to go.
     *
     * A navigation, not a pane. The canvas used to open beside the image, which
     * gave the figure half a window and the slide the other half -- and neither
     * job enough room to do. Composing a figure is a different activity from
     * looking down a microscope, so it gets the whole page and its own URL, and
     * this button is the door.
     *
     * The order is the whole of the care here: unattached captures are MEMORY,
     * and this navigation ends the memory. So everything waiting is written into
     * the figure first, and a write that fails stops the navigation rather than
     * carrying the captures off the page -- the strip keeps them and says why.
     */
    async goToCanvas() {
        if (!this.figureId || !this.state || !this.state.document) {
            this.askWhereToPut();
            return;
        }
        const stored = await this.attachCaptures();
        if (!stored) {
            this.fail("These captures could not be saved, so the canvas was not opened.");
            return;
        }
        this.rememberOrigin();
        PlexoraRouter.go(this.api.figureHref(this.figureId));
    }

    /**
     * Leave a note saying the canvas was opened from HERE.
     *
     * The figure page's back arrow reads it. Without it that arrow always goes
     * to the Figures library, which is the wrong door for the commonest trip
     * there is: capture a few fields, go and look at the figure, come back to
     * the slide for one more. The tool is named in the href, so arriving back
     * finds the dock already on the image rather than a viewer with no way to
     * capture from it.
     *
     * Keyed by figure and kept in sessionStorage, so it is this tab's answer
     * about this figure and a note left over from another one is ignored.
     */
    rememberOrigin() {
        if (!this.figureId || !this.datasource) return;
        try {
            window.sessionStorage.setItem("plexora:figure-builder-origin",
                JSON.stringify({
                    figure_id: this.figureId,
                    href: window.location.pathname + "?tool=figure_builder",
                    label: this.datasource,
                }));
        } catch (error) {
            /* see readRemembered -- the navigation is still worth making */
        }
    }

    // -- actions ---------------------------------------------------------

    readRemembered() {
        try {
            return window.localStorage.getItem(FigureBuilderSidebarController.STORAGE_KEY) || null;
        } catch (error) {
            // Private-browsing modes throw rather than returning null. Losing
            // the remembered figure is a small inconvenience; throwing here
            // would take the tool down with it.
            return null;
        }
    }

    remember(figureId) {
        try {
            if (figureId) window.localStorage.setItem(FigureBuilderSidebarController.STORAGE_KEY, figureId);
            else window.localStorage.removeItem(FigureBuilderSidebarController.STORAGE_KEY);
        } catch (error) {
            /* see readRemembered */
        }
    }

    async createFigure() {
        this.setStatus("Creating…");
        const result = await this.api.createFigure("");
        if (!result.ok) {
            this.fail("Could not create a figure.");
            return null;
        }
        this.figures.unshift({
            figure_id: result.data.figure_id,
            title: result.data.document.title,
            readable: true,
            revision: result.data.document.revision,
            page_count: result.data.document.pages.length,
            panel_count: 0,
            sources: [],
            has_thumbnail: false,
        });
        await this.selectFigure(result.data.figure_id);
        return result.data.figure_id;
    }

    selectFigure(figureId) {
        this.figureId = figureId || null;
        this.remember(this.figureId);
        this.render();
        return this.figureId ? this.openFigure(this.figureId) : Promise.resolve();
    }

    async openFigure(figureId) {
        this.state = new FigureDocumentState({ api: this.api, figureId: figureId });
        this.state.on("status", (payload) => this.renderStatus(payload));
        this.state.on("change", () => this.render());
        const opened = await this.state.load();
        if (!opened) {
            this.state = null;
            this.render();
            return;
        }
        this.render();

        // Only after the document is open: the panel being asked for is in it.
        if (this.pendingPanelId) {
            const panelId = this.pendingPanelId;
            const request = this.pendingEdit;
            this.pendingPanelId = null;
            this.pendingEdit = null;
            this.remember(this.figureId);
            this.editPanel(panelId, request);
        }

        // Every route into a figure passes through here, so this is the one
        // place the waiting captures have to be written out from.
        await this.attachCaptures();
    }

    // -- editing a panel's view ------------------------------------------

    /**
     * Reopen the view a panel was captured from.
     *
     * A panel belonging to ANOTHER image navigates: main.js boots per page and
     * the server holds one loaded datasource, so "swap the image under the live
     * viewer" is not something this app can do without pretending. The request
     * is left in sessionStorage and picked up on arrival.
     *
     * A panel belonging to THIS image is restored in place, which is the case
     * that matters -- it is what makes adjusting a capture feel like adjusting
     * the viewer rather than like reloading a page.
     */
    editPanel(panelId, request) {
        const panel = this.state?.panel(panelId);
        const source = panel && this.state.source(panel.source_id);
        if (!panel || !source) return;

        if (source.kind !== "plexora_project" || !source.datasource) {
            this.fail("This panel has no project image to reopen.");
            return;
        }
        if (source.datasource !== this.datasource) {
            // Hand the request to the page that CAN show it, the same way the
            // figure canvas does: the note in sessionStorage is read once on
            // arrival (takePendingEdit). Passed on whole, so the panel's shape
            // and where the user expects to end up survive the second hop.
            try {
                window.sessionStorage.setItem("plexora:figure-builder-pending",
                    JSON.stringify({ ...(request || {}),
                                     figure_id: this.figureId, panel_id: panelId }));
            } catch (error) {
                /* Private-browsing modes throw; the navigation is still worth doing. */
            }
            PlexoraRouter.go(this.api.url(encodeURIComponent(source.datasource))
                + "?tool=figure_builder");
            return;
        }
        this.beginEdit(panelId, request);
    }

    /**
     * Load a panel's scene into the live viewer, and outline its frame.
     *
     * The viewer's CURRENT state is stashed first, because this is a temporary
     * loan and Cancel has to put the user back where they were -- not where the
     * project last saved, and not wherever the last panel they looked at was.
     *
     * Then the panel's own edges are drawn on the image, using the capture
     * frame in FRAMING mode: the same locked outline that going back to a
     * capture produces, with the shutter taken off it. Without it the user is
     * looking at a viewer showing roughly the right place and has no way to see
     * what the panel will actually contain -- which is most of what they came
     * here to decide.
     *
     * The rect is the panel's CURRENT shape (`FigureSchema.aspectViewport`),
     * not the shape it was captured at: a square capture dragged into a wide
     * strip has to be reframed as a wide strip.
     */
    async beginEdit(panelId, request) {
        const panel = this.state.panel(panelId);
        if (!panel) return;

        this.capture.disarm();
        this.editing = {
            panelId: panelId,
            // Captured, not merely remembered: the stash goes back through the
            // same restore path, so returning is exactly as faithful as
            // arriving.
            stash: FigureScene.capture(this.ctx, panel.source_id,
                                       FigureScene.currentViewport(this.ctx)),
            //: Where to go when this session ends. The canvas sends the user
            //: here and expects them back; the dock's own Edit does not.
            returnTo: (request && request.return_to) || null,
        };
        this.render();

        const report = await FigureScene.restore(this.ctx, panel.scene);
        this.editing.report = report;
        this.render();
        this.frameThePanel(panel, request);
    }

    /**
     * Put the framing outline on the panel's region.
     *
     * Armed AFTER the restore and only once the viewer has stopped moving:
     * `lockOn` refuses a region it cannot currently project as a frame, and
     * OpenSeadragon carries on settling for a while after it says it has
     * finished -- the same thing that used to make a clicked capture come back
     * unselected.
     */
    frameThePanel(panel, request) {
        const aspect = Number(request && request.aspect) || 0;
        const source = this.state.source(panel.source_id);
        const rect = FigureSchema.aspectViewport(
            panel.scene.viewport, aspect, source && source.image);

        this.boxes.centerOn(rect, () => {
            if (!this.editing) return;
            this.capture.arm();
            // A frame, not a viewfinder: a capture taken through it would be a
            // second panel of a borrowed scene.
            this.capture.setFraming(true);
            this.capture.lockOn(rect, panel.title || "this panel");
            this.renderDock();
        });
    }

    /**
     * Write what is on screen back onto the panel.
     *
     * Both halves move together: the scene AND a fresh preview at a new render
     * revision. Updating one without the other is what leaves a panel whose
     * raster shows one thing and whose export shows another.
     *
     * The region comes from the PINNED FRAME rather than from the panel's
     * stored viewport. That is the deliberate change that makes this a round
     * trip rather than a re-render: the frame follows the region while the
     * viewer moves, and the user may have dragged it somewhere else entirely --
     * which is the whole reason to open a panel in the main viewer.
     */
    async updatePanel() {
        const session = this.editing;
        const panel = session && this.state.panel(session.panelId);
        if (!panel) return;
        this.setStatus("Updating…");

        const viewport = this.capture.pinned
            ? this.capture.clamp(this.capture.pinned)
            : panel.scene.viewport;
        const scene = FigureScene.capture(this.ctx, panel.source_id, viewport);
        const renderRevision = panel.render_revision + 1;
        const changes = { scene: scene, render_revision: renderRevision };

        const stored = await this.state.commit(
            [{ op: "update_panel", panel_id: session.panelId, changes: changes }],
            (draft) => { Object.assign(draft.panels[session.panelId], changes); });
        if (!stored) return;

        const screenRect = this.capture.toScreenRect(viewport);
        const preview = screenRect ? await this.capture.previewBlob(screenRect) : null;
        if (preview) {
            await this.api.putPreview(this.figureId, session.panelId, renderRevision,
                preview.blob, { width: preview.width, height: preview.height });
            // The strip shows this session's captures, and one of them may be
            // this panel: its thumbnail is now a picture of something that has
            // been edited since.
            const shown = this.captures.find((capture) => capture.panelId === session.panelId);
            if (shown) {
                if (shown.url) URL.revokeObjectURL(shown.url);
                shown.preview = preview;
                shown.url = URL.createObjectURL(preview.blob);
            }
        }
        const returnTo = session.returnTo;
        this.endEdit();
        // Back where the user came from, and only then: a navigation before the
        // write would take them to a canvas showing the panel they just edited,
        // unedited. The note goes with them, because the way back out of the
        // canvas is now this viewer rather than the library.
        if (returnTo === "canvas") {
            this.rememberOrigin();
            PlexoraRouter.go(this.api.figureHref(this.figureId));
        }
    }

    /** Put the viewer back where it was and leave the panel alone. */
    async cancelEdit() {
        const stash = this.editing?.stash;
        const returnTo = this.editing?.returnTo;
        this.endEdit();
        if (returnTo === "canvas") {
            // Nothing was changed, so there is nothing to restore for -- the
            // page is about to go.
            this.rememberOrigin();
            PlexoraRouter.go(this.api.figureHref(this.figureId));
            return;
        }
        if (stash) await FigureScene.restore(this.ctx, stash);
    }

    endEdit() {
        this.editing = null;
        this.capture.setFraming(false);
        this.capture.unpin(true);
        this.capture.disarm();
        this.render();
    }

    /**
     * A request left by the figure page before it navigated here.
     *
     * Read once and cleared, so a reload of this page does not silently reopen
     * an edit the user has already finished with.
     */
    takePendingEdit() {
        try {
            const raw = window.sessionStorage.getItem("plexora:figure-builder-pending");
            if (!raw) return null;
            window.sessionStorage.removeItem("plexora:figure-builder-pending");
            return JSON.parse(raw);
        } catch (error) {
            return null;
        }
    }

    /**
     * Make sure this image is one of the figure's sources, and answer with it.
     *
     * Registered lazily -- on the first capture that reaches a figure, not when
     * the tool opens -- because a figure should not acquire a reference to
     * every project the user happened to look at while it was selected. Sources
     * are what the provenance page lists and what "this source has changed" is
     * checked against; a list padded with images no panel came from makes both
     * of those harder to read for no gain.
     */
    async ensureSource() {
        if (!this.state || !this.state.document) return null;
        const existing = this.state.sourceForDatasource(this.datasource);
        if (existing) return existing;

        const described = await this.api.describeSource(this.datasource);
        if (!described.ok) return null;

        const source = { ...described.data.source, source_id: FigureSchema.newSourceId() };
        // Physical pixel size is not in the project record -- it lives in the
        // OME metadata, behind a loader. Fetched here, for the datasource that
        // is on screen and therefore already loaded, so no other source's
        // status check can ever cause a load. Absent stays absent: a figure
        // with no calibration disables its scale bars rather than inventing
        // one.
        source.pixel_size = await this.readPixelSize();

        const stored = await this.state.commit(
            [{ op: "add_source", source: source }],
            (draft) => { draft.sources[source.source_id] = source; });
        return stored ? source : null;
    }

    async readPixelSize() {
        try {
            const response = await fetch(
                this.ctx.url("get_ome_metadata") + "?" + new URLSearchParams({ datasource: this.datasource }));
            if (!response.ok) return null;
            const metadata = await response.json();
            const pixels = metadata?.images?.[0]?.pixels || {};
            const value = Number(pixels.physical_size_x);
            if (!(value > 0)) return null;
            return {
                value: value,
                unit: pixels.physical_size_x_unit || "µm",
                source: "metadata",
            };
        } catch (error) {
            return null;
        }
    }

    // -- rendering -------------------------------------------------------

    setStatus(text) {
        this.statusText = text || "";
        // A banner that outlives the thing it was about is a banner people
        // learn to ignore. Anything that gets far enough to report progress has
        // superseded the last failure.
        if (text) this.failure = "";
        this.renderDock();
    }

    fail(message) {
        this.failure = message;
        this.statusText = "";
        this.renderDock();
    }

    renderStatus(payload) {
        this.setStatus({
            loading: "Opening…",
            saving: "Saving…",
            saved: "Saved",
            unsaved: "Unsaved",
            failed: payload.detail || "Save failed",
            conflict: "Changed elsewhere",
            unreadable: "Cannot be opened",
        }[payload.status] || "");
    }

    render() {
        this.renderDock();
        this.renderBoxes();
    }

    /** Everything the dock draws, in one place, from one read of the state. */
    renderDock() {
        const open = Boolean(this.figureId && this.state && this.state.document);
        this.dock.render({
            armed: this.capture.active,
            figureTitle: open ? (this.state.title || "Untitled figure") : null,
            meta: this.metaLine(open),
            error: this.failure,
            editing: this.editing ? this.editSession() : null,
            selected: this.selected,
            captures: this.captures.map((capture) => ({
                id: capture.id,
                url: capture.url,
                pending: !capture.panelId,
                caption: this.captionFor(capture),
            })),
        });
    }

    /** The boxes on the image are the captures, in the same order and with the
     *  same selection -- one list, drawn twice. */
    renderBoxes() {
        this.boxes.setBoxes(this.captures.map((capture) => ({
            id: capture.id,
            rect: capture.scene.viewport,
        })));
        this.boxes.setSelected(this.selected);
    }

    metaLine(open) {
        const parts = [];
        if (open) {
            const document_ = this.state.document;
            parts.push(FigureSchema.countPhrase(Object.keys(document_.panels).length, "panel"));
            parts.push(FigureSchema.countPhrase(document_.pages.length, "page"));
        }
        if (this.statusText) parts.push(this.statusText);
        return parts.join(" · ");
    }

    /** How wide the captured field is, in the units the source can support. */
    captionFor(capture) {
        const source = this.state && this.state.document
            ? this.state.sourceForDatasource(this.datasource)
            : null;
        const span = FigureSchema.physicalWidthUm(source, capture.scene.viewport);
        return span
            ? FigureSchema.formatMicrons(span) + " wide"
            : Math.round(capture.scene.viewport.w) + " px wide";
    }

    /**
     * What the dock says while a panel's view is on loan to the live viewer.
     *
     * The capture half of the dock is hidden for as long as it runs: capturing
     * a new view into a figure while the viewer is showing a borrowed state
     * would produce a panel of somebody else's scene, and the user would have
     * no way to tell.
     */
    editSession() {
        const panel = this.state?.panel(this.editing.panelId);
        const notes = [];
        const report = this.editing.report;
        if (report) {
            if (report.missing_channels.length) {
                notes.push("Not in this image any more: " + report.missing_channels.join(", ")
                    + ". Nothing was substituted.");
            }
            const skipped = Object.keys(report.plugins)
                .filter((name) => report.plugins[name] !== "ok");
            if (skipped.length) {
                notes.push("Open " + skipped.join(", ")
                    + " to restore that overlay; the panel keeps what was captured either way.");
            }
        }
        return { label: (panel && panel.title) || "this panel", notes: notes };
    }
}

window.Plexora.registerPlugin({
    name: "figure_builder",
    createSidebarController: (ctx) => new FigureBuilderSidebarController(ctx),
    // Figure Builder captures whatever another plugin drew; claiming the cell
    // layer would evict the plugin whose colours are the thing being captured.
    ownsCellLayer: false,
});
