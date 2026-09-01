/**
 * miniMap.js -- the viewer's overview lens.
 *
 * A subtle circular button in the bottom-left corner of the viewer that
 * expands into a circular overview of the whole tissue, drawn from the same
 * channels, colours and contrast ranges as the main view, with a rectangle
 * showing what is currently on screen. Dragging the rectangle pans; clicking
 * elsewhere on the map recentres; the wheel zooms.
 *
 * The whole design is shaped by one constraint: the main viewer's frame budget
 * is ~8.3 ms at 15 channels and SKILL.md records three plausible-looking ideas
 * that were built, measured and reverted for making frames worse. So:
 *
 *   - Nothing here runs per frame except _syncIndicator(), which reads one
 *     rectangle and writes four style properties on one element.
 *   - Nothing here runs AT ALL until the user first opens the lens: the
 *     constructor builds DOM and registers guarded handlers, and that is it.
 *   - Colour and contrast changes recomposite from a cached greyscale array.
 *     Only a channel coming on costs a request.
 *
 * The pixels come from GET /generated/overview/<datasource>/<channel>, which
 * serves the server's already-resident downsampled array quantized through the
 * same window as the tiles -- see data_model.generate_channel_overview for why
 * no tile level is a usable substitute.
 */
class MiniMap {
    /** Per-channel alpha the fragment shader emits (frag.glsl's
     *  `u8_r_range(0.9)`), which canvas `lighter` then multiplies in. Matching
     *  it is what makes the lens read at the same brightness as the viewer. */
    static TILE_ALPHA = 0.9;

    /** Overview requests in flight at once. The server serialises all image
     *  I/O anyway (SKILL.md: zarr 3 funnels reads through one event-loop
     *  thread), so a wider fan-out only competes with tile requests for the
     *  connection pool while the user is panning. */
    static FETCH_CONCURRENCY = 3;

    /** A ring with a viewport box inside it -- the same two things the
     *  expanded map shows, which is the whole hint the collapsed state gets. */
    static LENS_ICON =
        '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">' +
        '<circle cx="12" cy="12" r="8.4" fill="none" stroke="currentColor" stroke-width="1.5"/>' +
        '<rect x="8.4" y="9.3" width="7.2" height="5.4" rx="1.1" fill="none" stroke="currentColor" stroke-width="1.4"/>' +
        "</svg>";

    constructor(imageViewer) {
        this.imageViewer = imageViewer;
        this.viewer = imageViewer.viewer;
        this.config = imageViewer.config;

        this.expanded = false;
        this.geom = null;

        /** srcIdx -> { data: Uint8Array, width, height }. Survives a collapse,
         *  so reopening the lens is instant and costs no requests. */
        this._gray = new Map();
        this._pending = new Set();
        /** srcIdx -> HTTP status of its last failed fetch. Read by _updateNote
         *  to tell a missing route from a transient error. */
        this._failed = new Map();
        /** The rebuild currently in flight, if any. Exposed so a caller --
         *  or a test -- can wait for the map to settle. */
        this._loading = null;
        this._drag = null;
        this._scratch = null;
        this._accum = null;
        this._image = null;

        if (!this._buildDom()) {
            return;
        }
        this._bindViewer();
        this._bindPointer();
    }

    // -- construction ----------------------------------------------------

    /**
     * Builds the collapsed lens plus the (hidden) map, as a sibling of
     * #openseadragon inside #openseadragon_wrapper -- the same host and the
     * same shape as initProjectLabel()/initLegend(). Being a sibling rather
     * than a child matters: OSD binds its mouse tracker to #openseadragon, so
     * pointer events here never reach it and nothing needs stopPropagation.
     *
     * The canvas element exists from the start but carries no backing store
     * until _layout() sizes it, so a viewer whose lens is never opened holds
     * a few empty nodes and no pixels.
     */
    _buildDom() {
        const wrapper = document.getElementById("openseadragon_wrapper");
        if (!wrapper || document.getElementById("viewer_mini_map")) {
            return false;
        }

        const root = document.createElement("div");
        root.id = "viewer_mini_map";
        root.className = "viewer-mini-map";

        const stage = document.createElement("div");
        stage.className = "viewer-mini-map-stage";

        const canvas = document.createElement("canvas");
        canvas.className = "viewer-mini-map-canvas";
        canvas.width = 0;
        canvas.height = 0;

        const indicator = document.createElement("div");
        indicator.className = "viewer-mini-map-indicator";

        // Empty until something goes wrong. A black circle is indistinguishable
        // from dark tissue, so a failed overview has to say so somewhere the
        // user actually looks -- a console.warn is not that place.
        const note = document.createElement("div");
        note.className = "viewer-mini-map-note";

        const lens = document.createElement("button");
        lens.type = "button";
        lens.className = "viewer-mini-map-lens";
        lens.setAttribute("aria-expanded", "false");
        MiniMap._label(lens, false);
        lens.innerHTML = MiniMap.LENS_ICON;
        lens.addEventListener("click", () => this.toggle());

        stage.appendChild(canvas);
        stage.appendChild(indicator);
        stage.appendChild(note);
        root.appendChild(stage);
        root.appendChild(lens);
        wrapper.appendChild(root);

        this.root = root;
        this.stage = stage;
        this.canvas = canvas;
        this.indicator = indicator;
        this.note = note;
        this.lens = lens;
        return true;
    }

    /**
     * Viewer subscriptions, registered ONCE and guarded on `expanded` rather
     * than added on expand and removed on collapse.
     *
     * That is deliberate and load-bearing. OSD's removeHandler needs the
     * identical function reference, so an add/remove pair written with inline
     * arrows silently leaks one closure per expand/collapse cycle, each of
     * them calling getBounds() on every animation frame for the rest of the
     * session -- which is exactly the per-frame cost this feature exists to
     * avoid. A guarded call that returns immediately is unmeasurable by
     * comparison.
     */
    _bindViewer() {
        this._onAnimation = () => {
            if (this.expanded) {
                this._syncIndicator();
            }
        };
        this._onResize = () => {
            if (this.expanded && this._layout()) {
                this._draw();
                this._syncIndicator();
            }
        };
        this.viewer.addHandler("animation", this._onAnimation);
        this.viewer.addHandler("animation-finish", this._onAnimation);
        this.viewer.addHandler("resize", this._onResize);

        // Flipping HD moves every slot's range between the byte and raw
        // domains (viewerSidebar.onHdModeChanged), so the cached greyscale is
        // still good but every LUT built from a range is not.
        window.addEventListener("plexora:hd-mode-changed", () => this.invalidate());
    }

    _bindPointer() {
        this.stage.addEventListener("pointerdown", (e) => this._onPointerDown(e));
        this.stage.addEventListener("pointermove", (e) => this._onPointerMove(e));
        this.stage.addEventListener("pointerup", (e) => this._onPointerUp(e));
        this.stage.addEventListener("pointercancel", (e) => this._onPointerUp(e));
        // Not passive: this wheel drives the viewer's zoom, so the page must
        // not also scroll.
        this.stage.addEventListener("wheel", (e) => this._onWheel(e), { passive: false });
    }

    // -- open / close ----------------------------------------------------

    toggle() {
        if (this.expanded) {
            this.collapse();
        } else {
            this.expand();
        }
    }

    expand() {
        if (this.expanded || !this.root) {
            return;
        }
        this.expanded = true;
        this.root.classList.add("is-expanded");
        this.lens.setAttribute("aria-expanded", "true");
        MiniMap._label(this.lens, true);
        if (this._layout()) {
            this._syncIndicator();
            this._loading = this._ensureData();
        }
    }

    collapse() {
        if (!this.expanded || !this.root) {
            return;
        }
        this.expanded = false;
        this.root.classList.remove("is-expanded");
        this.lens.setAttribute("aria-expanded", "false");
        MiniMap._label(this.lens, false);
        this._drag = null;
        // Requests already in flight are deliberately left to finish and fill
        // the cache. Aborting throws away bytes that have already been paid
        // for and makes reopening cost them again.
    }

    /**
     * Something about what should be drawn changed. Free while collapsed,
     * which is what lets ImageViewer call this from its channel mutators
     * without thinking about it.
     *
     * @param options.refetch - true when the set of active channels may have
     *   changed and greyscale may be missing. Colour and contrast changes are
     *   satisfied entirely from cache.
     */
    invalidate(options) {
        if (!this.expanded) {
            return;
        }
        if (options && options.refetch) {
            this._loading = this._ensureData();
        } else {
            this._draw();
        }
    }

    // Note there is deliberately no cache-invalidation hook. The greyscale is
    // keyed on a channel that cannot change under a live session: main.js's
    // refreshDataset() re-reads the config but assigns only `config.dataset`,
    // precisely because adopting a new imageData mid-session would shift every
    // index the tile path and the channel sliders are keyed on. The image
    // behind a loaded project is fixed for its lifetime.

    // -- geometry --------------------------------------------------------

    /**
     * Works out how the image sits inside the circle. Returns false when
     * there is nothing sensible to lay out yet.
     *
     * Uses offsetWidth/offsetHeight, NOT getBoundingClientRect: the collapsed
     * state is a CSS transform (a scale down into the lens button) rather than
     * a real size change, and getBoundingClientRect reports the transformed
     * box. Keeping the layout box constant is what lets the geometry be
     * computed at any time regardless of where the open/close transition is.
     */
    _layout() {
        const width = Number(this.config.width) || 0;
        const height = Number(this.config.height) || 0;
        const size = Math.min(this.stage.offsetWidth, this.stage.offsetHeight);
        if (!width || !height || !size) {
            return false;
        }

        // Fit the LONG side across the full diameter, letting the image's four
        // corners fall outside the circle to be clipped by border-radius. The
        // strict "no clipping" fit is size/hypot(width, height), which on a
        // square image spends 29% of the diameter on empty space to protect
        // corners that are background in every whole-slide image.
        const scale = size / Math.max(width, height);
        const drawWidth = width * scale;
        const drawHeight = height * scale;
        this.geom = {
            size,
            scale,
            width,
            height,
            drawWidth,
            drawHeight,
            offsetX: (size - drawWidth) / 2,
            offsetY: (size - drawHeight) / 2,
            // OSD viewport coordinates put the image at x in [0, 1] and y in
            // [0, height/width]. Everything below normalises through this so
            // both axes are [0, 1].
            aspect: height / width,
        };

        // Capped at 2: past that the display canvas grows quadratically for
        // detail nobody can see in a 220 px circle.
        const dpr = Math.min(window.devicePixelRatio || 1, 2);
        const style = this.canvas.style;
        style.left = `${this.geom.offsetX}px`;
        style.top = `${this.geom.offsetY}px`;
        style.width = `${drawWidth}px`;
        style.height = `${drawHeight}px`;
        this.canvas.width = Math.max(1, Math.round(drawWidth * dpr));
        this.canvas.height = Math.max(1, Math.round(drawHeight * dpr));
        return true;
    }

    /** Normalised [0, 1] image coordinates for a pointer event. */
    _stagePoint(event) {
        const geom = this.geom;
        const rect = this.stage.getBoundingClientRect();
        return {
            u: (event.clientX - rect.left - geom.offsetX) / geom.drawWidth,
            v: (event.clientY - rect.top - geom.offsetY) / geom.drawHeight,
        };
    }

    /** Current viewport centre in normalised [0, 1] image coordinates. */
    _viewportCentre() {
        const bounds = this.viewer.viewport.getBounds(true);
        return {
            u: bounds.x + bounds.width / 2,
            v: (bounds.y + bounds.height / 2) / this.geom.aspect,
        };
    }

    _panToNormalized(u, v, immediately) {
        this.viewer.viewport.panTo(
            new OpenSeadragon.Point(
                MiniMap._clamp01(u),
                MiniMap._clamp01(v) * this.geom.aspect
            ),
            immediately
        );
    }

    /** Tooltip and accessible name together -- a title that still says
     *  "Show" over an open map is the one people actually read. */
    static _label(lens, expanded) {
        const text = expanded ? "Hide overview map" : "Show overview map";
        lens.title = text;
        lens.setAttribute("aria-label", text);
    }

    static _clamp01(value) {
        if (!Number.isFinite(value)) {
            return 0;
        }
        return Math.min(Math.max(value, 0), 1);
    }

    /**
     * Positions the "you are here" rectangle.
     *
     * Deliberately NOT via world.getItemAt(0).viewportToImageRectangle, which
     * is how getVisibleCentroidTileState does the same job: there is no world
     * item when every channel is switched off, and the mini-map still has to
     * draw an indicator in that state. Normalising the viewport bounds needs
     * no item and is immune to extraZoomLevels.
     */
    _syncIndicator() {
        if (!this.geom) {
            return;
        }
        const geom = this.geom;
        const bounds = this.viewer.viewport.getBounds(true);
        const left = MiniMap._clamp01(bounds.x);
        const top = MiniMap._clamp01(bounds.y / geom.aspect);
        const right = MiniMap._clamp01(bounds.x + bounds.width);
        const bottom = MiniMap._clamp01((bounds.y + bounds.height) / geom.aspect);

        const style = this.indicator.style;
        style.left = `${geom.offsetX + left * geom.drawWidth}px`;
        style.top = `${geom.offsetY + top * geom.drawHeight}px`;
        style.width = `${Math.max(right - left, 0) * geom.drawWidth}px`;
        style.height = `${Math.max(bottom - top, 0) * geom.drawHeight}px`;
    }

    // -- data ------------------------------------------------------------

    /**
     * The channels to draw, as { slot, srcIdx, fullName }.
     *
     * Read through ImageViewer.getActiveLegendChannels() -- the same helper the
     * channel legend uses -- so the lens and the legend can never disagree
     * about what is on.
     */
    _activeChannels() {
        const slots = this.imageViewer.getActiveLegendChannels
            ? this.imageViewer.getActiveLegendChannels()
            : [];
        const imageData = this.config.imageData || [];
        const dataLayer = this.imageViewer.dataLayer;
        const channels = [];
        for (const slot of slots) {
            const fullName = dataLayer && dataLayer.getFullChannelName
                ? dataLayer.getFullChannelName(slot.name)
                : slot.name;
            const srcIdx = imageData.findIndex((entry) => entry.fullname === fullName);
            if (srcIdx < 0) {
                continue;
            }
            channels.push({ slot, srcIdx, fullName });
        }
        return channels;
    }

    _overviewUrl(fullName) {
        const name = typeof datasource !== "undefined" && datasource
            ? datasource
            : (window.flaskVariables && window.flaskVariables.datasource) || "";
        return plexoraUrl(
            `generated/overview/${encodeURIComponent(name)}/${encodeURIComponent(fullName)}`
        );
    }

    /**
     * Fetches whatever active channel has no cached greyscale, then draws.
     *
     * There is deliberately no "is this rebuild still current" guard, which
     * is the usual shape for this (see updateVisibleCentroidTiles). It would
     * be actively wrong here: _draw() captures nothing -- it re-reads the
     * active channels, their colours and their ranges every time -- so a draw
     * from a superseded rebuild paints exactly what a fresh one would.
     * Suppressing those draws instead loses them, because a rebuild that
     * arrives while every channel is already in flight finds nothing missing
     * and returns immediately: its predecessor's redraws were the only ones
     * left to do, and the map stays black until an unrelated event repaints
     * it. Toggling a channel OFF mid-load is enough to reach that.
     *
     * The one property that does matter: colours and ranges are read at DRAW
     * time, never captured before an await. That is what makes a colour
     * change, a contrast drag or a channel removal during a fetch correct for
     * free.
     */
    async _ensureData() {
        // Paint what is already cached first, so reopening the lens fills in
        // the same frame rather than after a round trip.
        this._draw();

        if (this._isBrightfield()) {
            // One layer, fetched once. There is no per-channel cache to fill
            // and nothing to composite -- see _fetchBrightfield.
            if (this._brightfield || this._brightfieldPending) return;
            this._brightfieldPending = true;
            try {
                this._brightfield = await this._fetchBrightfield();
            } catch (error) {
                console.warn("mini-map: overview unavailable", error);
            } finally {
                this._brightfieldPending = false;
            }
            this._draw();
            return;
        }

        const missing = this._activeChannels().filter(
            (channel) => !this._gray.has(channel.srcIdx) && !this._pending.has(channel.srcIdx)
        );
        if (!missing.length) {
            return;
        }

        let cursor = 0;
        const drain = async () => {
            while (cursor < missing.length) {
                const channel = missing[cursor];
                cursor += 1;
                this._pending.add(channel.srcIdx);
                try {
                    const gray = await this._fetchOverview(channel.fullName);
                    if (gray) {
                        this._gray.set(channel.srcIdx, gray);
                        this._failed.delete(channel.srcIdx);
                    }
                } catch (error) {
                    // One channel that will not load is a missing colour in an
                    // overview, not a reason to have no overview.
                    //
                    // Recorded per channel rather than as one last-error field:
                    // these drain concurrently, so a single field is won by
                    // whichever request happens to finish last and a failure
                    // beside a success reads as either one, at random.
                    this._failed.set(channel.srcIdx, error.status || 0);
                    console.warn("mini-map: overview unavailable for", channel.fullName, error);
                } finally {
                    this._pending.delete(channel.srcIdx);
                }
                this._draw();
            }
        };

        const workers = Math.min(MiniMap.FETCH_CONCURRENCY, missing.length);
        await Promise.all(Array.from({ length: workers }, drain));
    }

    /**
     * One channel's overview as a single-byte-per-pixel array.
     *
     * premultiplyAlpha and colorSpaceConversion are both "none", matching
     * workers/tileDecoder.js. These bytes are measurements that a contrast
     * window is about to be applied to, not a photograph: letting the browser
     * apply a colour profile to them changes the numbers.
     */
    /** Whether this project's overview is one colour image rather than a
     *  stack to composite. */
    _isBrightfield() {
        return this.config.image_kind === "brightfield";
    }

    /**
     * The brightfield overview, as a drawable image.
     *
     * Kept as a bitmap rather than unpacked into bytes like `_fetchOverview`
     * does. That function strips WebP down to one byte per pixel because a
     * channel's pixels are only an input to the colorize-and-add loop below;
     * these pixels are the answer, so the canvas can draw them directly.
     */
    async _fetchBrightfield() {
        const entry = (this.config.imageData || [])
            .find((channel) => channel.fullname !== "Area");
        if (!entry) return null;
        const response = await fetch(this._overviewUrl(entry.fullname));
        if (!response.ok) {
            const error = new Error(`HTTP ${response.status}`);
            error.status = response.status;
            throw error;
        }
        return createImageBitmap(await response.blob());
    }

    async _fetchOverview(fullName) {
        const response = await fetch(this._overviewUrl(fullName));
        if (!response.ok) {
            const error = new Error(`HTTP ${response.status}`);
            // Carried so _updateNote can tell "this Plexora has no overview
            // route" (404 -- a server older than the feature, which waitress
            // will not pick up without a restart) from a transient failure.
            error.status = response.status;
            throw error;
        }
        const blob = await response.blob();
        const bitmap = await createImageBitmap(blob, {
            premultiplyAlpha: "none",
            colorSpaceConversion: "none",
        });
        const width = bitmap.width;
        const height = bitmap.height;
        const scratch = document.createElement("canvas");
        scratch.width = width;
        scratch.height = height;
        const context = scratch.getContext("2d", { willReadFrequently: true });
        context.drawImage(bitmap, 0, 0);
        if (bitmap.close) {
            bitmap.close();
        }
        const rgba = context.getImageData(0, 0, width, height).data;
        // The server writes mode 'L' WebP, so r == g == b; one byte per pixel
        // is the whole signal and a quarter of the memory.
        const data = new Uint8Array(width * height);
        for (let pixel = 0, offset = 0; pixel < data.length; pixel += 1, offset += 4) {
            data[pixel] = rgba[offset];
        }
        return { data, width, height };
    }

    // -- drawing ---------------------------------------------------------

    /**
     * A channel's contrast window as [min, max] fractions of the byte domain,
     * ready to feed the same arithmetic as frag.glsl's range_clamp.
     *
     * slot.range is in byte units by default and raw 16-bit units in HD mode
     * (viewerSidebar.onHdModeChanged), while these overview pixels are always
     * quantized into the byte domain -- so HD needs converting back through
     * the same window the server quantized against.
     */
    _byteRange(slot) {
        let range = Array.isArray(slot.range) ? slot.range : null;
        if (!range) {
            return [0, 1];
        }
        const sidebar = window.__plexora && window.__plexora.viewerSidebar;
        if (sidebar && sidebar.isHdMode && sidebar.isHdMode()) {
            const packet = sidebar.quantWindow ? sidebar.quantWindow(slot.name) : null;
            if (!packet || !sidebar.rawToByteRange) {
                // quantWindow is absent until a channel's stats land. Showing
                // the channel at full range is wrong by a little; treating it
                // as black is wrong by everything.
                return [0, 1];
            }
            range = sidebar.rawToByteRange(range, packet);
        }
        return [range[0] / 255, range[1] / 255];
    }

    /**
     * Three 256-entry tables, one per colour component, folding the contrast
     * window and the channel colour into a single lookup.
     *
     * This is frag.glsl's u8_r_range with the per-pixel work hoisted out:
     * range_clamp(byte / 255) * u_tile_color, at the 0.9 alpha the shader
     * emits and canvas `lighter` multiplies in.
     */
    _luts(slot) {
        const [low, high] = this._byteRange(slot);
        // GLSL's clamp absorbs a zero-width window; JS division gives NaN and
        // a fully transparent canvas.
        const span = Math.max(high - low, 1 / 255);
        const color = d3.color(slot.colorHex) || d3.color("white");
        const floatColor = toFloatColor(color);
        const tables = [new Float32Array(256), new Float32Array(256), new Float32Array(256)];
        for (let value = 0; value < 256; value += 1) {
            const clamped = Math.min(Math.max((value / 255 - low) / span, 0), 1);
            const scaled = clamped * MiniMap.TILE_ALPHA * 255;
            tables[0][value] = scaled * floatColor[0];
            tables[1][value] = scaled * floatColor[1];
            tables[2][value] = scaled * floatColor[2];
        }
        return tables;
    }

    /**
     * Says why the circle is empty, when it is empty for a reason.
     *
     * Only speaks up once every active channel has been tried and none of them
     * produced any greyscale -- a partial load is a map with a colour missing,
     * which draws fine and needs no commentary, and a load still in flight is
     * not a failure yet.
     *
     * 404 is called out specifically because it has one cause in practice:
     * /generated/overview is younger than the running server. server_cli hands
     * the app to waitress.serve, which has no reloader, and Flask registers
     * routes at import -- so a Plexora updated underneath a live process keeps
     * serving the new templates and static JS (both read from disk per request)
     * while 404ing the new route. The lens then draws, tracks the viewport
     * correctly, and stays black.
     */
    _updateNote() {
        if (!this.note) {
            return;
        }
        const channels = this._activeChannels();
        const cached = channels.some((channel) => this._gray.has(channel.srcIdx));
        const pending = channels.some((channel) => this._pending.has(channel.srcIdx));
        const failed = channels.filter((channel) => this._failed.has(channel.srcIdx));
        let text = "";
        if (channels.length && !cached && !pending && failed.length) {
            const missingRoute = failed.every(
                (channel) => this._failed.get(channel.srcIdx) === 404
            );
            text = missingRoute
                ? "Overview unavailable — restart the Plexora server to enable it."
                : "Overview unavailable.";
        }
        if (this.note.textContent !== text) {
            this.note.textContent = text;
            this.root.classList.toggle("has-note", Boolean(text));
        }
    }

    /**
     * Composites every cached channel into the map.
     *
     * Done at the OVERVIEW's own resolution (~300 px) and scaled up on the way
     * to the display canvas, rather than at the display canvas's device-pixel
     * resolution. That keeps the cost of this loop independent of both the
     * lens's size and the device pixel ratio -- at dpr 3 the direct version
     * would be doing seven times the work for detail a 220 px circle cannot
     * show.
     */
    _draw() {
        if (!this.expanded || !this.geom || !this.canvas.width) {
            return;
        }
        const context = this.canvas.getContext("2d");
        // Black is the ground a fluorescence composite is built on, and white
        // is the ground a transmitted-light image already has -- so the lens
        // matches the viewer behind it either way.
        context.fillStyle = this._isBrightfield() ? "#fbfbfc" : "#000";
        context.fillRect(0, 0, this.canvas.width, this.canvas.height);

        if (this._isBrightfield()) {
            this._updateNote();
            if (!this._brightfield) return;
            context.imageSmoothingEnabled = true;
            context.imageSmoothingQuality = "high";
            context.drawImage(this._brightfield, 0, 0,
                              this.canvas.width, this.canvas.height);
            return;
        }

        const channels = this._activeChannels().filter((channel) => this._gray.has(channel.srcIdx));
        this._updateNote();
        if (!channels.length) {
            return;
        }

        const first = this._gray.get(channels[0].srcIdx);
        const width = first.width;
        const height = first.height;
        const pixels = width * height;
        const accumulator = this._accumulatorFor(pixels);

        for (const channel of channels) {
            const gray = this._gray.get(channel.srcIdx);
            if (gray.width !== width || gray.height !== height) {
                continue;
            }
            const tables = this._luts(channel.slot);
            const red = tables[0];
            const green = tables[1];
            const blue = tables[2];
            const data = gray.data;
            for (let pixel = 0, offset = 0; pixel < pixels; pixel += 1, offset += 3) {
                const value = data[pixel];
                // Background is most of a slide, and a zero byte contributes
                // nothing for any window whose minimum is >= 0 (all of them).
                if (!value) {
                    continue;
                }
                accumulator[offset] += red[value];
                accumulator[offset + 1] += green[value];
                accumulator[offset + 2] += blue[value];
            }
        }

        const image = this._imageFor(width, height);
        const out = image.data;
        for (let pixel = 0, offset = 0, index = 0; pixel < pixels; pixel += 1, offset += 3, index += 4) {
            // Uint8ClampedArray rounds and clamps on assignment, which is the
            // saturating add that `lighter` performs in the main viewer.
            out[index] = accumulator[offset];
            out[index + 1] = accumulator[offset + 1];
            out[index + 2] = accumulator[offset + 2];
        }

        const scratch = this._scratchFor(width, height);
        scratch.getContext("2d").putImageData(image, 0, 0);
        context.imageSmoothingEnabled = true;
        context.imageSmoothingQuality = "high";
        context.drawImage(scratch, 0, 0, this.canvas.width, this.canvas.height);
    }

    _accumulatorFor(pixels) {
        if (!this._accum || this._accum.length !== pixels * 3) {
            this._accum = new Float32Array(pixels * 3);
        } else {
            this._accum.fill(0);
        }
        return this._accum;
    }

    _imageFor(width, height) {
        if (!this._image || this._image.width !== width || this._image.height !== height) {
            this._image = new ImageData(width, height);
            // Opaque for the life of the buffer: only rgb is rewritten above.
            const data = this._image.data;
            for (let index = 3; index < data.length; index += 4) {
                data[index] = 255;
            }
        }
        return this._image;
    }

    _scratchFor(width, height) {
        if (!this._scratch) {
            this._scratch = document.createElement("canvas");
        }
        if (this._scratch.width !== width || this._scratch.height !== height) {
            this._scratch.width = width;
            this._scratch.height = height;
        }
        return this._scratch;
    }

    // -- interaction -----------------------------------------------------

    _onPointerDown(event) {
        if (event.button !== 0 || !this.expanded || !this.geom) {
            return;
        }
        const point = this._stagePoint(event);
        if (event.target === this.indicator) {
            // Grab the rectangle where it was grabbed, so it does not jump its
            // centre to the cursor.
            const centre = this._viewportCentre();
            this._drag = { u: centre.u - point.u, v: centre.v - point.v };
        } else {
            // Anywhere else on the map: recentre there, then keep dragging
            // from that point.
            this._drag = { u: 0, v: 0 };
            this._panToNormalized(point.u, point.v, false);
        }
        // Capture keeps the gesture alive when the cursor leaves the circle,
        // which on a 220 px target is most drags. It is an improvement, not a
        // requirement -- the move and up handlers are on the stage either way
        // -- so a browser that refuses the capture must not take the drag down
        // with it, or throw out of a pointerdown handler.
        try {
            this.stage.setPointerCapture(event.pointerId);
        } catch (error) {
            /* not capturable; the drag still works, just not off-element */
        }
        event.preventDefault();
    }

    _onPointerMove(event) {
        if (!this._drag) {
            return;
        }
        const point = this._stagePoint(event);
        // Immediately, not animated: a navigation gesture should track the
        // cursor rather than easing behind it.
        this._panToNormalized(point.u + this._drag.u, point.v + this._drag.v, true);
    }

    _onPointerUp(event) {
        if (!this._drag) {
            return;
        }
        this._drag = null;
        if (this.stage.hasPointerCapture && this.stage.hasPointerCapture(event.pointerId)) {
            this.stage.releasePointerCapture(event.pointerId);
        }
        // panTo(immediately) skips OSD's constraints, so re-apply them once
        // the gesture ends rather than fighting them during it.
        this.viewer.viewport.applyConstraints();
    }

    _onWheel(event) {
        if (!this.expanded || !this.geom) {
            return;
        }
        event.preventDefault();
        const point = this._stagePoint(event);
        // Zoom about the point under the cursor, so the wheel reads as
        // "magnify here" rather than "magnify wherever I happen to be".
        const reference = new OpenSeadragon.Point(
            MiniMap._clamp01(point.u),
            MiniMap._clamp01(point.v) * this.geom.aspect
        );
        this.viewer.viewport.zoomBy(Math.pow(1.0015, -event.deltaY), reference);
        this.viewer.viewport.applyConstraints();
    }
}
