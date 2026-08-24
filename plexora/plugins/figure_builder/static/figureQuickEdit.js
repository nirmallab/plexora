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
 * The compositing is the numpy in `server/render.render_panel`, which is itself
 * the transcription of `frag.glsl`. Three copies of one piece of arithmetic is
 * two too many, but the alternative is a preview that disagrees with the export
 * -- and the windows were chosen against this arithmetic, by eye.
 */
class FigureQuickEdit {

    /** The alpha every channel is drawn with. `render.CHANNEL_ALPHA`, which is
     *  frag.glsl's, which is what the user chose their windows against. */
    static get CHANNEL_ALPHA() { return 0.9; }

    /** Inset of the framing rectangle inside the mini view, in CSS pixels.
     *  What is left over is the surrounding tissue, which is how the user knows
     *  which way to drag. */
    static get FRAME_INSET() { return 28; }

    /** Widest preview written back on Done. A preview is a convenience raster,
     *  never the master -- the export re-renders from the source. */
    static get PREVIEW_WIDTH() { return 640; }

    /** How long the view has to stand still before its pixels are re-fetched. */
    static get SETTLE_MS() { return 140; }

    constructor(options) {
        this.workspace = options.workspace;
        this.api = options.api;
        this.state = options.state;
        this.figureId = options.figureId;
        this.onOpenInViewer = options.onOpenInViewer || (() => {});

        this.root = document.getElementById("fb_quickedit");
        this.canvasEl = document.getElementById("fb_quickedit_canvas");
        //: {panelId, aspect, view:{cx, cy, w}} while a session is open.
        this.session = null;
        //: channel key -> {data, width, height, box} for the region currently
        //: on screen. Cleared whenever the framing moves.
        this.planes = new Map();
        this.sidebar = null;
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
    }

    destroy() {
        this.close();
    }

    // -- opening and closing -------------------------------------------------

    /** Whether this panel can be quick-edited at all. */
    canEdit(panel) {
        const source = panel && this.state.source(panel.source_id);
        if (!source || source.kind !== "plexora_project" || !source.datasource) return false;
        return (this.state.sourceStatus[panel.source_id]?.status || "ok") !== "missing";
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
            title.textContent = panel.title
                || source.display_name || source.datasource || "Panel";
        }
        this.root.hidden = false;
        this.workspace.root?.classList.add("fb-quickedit-open");
        this.resizeCanvas();

        await this.mountChannels(panel, source);
        await this.refresh();
    }

    close() {
        if (!this.root) return;
        this.root.hidden = true;
        this.workspace.root?.classList.remove("fb-quickedit-open");
        this.session = null;
        this.planes.clear();
        this.sidebar = null;
        const list = document.getElementById("fbqe_channel_slot_list");
        if (list) list.innerHTML = "";
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
                // The byte domain the sliders work in is spread across what
                // this channel actually contains, so a 0-255 slider is 0-255
                // of something rather than of the sensor's theoretical range.
                qmin: low,
                qmax: Math.max(high, low + 1),
                // Where Auto lands: the same percentile window the viewer's
                // auto-level produces, so a channel switched on here arrives
                // looking the way it would there.
                vmin_hint: Number.isFinite(stats.p01) ? stats.p01 : low,
                vmax_hint: Number.isFinite(stats.p999) ? stats.p999 : high,
            };
        }));

        this.sidebar = new ViewerSidebar(
            { maxSelections: names.length }, names,
            this.dataLayerShim(described), new SimpleEventHandler(document.body),
            this.channelListShim(described),
            {
                root: this.root,
                idPrefix: "fbqe_",
                // See the class comment: these channels belong to a figure
                // panel, not to the project.
                persist: false,
                channelIndex: channelIndex,
            });

        // Recomposite on anything the widget reports. Only a channel APPEARING
        // needs pixels; colour and contrast are arithmetic over what is already
        // in memory, which is the whole point of fetching them separately.
        const repaint = () => this.refresh();
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
            // The window travels in RAW units, which is what a captured scene
            // stores and what the export renders from -- so it is converted
            // into whichever domain the sliders are currently in.
            slot.userRangeChanged = true;
            slot.autoLeveled = true;
            const colour = channel.color || { r: 255, g: 255, b: 255 };
            this.sidebar.setSlotColor(
                slot.index, this.sidebar.rgbToHex(colour.r, colour.g, colour.b), true);
            const packet = this.sidebar.quantWindow(name);
            const window_ = channel.window || [0, 65535];
            this.sidebar.setSlotRange(
                slot.index,
                packet ? this.sidebar.rawToByteRange(window_, packet) : window_, true);
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
            this.settle();
        });
        const stop = () => { this.drag = null; };
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
            this.settle();
        }, { passive: false });
    }

    /** Re-fetch once the view has stood still. Every frame of a drag would be a
     *  request per channel for a picture nobody has looked at yet. */
    settle() {
        window.clearTimeout(this._settleTimer);
        this._settleTimer = window.setTimeout(() => this.refresh(), FigureQuickEdit.SETTLE_MS);
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
        const out = {
            w: Math.max(1, Math.round(clamped.w / region.perPixel)),
            h: Math.max(1, Math.round(clamped.h / region.perPixel)),
        };
        const signature = [clamped.x, clamped.y, clamped.w, clamped.h, out.w, out.h]
            .map((value) => Math.round(value)).join(":");

        const wanted = this.activeSlots().map((slot) => this.session.keyByName[slot.name]);
        const fetches = wanted.filter((key) => {
            const held = this.planes.get(key);
            return !held || held.signature !== signature;
        });
        await Promise.all(fetches.map(async (key) => {
            const result = await this.api.readPixels(this.figureId, this.session.sourceId, {
                channel: key, x: clamped.x, y: clamped.y, w: clamped.w, h: clamped.h,
                out_w: out.w, out_h: out.h,
            });
            if (!result.ok) return;
            this.planes.set(key, {
                signature: signature, data: result.data,
                width: result.width, height: result.height,
                // Where these pixels belong on the canvas.
                left: (clamped.x - region.x) / region.perPixel,
                top: (clamped.y - region.y) / region.perPixel,
            });
        }));
        this.paint();
    }

    activeSlots() {
        if (!this.sidebar) return [];
        return this.sidebar.channelSlots.filter(
            (slot) => slot.enabled && slot.name && this.session.keyByName[slot.name]);
    }

    /**
     * Composite what is held and draw it.
     *
     * Same arithmetic as `server/render.render_panel`: clip each channel into
     * its window, multiply by its colour and the shared alpha, add, clip. A
     * preview that composited differently from the exporter would be a picture
     * of a figure nobody is going to get.
     */
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

        const slots = this.activeSlots();
        const held = slots
            .map((slot) => ({ slot: slot, plane: this.planes.get(this.session.keyByName[slot.name]) }))
            .filter((entry) => entry.plane);
        if (held.length) {
            const first = held[0].plane;
            const pixels = new ImageData(first.width, first.height);
            const out = pixels.data;
            for (let index = 0; index < first.width * first.height; index += 1) {
                out[index * 4 + 3] = 255;
            }
            for (const { slot, plane } of held) {
                if (plane.width !== first.width || plane.height !== first.height) continue;
                const raw = this.sidebar.toRawRangeForSlot(slot);
                const low = raw[0];
                const span = Math.max(1e-6, raw[1] - raw[0]);
                const weight = FigureQuickEdit.CHANNEL_ALPHA;
                const colour = [slot.color.r * weight, slot.color.g * weight, slot.color.b * weight];
                for (let index = 0; index < plane.data.length; index += 1) {
                    const t = Math.min(1, Math.max(0, (plane.data[index] - low) / span));
                    if (t <= 0) continue;
                    const at = index * 4;
                    out[at] = Math.min(255, out[at] + t * colour[0]);
                    out[at + 1] = Math.min(255, out[at + 1] + t * colour[1]);
                    out[at + 2] = Math.min(255, out[at + 2] + t * colour[2]);
                }
            }
            const bitmap = document.createElement("canvas");
            bitmap.width = first.width;
            bitmap.height = first.height;
            bitmap.getContext("2d").putImageData(pixels, 0, 0);
            context.drawImage(bitmap, first.left, first.top, first.width, first.height);
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

    // -- committing ------------------------------------------------------------

    /**
     * Write the reframed field and the channels back onto the panel.
     *
     * ONE commit on the workspace's own document state -- never a second
     * FigureDocumentState. Two of them on one figure in one tab would hold two
     * revisions of it and make each other stale on every save, in the same
     * window, with the user doing nothing wrong.
     */
    async commit() {
        const session = this.session;
        const panel = session && this.state.panel(session.panelId);
        if (!panel) {
            this.close();
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

        const stored = await this.state.commit(
            [{ op: "update_panel", panel_id: session.panelId, changes: changes }],
            (draft) => { Object.assign(draft.panels[session.panelId], changes); });

        if (stored) await this.uploadPreview(session.panelId, renderRevision);
        this.close();
    }

    /**
     * A fresh preview, cut out of what is on screen.
     *
     * Channels only. Overlays are not composited here, which matches what the
     * EXPORT renders -- a preview that showed a phenotype colouring the
     * exported figure will not have would be the one place in this tool where
     * the raster lies about the deliverable.
     */
    async uploadPreview(panelId, renderRevision) {
        const frame = this.frameRect();
        const dpr = this.dpr || 1;
        const out = document.createElement("canvas");
        const width = Math.min(FigureQuickEdit.PREVIEW_WIDTH, Math.round(frame.w * dpr));
        out.width = Math.max(1, width);
        out.height = Math.max(1, Math.round(width / (this.session?.aspect || 1)));
        out.getContext("2d").drawImage(
            this.canvasEl,
            frame.x * dpr, frame.y * dpr, frame.w * dpr, frame.h * dpr,
            0, 0, out.width, out.height);

        const blob = await new Promise((resolve) =>
            out.toBlob(resolve, "image/webp", 0.9));
        if (!blob) return;
        await this.api.putPreview(this.figureId, panelId, renderRevision, blob,
                                  { width: out.width, height: out.height });
    }
}
