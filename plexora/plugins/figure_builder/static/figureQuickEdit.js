/**
 * FigureQuickEdit - the microscopy view behind one panel, adjusted in place.
 *
 * Select a panel, press Quick Edit, and a slide-over opens on the right with a
 * small live view of the source and the channel controls under it. Reframe,
 * turn channels on and off, change their colours and contrast, press Done. The
 * canvas never leaves the screen.
 *
 * ## It is not a second viewer, and that is the design
 *
 * There is no OpenSeadragon here, no tile stack, no overlays, no plugins. The
 * mini view exists to answer two questions -- "is this the right field?" and
 * "do these channels read?" -- and nothing else. Everything past that is what
 * "Open in Main Viewer" is for, and it opens the REAL viewer rather than a
 * cut-down copy of it. A second scientific viewer inside Figure Builder would
 * be a second set of bugs and a second thing to learn.
 *
 * ## The channel controls ARE core's
 *
 * A second `ViewerSidebar` drives the markup in the slide-over, scoped to it by
 * `{root, idPrefix: "fbqe_", persist: false}`. So the slots, the colour
 * pickers, the marker pickers, the contrast sliders and the Auto button are
 * literally the ones in the viewer's sidebar: identical behaviour, one
 * implementation, and a fix to the real one lands here too.
 *
 * `persist: false` is load-bearing. The channels on screen belong to a FIGURE
 * PANEL, not to the project, and writing them to the project's saved channel
 * list would overwrite the user's own setup with a figure's, permanently.
 *
 * ## Why the pixels are fetched a channel at a time
 *
 * Each visible channel's greyscale is fetched once per framing change and kept
 * (see `server/pixels.py`). Colour and contrast are then arithmetic over bytes
 * already in memory, so dragging a slider repaints at once and costs NO
 * network. A route that returned a composited RGB would make every tweak a
 * round trip -- which is exactly the interaction this feature exists to make
 * cheap. MiniMap works the same way.
 *
 * The compositing itself is `FigurePanelCompositor`'s, shared with the previews
 * regenerated when one panel's rendering is copied onto others. That in turn is
 * the browser copy of the numpy in `server/render.render_panel`, which is the
 * transcription of `frag.glsl`. Two copies of one piece of arithmetic is one
 * too many, but the alternative is a preview that disagrees with the export --
 * and the windows were chosen against this arithmetic, by eye.
 */
class FigureQuickEdit {

    /** Inset of the framing rectangle inside the mini view, in CSS pixels.
     *  What is left over is the surrounding tissue, which is how the user knows
     *  which way to drag. */
    static get FRAME_INSET() { return 28; }

    /** Widest preview written back on Done. A preview is a convenience raster,
     *  never the master -- the export re-renders from the source. */
    static get PREVIEW_WIDTH() { return 640; }

    /** How long the view has to stand still before its pixels are re-fetched. */
    static get SETTLE_MS() { return 140; }

    /** Shortest gap between refetches WHILE a drag is still in progress. The
     *  settle timer covers standing still; this covers a slow pan that never
     *  does, so the surround fills in as the user goes rather than staying
     *  black until they let go. */
    static get DRAG_REFRESH_MS() { return 150; }

    /** How long a change has to stand still before the panel on the figure
     *  canvas is redrawn from it. Cheap -- a crop of a canvas already painted
     *  -- but not free, and a slider drag fires this a hundred times. */
    static get LIVE_PREVIEW_MS() { return 120; }

    constructor(options) {
        this.workspace = options.workspace;
        this.api = options.api;
        this.state = options.state;
        this.figureId = options.figureId;
        this.onOpenInViewer = options.onOpenInViewer || (() => {});
        //: Told whenever the slide-over opens or closes, so the workspace can
        //: settle the strip beside the canvas. Quick Edit is a dark island on
        //: a light desk and a contextual sidebar is a light card over the
        //: artwork; both open on the same double-click, and having all four
        //: levels of chrome on screen at once -- in two colour schemes -- was
        //: nothing ranking anything.
        this.onSessionChange = options.onSessionChange || (() => {});

        this.root = document.getElementById("fb_quickedit");
        this.canvasEl = document.getElementById("fb_quickedit_canvas");
        //: {panelId, aspect, view:{cx, cy, w}} while a session is open.
        this.session = null;
        //: channel key -> {data, width, height, box} for the region currently
        //: on screen. `box` is in IMAGE pixels, not canvas ones -- see paint().
        this.planes = new Map();
        this.sidebar = null;
        //: The composited channels as one canvas, plus the image-space box it
        //: covers. Rebuilt only when the pixels or the channel settings change;
        //: a pan or a zoom just redraws it somewhere else. See composite().
        this.sheet = null;
        this.sheetDirty = false;
        //: The event bus this session's sidebar reports on. Per session, so
        //: closing one really does stop it -- SimpleEventHandler has no unbind.
        this.bus = null;
        //: Superseded fetches must not land. Incremented per refresh; a batch
        //: whose number is no longer current throws its answer away.
        this.fetchSeq = 0;
        this.fetchAbort = null;
        this.lastFetchAt = 0;
        //: True while `switchTo` is tearing one session down and building the
        //: next, so the selection change it makes on the way does not re-enter.
        this._switching = false;
    }

    setup() {
        if (!this.root) return;
        document.getElementById("fb_quickedit_done")
            ?.addEventListener("click", () => this.commit());
        document.getElementById("fb_quickedit_cancel")
            ?.addEventListener("click", () => this.close());
        document.getElementById("fb_quickedit_close")
            ?.addEventListener("click", () => this.close());
        document.getElementById("fb_quickedit_main")
            ?.addEventListener("click", () => this.openInViewer());
        this.bindView();

        // The card is a flex child of the body and the body is the window's:
        // anything that changes the window changes the mini view's size, and
        // before this it was measured once, in open(), and never again.
        const view = this.root.querySelector(".fb-quickedit-view");
        if (view && window.ResizeObserver) {
            this.resizeObserver = new ResizeObserver(() => {
                if (!this.session) return;
                this.resizeCanvas();
                this.paint();
                this.settle();
            });
            this.resizeObserver.observe(view);
        }
    }

    destroy() {
        this.resizeObserver?.disconnect();
        this.close();
    }

    // -- opening and closing -------------------------------------------------

    /**
     * Whether this panel can be quick-edited at all.
     *
     * The registry's, not a second opinion. This method HAD the right answer
     * and `FigureActions.reopenable` had a weaker one, so the surfaces that
     * offer Quick Edit -- the sidebar and the right-click menu -- enabled it on
     * panels this then refused. One predicate; this is now the caller that
     * checks whether the door it is about to walk through is open.
     */
    canEdit(panel) {
        return FigureActions.reopenable(panel, { state: this.state });
    }

    async open(panelId) {
        const panel = this.state.panel(panelId);
        if (!panel || !this.canEdit(panel)) {
            FigureConfirm.tell({
                title: "This panel has no project image to edit.",
                body: "It was captured from an image the figure no longer references.",
            });
            return;
        }
        // Whatever was open is torn down first, whether this is a reopen or a
        // follow of the selection. Without it each open leaves behind a bus
        // still bound to a dead sidebar, and every one of them repaints.
        if (this.session) this.teardownSession();
        const source = this.state.source(panel.source_id);
        const place = panel.placement;
        // The panel's CURRENT shape, not the shape it was captured at. A square
        // capture that has since been dragged into a wide rectangle must be
        // edited as a wide rectangle, or Done would put the figure back to a
        // proportion the user deliberately changed.
        const aspect = place && place.h_mm > 0 ? place.w_mm / place.h_mm
            : (panel.scene.viewport.h > 0
                ? panel.scene.viewport.w / panel.scene.viewport.h : 1);

        // The same rule the main-viewer round trip uses, from the same place:
        // two implementations of "which region does a panel of this shape
        // show" would disagree, and the disagreement would be a panel that
        // framed one thing in Quick Edit and another in the viewer.
        const framed = FigureSchema.aspectViewport(
            panel.scene.viewport, aspect, source.image);
        this.session = {
            panelId: panelId,
            sourceId: panel.source_id,
            source: source,
            aspect: aspect,
            view: {
                cx: framed.x + framed.w / 2,
                cy: framed.y + framed.h / 2,
                w: framed.w,
            },
        };
        this.planes.clear();

        const title = document.getElementById("fb_quickedit_title");
        if (title) {
            title.textContent = FigureSchema.panelCaption(panel)
                || source.display_name || source.datasource || "Panel";
        }
        this.root.hidden = false;
        this.onSessionChange(true);
        this.resizeCanvas();

        await this.mountChannels(panel, source);
        await this.refresh();
    }

    /**
     * Everything one session owns, let go of.
     *
     * Including the live preview override: the panel on the canvas is showing
     * an unsaved picture, and every way out of a session -- Done, Cancel, the
     * ×, switching panels -- comes through here, so there is one place that
     * has to remember to put it back rather than four.
     */
    teardownSession() {
        const panelId = this.session && this.session.panelId;
        this.fetchAbort?.abort();
        this.fetchAbort = null;
        this.fetchSeq += 1;
        window.clearTimeout(this._settleTimer);
        window.clearTimeout(this._previewTimer);
        this.session = null;
        this.planes.clear();
        this.sheet = null;
        this.sheetDirty = false;
        this.sidebar = null;
        this.bus = null;
        this.drag = null;
        const list = document.getElementById("fbqe_channel_slot_list");
        if (list) list.innerHTML = "";
        if (panelId) this.clearLivePreview(panelId);
    }

    close() {
        if (!this.root) return;
        // Only when there was something to close. `destroy` and `setup` both come
        // through here, and a session-change fired when no session existed made
        // the workspace re-arbitrate its sidebar strip before the figure had
        // loaded.
        const wasOpen = !this.root.hidden;
        this.teardownSession();
        this.root.hidden = true;
        if (wasOpen) this.onSessionChange(false);
    }

    /**
     * Follow the canvas selection.
     *
     * A user who has Quick Edit open and clicks another panel means to edit
     * that one; before this the slide-over went on showing the panel they had
     * left. What they have already changed is SAVED on the way -- the same
     * decision "Open in Main Viewer" makes below, for the same reason: moving
     * forward should never be the expensive way to lose work.
     *
     * Anything that is not exactly one editable panel leaves the session
     * alone. A selection of three panels, or of an arrow, is not an
     * instruction about which panel to quick-edit, and closing on it would
     * make the slide-over flicker shut every time the user drew a box.
     */
    update(ids) {
        if (!this.session || this._switching) return;
        const list = Array.from(ids || []);
        if (list.length !== 1) return;
        const panelId = list[0];
        if (panelId === this.session.panelId) return;
        const panel = this.state.panel(panelId);
        if (!panel || !this.canEdit(panel)) return;
        // Returned rather than fired and forgotten, so a caller that wants to
        // know when the switch has finished can. The workspace does not wait.
        return this.switchTo(panelId);
    }

    async switchTo(panelId) {
        this._switching = true;
        try {
            await this.commit({ keepOpen: true });
            await this.open(panelId);
        } finally {
            this._switching = false;
        }
    }

    /**
     * Hand this panel to the real viewer.
     *
     * What has been done here is COMMITTED first. Whatever the user has already
     * reframed is work, and losing it on the way to a more capable tool would
     * be the one place in this flow where moving forwards costs something --
     * and the viewer would then open on the field they had just moved away
     * from. Cancel is still how a session is thrown away.
     */
    async openInViewer() {
        const panelId = this.session && this.session.panelId;
        if (!panelId) return;
        await this.commit();
        this.onOpenInViewer(panelId);
    }

    // -- core's channel widget, mounted here ---------------------------------

    /**
     * Build the second ViewerSidebar, and everything it needs to talk to.
     *
     * Stats for every channel are fetched UP FRONT and handed to `init` as its
     * database description. Core's own ChannelList fetches them lazily and
     * draws d3 curves into `#channel_list`, an element this page does not have;
     * pre-fetching instead is one round trip, needs no element, and leaves the
     * sidebar's arithmetic -- byte/raw conversion, Auto's hints, the slider
     * domain -- reading exactly the fields it expects.
     */
    async mountChannels(panel, source) {
        const channels = (source.channels || []).filter((channel) => channel.key);
        const names = [];
        const keyByName = {};
        const channelIndex = {};
        channels.forEach((channel, index) => {
            let name = channel.fullname_at_capture || channel.key;
            // Two channels with one display name would make the marker picker
            // ambiguous, so the second falls back to its stable key.
            if (keyByName[name] !== undefined) name = channel.key;
            names.push(name);
            keyByName[name] = channel.key;
            channelIndex[name] = index;
        });
        this.session.keyByName = keyByName;

        const described = {};
        await Promise.all(names.map(async (name) => {
            const result = await this.api.pixelInfo(
                this.figureId, this.session.sourceId, keyByName[name]);
            const stats = (result.ok && result.data.stats) || {};
            const low = Number.isFinite(stats.min) ? stats.min : 0;
            const high = Number.isFinite(stats.max) ? stats.max : 65535;
            described[name] = {
                image_min: low,
                image_max: Math.max(high, low + 1),
                // The slider's domain, and it is this channel's own content
                // rather than the sensor's theoretical range: a 16-bit slider
                // spread over [0, 65535] for a channel whose brightest pixel
                // is 900 would put every usable setting in its first 1.4%.
                qmin: low,
                qmax: Math.max(high, low + 1),
                // Where Auto lands: the same percentile window the viewer's
                // auto-level produces, so a channel switched on here arrives
                // looking the way it would there.
                vmin_hint: Number.isFinite(stats.p01) ? stats.p01 : low,
                vmax_hint: Number.isFinite(stats.p999) ? stats.p999 : high,
            };
        }));

        // A bus of this session's own. `SimpleEventHandler` has no unbind, so a
        // handler bound to document.body would outlive every close and fire
        // into the dead session the next time any Quick Edit repainted. A
        // detached element is unreachable from anywhere else and dies with the
        // session that made it.
        this.bus = document.createElement("div");

        this.sidebar = new ViewerSidebar(
            { maxSelections: names.length }, names,
            this.dataLayerShim(described), new SimpleEventHandler(this.bus),
            this.channelListShim(described),
            {
                root: this.root,
                idPrefix: "fbqe_",
                // See the class comment: these channels belong to a figure
                // panel, not to the project.
                persist: false,
                channelIndex: channelIndex,
                // Always HD here, whatever the viewer is doing. The pixels this
                // view composites are full-precision uint16 straight out of the
                // source (server/pixels.py) -- there are no quantized WebP tiles
                // in Quick Edit for a byte-domain slider to match, so the byte
                // domain would only be 256 steps thrown across a 16-bit window,
                // and every seed/commit round trip would quantize the panel's
                // stored window a little further. Also: `isHdMode` reads the OSD
                // viewer manager, which does not exist on this page at all, so
                // without this the answer is permanently "no".
                hdMode: true,
            });

        // Recomposite on anything the widget reports. Only a channel APPEARING
        // needs pixels; colour and contrast are arithmetic over what is already
        // in memory, which is the whole point of fetching them separately --
        // so the repaint is synchronous and the refresh, which usually has
        // nothing to fetch, follows behind it.
        const repaint = () => {
            this.sheetDirty = true;
            this.paint();
            this.scheduleLivePreview();
            this.refresh();
        };
        for (const event of [ChannelList.events.CHANNELS_CHANGE,
                             ChannelList.events.COLOR_TRANSFER_CHANGE,
                             ChannelList.events.BRUSH_MOVE]) {
            this.sidebar.eventHandler.bind(event, repaint);
        }

        await this.sidebar.init(described);
        this.seedFromScene(panel, names, keyByName);
    }

    /**
     * Put the panel's captured channels into the widget.
     *
     * Through the ordinary setters rather than by writing slots directly, so a
     * seeded slot is indistinguishable from one the user set up by hand -- and
     * so any behaviour the setters gain in future applies here too.
     */
    seedFromScene(panel, names, keyByName) {
        const captured = (panel.scene.channels || []).filter((channel) => channel.visible !== false);
        const nameFor = {};
        for (const name of names) nameFor[keyByName[name]] = name;

        captured.forEach((channel, index) => {
            const name = nameFor[channel.key];
            const slot = this.sidebar.channelSlots[index];
            if (!name || !slot) return;
            this.sidebar.setSlotMarker(slot.index, name,
                                       { keepColor: true, enable: true, force: true });
            slot.userRangeChanged = true;
            slot.autoLeveled = true;
            const colour = channel.color || { r: 255, g: 255, b: 255 };
            this.sidebar.setSlotColor(
                slot.index, this.sidebar.rgbToHex(colour.r, colour.g, colour.b), true);
            // Straight in, no conversion: this sidebar is pinned to HD, so its
            // sliders ARE in raw 16-bit units -- the same units a captured
            // scene stores and the export renders from. The byte round trip
            // this used to make was lossy in both directions, so opening Quick
            // Edit and pressing Done without touching anything could move a
            // window by a couple of hundred raw levels.
            this.sidebar.setSlotRange(slot.index, channel.window || [0, 65535], true);
        });
        // Anything the panel did not have is switched off, so the mini view
        // starts as a picture of the panel rather than of the image.
        this.sidebar.channelSlots.slice(captured.length).forEach((slot) => {
            if (slot.enabled) this.sidebar.setSlotEnabled(slot.index, false);
        });
    }

    /**
     * The small part of core's DataLayer the sidebar actually uses.
     *
     * Names are already full names here -- there is no marker/channel
     * distinction in a figure source -- so `getFullChannelName` is the
     * identity, and every saved-list call is a no-op for the reason in the
     * class comment.
     */
    dataLayerShim(described) {
        return {
            imageBitRange: [0, 65535],
            getFullChannelName: (name) => name,
            getSavedChannelList: () => Promise.resolve([]),
            saveChannelList: () => Promise.resolve(null),
            getImageChannelStats: (name) => Promise.resolve(described[name] || {}),
        };
    }

    /**
     * The bookkeeping half of core's ChannelList, without the widget half.
     *
     * The real one draws a d3 distribution curve into `#channel_list` and runs
     * a server-side Gaussian fit for the Auto button. Neither is reachable
     * here: that element belongs to the viewer page, and the fit is a
     * datasource-level endpoint of exactly the kind Quick Edit is built to
     * avoid. What the SIDEBAR uses of it is a handful of dictionaries and two
     * async hooks, and those are what this provides -- so `Auto` lands on the
     * percentile window from `pixel_info` (the sidebar's first pass) and stops
     * there rather than waiting for a fit that will never come.
     */
    channelListShim(described) {
        return {
            selections: [],
            sel: {},
            image_channels: {},
            rangeConnector: {},
            colorConnector: {},
            hasChannelGMM: {},
            databaseDescription: described,
            ensureChannelStats: () => Promise.resolve(),
            getAndDrawChannelGMM: () => Promise.resolve(null),
        };
    }

    // -- the mini view -------------------------------------------------------

    bindView() {
        const canvas = this.canvasEl;
        if (!canvas) return;

        canvas.addEventListener("pointerdown", (event) => {
            if (!this.session || event.button !== 0) return;
            canvas.setPointerCapture?.(event.pointerId);
            this.drag = { x: event.clientX, y: event.clientY,
                          cx: this.session.view.cx, cy: this.session.view.cy };
        });
        canvas.addEventListener("pointermove", (event) => {
            if (!this.drag || !this.session) return;
            const perPixel = this.session.view.w / this.frameRect().w;
            this.session.view.cx = this.drag.cx - (event.clientX - this.drag.x) * perPixel;
            this.session.view.cy = this.drag.cy - (event.clientY - this.drag.y) * perPixel;
            this.paint();
            this.scheduleRefresh();
        });
        const stop = () => {
            if (!this.drag) return;
            this.drag = null;
            // At once rather than after the settle: the user has finished, and
            // the sharp version of what they chose is what they are waiting to
            // see. Anything already in flight for a framing they have moved
            // past is abandoned by refresh() itself.
            if (this.session) this.refresh();
        };
        canvas.addEventListener("pointerup", stop);
        canvas.addEventListener("pointercancel", stop);

        canvas.addEventListener("wheel", (event) => {
            if (!this.session) return;
            event.preventDefault();
            const factor = Math.exp(event.deltaY * 0.0015);
            const source = this.session.source.image || {};
            const widest = Math.max(source.width || 1, source.height || 1);
            this.session.view.w = Math.max(16, Math.min(widest * 2,
                                                        this.session.view.w * factor));
            this.paint();
            this.scheduleRefresh();
        }, { passive: false });
    }

    /** Re-fetch once the view has stood still. Every frame of a drag would be a
     *  request per channel for a picture nobody has looked at yet. */
    settle() {
        window.clearTimeout(this._settleTimer);
        this._settleTimer = window.setTimeout(() => this.refresh(), FigureQuickEdit.SETTLE_MS);
    }

    /**
     * What a pan or a zoom asks for: a sharp version, soon.
     *
     * Two timers rather than one. `settle` is the trailing edge -- the user has
     * stopped, fetch what they are looking at. The leading-edge one is for the
     * drag that never stops: painting reprojects the pixels already held, so
     * the picture keeps up, but past the edge of what was fetched there is
     * nothing to reproject and a long slow pan would be a black surround until
     * the user let go.
     */
    scheduleRefresh() {
        this.settle();
        const now = Date.now();
        if (now - this.lastFetchAt >= FigureQuickEdit.DRAG_REFRESH_MS) this.refresh();
    }

    resizeCanvas() {
        const canvas = this.canvasEl;
        if (!canvas) return;
        const box = canvas.parentElement.getBoundingClientRect();
        const dpr = window.devicePixelRatio || 1;
        canvas.style.width = Math.round(box.width) + "px";
        canvas.style.height = Math.round(box.height) + "px";
        canvas.width = Math.max(1, Math.round(box.width * dpr));
        canvas.height = Math.max(1, Math.round(box.height * dpr));
        this.dpr = dpr;
    }

    /** Where the panel's own frame sits on the mini view, in CSS pixels. */
    frameRect() {
        const canvas = this.canvasEl;
        const width = canvas.width / (this.dpr || 1);
        const height = canvas.height / (this.dpr || 1);
        const inset = FigureQuickEdit.FRAME_INSET;
        const aspect = this.session ? this.session.aspect : 1;
        let w = width - inset * 2;
        let h = w / aspect;
        if (h > height - inset * 2) {
            h = height - inset * 2;
            w = h * aspect;
        }
        return { x: (width - w) / 2, y: (height - h) / 2, w: w, h: h };
    }

    /** The image region the whole mini view covers, in full-resolution pixels. */
    viewRegion() {
        const canvas = this.canvasEl;
        const frame = this.frameRect();
        const perPixel = this.session.view.w / frame.w;
        const width = canvas.width / (this.dpr || 1);
        const height = canvas.height / (this.dpr || 1);
        return {
            x: this.session.view.cx - (width / 2) * perPixel,
            y: this.session.view.cy - (height / 2) * perPixel,
            w: width * perPixel,
            h: height * perPixel,
            perPixel: perPixel,
        };
    }

    /** Fetch whatever the current framing needs, then repaint. */
    async refresh() {
        if (!this.session || !this.sidebar) return;
        const region = this.viewRegion();
        const image = this.session.source.image || {};
        // Clamped on THIS side rather than relying on the server's clipping: a
        // clipped response resampled to the requested size would stretch the
        // part that exists across the whole canvas, which looks like a zoom
        // nobody asked for.
        const clamped = {
            x: Math.max(0, region.x),
            y: Math.max(0, region.y),
        };
        clamped.w = Math.min(image.width || region.w, region.x + region.w) - clamped.x;
        clamped.h = Math.min(image.height || region.h, region.y + region.h) - clamped.y;
        if (!(clamped.w > 0) || !(clamped.h > 0)) {
            this.paint();
            return;
        }
        // Asked for at the DISPLAY's resolution, not the layout's. On a 2x
        // screen a 400-CSS-pixel view drawn from a 400-pixel fetch is a
        // half-resolution picture being judged for contrast and focus. Capped
        // at what one read returns (server MAX_OUT_PIXELS, per side).
        const dpr = this.dpr || 1;
        const out = {
            w: Math.max(1, Math.min(1024, Math.round(clamped.w / region.perPixel * dpr))),
            h: Math.max(1, Math.min(1024, Math.round(clamped.h / region.perPixel * dpr))),
        };
        const signature = [clamped.x, clamped.y, clamped.w, clamped.h, out.w, out.h]
            .map((value) => Math.round(value)).join(":");

        const wanted = this.activeSlots().map((slot) => this.session.keyByName[slot.name]);
        const fetches = wanted.filter((key) => {
            const held = this.planes.get(key);
            return !held || held.signature !== signature;
        });
        if (!fetches.length) {
            this.paint();
            return;
        }

        // One batch at a time. A pan fires these faster than they return, and
        // without this the server works through a queue of framings the user
        // has already left -- each one arriving to overwrite the planes with
        // an older picture than the one on screen.
        this.fetchAbort?.abort();
        const controller = new AbortController();
        this.fetchAbort = controller;
        const seq = (this.fetchSeq += 1);
        this.lastFetchAt = Date.now();

        const results = await Promise.all(fetches.map(async (key) => ({
            key: key,
            result: await this.api.readPixels(this.figureId, this.session.sourceId, {
                channel: key, x: clamped.x, y: clamped.y, w: clamped.w, h: clamped.h,
                out_w: out.w, out_h: out.h, signal: controller.signal,
            }),
        })));
        if (seq !== this.fetchSeq || !this.session) return;
        // All of them or none: a batch where one channel landed and another was
        // aborted would composite this framing's red over the last one's blue.
        if (results.some(({ result }) => !result.ok)) return;

        for (const { key, result } of results) {
            this.planes.set(key, {
                signature: signature, data: result.data,
                width: result.width, height: result.height,
                // Where these pixels are in the IMAGE. Not where they are on
                // the canvas: the canvas is a view of the image that moves,
                // and pixels that remembered a position on it would be stuck
                // wherever the view happened to be when they were fetched --
                // which is exactly what made panning feel dead until the
                // fetch landed. paint() projects this box through the CURRENT
                // view every frame.
                box: { x: clamped.x, y: clamped.y, w: clamped.w, h: clamped.h },
            });
        }
        this.sheetDirty = true;
        this.paint();
        this.scheduleLivePreview();
    }

    activeSlots() {
        if (!this.sidebar) return [];
        return this.sidebar.channelSlots.filter(
            (slot) => slot.enabled && slot.name && this.session.keyByName[slot.name]);
    }

    /**
     * Composite what is held into one canvas, once.
     *
     * Same arithmetic as `server/render.render_panel`: clip each channel into
     * its window, multiply by its colour and the shared alpha, add, clip. A
     * preview that composited differently from the exporter would be a picture
     * of a figure nobody is going to get.
     *
     * Split out from `paint` because it is the expensive half -- a loop over
     * every pixel of every channel -- and it depends on the PIXELS and the
     * CHANNEL SETTINGS, neither of which a pan or a zoom changes. Dragging the
     * view used to redo all of it per frame for a picture that had merely
     * moved.
     */
    composite() {
        this.sheetDirty = false;
        this.sheet = null;
        if (!this.session || !this.sidebar) return;

        const held = this.activeSlots()
            .map((slot) => ({ slot: slot, plane: this.planes.get(this.session.keyByName[slot.name]) }))
            .filter((entry) => entry.plane);
        if (!held.length) return;

        // The arithmetic itself is FigurePanelCompositor's, which is also what
        // regenerates a panel's preview after its rendering is copied onto it.
        // Two pictures of the same channels made by two loops would agree until
        // one of them was fixed.
        const bitmap = FigurePanelCompositor.composite(held.map(({ slot, plane }) => ({
            data: plane.data, width: plane.width, height: plane.height,
            window: this.sidebar.toRawRangeForSlot(slot),
            color: slot.color,
        })));
        if (bitmap) this.sheet = { canvas: bitmap, box: held[0].plane.box };
    }

    /**
     * Where a box of image pixels lands on the mini view, in CSS pixels.
     *
     * The whole of the pan fix, in four divisions: the pixels are fixed to the
     * IMAGE and the view moves over them, so every frame of a drag is the same
     * composited sheet drawn somewhere slightly different. Pure and static so
     * the arithmetic can be tested without a canvas.
     */
    static projectBox(box, region) {
        return {
            x: (box.x - region.x) / region.perPixel,
            y: (box.y - region.y) / region.perPixel,
            w: box.w / region.perPixel,
            h: box.h / region.perPixel,
        };
    }

    /** Draw the composited sheet where the current view puts it, and frame it. */
    paint() {
        const canvas = this.canvasEl;
        const context = canvas && canvas.getContext("2d");
        if (!context || !this.session) return;
        const dpr = this.dpr || 1;
        context.setTransform(dpr, 0, 0, dpr, 0, 0);
        const width = canvas.width / dpr;
        const height = canvas.height / dpr;
        context.clearRect(0, 0, width, height);
        context.fillStyle = "#000000";
        context.fillRect(0, 0, width, height);

        if (this.sheetDirty) this.composite();
        if (this.sheet) {
            const at = FigureQuickEdit.projectBox(this.sheet.box, this.viewRegion());
            context.drawImage(this.sheet.canvas, at.x, at.y, at.w, at.h);
        }

        // Everything outside the frame is dimmed rather than hidden: it is the
        // surrounding tissue, and it is how the user knows which way to drag.
        const frame = this.frameRect();
        context.fillStyle = "rgba(0, 0, 0, 0.55)";
        context.fillRect(0, 0, width, frame.y);
        context.fillRect(0, frame.y + frame.h, width, height - frame.y - frame.h);
        context.fillRect(0, frame.y, frame.x, frame.h);
        context.fillRect(frame.x + frame.w, frame.y, width - frame.x - frame.w, frame.h);
        context.strokeStyle = "rgba(120, 190, 255, 0.95)";
        context.lineWidth = 1;
        context.strokeRect(frame.x + 0.5, frame.y + 0.5, frame.w - 1, frame.h - 1);
    }

    // -- the panel on the canvas, live -----------------------------------------

    /**
     * What the frame is showing, as its own canvas.
     *
     * The crop `uploadPreview` writes on Done and the picture the live preview
     * puts on the panel are the same crop of the same canvas -- so it is cut
     * once, here, and the two callers differ only in what they do with it.
     */
    frameBitmap(maxWidth) {
        const frame = this.frameRect();
        const dpr = this.dpr || 1;
        const out = document.createElement("canvas");
        const width = Math.min(maxWidth, Math.round(frame.w * dpr));
        out.width = Math.max(1, width);
        out.height = Math.max(1, Math.round(width / (this.session?.aspect || 1)));
        out.getContext("2d").drawImage(
            this.canvasEl,
            frame.x * dpr, frame.y * dpr, frame.w * dpr, frame.h * dpr,
            0, 0, out.width, out.height);
        return out;
    }

    /**
     * Show the change on the figure, without saving anything.
     *
     * The user is looking at two pictures of one panel -- the mini view and the
     * panel itself -- and before this the second one only caught up on Done. So
     * the canvas gets an OVERRIDE: a data URL that `FigureCanvas.panelImageUrl`
     * prefers over the stored preview, held only while this session is open.
     *
     * Deliberately not a commit. Writing the panel on every slider move would
     * be a document revision and a preview upload per frame of a drag, an undo
     * history nobody can use, and a network round trip standing between the
     * user and the next frame. Done is still what saves; Cancel still throws
     * the whole session away, and the override goes with it.
     */
    scheduleLivePreview() {
        window.clearTimeout(this._previewTimer);
        this._previewTimer = window.setTimeout(
            () => this.syncLivePreview(), FigureQuickEdit.LIVE_PREVIEW_MS);
    }

    syncLivePreview() {
        const canvas = this.workspace?.canvas;
        if (!this.session || !canvas) return;
        const panelId = this.session.panelId;
        const url = this.frameBitmap(FigureQuickEdit.PREVIEW_WIDTH).toDataURL("image/webp", 0.9);
        canvas.previewOverrides.set(panelId, url);
        // Straight onto the element that is already there. A full re-render
        // would rebuild every panel of the page and lose the selection
        // handles, mid-drag, for a picture that has merely changed.
        const image = canvas.surfaceEl?.querySelector(
            `.fb-panel[data-panel-id="${panelId}"] .fb-panel-image`);
        if (image) image.src = url;
    }

    /** Put the panel back to what is actually saved for it. */
    clearLivePreview(panelId) {
        const canvas = this.workspace?.canvas;
        if (!canvas || !canvas.previewOverrides.has(panelId)) return;
        canvas.previewOverrides.delete(panelId);
        canvas.render();
    }

    // -- committing ------------------------------------------------------------

    /**
     * Write the reframed field and the channels back onto the panel.
     *
     * ONE commit on the workspace's own document state -- never a second
     * FigureDocumentState. Two of them on one figure in one tab would hold two
     * revisions of it and make each other stale on every save, in the same
     * window, with the user doing nothing wrong.
     *
     * `keepOpen` is for the session that is being replaced rather than ended --
     * see `switchTo`. The panel is saved either way.
     */
    async commit(options) {
        const session = this.session;
        const panel = session && this.state.panel(session.panelId);
        if (!panel) {
            if (!(options && options.keepOpen)) this.close();
            return;
        }
        const height = session.view.w / session.aspect;
        const scene = {
            ...JSON.parse(JSON.stringify(panel.scene)),
            viewport: {
                x: Math.round(session.view.cx - session.view.w / 2),
                y: Math.round(session.view.cy - height / 2),
                w: Math.round(session.view.w),
                h: Math.round(height),
            },
            channels: this.activeSlots().map((slot) => {
                const raw = this.sidebar.toRawRangeForSlot(slot);
                return {
                    key: session.keyByName[slot.name],
                    fullname_at_capture: slot.name,
                    color: { ...slot.color },
                    window: [raw[0], raw[1]],
                    visible: true,
                };
            }),
            captured_at: new Date().toISOString(),
        };
        const renderRevision = panel.render_revision + 1;
        const changes = { scene: scene, render_revision: renderRevision };

        // Cut BEFORE the commit, and kept in front of the panel until the
        // upload lands. The commit re-renders the canvas, which asks for the
        // preview at the new revision -- a URL the server does not have a
        // picture for yet -- so without the override the panel blinks to the
        // old raster, or to nothing, for as long as the upload takes.
        const bitmap = this.frameBitmap(FigureQuickEdit.PREVIEW_WIDTH);
        const canvas = this.workspace?.canvas;
        canvas?.previewOverrides.set(session.panelId, bitmap.toDataURL("image/webp", 0.9));

        const stored = await this.state.commit(
            [{ op: "update_panel", panel_id: session.panelId, changes: changes }],
            (draft) => { Object.assign(draft.panels[session.panelId], changes); });

        if (stored) await this.uploadPreview(bitmap, session.panelId, renderRevision);
        if (options && options.keepOpen) {
            this.teardownSession();
        } else {
            this.close();
        }
    }

    /**
     * A fresh preview, from the crop `frameBitmap` cut.
     *
     * Channels only. Overlays are not composited here, which matches what the
     * EXPORT renders -- a preview that showed a phenotype colouring the
     * exported figure will not have would be the one place in this tool where
     * the raster lies about the deliverable.
     */
    async uploadPreview(bitmap, panelId, renderRevision) {
        const blob = await new Promise((resolve) =>
            bitmap.toBlob(resolve, "image/webp", 0.9));
        if (!blob) return;
        await this.api.putPreview(this.figureId, panelId, renderRevision, blob,
                                  { width: bitmap.width, height: bitmap.height });
    }
}
