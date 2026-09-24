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
 * ## Captures do not need a figure, and no longer wait on one
 *
 * Making the user answer "which figure?" before they have decided which
 * regions are worth keeping is a management decision demanded at the worst
 * possible moment, and the honest answer early on is "I do not know yet". This
 * file used to accept that answer by holding such captures in PAGE MEMORY: the
 * strip marked them as at risk, leaving the page asked for confirmation, and a
 * navigation really could lose them.
 *
 * Every capture now goes to the server the moment it is taken, into the
 * captures bin (figureCaptureBin.js, server/captures.py), which belongs to no
 * figure by construction. So:
 *
 *   - nothing here is asked about a figure, ever, until "Figure Canvas" is
 *     pressed -- and then it is asked once, visually, by
 *     figureDestinationPicker.js;
 *   - there is nothing left to lose by reloading, so the beforeunload guard and
 *     the "these will be lost" confirmation on Close are gone;
 *   - the strip IS the bin, filtered to the image on screen. Ticking captures
 *     is what says which of them travel; the figure they travel to is chosen
 *     at the door.
 *
 * The figure is adopted into on ARRIVAL at the canvas, not before leaving here:
 * the picker leaves a note and the figure page does the work with the document
 * already open. A failure there is harmless -- the captures are still in the
 * bin and the canvas says so -- where the old order had to write everything
 * out first and cancel the navigation if it could not.
 *
 * One consequence worth stating: a capture's outline on the image lasts as long
 * as the capture is IN THE BIN. Adopting it into a figure takes the outline
 * with it, because the bin holds what has not been assigned and the mark is
 * drawn from the bin. Captures kept back are still marked, still clickable, and
 * still there tomorrow.
 *
 * ## Every capture leaves a mark
 *
 * The region a capture came from stays outlined on the image for as long as it
 * is in the bin (figureCaptureBoxes). Selecting one -- from the strip or by clicking
 * its outline -- highlights both and puts the viewer back over that field. It
 * restores NOTHING about the rendering, on purpose: going back to a region,
 * changing the channels and capturing it again is how two panels of one field
 * under two renderings are made, and it has to be the easy thing to do.
 * Reopening a panel's whole captured scene is a different action, is reached
 * from the canvas, and says so on screen while it is running.
 *
 * Which figure was last worked on is remembered in localStorage rather than on
 * the server. It is a property of this browser and this person's train of
 * thought, not of the figure or of the project: two people (or two windows)
 * working on different figures from the same image is ordinary, and a
 * server-side "current figure" would make each of them keep switching the
 * other's back. It is now a HINT and nothing more -- the picker puts that
 * figure first and focuses it, so the round trip canvas → viewer → capture →
 * canvas is one click, and nothing is written anywhere without one.
 */
class FigureBuilderSidebarController {

    static get STORAGE_KEY() { return "plexora.figure_builder.currentFigure"; }

    /** Core's name for this plugin, which is also the handle the tool loader
     *  answers to. */
    static get TOOL() { return "figure_builder"; }

    // `panelFor` used to be here. It lives on FigureCaptureBin now: the viewer
    // is no longer the only place a capture turns into a panel -- the canvas's
    // tray dialog and the library's Captures tab do it too -- and the defaults
    // for a panel have to be in one place whichever path built it.

    constructor(ctx) {
        this.ctx = ctx;
        this.datasource = ctx.datasource;
        this.api = new FigureBuilderApi({ url: ctx.url });

        this.figures = [];
        //: A figure open in the viewer, which now happens for exactly one
        //: reason: a panel whose captured view the user asked to reopen. The
        //: capture path does not open one at all.
        this.figureId = null;
        this.state = null;
        //: The captures bin. Persistent and server-side, so the strip below is
        //: not this page's memory any more.
        this.bin = new FigureCaptureBin({ api: this.api });

        //: The bin, filtered to THIS image, newest first. Each entry is
        //: {id, datasource, scene, source, caption, url, objectUrl, checked,
        //: fresh, unsaved}. Filtered because a capture's outline is drawn in
        //: the coordinates of the image it came from, and one from another
        //: slide has nowhere to be drawn here.
        this.captures = [];
        //: The one the shutter is AIMED at, in the strip and on the image at
        //: the same time. One id, not a flag per capture, because "aimed" is a
        //: property of the session and two things render it. Emphatically not
        //: the same notion as `checked` -- see the dock's class comment.
        this.selected = null;
        //: This image as a figure source, described once and kept: every
        //: capture records it, and asking the server per capture would be a
        //: request per shutter press for an answer that cannot change while the
        //: page is open. A promise rather than a value, so a burst of captures
        //: shares one request.
        this._describing = null;
        //: The bin read currently in flight, for the same reason: core's boot
        //: lifecycle and `onShow` both ask for it.
        this._reading = null;

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
            onToggleCheck: (id) => this.toggleCheck(id),
            onCheckAll: (on) => this.checkAll(on),
            onDeleteChecked: () => this.removeChecked(),
            onOpenCanvas: () => this.goToCanvas(),
            onUpdatePanel: () => this.updatePanel(),
            onCancelEdit: () => this.cancelEdit(),
            onClose: () => this.close(),
        });

        // Through the plugin's own cleanup list so the viewer, document and
        // window listeners this tool installs go when the plugin does. Left
        // behind, a drag meant for another tool would move a viewfinder over an
        // image nobody is capturing from.
        ctx.onCleanup?.(() => this.destroy());
    }

    /** How many captures are ticked to travel to a figure next. */
    checkedCount() {
        return this.checked().length;
    }

    /** The ticked captures, oldest first -- the order they become panels in.
     *  The strip reads newest-first because that is where the eye goes; a
     *  figure is a record and records run forwards. One that the bin refused is
     *  never included: there is nothing on the server to adopt. */
    checked() {
        return this.captures.filter((capture) => capture.checked && !capture.unsaved)
            .slice().reverse();
    }

    // -- lifecycle -------------------------------------------------------

    setup() {
        // No beforeunload guard any more, and nothing to build: captures reach
        // the server as they are taken, so leaving this page costs nothing, and
        // the destination dialog is built per question by
        // FigureDestinationPicker.
        this.mount();
    }

    destroy() {
        this.capture.destroy();
        this.boxes.destroy();
        this.dock.destroy();
        // Only the ones taken in this session hold a blob URL -- everything
        // loaded from the bin is a server route, which is what makes the strip
        // survive a reload.
        for (const capture of this.captures) {
            if (capture.objectUrl && capture.url) URL.revokeObjectURL(capture.url);
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
    close() {
        // Nothing to ask. Closing used to warn that captures not yet in a
        // figure would be lost, because they were page memory; they are in the
        // bin now, and a confirmation about work that is safely stored is a
        // confirmation people learn to click through.
        //
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
        // The bin is read every time the tool is shown, not once at boot: a
        // capture may have been adopted into a figure on the canvas since, or
        // deleted from the Captures tab, and a strip showing a capture that no
        // longer exists is an outline on the image pointing at nothing.
        this.loadCaptures();
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
        // The shape is checked rather than assumed: this is now on the path
        // into the destination picker, and a malformed answer there would
        // throw where the honest outcome is a picker offering "Create new
        // figure" and nothing else.
        if (!result.ok || !Array.isArray(result.data.figures)) {
            this.fail("The figure library could not be read.");
            return null;
        }
        this.figures = result.data.figures.filter((figure) => figure.readable);
        return this.figures;
    }

    applyOrDefault() {
        // Core's boot lifecycle: setup, fetchSaved, then this. The bin is read
        // here as well as in onShow because a panel-less plugin may be brought
        // up without ever being "shown", and a strip that filled only on the
        // second visit would look like a bin that had lost everything.
        this.loadCaptures();
        this.adopt(this.takePendingEdit());
    }

    /**
     * Open a figure in the viewer -- which now happens for exactly one reason.
     *
     * A panel the canvas asked to reopen: the request is in `pending`, the
     * panel is inside that figure, and the figure has to be open to reach it.
     *
     * It used to fall back to the REMEMBERED figure and open that whenever the
     * tool came up, because captures were written into a figure as they were
     * taken and there had to be one open to write into. Nothing needs one any
     * more -- captures go to the bin -- so opening a document because the user
     * once looked at it would be a fetch, a revision to keep fresh and a save
     * status in the dock, all for a figure nobody asked for.
     */
    adopt(pending) {
        const requested = pending ? pending.figure_id : null;
        const exists = Boolean(requested)
            && this.figures.some((figure) => figure.figure_id === requested);
        this.figureId = exists ? requested : null;
        this.pendingPanelId = exists ? pending.panel_id : null;
        //: The whole request, not just the panel: it also says which SHAPE the
        //: panel is now and where the user expects to end up. Kept rather than
        //: reduced to an id, because both of those are decisions made on the
        //: canvas that this page has no other way to learn.
        this.pendingEdit = exists ? pending : null;
        this.render();
        if (this.figureId) this.openFigure(this.figureId);
    }

    /**
     * Read the bin for this image into the strip.
     *
     * Merged rather than replaced, because the two halves of the list are not
     * equally knowable: a capture taken a second ago has a decoded blob on
     * hand, a tick the user may already have cleared, and possibly a POST
     * still in flight, none of which the server's answer knows about. What
     * arrives from the server is the authority on what EXISTS; this page stays
     * the authority on what the user has done to it since.
     *
     * Captures from an earlier session arrive UNTICKED. Fifty of them all
     * ticked would mean one press of "Figure Canvas" carried the lot into a
     * figure, and would carpet the slide in outlines on the way.
     */
    loadCaptures() {
        // Coalesced, because core's boot lifecycle and `onShow` both ask and
        // they can both be in flight: two identical requests answering into the
        // same list is a wasted round trip at best, and at worst the older
        // answer landing second.
        if (!this._reading) {
            this._reading = this._readBin().finally(() => { this._reading = null; });
        }
        return this._reading;
    }

    async _readBin() {
        const rows = await this.bin.list(this.datasource);
        if (!rows) {
            this.fail("Your captures bin could not be read.");
            return;
        }
        const previous = new Map(this.captures.map((entry) => [entry.id, entry]));
        const merged = rows.map((row) => {
            const known = previous.get(row.capture_id);
            if (known) {
                known.scene = row.scene || known.scene;
                known.source = row.source || known.source;
                known.caption = row.caption || known.caption;
                // A blob this page already holds beats a refetch of the same
                // pixels; anything else takes the route.
                if (!known.objectUrl) known.url = row.url;
                known.unsaved = false;
                return known;
            }
            return {
                id: row.capture_id,
                datasource: row.datasource || this.datasource,
                scene: row.scene,
                source: row.source,
                caption: row.caption,
                url: row.url,
                objectUrl: false,
                checked: false,
                fresh: false,
                unsaved: false,
            };
        });
        // Taken in this session and not in the server's answer: its write is
        // still in flight, or it was refused. Either way this page is the only
        // thing holding it, so it stays at the top where it was.
        const seen = new Set(rows.map((row) => row.capture_id));
        const held = this.captures.filter(
            (entry) => !seen.has(entry.id) && (entry.fresh || entry.unsaved));
        this.captures = held.concat(merged);
        if (this.selected && !this.captures.some((entry) => entry.id === this.selected)) {
            this.deselect();
        }
        this.render();
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
            datasource: this.datasource,
            // No source id yet -- a capture in the bin belongs to no figure, so
            // nothing has given this image an id inside one. It is filled in by
            // FigureCaptureBin.panelFor when the capture is adopted.
            scene: FigureScene.capture(this.ctx, "", rect),
            source: null,
            caption: "",
            url: null,
            objectUrl: false,
            //: Ticked on arrival. Taking a capture and then pressing "Figure
            //: Canvas" is the common path, and making the user tick what they
            //: have just deliberately photographed would be asking the same
            //: question twice.
            checked: true,
            //: Taken in this session, which is what decides whether its
            //: outline is drawn -- see renderBoxes.
            fresh: true,
            unsaved: false,
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
            // An object URL, not the preview route: the thumbnail is then there
            // the moment the shutter closes rather than after a round trip to
            // fetch back pixels this page already has.
            capture.url = URL.createObjectURL(preview.blob);
            capture.objectUrl = true;
        }
        this.renderDock();
        await this.keep(capture, preview);
    }

    /**
     * Put a capture in the bin.
     *
     * The row first and the raster second, because the row is the record: the
     * scene is what re-renders at publication resolution years later and the
     * thumbnail is a convenience. The other order would leave a preview
     * belonging to a capture the bin has never heard of.
     *
     * The image is described once per page (see `_describing`) and stored ON
     * the capture. That is what lets a capture be adopted into a figure long
     * afterwards, from a page with no viewer and possibly with this datasource
     * no longer loaded: the dimensions, channel keys, fingerprint and pixel
     * size are all recorded at the moment of capture, which is also the only
     * moment they are certainly true.
     */
    async keep(capture, preview) {
        const source = await this.describeThisImage();
        if (source) {
            capture.source = source;
            capture.caption = this.captionFor(capture);
        }
        const stored = await this.bin.add({
            capture_id: capture.id,
            datasource: this.datasource,
            source: source || {},
            scene: capture.scene,
            caption: capture.caption || "",
        });
        if (!stored) {
            capture.unsaved = true;
            this.fail("This capture could not be saved to your captures bin. "
                + "It is still here, but it will not survive a reload.");
            return false;
        }
        capture.unsaved = false;
        if (preview) await this.bin.putPreview(capture.id, preview.blob, preview);
        this.setStatus("Kept in captures");
        return true;
    }

    /**
     * This image as a figure source, described once.
     *
     * Once, not once per capture: it is a request, and the answer cannot change
     * while the page is open. Held as the PROMISE rather than the value so that
     * a burst of six captures shares one request instead of racing six.
     */
    describeThisImage() {
        if (!this._describing) {
            this._describing = (async () => {
                const described = await this.api.describeSource(this.datasource);
                if (!described.ok) return null;
                const source = { ...described.data.source };
                // Physical pixel size is not in the project record -- it lives
                // in the OME metadata, behind a loader. Read here, for the
                // datasource that is on screen and therefore already loaded, so
                // no other source's description can ever cause a load. Absent
                // stays absent: a capture with no calibration disables its
                // scale bar rather than inventing one.
                source.pixel_size = await this.readPixelSize();
                return source;
            })().catch(() => null);
        }
        return this._describing;
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

    /** Tick or untick one capture: whether it travels to a figure next, and
     *  whether Delete acts on it. Not the same thing as aiming the shutter at
     *  it -- see the dock's class comment. */
    toggleCheck(id) {
        const capture = this.captures.find((entry) => entry.id === id);
        if (!capture) return;
        capture.checked = !capture.checked;
        this.renderDock();
        // The outlines follow: an untick takes the mark off the image unless the
        // capture was taken this session or is the one being aimed at.
        this.renderBoxes();
    }

    checkAll(on) {
        for (const capture of this.captures) capture.checked = Boolean(on);
        this.renderDock();
        this.renderBoxes();
    }

    /**
     * Drop a capture from the bin.
     *
     * Its outline goes with it -- an outline pointing at a capture nobody can
     * open is a mark with nothing behind it. Any panel already made from it is
     * untouched: the panel is a copy of the scene and lives in the figure, and
     * deleting a capture is not a way to edit a figure.
     */
    async removeCapture(id) {
        const index = this.captures.findIndex((capture) => capture.id === id);
        if (index < 0) return;
        const [capture] = this.captures.splice(index, 1);
        if (capture.objectUrl && capture.url) URL.revokeObjectURL(capture.url);
        // The lock and the highlight are one state, so a capture that is gone
        // must not leave the shutter still aimed at where it used to be.
        if (this.selected === id) {
            this.selected = null;
            this.capture.unpin(true);
        }
        this.renderDock();
        this.renderBoxes();
        if (!capture.unsaved) await this.bin.remove([capture.id]);
    }

    /**
     * Discard everything ticked, having asked once.
     *
     * One question for the batch rather than one per capture: emptying a strip
     * of twelve is one thing the user decided, and twelve confirmations is a
     * dialog people learn to dismiss without reading.
     */
    async removeChecked() {
        const doomed = this.captures.filter((capture) => capture.checked);
        if (!doomed.length) return;
        // FigureConfirm and not `window.confirm`, for the reasons in its
        // docstring. On this page it lands on <body> rather than in the
        // workspace, which is where it should be: the viewer is dark, and
        // core's tokens are the right ones to inherit here.
        const ok = await FigureConfirm.ask({
            title: doomed.length === 1
                ? "Discard this capture?" : `Discard ${doomed.length} captures?`,
            body: "The region and the rendering they recorded go from your captures "
                + "bin. Any panel already made from one is untouched.",
            confirm: "Discard",
        });
        if (!ok) return;

        for (const capture of doomed) {
            if (capture.objectUrl && capture.url) URL.revokeObjectURL(capture.url);
        }
        this.captures = this.captures.filter((capture) => !capture.checked);
        if (doomed.some((capture) => capture.id === this.selected)) {
            this.selected = null;
            this.capture.unpin(true);
        }
        this.renderDock();
        this.renderBoxes();
        // Only the ones the bin actually has. A capture it refused exists
        // nowhere but this page, and asking to delete it would be a 404 for
        // something that has already gone.
        await this.bin.remove(doomed.filter((capture) => !capture.unsaved)
            .map((capture) => capture.id));
    }

    // -- choosing where the captures go ----------------------------------

    /**
     * Leave for the Figure Canvas, having asked which figure once.
     *
     * The whole of the old flow lived here and it was in the wrong order: the
     * strip was the only copy of an unattached capture, so everything had to be
     * written into a figure BEFORE the page could change, and a write that
     * failed had to cancel the trip. The figure also had to be chosen earlier,
     * through a dropdown, because the writing happened here.
     *
     * Captures are in the bin now, so this navigation costs nothing. What
     * happens instead:
     *
     *   1. ask, visually, with thumbnails -- FigureDestinationPicker;
     *   2. leave a NOTE saying which captures should join which figure;
     *   3. go.
     *
     * The figure page reads the note on arrival and does the adoption with the
     * document already open. Nothing is written here, so nothing here can fail
     * in a way that loses a capture -- the worst case is a trip to a figure
     * with the captures still sitting in the bin, which is exactly the state
     * "From captures…" on the canvas exists for.
     */
    async goToCanvas() {
        const travelling = this.checked();
        // Refreshed rather than trusted: figures get created and deleted on
        // other pages, and a picker offering one that has been deleted is a
        // card that opens an error.
        await this.fetchSaved();
        const answer = await FigureDestinationPicker.choose({
            api: this.api,
            figures: this.figures,
            count: travelling.length,
            // A hint, not a default -- it is still a click. See readRemembered.
            preferred: this.readRemembered(),
        });
        if (!answer) return;

        let figureId = answer.figureId || null;
        if (answer.kind === "new") {
            this.setStatus("Creating…");
            const result = await this.api.createFigure("");
            if (!result.ok) {
                this.fail("Could not create a figure.");
                return;
            }
            figureId = result.data.figure_id;
        }
        if (!figureId) return;

        if (travelling.length) {
            FigureCaptureBin.leaveAdoptNote({
                figure_id: figureId,
                capture_ids: travelling.map((capture) => capture.id),
            });
        }
        this.remember(figureId);
        this.rememberOrigin(figureId);
        PlexoraRouter.go(this.api.figureHref(figureId));
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
    rememberOrigin(figureId) {
        const figure = figureId || this.figureId;
        if (!figure || !this.datasource) return;
        try {
            window.sessionStorage.setItem("plexora:figure-builder-origin",
                JSON.stringify({
                    figure_id: figure,
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

    // `createFigure` and `selectFigure` used to be here, as capture-path
    // concepts: one made a figure to capture into and the other chose which
    // one that was. Neither is a thing any more -- captures go to the bin and
    // the figure is chosen at the door, by goToCanvas, which creates one
    // through the API directly and navigates rather than opening a document
    // nobody is going to edit here.

    /**
     * Open a figure's document in the viewer, for a panel edit and nothing else.
     *
     * Reached from `adopt` alone, with a request from the canvas in hand. The
     * capture path does not come through here: a capture needs no figure, so
     * opening one in order to take pictures is a document held open, kept
     * fresh and reported on for no reason.
     */
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
            this.capture.lockOn(rect, FigureSchema.panelCaption(panel) || "this panel");
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
            // Only the figure's own preview. The strip is the captures BIN, and
            // a capture that became this panel left the bin when it was
            // adopted -- so there is no longer a thumbnail here showing the
            // same pixels to keep in step.
            await this.api.putPreview(this.figureId, session.panelId, renderRevision,
                preview.blob, { width: preview.width, height: preview.height });
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

    // `ensureSource` used to be here: it registered this image as a source of
    // the OPEN figure, lazily, on the first capture that reached one. Nothing
    // reaches a figure from this page any more, so what a capture needs is the
    // DESCRIPTION of the image -- see describeThisImage, which is stored on the
    // capture and turned into a figure source by FigureCaptureBin.adoptInto,
    // in the same batch as the panels that use it.

    async readPixelSize() {
        try {
            const response = await fetch(
                this.ctx.url("get_ome_metadata") + "?" + new URLSearchParams({ datasource: this.datasource }));
            if (!response.ok) return null;
            const metadata = await response.json();
            // `/get_ome_metadata` returns the pixels dict FLAT -- see local.py's
            // `from_xml(xml).images[0].pixels`, and core's viewer, which reads
            // `imgMetadata.physical_size_x` straight off the response. Digging
            // for `images[0].pixels` found nothing every time, so no figure has
            // ever auto-calibrated from metadata: every scale bar came from a
            // number somebody typed. Both shapes are tolerated here rather than
            // one of them assumed -- the only thing worse than a missing
            // calibration is a wrong one, and neither shape can invent a value.
            const pixels = metadata?.images?.[0]?.pixels || metadata || {};
            const value = Number(pixels.physical_size_x);
            if (!(value > 0)) return null;
            return {
                value: value,
                unit: pixels.physical_size_x_unit || "µm",
                // The endpoint now says which of the two this is: a project
                // calibrated by hand in the viewer serves its value here, and
                // reporting that as "metadata" would put a claim on the
                // provenance page that the file never made.
                source: pixels.pixel_size_source === "manual" ? "manual" : "metadata",
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
        this.dock.render({
            armed: this.capture.active,
            meta: this.metaLine(),
            error: this.failure,
            editing: this.editing ? this.editSession() : null,
            selected: this.selected,
            captures: this.captures.map((capture) => ({
                id: capture.id,
                url: capture.url,
                checked: Boolean(capture.checked),
                unsaved: Boolean(capture.unsaved),
                caption: capture.caption || this.captionFor(capture),
            })),
        });
    }

    /**
     * The outlines on the image.
     *
     * Not every capture in the bin. The bin persists, so a slide somebody has
     * worked over for a week would come back carpeted in outlines -- fifty
     * rectangles over the tissue, none of which the user asked to see today. So
     * a mark is drawn for a capture that is any of:
     *
     *   - taken in THIS session, which is the map of where you have just been;
     *   - ticked, which is what is about to become panels;
     *   - aimed at, which is what the shutter will take next.
     *
     * Everything else is still in the strip, one click from being any of the
     * three -- clicking a thumbnail flies the viewer to it and puts its outline
     * back on the image, because selecting it makes it the aimed one.
     */
    renderBoxes() {
        this.boxes.setBoxes(this.captures
            .filter((capture) => capture.fresh || capture.checked
                || capture.id === this.selected)
            .map((capture) => ({
                id: capture.id,
                rect: capture.scene.viewport,
            })));
        this.boxes.setSelected(this.selected);
    }

    /** The status line under the strip. No longer a figure's panel and page
     *  counts: no figure is open while capturing, and a count of a document
     *  nobody is editing was describing something the user could not see. */
    metaLine() {
        return this.statusText || "";
    }

    /**
     * How wide the captured field is, in the units the source can support.
     *
     * From the capture's OWN stored source rather than from an open figure's:
     * nothing is open while capturing, and the pixel size is a property of the
     * image the capture came from, which the capture records.
     */
    captionFor(capture) {
        const viewport = (capture.scene && capture.scene.viewport) || { w: 0 };
        const span = FigureSchema.physicalWidthUm(capture.source, viewport);
        return span
            ? FigureSchema.formatMicrons(span) + " wide"
            : Math.round(viewport.w ? FigureSchema.frameSize(viewport).w : 0) + " px wide";
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
        return { label: (panel && FigureSchema.panelCaption(panel)) || "this panel",
                 notes: notes };
    }
}

window.Plexora.registerPlugin({
    name: "figure_builder",
    createSidebarController: (ctx) => new FigureBuilderSidebarController(ctx),
    // Figure Builder captures whatever another plugin drew; claiming the cell
    // layer would evict the plugin whose colours are the thing being captured.
    ownsCellLayer: false,
});
