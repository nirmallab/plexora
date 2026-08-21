/**
 * viewer.js.
 *
 * @class ImageViewer to render multiplexed imaging data (based on OpenSeadragon)
 */

/* todo
 1. major - the viewer managers should not be looking up the same renderTF
 */

// Small standalone helper (not a class method) -- used by drawLegendVector's
// PDF export path to turn a channel's stored #rrggbb colorHex into the r/g/b
// triplet jsPDF's setFillColor() takes.
function hexToRgb(hex) {
    const match = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex || "");
    if (!match) return { r: 255, g: 255, b: 255 };
    return {
        r: parseInt(match[1], 16),
        g: parseInt(match[2], 16),
        b: parseInt(match[3], 16),
    };
}

/**
 * Byte-budgeted LRU of WebGL tile textures.
 *
 * Replaces a 24-entry round-robin slot table that could never register a hit:
 * it tracked labels but handed every slot the SAME single WebGLTexture (one
 * gl.createTexture() in GLRenderer), and its "is it already resident" check
 * only compared against the most recently bound slot. The net effect was a
 * full gl.texImage2D re-upload of every visible tile of every channel on every
 * frame -- 1 MB per u8 1024x1024 tile, so tens of MB per frame at 7+ channels,
 * which on its own is enough to stop a pan from being smooth.
 *
 * Keys must include the pixel format, not just tile identity: the source's
 * getTileKey() does not distinguish the HD (16-bit) variant from the default
 * 8-bit one, so a format-blind key could serve a u8 texture to a u16 draw.
 */
class GLTileTextureCache {
    constructor(gl, byteBudget) {
        this.gl = gl;
        this.byteBudget = byteBudget;
        this.entries = new Map(); // insertion order == LRU order
        this.bytes = 0;
    }

    /**
     * @returns {{texture: WebGLTexture, resident: boolean}} `resident` is true
     * when the texture already holds this key's pixels, so the caller can skip
     * the upload entirely.
     */
    acquire(key, byteLength) {
        const existing = this.entries.get(key);
        if (existing) {
            // Re-insert to move this entry to the most-recently-used end.
            this.entries.delete(key);
            this.entries.set(key, existing);
            return { texture: existing.texture, resident: true };
        }
        while (this.bytes + byteLength > this.byteBudget && this.entries.size > 0) {
            const [oldestKey, oldest] = this.entries.entries().next().value;
            this.entries.delete(oldestKey);
            this.bytes -= oldest.byteLength;
            this.gl.deleteTexture(oldest.texture);
        }
        const texture = this.gl.createTexture();
        this.entries.set(key, { texture, byteLength });
        this.bytes += byteLength;
        return { texture, resident: false };
    }

    clear() {
        for (const entry of this.entries.values()) {
            this.gl.deleteTexture(entry.texture);
        }
        this.entries.clear();
        this.bytes = 0;
    }
}


/**
 * Round-robin pool of tile-decode workers (see workers/tileDecoder.js).
 *
 * Decoding is per tile per channel, so the work scales with channel count: a
 * CPU profile put it at ~2% of a 7-channel pan but ~60% of a 15-channel one.
 * Spreading it across workers both takes it off the main thread and lets
 * several tiles decode in parallel.
 *
 * Falls back to null from create() if Worker/OffscreenCanvas are unavailable,
 * and callers then decode inline -- the inline path has to exist anyway for
 * the HD and segmentation formats.
 */
class TileDecoderPool {
    static create(url, size) {
        if (typeof Worker === "undefined" || typeof OffscreenCanvas === "undefined") {
            return null;
        }
        try {
            return new TileDecoderPool(url, size);
        } catch (err) {
            console.warn("Tile decode workers unavailable, decoding inline:", err);
            return null;
        }
    }

    constructor(url, size) {
        this.pending = new Map();
        this.nextId = 0;
        this.next = 0;
        this.workers = [];
        for (let i = 0; i < size; i++) {
            const worker = new Worker(url);
            worker.onmessage = (event) => {
                const { id, ok, array, width, height, error, unsupported } = event.data;
                const entry = this.pending.get(id);
                if (!entry) {
                    return;
                }
                this.pending.delete(id);
                if (ok) {
                    entry.resolve({ array, width, height });
                } else {
                    const err = new Error(error);
                    // Distinguishes "this worker cannot handle this input" from
                    // "the worker broke": the caller falls back rather than
                    // treating it as a failure.
                    err.unsupported = !!unsupported;
                    entry.reject(err);
                }
            };
            worker.onerror = (err) => {
                // A worker-level failure orphans everything queued on it, so
                // reject all outstanding work rather than leaving tiles hung on
                // promises OSD is awaiting.
                for (const [id, entry] of this.pending) {
                    entry.reject(err instanceof Error ? err : new Error(String(err && err.message)));
                    this.pending.delete(id);
                }
            };
            this.workers.push(worker);
        }
    }

    /**
     * @param buffer - the raw tile bytes as delivered by the server
     * @param kind - "webp" (default 8-bit path) or "gray16png" (HD path)
     * @returns Promise<{array: Uint8Array, width: number, height: number}>
     */
    decode(buffer, kind) {
        const id = this.nextId++;
        const worker = this.workers[this.next];
        this.next = (this.next + 1) % this.workers.length;
        return new Promise((resolve, reject) => {
            this.pending.set(id, { resolve, reject });
            // `buffer` is deliberately copied, not transferred: it belongs to
            // OpenSeadragon's XHR response and detaching it here would corrupt
            // whatever OSD does with the response afterwards. The decoded
            // result, which is the large one, IS transferred back.
            worker.postMessage({ id, buffer, kind });
        });
    }

    terminate() {
        this.workers.forEach((w) => w.terminate());
        this.workers = [];
        this.pending.clear();
    }
}


class ImageViewer {
    // Vars
    viewerManagers = [];

    //: Filled masks hide the tissue underneath at full strength, and outlines
    //: read poorly at low strength. One control has to serve both, so this is a
    //: compromise leaning towards keeping the image visible.
    static DEFAULT_CELL_LAYER_OPACITY = 0.7;

    //: The modes drawn from the label tiles, and therefore the ones the sidebar
    //: order restacks. Centroids are drawn on the overlay canvas instead, which
    //: is above every tile whatever the order says -- see centroidDrawList.
    static MASK_MODES = ["outlines", "filled"];

    //: The stand-in layer used while no plugin has registered one. Named so a
    //: per-tile canvas keyed by it can never collide with a plugin's name.
    static CORE_LAYER = "__core__";

    //: How big centroid dots are drawn, as a multiplier on the zoom-adaptive
    //: size. A multiplier rather than a size: the adaptive part is what keeps
    //: points visible when the whole slide is on screen and from swamping the
    //: tissue at full zoom, and a fixed size chosen at one zoom is wrong at the
    //: other. Bounds are generous because cell density varies by orders of
    //: magnitude between a tissue microarray core and a whole section.
    static DEFAULT_CENTROID_SCALE = 1;
    static MIN_CENTROID_SCALE = 0.4;
    static MAX_CENTROID_SCALE = 3;

    /**
     * Constructor for ImageViewer.
     *
     * @param config - the cinfiguration file (json)
     * @param imgMetadata - image metadata from ome
     * @param numericData - custom numeric data layer
     * @param eventHandler - the event handler for distributing interface and data updates
     */
    constructor(config, dataLayer, imgMetadata, numericData, eventHandler) {
        this.ready = false;
        this.config = config;
        this.dataLayer = dataLayer;
        // Every plugin currently colouring cells, keyed by name. Each entry is
        // one LAYER -- see registerCellLayer for the record's shape -- and
        // several may be live at once, which is what lets a phenotype map and a
        // gate be looked at together rather than one replacing the other.
        //
        // Order is held separately because it is the z-order the layers
        // composite in (bottom first) and the user sets it by dragging cards in
        // the sidebar; membership and stacking change independently.
        this._cellLayers = new Map();
        this._cellLayerOrder = [];
        // Which layer the shared controls, picking and gating act on. Exactly
        // one, or none: "visible" is a property of every layer, "active" is a
        // property of the session, and conflating the two is what made opening a
        // second tool silently take the first one's colours away.
        this._activeCellLayer = null;
        // Which of the three representations the cell layer draws when NO plugin
        // has registered one. Core's own mode, kept so a plain viewer -- or one
        // running only Thresholding before it registers -- draws exactly what it
        // always did. Once layers exist, each carries its own mode.
        this.cellDisplayMode = "outlines";
        // The record coreLayerView() hands the renderers. One object, refreshed
        // in place, because it is read once per label tile per frame.
        this._coreLayerView = {
            name: ImageViewer.CORE_LAYER,
            provider: null,
            lut: null,
            mode: this.cellDisplayMode,
            userMode: null,
            supportedModes: null,
            opacity: 1,
            visible: true,
            filterIds: null,
            filterRequest: 0,
            styleCache: new Map(),
        };
        // Centroid dot size, as a multiplier -- see setCentroidPointScale.
        this.centroidPointScale = ImageViewer.DEFAULT_CENTROID_SCALE;
        this.channelList = null;
        this.imgMetadata = imgMetadata;
        this.numericData = numericData;
        this.eventHandler = eventHandler;
        this.pickingChanged = false;
        this._cacheKeys = {};
        this._picking = [];
        this.pickedId = -1;
        this.glReady = new Promise((resolve) => {
            this.resolveGLReady = resolve;
        });

        this.centers = [];
        this.ids = [];
        this.centroidManifest = null;
        this.centroidTiles = new Map();
        this.centroidFilter = {};
        this.centroidFilterSignature = "{}";
        this.centroidTileTimer = null;
        this.centroidTileRequest = 0;
        this.centroidFirstLoad = true;
        this.centroidMode = "tiled";
        this.fullResolutionCenters = [];
        this.idToCenterOffset = new Map();
        this.centroidIdSet = null;
        this.centroidsReady = false;
        this.centroidsLoading = null;
        this.segmentationReady = false;
        this.segmentationLoading = null;
        // Core's own gate -- the one that applies when no plugin has registered
        // a layer. A registered layer carries its own `filterIds` instead, so
        // one tool's gate cannot subtract cells from another tool's colours.
        this.segmentationFilterIds = null;
        this.segmentationFilterRequest = 0;

        // Viewer
        this.viewer = {};

        // OSD plugins
        this.show_scalebar = true;
        this.show_centroids = false;

        // Transfer function constant
        this.numTFBins = 1024;

        // Transfer function per channel (min,max, start color, end color)
        this.channelTF = [];

        for (let i = 0; i < this.config["imageData"].length; i = i + 1) {
            const start_color = d3.rgb(0, 0, 0);
            const end_color = d3.rgb(255, 255, 255);

            const tf_def = this.createTFArray(0, 65535, start_color, end_color, this.numTFBins);
            tf_def.name = this.config["imageData"][i].name;

            this.channelTF.push(tf_def);
        }

        // Applying TF to selection, subset, or all
        this.show_subset = false;
        this.show_selection = true;

        // Hide Loader
        this.setLoading(false);

        // Config viewer
        const viewer_config = {
            id: "openseadragon",
            prefixUrl: plexoraUrl("client/external/openseadragon-bin-2.4.0/openseadragon-flat-toolbar-icons-master/images/"),
            minZoomImageRatio: 0.1,
            maxZoomPixelRatio: 15,
            compositeOperation: "lighter",
            loadTilesWithAjax: true,
            immediateRender: false,
            // OpenSeadragon 6 creates ONE TileCache on the viewer and hands the
            // same instance to every TiledImage, so this budget has to cover
            // (visible tiles x active channels), not just visible tiles. At
            // 1024^2 tiles the old value of 100 was below a single viewport's
            // worth once ~8 channels were on, so OSD kept evicting tiles it was
            // about to redraw -- refetch and redecode mid-pan, which is the
            // classic tile-popping stutter. Each cached tile also pins a ~1 MB
            // decoded Uint8Array, so this doubles as the memory ceiling.
            maxImageCacheCount: Math.min(512, Math.max(200, ((config["imageData"] || []).length) * 40)),
            // Left at OSD's default of 0 (unlimited), OSD opens every queued
            // tile request at once; the browser then serves them ~6 at a time
            // per origin in issue order, so tiles for where the viewport USED to
            // be block the ones where it is now. Capping the in-flight set keeps
            // OSD's own ordering in charge.
            imageLoaderLimit: 10,
            timeout: 90000,
            collectionMode: false,
            preload: false,
            homeFillsViewer: true,
            visibilityRatio: 0,
            // Force the canvas drawer: our per-tile WebGL colorize pass needs
            // the 'tile-drawing' event's 2D `rendered` context, which is only
            // guaranteed under the canvas drawer (OSD 6's WebGL drawer has no
            // documented custom-shader hook as of this writing).
            drawer: "canvas",
        };

        // Instantiate the real OpenSeadragon viewer
        this.viewer = OpenSeadragon(viewer_config);
        // Lets the navbar status indicator report tiles that are still
        // streaming in -- see appStatus.js watchViewer(), which tracks each
        // TiledImage rather than the viewer's own aggregate.
        window.PlexoraStatus?.watchViewer(this.viewer);
        this.initProjectLabel();
        this.initLegend();
        this.addScaleBar();
        this.selectionPolygonToDraw = [];

        // OSD's own full-page button only resizes the #openseadragon element
        // itself (it reparents that element to <body>), leaving the sidebar
        // behind. Redirect it to a native Fullscreen API toggle on the whole
        // app shell (sidebar + viewer) instead.
        this.viewer.addHandler("pre-full-page", (event) => {
            event.preventDefaultAction = true;
            const shell = document.getElementById("bodyDiv");
            if (!shell) {
                return;
            }
            if (document.fullscreenElement) {
                document.exitFullscreen();
            } else {
                shell.requestFullscreen();
            }
        });

        // Get and shrink all button images
        this.parent = d3.select(`#openseadragon`);
        this.parent.selectAll('img')
            .attr('height', 40);

        // Force controls to bottom right
        const controlsAnchor = this.parent.select('img').node().parentElement.parentElement.parentElement.parentElement;
        controlsAnchor.style.right = 'unset';
        controlsAnchor.style.top = 'unset';
        controlsAnchor.style.left = '40vh';
        controlsAnchor.style.bottom = '2vh';

        // Flexible use of textures. Marker and constant textures occupy fixed
        // texture units at the top of the range; tile textures do NOT get a
        // unit each -- the fragment shader's u_tile sampler is pinned to unit 0
        // once in GLRenderer.toBuffers(), so the tile being drawn is always
        // bound to unit 0 and the cache below decides which texture object that
        // is.
        const constantTextures = ["ids", "centers", "ranges", "pickings"];
        const otherOffset = 32 - constantTextures.length;
        const renderer = new GLRenderer();
        const nMarkers = 4;
        const markerOffset = otherOffset - nMarkers;
        const markerTextureKeys = [...Array(nMarkers).keys()];
        renderer._otherOffset = otherOffset;
        renderer._markerOffset = markerOffset;
        renderer._markerTextures = markerTextureKeys.map(() => "");
        renderer._constantTextures = constantTextures;
        renderer._activeMarkerTexture = 0;
        renderer._nextMarkerTexture = 0;
        // 384 MB holds ~384 u8 1024x1024 tiles, comfortably more than the
        // visible tiles x active channels a viewport can ask for (7+ channels x
        // ~6 tiles), so panning back over recent ground re-binds instead of
        // re-uploading. QuPath's equivalent tile cache is a similar fraction of
        // available memory.
        renderer._tileTextureCache = new GLTileTextureCache(renderer.gl, 384 * 1024 * 1024);
        this.glRenderer = renderer;

        const indexOfTexture = this.indexOfTexture.bind(this);
        const selectTexture = this.selectTexture.bind(this);
        const resolveGLReady = this.resolveGLReady;

        renderer.loadArray = function (e, w, h) {
            // Allow for custom drawing in webGL
            var gl = this.gl;
            const { source } = e.tiledImage;
            const tileArgs = [e.tile.level, e.tile.x, e.tile.y];
            const format = e.tile._format || `u${source.format}`;
            const okFormat = ["u16", "u32", "u8"].includes(format);
            const pixels = e.tile._array;

            // Clear before starting all the draw calls
            gl.clearColor(0, 0, 0, 0);
            gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);

            if (okFormat) {
                // Format is part of the key because getTileKey() does not
                // distinguish the HD 16-bit variant of a tile from the default
                // 8-bit one -- without it a cache hit could bind u8 pixels to a
                // draw that reads them as u16.
                const cacheKey = `${source.getTileKey(...tileArgs)}|${format}`;
                const { texture, resident } = this._tileTextureCache.acquire(
                    cacheKey, pixels ? pixels.byteLength : 0);
                if (resident) {
                    // Already on the GPU: bind it and skip the upload. This is
                    // the whole point of the cache -- it turns a per-tile,
                    // per-frame megabyte transfer into a bind.
                    gl.activeTexture(gl.TEXTURE0);
                    gl.bindTexture(gl.TEXTURE_2D, texture);
                } else {
                    selectTexture(gl, texture, 0);
                    const textureArgs = {
                        u16: [gl.RG8UI, w, h, 0, gl.RG_INTEGER],
                        u32: [gl.RGBA8UI, w, h, 0, gl.RGBA_INTEGER],
                        u8: [gl.R8UI, w, h, 0, gl.RED_INTEGER],
                    }[format];

                    // Send the tile into the texture.
                    gl.texImage2D(gl.TEXTURE_2D, 0, ...textureArgs, gl.UNSIGNED_BYTE, pixels);
                }
            }

            this.gl_arguments.tile_shape_2fv = new Float32Array([w, h]);

            // Call gl-drawing after loading
            this["gl-drawing"].call(this);
            gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
            return gl.canvas;
        };

        renderer.vShader = plexoraUrl("client/src/shaders/vert.glsl");
        renderer.fShader = plexoraUrl("client/src/shaders/frag.glsl");

        // Default tile-drawing behavior, invoked as the "callback" from the
        // custom handler below.
        function tileDrawingDefault(e) {
            var w = e.rendered.canvas.width;
            var h = e.rendered.canvas.height;
            var gl_w = renderer.width;
            var gl_h = renderer.height;

            // Render a webGL canvas to an input canvas
            var output = renderer.loadArray(e, w, h);
            e.rendered.drawImage(output, 0, 0, gl_w, gl_h, 0, 0, w, h);
        }

        const { floatRange } = this.numericData;
        const findCurrentChannel = this.findCurrentChannel.bind(this);
        const selectCenterProps = this.selectCenterProps.bind(this);
        const labelOutlinesEnabled = () => !!this.viewerManagerVMain?.sel_outlines;
        const modeFlags = () => this.modeFlags;
        // Custom tile-drawing handler
        const tileDrawingCustom = async (callback, e) => {
            // Read parameters from each tile
            const { source } = e.tiledImage;
            const { tileFormat } = source;
            const w = e.rendered.canvas.width;
            const h = e.rendered.canvas.height;

            if (tileFormat != 32) {
                // A tile's URL never changes, so derive sub_url once and keep it
                // on the tile. This was a getUrl() + String.split() allocation
                // per tile per channel per frame.
                let sub_url = e.tile._subUrl;
                if (sub_url === undefined) {
                    const group = e.tile.getUrl().split("/");
                    sub_url = group[group.length - 3];
                    e.tile._subUrl = sub_url;
                }
                const channel = findCurrentChannel(sub_url);
                const range = _.get(channel, "range", floatRange);
                const color = _.get(channel, "color", d3.color("white"));
                const floatColor = toFloatColor(color);
                // The fast/default tile path quantizes 16-bit -> 8-bit
                // server-side, linear against the channel's true max (see
                // get_channel_quantization_window). The shader works directly
                // in that same [0, 255] byte domain -- u_tile_range is
                // expressed in byte units too in this mode (see
                // viewerSidebar.js's getImageRange/toImageConnectorRange),
                // so no reconstruction back into 16-bit units is needed here.
                const tileFmt = e.tile._format === "u8" ? 8 : 16;
                const modes = modeFlags();

                // `e.rendered` is this tile's OWN persistent 2D context -- OSD
                // resolves it per tile via DrawerBase.getDataToDraw() (the tile
                // cache) and blits it onto the canvas right after this handler
                // returns. So when it already holds exactly this colorization
                // there is nothing to do.
                //
                // This early return is the single biggest win available on the
                // client. A CPU profile of a 7-channel pan attributed 81.9% of
                // all wall time to one call: the WebGL-canvas -> 2D-canvas blit
                // at the end of this path, running ~103 times per frame at
                // ~2.3 ms each, because OSD re-raises tile-drawing for every
                // visible tile of every channel on every frame. The pixels were
                // almost always identical to the previous frame's.
                //
                // The signature covers everything the draw below depends on:
                // tile identity (cacheKey also changes when HD swaps the pixel
                // data), the pixel format, and the channel's colour/range/mode.
                const sig = `${e.tile.cacheKey}|${tileFmt}|${floatColor}|${range}|${modes.edge},${modes.or}`;
                if (e.rendered._plexoraSig === sig) {
                    return;
                }

                if (!e.tile._array) {
                    // Not loaded yet -- e.g. right after the HD toggle forces
                    // every visible tile to redraw immediately, before the
                    // freshly-invalidated tile's fetch/decode has finished.
                    // Falling through with pixels=undefined would still reach
                    // gl.texImage2D, which allocates the texture with whatever
                    // GPU memory happened to be there -- rendered as solid
                    // static instead of skipping this frame.
                    e.rendered._plexoraSig = null;
                    e.rendered.fillStyle = "black";
                    e.rendered.fillRect(0, 0, w, h);
                    console.warn("Missing Array for tile:", e.tile.getUrl(), "- skipping rendering");
                    return;
                }

                // The black fill is load-bearing, not redundant: the shader
                // emits alpha 0.9, so the GL output composites over whatever is
                // already in this reused canvas.
                e.rendered.fillStyle = "black";
                e.rendered.fillRect(0, 0, w, h);

                // Store channel color and range to send to shader
                renderer.gl_arguments = {
                    ...selectCenterProps(e.tile, source),
                    centers: [],
                    id_end_1i: 0,
                    picked_end_1i: 0,
                    color_3fv: new Float32Array(floatColor),
                    range_2fv: new Float32Array(range),
                    fmt_1i: tileFmt,
                };
                callback(e);
                e.rendered._plexoraSig = sig;
                return;
            }

            // Label/segmentation tiles must stay transparent outside outlines
            // so image channels underneath remain visible. Not signature-cached:
            // the label tiled image is a single item (so this is not multiplied
            // by the channel count), and what it draws also depends on each
            // layer's gate and colours, which rerenderSegmentationTiles()
            // rebuilds out of band.
            e.rendered.clearRect(0, 0, w, h);

            if (e.tile._layerContexts) {
                if (labelOutlinesEnabled()) {
                    // BLIT ORDER IS Z-ORDER, and it is the whole of the stacking
                    // model for mask layers: bottom first, each one painting over
                    // whatever the layers below it left. Reordering the sidebar
                    // therefore costs a redraw and never a re-render -- the tile
                    // canvases already hold the right pixels.
                    //
                    // Opacity is applied HERE rather than baked into a layer's
                    // pixels for the same reason. This branch already re-runs
                    // every frame, so a slider drag costs one extra blit
                    // argument; folding it into renderLabelTile would instead
                    // re-derive every visible tile's boundaries on every pointer
                    // move.
                    const previous = e.rendered.globalAlpha;
                    let alpha = previous;
                    for (const layer of this.maskDrawList()) {
                        const context = e.tile._layerContexts.get(layer.name);
                        if (!context) continue;
                        // A layer with no colours is the plain white cell layer,
                        // which predates the opacity control and must keep
                        // compositing at full strength -- see layerAlpha.
                        const next = layer.lut ? layer.opacity : 1;
                        if (next !== alpha) {
                            e.rendered.globalAlpha = next;
                            alpha = next;
                        }
                        e.rendered.drawImage(context.canvas, 0, 0, w, h);
                    }
                    if (alpha !== previous) e.rendered.globalAlpha = previous;
                }
                return;
            }

            if (!e.tile._array) {
                console.warn("Missing Array for tile:", e.tile.getUrl(), "- skipping rendering");
                return;
            }

            // Use new parameters for this tile
            renderer.gl_arguments = {
                ...selectCenterProps(e.tile, source),
                color_3fv: new Float32Array([1, 1, 1]),
                range_2fv: new Float32Array([0, 1]),
                fmt_1i: 32,
            };

            // Start webGL rendering
            callback(e);
        };

        renderer["gl-drawing"] = function () {
            const args = this.gl_arguments;

            // Send color and range to shader
            this.gl.uniform2fv(this.u_tile_shape, args.tile_shape_2fv);
            this.gl.uniform4iv(this.u_marker_sample, args.marker_sample_4iv);
            this.gl.uniform2iv(this.u_magnitude_shape, args.magnitude_2iv);
            this.gl.uniform1f(this.u_tile_fraction, args.tile_fraction_1f);
            this.gl.uniform1f(this.u_tile_scale, args.tile_scale_1f);
            this.gl.uniform1f(this.u_pie_radius, args.pie_radius_1f);
            this.gl.uniform1i(this.u_picked_end, args.picked_end_1i);
            this.gl.uniform2fv(this.u_tile_origin, args.origin_2fv);
            this.gl.uniform3fv(this.u_tile_color, args.color_3fv);
            this.gl.uniform2fv(this.u_tile_range, args.range_2fv);
            this.gl.uniform2iv(this.u_draw_mode, args.modes_2i);
            this.gl.uniform2fv(this.u_x_bounds, args.x_bounds_2fv);
            this.gl.uniform2fv(this.u_y_bounds, args.y_bounds_2fv);
            this.gl.uniform1i(this.u_tile_fmt, args.fmt_1i);
            this.gl.uniform1i(this.u_id_end, args.id_end_1i);
        };

        renderer["gl-loaded"] = function (program) {
            // Uniform variables for coloring
            this.u_ids_shape = this.gl.getUniformLocation(program, "u_ids_shape");
            this.u_tile_shape = this.gl.getUniformLocation(program, "u_tile_shape");
            this.u_cell_range_shape = this.gl.getUniformLocation(program, "u_cell_range_shape");
            this.u_center_shape = this.gl.getUniformLocation(program, "u_center_shape");
            this.u_picking_shape = this.gl.getUniformLocation(program, "u_picking_shape");
            this.u_marker_sample = this.gl.getUniformLocation(program, "u_marker_sample");
            this.u_magnitude_shape = this.gl.getUniformLocation(program, "u_magnitude_shape");
            this.u_tile_fraction = this.gl.getUniformLocation(program, "u_tile_fraction");
            this.u_tile_scale = this.gl.getUniformLocation(program, "u_tile_scale");
            this.u_pie_radius = this.gl.getUniformLocation(program, "u_pie_radius");
            this.u_tile_origin = this.gl.getUniformLocation(program, "u_tile_origin");
            this.u_tile_range = this.gl.getUniformLocation(program, "u_tile_range");
            this.u_tile_color = this.gl.getUniformLocation(program, "u_tile_color");
            this.u_draw_mode = this.gl.getUniformLocation(program, "u_draw_mode");
            this.u_x_bounds = this.gl.getUniformLocation(program, "u_x_bounds");
            this.u_y_bounds = this.gl.getUniformLocation(program, "u_y_bounds");
            this.u_tile_fmt = this.gl.getUniformLocation(program, "u_tile_fmt");
            this.u_picked_end = this.gl.getUniformLocation(program, "u_picked_end");
            this.u_id_end = this.gl.getUniformLocation(program, "u_id_end");

            // Texture for colormap
            const u_ids = this.gl.getUniformLocation(program, "u_ids");
            const u_cell_ranges = this.gl.getUniformLocation(program, "u_cell_ranges");
            const u_centers = this.gl.getUniformLocation(program, "u_centers");
            const u_pickings = this.gl.getUniformLocation(program, "u_pickings");
            this.gl.uniform1i(u_ids, indexOfTexture("ids", null));
            this.gl.uniform1i(u_cell_ranges, indexOfTexture("ranges", null));
            this.gl.uniform1i(u_centers, indexOfTexture("centers", null));
            this.gl.uniform1i(u_pickings, indexOfTexture("pickings", null));
            for (const i of [0, 1, 2, 3]) {
                const u_mag_i = this.gl.getUniformLocation(program, `u_mag_${i}`);
                this.gl.uniform1i(u_mag_i, i + this._markerOffset);
            }
            setTimeout(() => resolveGLReady(), 0);
        };

        const matchTile = (e, { x, y, level }) => {
            const grid = e.tiledImage.tilesMatrix[level];
            return ((grid || {})[x] || {})[y] || {};
        };

        const decodeLabelTile = (responseArray) => {
            const upng = window.UPNG;
            if (upng) {
                const img = upng.decode(responseArray);
                if (img.ctype == 6 && img.depth == 8) {
                    return {
                        data: img.data.slice(0, 4 * img.width * img.height),
                        width: img.width,
                        height: img.height,
                    };
                }
            }

            const pngBuffer = new Buffer(responseArray);
            const pngArray = PNG.sync.read(pngBuffer);
            return {
                data: pngArray.data.slice(0, 4 * pngArray.width * pngArray.height),
                width: pngArray.width,
                height: pngArray.height,
            };
        };

        // `layer` is one entry of the cell-layer registry, or coreLayerView()
        // when no plugin has registered one -- it carries the gate, the colours
        // and the mode this pass draws with. Passed in rather than read off
        // `this` because several layers are rendered from the SAME decoded tile,
        // once each, and they must not see each other's state.
        const renderLabelTile = (tileArray, width, height, layer) => {
            const allowedIds = layer.filterIds;
            // Per-cell colour from the plugin owning this layer, or null for the
            // plain white layer this has always drawn. Read once per tile rather
            // than per pixel, and destructured so the inner loop does no
            // property lookups at all.
            const lut = layer.lut;
            const lutColors = lut?.colors || null;
            const lutMaxId = lutColors ? lut.maxId : 0;
            const lutMap = lutColors ? null : (lut?.map || null);
            // Filled draws the whole cell rather than its boundary. Only
            // possible where the labels are stored whole -- a pyramid that was
            // pre-reduced to outlines has no interior pixels to paint, so there
            // is nothing to fill and asking for it changes nothing.
            const fillMode = layer.mode === "filled"
                && this.config?.segmentationMode === "filled";
            // Datasources imported with "Outline cells while viewing" store the
            // labels whole rather than pre-reduced to boundaries (config
            // segmentationMode = "filled"), so the boundary is derived here --
            // once per tile at load, not per frame. Everything else arrives
            // already reduced, where every labelled pixel *is* an outline.
            // Filled skips the derivation entirely, which is also the cheaper
            // path: no neighbour unpacking and no eight-way test per pixel.
            const deriveOutlines = this.config?.segmentationMode === "filled" && !fillMode;
            const canvas = document.createElement("canvas");
            canvas.width = width;
            canvas.height = height;
            const context = canvas.getContext("2d");
            const imageData = context.createImageData(width, height);
            const output = imageData.data;

            // Unpacked up front so the neighbour lookups below are plain array
            // reads instead of re-decoding four bytes per tap.
            let ids = null;
            if (deriveOutlines) {
                ids = new Uint32Array(width * height);
                for (let p = 0; p < ids.length; p += 1) {
                    const i = p * 4;
                    ids[p] = tileArray[i]
                        + tileArray[i + 1] * 256
                        + tileArray[i + 2] * 65536
                        + tileArray[i + 3] * 16777216;
                }
            }

            for (let i = 0; i < tileArray.length; i += 4) {
                const cellId = ids
                    ? ids[i >> 2]
                    : tileArray[i]
                        + tileArray[i + 1] * 256
                        + tileArray[i + 2] * 65536
                        + tileArray[i + 3] * 16777216;
                if (!cellId || (allowedIds && !allowedIds.has(cellId))) continue;
                // White at the layer's long-standing alpha unless a plugin says
                // otherwise. Looked up BEFORE the boundary derivation below: a
                // hidden category or a cell with no value is skipped here, so it
                // never pays for the eight-neighbour test -- which is what makes
                // hiding most of a legend faster to draw, not slower.
                let red = 255;
                let green = 255;
                let blue = 255;
                let alpha = 220;
                if (lutColors) {
                    // Above maxId means the LUT simply does not describe this
                    // cell -- a segmentation object with no row in the table.
                    // Transparent, like every other kind of no-value.
                    if (cellId > lutMaxId) continue;
                    const offset = cellId * 4;
                    alpha = lutColors[offset + 3];
                    if (!alpha) continue;
                    red = lutColors[offset];
                    green = lutColors[offset + 1];
                    blue = lutColors[offset + 2];
                } else if (lutMap) {
                    const entry = lutMap.get(cellId);
                    if (!entry || !entry[3]) continue;
                    [red, green, blue, alpha] = entry;
                }
                if (ids) {
                    const p = i >> 2;
                    const x = p % width;
                    const y = (p - x) / width;
                    // Eight-neighbour, matching the "exact" method the offline
                    // pyramid writer uses (segmentation_pyramid.py). Four-
                    // neighbour leaves cells that meet only corner-to-corner
                    // sharing an unbroken block of white, which reads as one
                    // large cell; measured against a precomputed pyramid of the
                    // same mask it also drew ~28% fewer boundary pixels overall.
                    //
                    // Compared against raw ids, not the gated subset: a cell's
                    // edge against a filtered-out neighbour is still its edge.
                    // A tile's own border counts as "same" rather than as a
                    // boundary -- treating it as one would draw a bright grid
                    // along every tile seam. The cost is that a real boundary
                    // landing exactly on a seam loses that pixel; a cell simply
                    // continuing across the seam stays correct, which is the
                    // overwhelmingly common case.
                    const up = y > 0;
                    const down = y < height - 1;
                    const left = x > 0;
                    const right = x < width - 1;
                    const onBoundary =
                        (left && ids[p - 1] !== cellId)
                        || (right && ids[p + 1] !== cellId)
                        || (up && ids[p - width] !== cellId)
                        || (down && ids[p + width] !== cellId)
                        || (up && left && ids[p - width - 1] !== cellId)
                        || (up && right && ids[p - width + 1] !== cellId)
                        || (down && left && ids[p + width - 1] !== cellId)
                        || (down && right && ids[p + width + 1] !== cellId);
                    if (!onBoundary) continue;
                }
                output[i] = red;
                output[i + 1] = green;
                output[i + 2] = blue;
                output[i + 3] = alpha;
            }
            context.putImageData(imageData, 0, 0);
            return context;
        };
        this.renderLabelTile = renderLabelTile;
        // Decode workers. Capped at 4: decode is memory-bandwidth bound, and
        // more workers than that mostly adds ~3 MB of live intermediates each
        // without decoding faster.
        const decoderPool = TileDecoderPool.create(
            plexoraUrl("client/src/js/workers/tileDecoder.js"),
            Math.max(2, Math.min(4, navigator.hardwareConcurrency || 4)),
        );
        this.decoderPool = decoderPool;

        // Same algorithm as the worker, for when workers aren't available.
        const decodeWebpInline = async (responseArray) => {
            const blob = new Blob([responseArray], { type: "image/webp" });
            const bitmap = await createImageBitmap(blob, { premultiplyAlpha: "none", colorSpaceConversion: "none" });
            const { width, height } = bitmap; // capture before close() -- closing zeroes these
            const canvas = new OffscreenCanvas(width, height);
            const ctx = canvas.getContext("2d", { willReadFrequently: true });
            ctx.drawImage(bitmap, 0, 0);
            const rgba = ctx.getImageData(0, 0, width, height).data;
            bitmap.close();
            const array = new Uint8Array(width * height);
            for (let i = 0; i < array.length; i++) {
                array[i] = rgba[i * 4];
            }
            return { array, width, height };
        };


        // tile-loaded handling: decode the raw tile bytes ourselves. OpenSeadragon
        // 6.x exposes the underlying XHR on `e.tileRequest` (a stable, non-deprecated
        // property) with `response` as an ArrayBuffer of the raw compressed tile (OSD
        // uses responseType: "arraybuffer" for AJAX-loaded tiles), so no forked
        // ImageJob is needed to reach it. Registered directly as an async function:
        // OpenSeadragon awaits a handler's returned promise (raiseEventAwaiting) before
        // considering the tile loaded, so no explicit getCompletionCallback() is needed.
        const forceRepaint = this.forceRepaint.bind(this);
        const handleTileLoaded = async (e) => {
            const { source } = e.tiledImage;
            const { tileFormat } = source;
            try {
                const responseArray = e.tileRequest?.response;
                if (tileFormat == 32) {
                    e.tile._isLabel = true;
                    if (!e.tile?._array && responseArray) {
                        const decoded = decodeLabelTile(responseArray);
                        e.tile._array = decoded.data;
                        e.tile._format = "u32";
                        // Kept so a layer turned back on later can be rendered
                        // into this tile without re-deriving the shape from the
                        // canvas it no longer has.
                        e.tile._labelWidth = decoded.width;
                        e.tile._labelHeight = decoded.height;
                        this.renderTileLayers(e.tile);
                    }
                    if (e.tile?._layerContexts) {
                        return;
                    }
                }
                // Trigger loading of image
                const tileArgs = [e.tile.level, e.tile.x, e.tile.y];
                const tl = source.toTileLevels(...tileArgs);
                if (tl.relativeImageScale < 1) {
                    const tile = matchTile(e, tl.outputTile);
                    if (tile?._array && tile?._format) {
                        e.tile._format = tile._format;
                        e.tile._array = tile._array;
                    }
                    if (e.tile?._array) {
                        return;
                    }
                }
                else if (tileFormat == 32) {
                    return;
                }
                else if (responseArray) {
                    const sig = new Uint8Array(responseArray, 0, 4);
                    const isWebp = sig[0] === 0x52 && sig[1] === 0x49 && sig[2] === 0x46 && sig[3] === 0x46; // "RIFF"
                    if (isWebp) {
                        // Fast/default tile path: quantized 8-bit, single value
                        // per pixel. Decoded via createImageBitmap (not
                        // UPNG.js, which can't parse WebP) -- confirmed safe
                        // for this opaque/single-channel case by an in-browser
                        // spike; segmentation tiles never take this path (see
                        // decodeLabelTile) because the same decode approach
                        // was found to corrupt RGB wherever alpha=0.
                        //
                        // Preferably in a worker: the canvas readback and the
                        // per-pixel RGBA->R strip cost ~60% of a 15-channel pan
                        // when run on the main thread. The inline branch is the
                        // fallback when workers or OffscreenCanvas are absent.
                        let decoded;
                        try {
                            decoded = decoderPool
                                ? await decoderPool.decode(responseArray, "webp")
                                : await decodeWebpInline(responseArray);
                        } catch (workerErr) {
                            // A worker failure must never cost us the tile --
                            // OSD is awaiting this handler, and a rejection here
                            // would leave the tile permanently blank.
                            console.warn("Worker tile decode failed, falling back inline:", workerErr);
                            decoded = await decodeWebpInline(responseArray);
                        }
                        e.tile._array = decoded.array;
                        e.tile._format = "u8";
                    } else {
                        // HD (16-bit grayscale PNG) and segmentation tiles.
                        //
                        // The HD path goes to the worker, which inflates with the
                        // browser's native DecompressionStream rather than
                        // UPNG.js/pako. A CPU profile of a 7-channel HD pan put
                        // 81% of ALL time in pako's JS inflate. Segmentation
                        // tiles stay on UPNG here: they are RGBA8 rather than
                        // gray16, and there is only one label layer so the cost
                        // is not multiplied by the channel count.
                        let decoded = null;
                        if (decoderPool && tileFormat != 32) {
                            try {
                                decoded = await decoderPool.decode(responseArray, "gray16png");
                            } catch (workerErr) {
                                if (!workerErr.unsupported) {
                                    console.warn("Worker HD decode failed, falling back to UPNG:", workerErr);
                                }
                            }
                        }
                        if (decoded) {
                            e.tile._array = decoded.array;
                            e.tile._format = "u16";
                        } else {
                            const img = window.UPNG.decode(responseArray);
                            if (img.ctype == 0 && img.depth == 16) {
                                e.tile._array = img.data.slice(0, 2 * img.width * img.height);
                                e.tile._format = "u16";
                            } else if (img.ctype == 6 && img.depth == 8) {
                                e.tile._array = img.data.slice(0, 4 * img.width * img.height);
                                e.tile._format = "u32";
                            }
                        }
                    }
                }
            } catch (err) {
                console.log("Load Error, Refreshing", err, e.tile.getUrl());
                forceRepaint();
            }
        };


        // Disable canvas image smoothing once per drawer canvas, not once per
        // tile per frame. This used to live in a "tile-drawn" handler that also
        // ran _.size() over the entire shared tile cache and re-fetched the 2D
        // context for every tile of every channel on every frame -- O(tiles^2)
        // work per frame whose only lasting effect was these four flags. (The
        // _imagesLoadedCount it maintained is an OpenSeadragon 2.x field that
        // OSD 6 no longer reads.)
        const disableSmoothing = () => {
            const canvas = this.viewer?.drawer?.canvas;
            const context = canvas?.getContext?.("2d");
            if (!context) {
                return;
            }
            context.mozImageSmoothingEnabled = false;
            context.webkitImageSmoothingEnabled = false;
            context.msImageSmoothingEnabled = false;
            context.imageSmoothingEnabled = false;
        };
        this.viewer.addHandler("open", disableSmoothing);
        this.viewer.addHandler("resize", disableSmoothing);

        this.viewer.addHandler("tile-unloaded", (e) => {
            delete e.tile._array;
            // The per-layer canvases are the larger half of a label tile's
            // footprint -- one 4 MB RGBA canvas per visible layer against the
            // 4 MB decoded array -- and they used to be left behind here
            // entirely, so panning a slide with the mask on grew the heap for
            // as long as the session lasted.
            e.tile._layerContexts?.clear();
            delete e.tile._layerContexts;
        });

        // GL initialization: on 'open', size the GL canvas, compile shaders,
        // then wire the real tile-loaded/tile-drawing handlers and force
        // existing items to redraw. viewerManager.js manually re-raises 'open'
        // after adding the label tiled image, so this runs more than once by
        // design.
        const initGL = () => {
            renderer.width = renderer.width || this.config.tileWidth;
            renderer.height = renderer.height || this.config.tileHeight;
            renderer.updateShape(renderer.width, renderer.height);
            renderer.init().then(() => {
                this.viewer.addHandler("tile-loaded", handleTileLoaded);
                this.viewer.addHandler("tile-drawing", (e) => tileDrawingCustom(tileDrawingDefault, e));

                const world = this.viewer.world;
                for (let i = 0; i < world.getItemCount(); i++) {
                    world.getItemAt(i)._needsDraw = true;
                }
                world.update();
            });
        };
        this.viewer.addHandler("open", initGL);

        // No tile-cache monitor here on purpose. OpenSeadragon 6 evicts against
        // maxImageCacheCount itself (TileCache._freeOldRecordRoutine) and does
        // it correctly; the interval that used to live here summed
        // item._tileCache._tilesLoaded.length across every item, but OSD hands
        // the SAME TileCache instance to every TiledImage, so the total was
        // multiplied by the channel count and the threshold tripped at roughly
        // 1000/N real tiles -- then called an evictor that freed nothing and
        // corrupted OSD's LRU bookkeeping (see clearTileCache).

        this.viewer.scalebar({
            location: OpenSeadragon.ScalebarLocation.BOTTOM_RIGHT,
            minWidth: "100px",
            type: OpenSeadragon.ScalebarType.MICROSCOPY,
            stayInsideImage: true,
            pixelsPerMeter: this.getPixelsPerMeter(),
            fontColor: "rgb(255, 255, 255)",
            color: "rgb(255, 255, 255)",
            backgroundColor: "rgba(0, 0, 0, 0.45)",
            barThickness: 3,
        });
        this.styleScaleBar();

        // Add event mouse handler (cell selection)
        this.viewer.addHandler("canvas-nonprimary-press", (e) => {
            // Right click (cell selection)
            if (event.button === 2) {
                const { numericData } = this;
                const { source } = e.eventSource;
                const tiledImage = this.viewer.world.getItemAt(0);
                const imageCoords = source.getImagePixel(tiledImage, e.position);
                return numericData.getNearestCell(...imageCoords).then((item) => {
                    if (item !== null && item !== undefined) {
                        // Check if user is doing multi-selection or not
                        let clearPriors = true;
                        if (e.originalEvent.ctrlKey) {
                            clearPriors = false;
                        }
                        // Trigger event
                        const imageClick = ImageViewer.events.imageClickedMultiSel;
                        this.eventHandler.trigger(imageClick, { item, clearPriors });
                    }
                });
            }
        });

        let that = this;

        let primaryTracker = new OpenSeadragon.MouseTracker({
            element: that.viewer.canvas,
            nonPrimaryReleaseHandler(event) {
                if (that.selectButton.classList.contains('selected') && !that.lassoing) {
                    const webPoint = event.position;
                    // Convert that to viewport coordinates, the lingua franca of OpenSeadragon coordinates.
                    const viewportPoint = that.viewer.viewport.pointFromPixel(webPoint);
                    // Convert from viewport coordinates to image coordinates.
                    let imagePoint = that.viewer.world.getItemAt(0).viewportToImageCoordinates(viewportPoint);
                    const zoomScale = 2 ** config.extraZoomLevels;
                    imagePoint = { x: imagePoint.x / zoomScale, y: imagePoint.y / zoomScale }
                    return that.dataLayer.getNearestCell(imagePoint.x, imagePoint.y)
                        .then(selectedItem => {
                            if (selectedItem !== null && selectedItem !== undefined) {
                                // Check if user is doing multi-selection or not
                                let clearPriors = true;
                                if (event.originalEvent.ctrlKey) {
                                    clearPriors = false;
                                }
                                // Trigger event
                                that.eventHandler.trigger(ImageViewer.events.imageClickedMultiSel, {
                                    selectedItem,
                                    clearPriors
                                });
                            }
                        })
                }
            }
        })

        this.canvasOverlay = new OpenSeadragon.CanvasOverlayHd(this.viewer, {
            onRedraw: function (opts) {
                const context = opts.context;
                //area selection polygon
                if (that.selectionPolygonToDraw && that.selectionPolygonToDraw.length > 0) {
                    var d = that.selectionPolygonToDraw;
                    context.globalAlpha = 0.7;
                    context.strokeStyle = 'orange';
                    context.lineWidth = 10;
                    context.beginPath();
                    d.forEach(function (xVal, i) {
                        if (i === 0) {
                            context.moveTo(d[i].x, d[i].y);
                        } else {
                            context.lineTo(d[i].x, d[i].y);
                        }
                    });
                    context.closePath();
                    context.stroke();
                    // context.globalAlpha = 1.0;
                }
                if (that.shouldDrawCentroids()) {
                    that.drawCentroids(context, opts.zoom);
                }
            },
        });
        this.viewer.addHandler("animation", () => this.scheduleCentroidTileUpdate());
        this.viewer.addHandler("animation-finish", () => this.scheduleCentroidTileUpdate(0));
        this.viewer.addHandler("resize", () => this.scheduleCentroidTileUpdate(0));
        this.viewer.addHandler("open", () => this.scheduleCentroidTileUpdate(0));
    }

    /**
     * @function init - initializes OSD channel and selection-provider options
     * @param viewerManager - Viewer Manager Instance
     * @param channelList - ChannelList instance
     * @param cellLayer - optional plugin instance implementing
     *   { getSelectedIds(filter), supportsColorCoding(), getColorCodedRanges() }.
     *   Claims the cell layer if given; null for a plain viewer.
     * @param centers - List of image pixel coordinates per cell
     * @param ids - List of integer ids per cell
     */
    async init(viewerManager, channelList, cellLayer, centers, ids) {
        this.channelList = channelList;
        if (cellLayer) {
            this.registerCellLayer(cellLayer.pluginName || "unknown", cellLayer);
        }
        this.centers = centers || [];
        this.ids = ids || [];
        // Instantiate viewer managers
        this.viewerManagerVMain = viewerManager;
        this.viewerManagers.push(this.viewerManagerVMain);
        this.setLoading(true);
        try {
            if (!this.noLabel) {
                await this.waitForGLReady();
            }
            const renderer = this.glRenderer;
            renderer.texture_mag = [renderer.gl.createTexture(), renderer.gl.createTexture(), renderer.gl.createTexture(), renderer.gl.createTexture()];
            renderer.texture_ids = renderer.gl.createTexture();
            renderer.texture_mask = renderer.gl.createTexture();
            renderer.texture_ranges = renderer.gl.createTexture();
            renderer.texture_centers = renderer.gl.createTexture();
            renderer.texture_pickings = renderer.gl.createTexture();
            this.bindPickings(renderer, []);
            this.ready = true;
            if (ids.length && centers.length) {
                this.bindSegmentationBuffers(ids, centers);
                this.clearTileCache(true);
                await this.forceRepaint();
            }
        } finally {
            this.setLoading(false);
        }

    }

    waitForGLReady(timeoutMs = 5000) {
        return Promise.race([
            this.glReady,
            new Promise((resolve) => setTimeout(resolve, timeoutMs)),
        ]);
    }


    /**
     * The provider of the ACTIVE cell layer, or null.
     *
     * "Active" is the layer the shared controls, picking and gating act on --
     * one at a time, or none. Every caller that asks "who holds the cell layer"
     * means this one; the other layers are still on screen, they just are not
     * the thing being worked on.
     */
    get cellLayer() {
        return this._cellLayers.get(this._activeCellLayer)?.provider || null;
    }

    /**
     * The name of the active cell layer's plugin, or null.
     */
    get cellLayerOwner() {
        return this._activeCellLayer;
    }

    /** One layer's record by name, or null. */
    getCellLayer(name) {
        return this._cellLayers.get(name) || null;
    }

    /** Every registered layer, bottom of the stack first. */
    cellLayers() {
        return this._cellLayerOrder
            .map((name) => this._cellLayers.get(name))
            .filter(Boolean);
    }

    /**
     * The layer core draws with when NO plugin has registered one.
     *
     * A stand-in with the same shape as a real record, so the renderers below
     * take one code path rather than branching on "is anything registered" at
     * every read. Mutated in place rather than rebuilt: this is called once per
     * label tile per frame.
     */
    coreLayerView() {
        const view = this._coreLayerView;
        view.mode = this.cellDisplayMode;
        view.filterIds = this.segmentationFilterIds;
        return view;
    }

    /**
     * @function maskDrawList - which layers the label tiles draw, bottom first.
     *
     * The returned order IS the z-order: tileDrawingCustom blits them in this
     * sequence, so the last one wins wherever they overlap.
     *
     * With nothing registered this is core's own layer, which is what keeps a
     * plain viewer -- and one running only Thresholding -- pixel-identical to
     * what it drew before layers existed. Once a plugin registers, core's plain
     * white layer steps aside entirely: turning every plugin layer off then
     * draws nothing, rather than falling back to white outlines the user did
     * not ask for.
     */
    maskDrawList() {
        if (!this._cellLayers.size) {
            return [this.coreLayerView()];
        }
        const list = [];
        for (const name of this._cellLayerOrder) {
            const layer = this._cellLayers.get(name);
            if (layer?.visible && ImageViewer.MASK_MODES.includes(layer.mode)) {
                list.push(layer);
            }
        }
        return list;
    }

    /**
     * @function centroidDrawList - which layers the point overlay draws.
     *
     * Same ordering rule, but a DIFFERENT stack: points are drawn onto core's
     * overlay canvas, which sits above every label tile whatever the sidebar
     * says. So dragging reorders points among themselves and masks among
     * themselves, and a centroid layer is always over a mask layer.
     */
    centroidDrawList() {
        if (!this._cellLayers.size) {
            return [this.coreLayerView()];
        }
        return this._cellLayerOrder
            .map((name) => this._cellLayers.get(name))
            .filter((layer) => layer?.visible && layer.mode === "centroids");
    }

    /**
     * @function registerCellLayer - add (or re-adopt) a plugin's cell layer.
     *
     * Registering the SAME name again keeps everything the layer already holds
     * -- colours, gate, mode, opacity -- which is what makes switching a tool
     * away and back instant rather than a reload. Only unregisterCellLayer
     * throws state away.
     *
     * A new layer goes on TOP of the stack: a tool the user just opened is the
     * one they are looking at.
     *
     * @param name - the plugin's name
     * @param provider - object implementing the selection-provider shape:
     *   { getSelectedIds(filter), supportsColorCoding(), getColorCodedRanges(),
     *     eval_mode }
     * @param options - { mode, opacity, visible, makeActive, supportedModes }
     * @returns the layer record
     */
    registerCellLayer(name, provider, options = {}) {
        if (!name) return null;
        let layer = this._cellLayers.get(name);
        if (!layer) {
            layer = {
                name,
                provider: provider || null,
                // Per-cell colour, or null for the plain white cell layer.
                //
                // A lookup table rather than per-cell geometry on purpose:
                // changing a palette, hiding a category or moving a continuous
                // range then recolours without refetching anything or rebuilding
                // a single boundary. See renderLabelTile.
                //
                // Shape: { colors: Uint8Array of 4*(maxId+1) RGBA bytes, maxId }
                // dense, or { map: Map<cellId, [r,g,b,a]> } when the ids are too
                // sparse for that to be worth allocating. Alpha 0 means "do not
                // draw this cell" -- it is how both "hidden category" and "no
                // value for this cell" arrive, so neither needs its own channel.
                lut: null,
                //: "none" | "centroids" | "outlines" | "filled". Starts at none:
                //: nothing is drawn over the image until something asks, and the
                //: asking is viewerControls.enableCellLayer.
                mode: "none",
                //: Set once the user picks a mode for this layer themselves, so
                //: a plugin restoring a stored preference can tell "not chosen
                //: yet" from "chosen, and the choice was the same".
                userMode: null,
                //: Which of the four the plugin can actually draw, or null for
                //: "whatever the project can". The shared Cells control offers
                //: the intersection.
                supportedModes: null,
                opacity: ImageViewer.DEFAULT_CELL_LAYER_OPACITY,
                visible: true,
                //: This layer's own gate. Per layer so one tool's selection
                //: cannot subtract cells from another tool's colours.
                filterIds: null,
                filterRequest: 0,
                //: Distinct fill strings, memoized so the centroid path does not
                //: build a colour string per point per frame. Keyed on the packed
                //: RGB, so it is bounded by the number of colours in use rather
                //: than the cell count.
                styleCache: new Map(),
            };
            this._cellLayers.set(name, layer);
            this._cellLayerOrder.push(name);
        } else if (provider) {
            layer.provider = provider;
        }
        if (options.supportedModes) layer.supportedModes = [...options.supportedModes];
        if (options.mode) layer.mode = options.mode;
        if (options.opacity !== undefined) this.setLayerOpacity(name, options.opacity);
        if (options.visible !== undefined) layer.visible = Boolean(options.visible);
        if (options.makeActive !== false) this.setActiveCellLayer(name);
        return layer;
    }

    /**
     * @function unregisterCellLayer - drop a layer and everything it held.
     *
     * The teardown counterpart of register: colours, gate and per-tile canvases
     * all go. A no-op for a name that is not registered, so a plugin shutting
     * down cannot clear a layer someone else owns.
     */
    unregisterCellLayer(name) {
        if (!this._cellLayers.has(name)) return false;
        this._cellLayers.delete(name);
        this._cellLayerOrder = this._cellLayerOrder.filter((entry) => entry !== name);
        if (this._activeCellLayer === name) {
            // The topmost survivor takes over rather than leaving nothing
            // active, so removing the tool being looked at does not strand the
            // shared controls while other layers are still on screen.
            this._activeCellLayer = this._cellLayerOrder.length
                ? this._cellLayerOrder[this._cellLayerOrder.length - 1]
                : null;
        }
        this.applyCellColor();
        return true;
    }

    /**
     * @function setActiveCellLayer - which layer the shared controls act on.
     *
     * Pixels do not change: every layer that was visible stays visible. This is
     * about where the Cells control, the opacity slider, picking and the gate
     * flows point.
     *
     * @returns the displaced layer's name, or null
     */
    setActiveCellLayer(name) {
        const next = name && this._cellLayers.has(name) ? name : null;
        const previous = this._activeCellLayer;
        if (previous === next) return null;
        this._activeCellLayer = next;
        return previous;
    }

    /**
     * @function setCellLayerVisible - draw this layer, or stop drawing it.
     *
     * Hiding DROPS the layer's per-tile canvases and KEEPS its lookup table.
     * That split is what makes a loaded-but-hidden plugin cheap enough that
     * there is no need to cap how many may be loaded: the canvases are ~4 MB
     * per label tile in view and rebuild in a few milliseconds from the decoded
     * array that is still on the tile, while the table is four bytes per cell
     * and is the expensive thing to recompute (a request, or a pass over a
     * column). A hidden layer also stops growing as the user pans, because it
     * renders nothing into newly loaded tiles.
     */
    setCellLayerVisible(name, visible) {
        const layer = this._cellLayers.get(name);
        if (!layer) return false;
        const next = Boolean(visible);
        if (layer.visible === next) return false;
        layer.visible = next;
        if (next) {
            this.rerenderSegmentationTiles(name);
        } else {
            this.dropLayerContexts(name);
        }
        this.viewer?.forceRedraw?.();
        return true;
    }

    /**
     * @function setCellLayerMode - how one layer draws its cells.
     *
     * "none" | "centroids" | "outlines" | "filled". Only the transitions that
     * change what the tile canvases HOLD cost a re-render: moving between
     * outlines and filled, and moving in or out of the mask stack at all.
     * Everything else is a redraw.
     */
    setCellLayerMode(name, mode) {
        const layer = this._cellLayers.get(name);
        if (!layer) return false;
        const next = mode || "none";
        layer.userMode = next;
        if (next === layer.mode) return false;
        const wasMask = ImageViewer.MASK_MODES.includes(layer.mode);
        const nowMask = ImageViewer.MASK_MODES.includes(next);
        const filledChanged = layer.mode === "filled" || next === "filled";
        layer.mode = next;
        if (wasMask !== nowMask || (nowMask && filledChanged)) {
            this.applyCellColor(nowMask ? name : null);
        } else {
            this.viewer?.forceRedraw?.();
        }
        return true;
    }

    /**
     * @function setLayerOpacity - how strongly one layer sits over what is
     * under it.
     *
     * Composite-time, so this is a redraw and never a re-render: the tile
     * canvases already hold the right pixels and only the blit's alpha changes.
     * That is what makes dragging the slider smooth on a mask with thousands of
     * cells in view.
     */
    setLayerOpacity(name, value) {
        const layer = this._cellLayers.get(name);
        if (!layer) return false;
        const next = Math.max(0, Math.min(1, Number(value)));
        if (!Number.isFinite(next) || next === layer.opacity) return false;
        layer.opacity = next;
        this.viewer?.forceRedraw?.();
        return true;
    }

    /**
     * @function setCellLayerOrder - restack the layers, bottom first.
     *
     * Composite order only, so this costs one redraw however many cells are on
     * screen -- which is what lets the sidebar cards be dragged live. Names that
     * are not registered are ignored, and registered names the caller did not
     * mention keep their relative places underneath, so a partial order can
     * never drop a layer off the stack.
     */
    setCellLayerOrder(names) {
        const wanted = (names || []).filter((name) => this._cellLayers.has(name));
        const mentioned = new Set(wanted);
        const rest = this._cellLayerOrder.filter((name) => !mentioned.has(name));
        const next = [...rest, ...wanted];
        const same = next.length === this._cellLayerOrder.length
            && next.every((name, index) => name === this._cellLayerOrder[index]);
        if (same) return false;
        this._cellLayerOrder = next;
        this.viewer?.forceRedraw?.();
        return true;
    }

    /**
     * @function setCellColorLUT - colour one layer's cells by id.
     *
     * Gated on the layer existing, and silently so: a plugin that has been
     * removed may still have an in-flight request whose response lands
     * afterwards, and applying it would repaint cells for a tool that is no
     * longer there. Returning false lets the caller notice; ignoring the return
     * value is also correct, because doing nothing IS the right outcome.
     *
     * Note what is NOT checked: whether this layer is the active one. A tool the
     * user has switched away from but left visible goes on showing its own
     * colours, and goes on being allowed to update them.
     *
     * @param name - the layer's plugin name
     * @param lut - { colors: Uint8Array, maxId } | { map: Map } | null to clear
     * @returns whether the LUT was applied
     */
    setCellColorLUT(name, lut) {
        const layer = this._cellLayers.get(name);
        if (!layer) return false;
        layer.lut = lut || null;
        layer.styleCache = new Map();
        this.applyCellColor(name);
        return true;
    }

    /**
     * @function setCellDisplayMode - core's own mode, for a viewer with no
     * plugin layers.
     *
     * Only "filled" changes what renderLabelTile produces, so only that
     * transition costs a re-render; switching between centroids and outlines is
     * a matter of which layer is showing, which viewerControls handles.
     */
    setCellDisplayMode(mode) {
        const next = mode || "outlines";
        if (next === this.cellDisplayMode) return false;
        const wasFilled = this.cellDisplayMode === "filled";
        this.cellDisplayMode = next;
        if (wasFilled || next === "filled") {
            this.applyCellColor();
        }
        return true;
    }

    /**
     * @function layerAlpha - the alpha one layer composites at.
     *
     * 1 whenever the layer has no colours. The opacity control belongs to the
     * plugin that owns the colours, so a plain viewer -- or one running only
     * Thresholding -- must not inherit its default and start drawing dimmer
     * outlines than it did before any of this existed.
     */
    layerAlpha(layer) {
        return layer?.lut ? layer.opacity : 1;
    }

    /**
     * @function cellColorStyle - a canvas fill for one cell, or null to skip it.
     *
     * Alpha is treated as a yes/no here rather than blended in: the centroid
     * layer already carries its own globalAlpha, and stacking a second per-point
     * alpha on top of it makes a hidden cell "mostly invisible" instead of
     * absent. Strings are memoized because this runs per point per frame.
     */
    cellColorStyle(cellId, layer) {
        const lut = layer?.lut;
        if (!lut) return null;
        let r;
        let g;
        let b;
        if (lut.colors) {
            if (cellId > lut.maxId) return null;
            const offset = cellId * 4;
            if (!lut.colors[offset + 3]) return null;
            r = lut.colors[offset];
            g = lut.colors[offset + 1];
            b = lut.colors[offset + 2];
        } else {
            const entry = lut.map?.get(cellId);
            if (!entry || !entry[3]) return null;
            [r, g, b] = entry;
        }
        const key = (r << 16) | (g << 8) | b;
        let style = layer.styleCache.get(key);
        if (style === undefined) {
            style = `rgb(${r},${g},${b})`;
            layer.styleCache.set(key, style);
        }
        return style;
    }

    /**
     * @function applyCellColor - redraw the cell layer after a colour change.
     *
     * Re-renders the label tiles (their pixels carry the colours) and forces one
     * viewer redraw, which is also what repaints the centroid overlay. Geometry
     * is untouched: nothing is refetched and no boundary is re-derived for a
     * tile whose ids have not changed.
     *
     * @param name - re-render only this layer's canvases, leaving the rest of
     *   the stack alone. That is what makes a gate edit over a phenotype map
     *   cost the gate layer and nothing else.
     */
    applyCellColor(name = null) {
        this.rerenderSegmentationTiles(name);
        this.viewer?.forceRedraw?.();
    }

    /**
     * @function indexOfTexture - return the texture unit for a named texture
     * @param label - the texture key label
     * @param scope - "M" for a marker magnitude texture, null for a constant
     * @returns number
     *
     * Tile textures are deliberately absent here: they are not assigned a unit
     * each (the shader's u_tile sampler is pinned to unit 0), they are keyed by
     * content in GLTileTextureCache instead.
     */
    indexOfTexture(label, scope = null) {
        const renderer = this.glRenderer;
        // magnitudes
        if (scope == "M") {
            const index = renderer._markerTextures.indexOf(label);
            if (index > -1) {
                return index;
            }
            const newIndex = renderer._nextMarkerTexture;
            const maximum = renderer._markerTextures.length;
            renderer._nextMarkerTexture = (newIndex + 1) % maximum;
            renderer._markerTextures[newIndex] = label;
            return newIndex + renderer._markerOffset;
        }
        // other
        const index = renderer._constantTextures.indexOf(label);
        if (index > -1) {
            return index + renderer._otherOffset;
        }
        return -1;
    }


    /**
     * @function findMarkerTexture - Check if texture label is active.
     * @param label - marker texture label
     * @returns number
     */
    findMarkerTexture(label) {
        const renderer = this.glRenderer;
        return renderer._markerTextures.indexOf(label);
    }

    /**
     * @function findCurrentChannel - Return given channel for partial url
     * @param sub_url - partial url
     * @returns - current channel
     */
    findCurrentChannel(sub_url) {
        const channels = Object.values(this.currentChannels);
        return channels.find((e) => e.sub_url == sub_url);
    }

    /**
     * Flag for webGL rendering.
     *
     * @type {boolean}
     */
    get ready() {
        return this._ready || false;
    }

    set ready(bool) {
        this._ready = bool;
        this.viewerManagers.forEach(({ viewer }) => {
            viewer.world._needsDraw = bool;
        });
    }

    /**
     * Flags for mode of webGL rendering.
     *
     * @typedef {object} ModeFlags
     * @property {boolean} edge - render outlines
     * @property {boolean} or - render pie charts
     * @type {ModeFlags}
     */
    get modeFlags() {
        return {
            edge: !!this.viewerManagerVMain?.sel_outlines,
            or: this.cellLayer?.supportsColorCoding?.() ? this.cellLayer.eval_mode == "or" : false,
        };
    }

    /**
     * Color-coded (multi-range gate) keys for webGL rendering. Empty unless
     * the active selection provider supports color coding (see the scope
     * note on colorCodedRanges below) -- kept opt-in by design, not a
     * generalized concept every plugin needs to implement.
     *
     * @type {Array}
     */
    get colorCodedKeys() {
        const keys = Object.keys(this.colorCodedRanges);
        return [...keys.sort()];
    }

    /**
     * Color-coded (multi-range gate) selections. This is deliberately not a
     * generalized "selection" concept -- it's the cell-layer owner's per-channel
     * threshold-range rendering path (u_cell_range_shape/texture_ranges), only
     * ever populated when the active provider opts in via supportsColorCoding().
     *
     * @type {Array}
     */
    get colorCodedRanges() {
        if (!this.cellLayer?.supportsColorCoding?.()) {
            return {};
        }
        return this.cellLayer.getColorCodedRanges?.() || {};
    }

    /**
     * Channel selections.
     *
     * @type {Array}
     */
    get currentChannels() {
        return this.channelList.currentChannels || {};
    }

    /**
     * @function toCacheKey - generate cache keys of gl properties
     * @param keys - active marker channels
     * @param markerLists - data for each marker
     * @returns string
     */
    toCacheKey(keys, markerLists) {
        const precisions = [2 ** 25, 2 ** 25, 255, 255, 255];
        const tuples = keys.map((channel, i) => {
            const idx = 1 + this.selectMaskIndex(channel);
            const keyData = markerLists[i] || [];
            const hashes = keyData.map((r, j) => {
                // use precision for each item
                const integral = r * precisions[j];
                return parseInt(integral).toString(36);
            });
            return [idx, ...hashes].join("-");
        });
        return tuples.join("-");
    }

    /**
     * Wrapper to set/get pickedIds
     *
     * @type {string}
     */

    get pickedIds() {
        return this._picking;
    }

    set pickedIds(key) {
        this._picking = key;
        this.pickingChanged = true;
    }

    /**
     * Cache key for the per-cell range webGL buffer.
     *
     * @type {string}
     */

    get selectionCacheKey() {
        return this._cacheKeys.selection;
    }

    set selectionCacheKey(key) {
        this._cacheKeys.selection = key;
    }

    /**
     * Cache key for most webGL buffers.
     *
     * @type {string}
     */

    get markerCacheKey() {
        return this._cacheKeys.main;
    }

    set markerCacheKey(key) {
        this._cacheKeys.main = key;
    }

    /**
     * @function loadBuffers - loads segmentation mask data to WebGL
     */
    async loadBuffers() {
        const keys = this.colorCodedKeys;
        const rangeLists = this.selectRanges(keys);
        const changes = this.updateCache(keys, rangeLists);
        const { markersChanged, rangesChanged } = changes;

        // Bind picked ids 
        if (this.pickingChanged || rangesChanged) {
            this.bindPickings(this.glRenderer, this.pickedIds);
            this.pickingChanged = false;
        }
        // Bind buffers per-channel
        if (rangesChanged) {
            const ranges = [];
            for (const rangeTuple of rangeLists) {
                for (const value of rangeTuple) {
                    ranges.push(value);
                }
            }
            this.bindRanges(this.glRenderer, ranges, 5);
        }
        // Bind or-mode buffers per-cell
        if (markersChanged) {
            const newKeys = keys.filter((k) => {
                return this.findMarkerTexture(k) == -1;
            });
            const m = await this.numericData.getAllFloat32Entries(newKeys);
            const nNew = newKeys.length;
            // Deinterleave in one linear pass instead of one full-array
            // .filter() per key (was O(nNew^2 * cellCount)).
            const perKey = newKeys.map(() => new Float32Array(m.length / nNew));
            for (let i = 0, o = 0; i < m.length; i += nNew, o++) {
                for (let ki = 0; ki < nNew; ki++) {
                    perKey[ki][o] = m[i + ki];
                }
            }
            newKeys.forEach((k, ki) => {
                const mk = perKey[ki];
                // Attempt to bind marker magnitude texture
                try {
                    this.bindMagnitudes(this.glRenderer, mk, k);
                } catch (e) {
                    if (e instanceof TypeError) {
                        console.warn(`Unable to bind ${k} marker texture.`);
                    } else {
                        throw e;
                    }
                }
            });
        }
    }

    /**
     * @function selectCenterProps - return cell centers properties
     * @param tile - openseadragon tile
     * @param source - openseadragon tile source
     * @typedef {object} CenterProps
     * @property {number} pie_radius_1f - radius of or-mode circles
     * @property {number} magnitude_2iv - the shape of each magnitude array
     * @property {number} id_end_1i - the last id in list of ids
     * @property {number} picked_end_1i - the last picked id index
     * @property {number} marker_sample_4iv - indices of magnitudes
     * @property {number} modes_2i - the currently active mode flags
     * @property {number} tile_fraction_1f - subtile fraction <=1
     * @property {number} tile_scale_1f - image tile scale >=1
     * @property {Array} x_bounds_2fv - subtile start/end in x
     * @property {Array} y_bounds_2fv - subtile start/end in y
     * @property {Array} origin_2fv - origin at texture resolution
     * @returns CenterProps
     */
    selectCenterProps(tile, source) {
        const renderer = this.glRenderer;
        const modes = this.modeFlags;
        const w = this.config.tileWidth;
        const h = this.config.tileHeight;
        const lastId = (this.idCount || 0) - 1;
        const lastPick = this.pickedIds.length - 1;
        const tileArgs = [tile.level, tile.x, tile.y];
        const tl = source.toTileLevels(...tileArgs);
        const { outputTile, relativeImageScale } = tl;
        const origin = [outputTile.x * w, outputTile.y * h];
        const bounds = source.toMagnifiedBounds(...tileArgs);
        // Assume uniform shape of all magnitude buffers
        const magnitude_2iv = this.toTextureShape(renderer.gl, this.idCount);
        const markerSamples = [0, 1, 2, 3].map((i) => {
            const label = renderer._markerTextures[i];
            return this.colorCodedKeys.indexOf(label);
        });
        return {
            pie_radius_1f: 8.5,
            magnitude_2iv: magnitude_2iv,
            id_end_1i: Math.max(lastId, 0),
            picked_end_1i: Math.max(lastPick, -1),
            modes_2i: [modes.edge, modes.or],
            key_end_1i: this.colorCodedKeys.length,
            marker_sample_4iv: markerSamples,
            tile_scale_1f: Math.max(relativeImageScale, 1.0),
            tile_fraction_1f: Math.min(relativeImageScale, 1.0),
            x_bounds_2fv: new Float32Array(bounds.x),
            y_bounds_2fv: new Float32Array(bounds.y),
            origin_2fv: new Float32Array(origin),
        };
    }

    /**
     * @function selectRanges - per-marker threshold ranges for the shader
     * @param keys - active marker channels
     * @returns - lists of min, max, r, g, b values
     */
    selectRanges(keys) {
        const rangeLists = [];
        const selections = this.colorCodedRanges;
        for (const key of keys) {
            const range = selections[key].map((x) => parseFloat(x));
            const color = this.selectMaskColor(key);
            const floatColor = toFloatColor(color);
            const rangeTuple = range.concat(floatColor);
            rangeLists.push(rangeTuple);
        }

        return rangeLists;
    }

    /**
     * @function updateCache - update cache keys
     * @param keys - active marker channels
     * @param rangeLists - lists of min, max, r, g, b values
     * @typedef {object} Changes
     * @property {boolean} markersChanged - if marker lists have changed
     * @property {boolean} rangesChanged - if the range table changed
     * @returns Changes
     */
    updateCache(keys, rangeLists) {
        const markerCacheKey = this.toCacheKey(keys, []);
        const markersChanged = this.markerCacheKey !== markerCacheKey;
        if (markersChanged) {
            this.markerCacheKey = markerCacheKey;
        }
        const selectionCacheKey = this.toCacheKey(keys, rangeLists);
        const rangesChanged = this.selectionCacheKey !== selectionCacheKey;
        if (rangesChanged) {
            this.selectionCacheKey = selectionCacheKey;
        }
        return { markersChanged, rangesChanged };
    }

    /**
     * @function selectMaskColor - select color for mask
     * @param channel - the channel label
     * @typedef {object} Color
     * @property {number} r - 0-255
     * @property {number} g - 0-255
     * @property {number} b - 0-255
     * @returns Color
     */
    selectMaskColor(channel) {
        const white = {
            r: 255,
            g: 255,
            b: 255,
        };
        if (!channel) {
            return white;
        }
        const channels = this.currentChannels;
        const idxString = (this.selectMaskIndex(channel) + 1).toString();
        if (idxString == "0" || !Object.keys(channels).includes(idxString)) {
            return white;
        }
        const data = channels[idxString];
        return data.color;
    }

    /**
     * @function selectMaskIndex - select index for mask
     * @param channel - the channel label
     * @returns number
     */
    selectMaskIndex(channel) {
        const columns = this.channelList?.columns || [];
        return columns.indexOf(channel);
    }

    /**
     * @function selectTexture - activate a WebGL texture
     * @param gl - the WebGL2 context
     * @param texture - the WebGL2 texture
     * @param idx - the texture index
     */
    selectTexture(gl, texture, idx) {
        if (texture === undefined) {
            throw new TypeError(`Cannot bind undefined to texture ${idx}.`);
        }
        // Set texture for GLSL
        gl.activeTexture(gl["TEXTURE" + idx]);
        gl.bindTexture(gl.TEXTURE_2D, texture);
        gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, 1);
        gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);

        // Assign texture parameters
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    }

    /**
     * @function toTextureShape - shape of texture data
     * @param gl - the WebGL2 context
     * @param length - the 1D length of the data
     * @returns Array
     */
    toTextureShape(gl, length) {
        // MAX_TEXTURE_SIZE is constant for the context, but gl.getParameter is a
        // synchronous round trip to the driver that stalls the GL pipeline --
        // and selectCenterProps() calls this once per tile per frame, so it was
        // running tens of times a frame purely to re-read a constant.
        if (this._maxTextureSize === undefined) {
            this._maxTextureSize = gl.getParameter(gl.MAX_TEXTURE_SIZE);
        }
        const width = this._maxTextureSize;
        const height = Math.max(1, Math.ceil(length / width));
        return [width, height];
    }

    /**
     * @function packFloat32 - pack Float32 Texture
     * @param a - the texture data as an array
     * @param width - the texture width
     * @param height - the texture height
     * @returns array
     */
    packFloat32(a, width, height) {
        // Create 2D array of pixels
        const full_size = width * height;
        const arr = new ArrayBuffer(4 * full_size);
        const view = new DataView(arr);
        for (const i in a) {
            view.setFloat32(4 * i, a[i], true);
        }
        return new Float32Array(arr);
    }

    /**
     * @function packUint32 - pack Uint32 Texture
     * @param a - the texture data as an array
     * @param width - the texture width
     * @param height - the texture height
     * @returns array
     */
    packUint32(a, width, height) {
        // Create 2D array of pixels
        const full_size = width * height;
        const arr = new ArrayBuffer(4 * full_size);
        const view = new DataView(arr);
        for (const i in a) {
            view.setUint32(4 * i, a[i], true);
        }
        return new Uint8Array(arr);
    }

    /**
     * @function setIntegerTexture - set an integer texture
     * @param gl - the WebGL2 context
     * @param idx - texture index
     * @param texture - the WebGL2 texture
     * @param values - the texture data as 2d array
     */
    setIntegerTexture(gl, idx, texture, values) {
        const [width, height] = this.toTextureShape(gl, values.length);
        const pixels = this.packUint32(values, width, height);
        // Set texture for GLSL
        this.selectTexture(gl, texture, idx);
        // Send an empty array to the texture
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8UI, width, height, 0, gl.RGBA_INTEGER, gl.UNSIGNED_BYTE, pixels);
    }

    /**
     * @function setFloatTexture - set a floating point texture
     * @param gl - the WebGL2 context
     * @param idx - texture index
     * @param texture - the WebGL2 texture
     * @param values - the texture data as 2d array
     * @param width - the texture width
     * @param height - the texture height
     */
    setFloatTexture(gl, idx, texture, values, width, height) {
        this.selectTexture(gl, texture, idx);
        const pixels = this.packFloat32(values, width, height);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.R32F, width, height, 0, gl.RED, gl.FLOAT, pixels);
    }

    /**
     * @function bindLabels - bind segmentation mask ids
     * @param renderer - the GL renderer
     * @param values - the texture data as 2d array
     */
    bindLabels(renderer, values) {
        // Add id mask map
        const idx = this.indexOfTexture("ids", null);
        const ids_2iv = this.toTextureShape(renderer.gl, values.length);
        renderer.gl.uniform2iv(renderer.u_ids_shape, ids_2iv);
        this.setIntegerTexture(renderer.gl, idx, renderer.texture_ids, values);
    }

    /**
     * @function bindMagnitudes - bind segmentation mask magnitudes
     * @param renderer - the GL renderer
     * @param values - the texture data as 2d array
     * @param key - the marker label
     */
    bindMagnitudes(renderer, values, key) {
        // Add a mask magnitude map
        const idx = this.indexOfTexture(key, "M");
        const texture = renderer.texture_mag[idx - renderer._markerOffset];
        const [width, height] = this.toTextureShape(renderer.gl, values.length);
        this.setFloatTexture(renderer.gl, idx, texture, values, width, height);
    }

    /**
     * @function bindCenters - bind segmentation mask centers
     * @param renderer - the GL renderer
     * @param values - the texture data as 2d array
     */
    bindCenters(renderer, values) {
        // Add a mask center map
        const idx = this.indexOfTexture("centers", null);
        const [width, height] = this.toTextureShape(renderer.gl, values.length);
        renderer.gl.uniform3iv(renderer.u_center_shape, [width, height, 2]);
        this.setFloatTexture(renderer.gl, idx, renderer.texture_centers, values, width, height);
    }

    /**
     * @function bindRanges - bind the per-cell range table
     * @param renderer - the GL renderer
     * @param values - the texture data as 2d array
     * @param width - the texture width
     */
    bindRanges(renderer, values, width) {
        // Add a mask range map
        const idx = this.indexOfTexture("ranges", null);
        const height = Math.floor(values.length / width);
        const range_2iv = [width, height];
        renderer.gl.uniform2iv(renderer.u_cell_range_shape, range_2iv);
        this.setFloatTexture(renderer.gl, idx, renderer.texture_ranges, values, width, height);
    }

    /**
     * @function bindPickings - bind segmentation mask pickings
     * @param renderer - the GL renderer
     * @param values - the texture data as 2d array
     */
    bindPickings(renderer, values) {
        // Add a mask gating map
        const idx = this.indexOfTexture("pickings", null);
        const picking_2iv = this.toTextureShape(renderer.gl, values.length);
        renderer.gl.uniform2iv(renderer.u_picking_shape, picking_2iv);
        this.setIntegerTexture(renderer.gl, idx, renderer.texture_pickings, values);
    }

    // =================================================================================================================
    // Tile cache management
    // =================================================================================================================

    /**
     * @function createTFArray - creates an array of colors as a transfer/lookup table for pixel values.
     * @param min - the minimum value
     * @param max - the maximum value
     * @param rgb1 - the start color (min)
     * @param rgb2 - the end color (max)
     * @param numBins - the bins for the color interpolation steps
     * @typedef {object} TF
     * @property {Array} tf - color list
     * @property {number} min - min cutoff
     * @property {number} max - max cutoff
     * @property {number} num_bins - number of bins
     * @property {object} start_color - lower limit color
     * @property {object} end_color - upper limit color
     * @returns TF
     */
    createTFArray(min, max, rgb1, rgb2, numBins) {
        const tfArray = [];

        const numBinsF = parseFloat(numBins);
        const col1 = d3.rgb(rgb1);
        const col2 = d3.rgb(rgb2);

        for (let i = 0; i < numBins; i++) {
            const rgbTupel = {};
            const lerpFactor = i / (numBinsF - 1.0);

            rgbTupel.r = col1.r + (col2.r - col1.r) * lerpFactor;
            rgbTupel.g = col1.g + (col2.g - col1.g) * lerpFactor;
            rgbTupel.b = col1.b + (col2.b - col1.b) * lerpFactor;

            const lerpCol = d3.rgb(rgbTupel.r, rgbTupel.g, rgbTupel.b);
            tfArray.push(lerpCol);
        }

        return {
            min: min,
            max: max,
            start_color: rgb1,
            end_color: rgb2,
            num_bins: numBins,
            tf: tfArray,
        };
    }

    /**
     * @function forceRepaint - for all active viewers repaint the canvas
     */
    async forceRepaint() {
        if (!this.ready) {
            return;
        }
        this.ready = false;
        if (this.idCount) {
            await this.loadBuffers();
        }
        this.ready = true;
        // Trigger change of full cache
        this.viewerManagers.forEach(({ viewer }) => {
            viewer.forceRedraw();
        });
    }
    /**
     * @function scheduleRepaint - coalesce repaints onto the next animation frame
     *
     * For anything driven by a continuous gesture. The contrast sliders emit a
     * BRUSH_MOVE per input event (viewerSidebar.js -> main.js ->
     * updateChannelRange), and each one used to run a full forceRepaint, so a
     * single drag queued dozens of complete redraws of every visible tile of
     * every channel. Collapsing them to one repaint per frame keeps the drag
     * responsive without changing what is ultimately drawn.
     */
    scheduleRepaint() {
        if (this._repaintScheduled) {
            return;
        }
        this._repaintScheduled = true;
        requestAnimationFrame(() => {
            this._repaintScheduled = false;
            this.forceRepaint();
        });
    }


    /**
     * @function updateActiveChannels
     * @param name - image channel name
     * @param action - "add" or "remove"
     */
    updateActiveChannels(name, action) {
        const channelIdx = imageChannels[name];

        if (action == "add") {
            this.viewerManagers.forEach((vM) => {
                vM.channel_add(channelIdx);
            });
        } else {
            this.viewerManagers.forEach((vM) => {
                vM.channel_remove(channelIdx);
            });
        }

        this.forceRepaint();
    }

    /**
     * @function updateChannelRange
     * @param name - image channel name
     * @param tfmin - minimum
     * @param tfmax - maximum
     */
    updateChannelRange(name, tfmin, tfmax) {
        // In the default (non-HD) mode tfmin/tfmax already arrive in [0, 255]
        // byte units (see viewerSidebar.js) -- divide by 255, not the full
        // 16-bit range, so this matches what the shader's u8 path now reads
        // u_tile_range as directly (see frag.glsl's u8_r_range). HD mode
        // keeps the original raw-16-bit-unit behavior.
        const hd = Boolean(this.viewerManagerVMain?.isHdMode?.());
        const range = hd ? this.numericData.intRange : [0, 255];
        const channelIdx = imageChannels[name];
        if (this.currentChannels[channelIdx]) {
            let channelRange = [tfmin / range[1], tfmax / range[1]];
            this.currentChannels[channelIdx].range = channelRange;
            this.channelList.rangeConnector[channelIdx] = channelRange;
        }
        this.scheduleRepaint();
    }

    /**
     * @function updateChannelColors
     * @param name - image channel name
     * @param color - rgb object with values 0-255
     */
    updateChannelColors(name, color) {
        const channelIdx = imageChannels[name];
        if (this.currentChannels[channelIdx]) {
            this.channelList.colorConnector[channelIdx] = { color: color };
            this.currentChannels[channelIdx].color = color;
        }
        // Also gesture-driven (dragging in the colour picker), so coalesce.
        this.scheduleRepaint();
    }

    /**
     * @function updateRenderingMode
     * @param mode - subset or selection
     */
    updateRenderingMode(mode) {
        // Mode is a string: 'show-subset', 'show-selection'
        if (mode === "show-subset") {
            this.show_subset = !this.show_subset;
        }
        if (mode === "show-selection") {
            this.show_selection = !this.show_selection;
        }

        this.forceRepaint();
    }

    async updateCentroidVisibility(isVisible) {
        this.show_centroids = isVisible;
        if (isVisible) {
            await this.ensureCentroidsReady(true);
            this.scheduleCentroidTileUpdate(0, true);
        }
        this.refreshCentroidOverlay();
    }

    async updateCentroidFallback(isFallback) {
        // Only ever called with true (turn centroids on as a one-time default/
        // fallback, e.g. no segmentation registered or outlines failed to load).
        // Routes through updateCentroidVisibility so show_centroids and the
        // Cells control agree from the start -- previously this set a separate
        // force_centroids flag that shouldDrawCentroids() OR'd in permanently,
        // so turning centroids off afterward couldn't actually do it.
        // Remembered so a mask that lands later can tell "centroids because
        // there was nothing else" from "centroids because the user asked", and
        // take the drawing over only in the first case.
        this.centroidsFromFallback = Boolean(isFallback);
        if (!isFallback) return;
        await this.updateCentroidVisibility(true);
        // The control has to show what is actually drawn. Through adoptMode
        // rather than the ordinary selection path: the drawing has already been
        // switched above, and re-entering that path would recurse -- this method
        // is called from inside it, when a mask fails to load.
        window.__plexora?.viewerControls?.adoptMode?.("centroids");
    }

    shouldDrawCentroids() {
        return this.show_centroids;
    }

    updateCentroidIds() {
        this.scheduleCentroidTileUpdate(0, true);
    }

    updateCentroidFilter(filter = {}, showSpinner = false) {
        this.centroidFilter = filter || {};
        const signature = JSON.stringify(this.centroidFilter);
        if (signature !== this.centroidFilterSignature) {
            this.centroidFilterSignature = signature;
            this.centroidTiles.clear();
            this.centroidTileRequest += 1;
        }
        if (this.centroidMode === "legacy") {
            const hasFilter = !!(this.centroidFilter && Object.keys(this.centroidFilter).length);
            const provider = this.cellLayer;
            const idsPromise = (hasFilter && provider)
                ? provider.getSelectedIds(this.centroidFilter)
                : Promise.resolve(null);
            idsPromise.then((ids) => {
                this.centroidIdSet = hasFilter ? (ids instanceof Set ? ids : new Set()) : null;
                this.refreshCentroidOverlay();
            });
            return;
        }
        this.scheduleCentroidTileUpdate(0, showSpinner);
        this.refreshCentroidOverlay();
    }

    refreshCentroidOverlay() {
        if (this.canvasOverlay) {
            this.canvasOverlay.resize();
            this.canvasOverlay.clear();
            this.canvasOverlay._updateCanvas();
        }
    }

    async ensureCentroidsReady(showSpinner = false) {
        if (this.centroidsReady) return;
        if (this.centroidsLoading) {
            await this.centroidsLoading;
            return;
        }
        this.centroidsLoading = (async () => {
            if (showSpinner) {
                this.setLoading(true);
            }
            try {
                this.centroidManifest = await this.dataLayer.getCentroidManifest();
                if (!this.centroidManifest) {
                    const { ids, centers } = await this.numericData.loadCells();
                    this.ids = ids || [];
                    this.centers = centers || [];
                    this.prepareLegacyCentroidCache();
                    this.centroidMode = "legacy";
                } else {
                    this.centroidMode = "tiled";
                }
                this.centroidsReady = true;
            } finally {
                if (showSpinner) {
                    this.setLoading(false);
                }
                this.centroidsLoading = null;
            }
        })();
        await this.centroidsLoading;
    }

    scheduleCentroidTileUpdate(delay = 100, showSpinner = false) {
        if (!this.shouldDrawCentroids() || !this.centroidsReady) return;
        if (this.centroidMode === "legacy") {
            this.refreshCentroidOverlay();
            return;
        }
        if (this.centroidTileTimer) {
            clearTimeout(this.centroidTileTimer);
        }
        this.centroidTileTimer = setTimeout(() => {
            this.updateVisibleCentroidTiles(showSpinner);
        }, delay);
    }

    async updateVisibleCentroidTiles(showSpinner = false) {
        if (!this.shouldDrawCentroids() || !this.centroidManifest || !this.viewer?.viewport) return;
        const tileState = this.getVisibleCentroidTileState();
        if (!tileState) return;
        const { level, tiles, keepKeys } = tileState;
        for (const key of this.centroidTiles.keys()) {
            if (!keepKeys.has(key)) {
                this.centroidTiles.delete(key);
            }
        }
        const missing = tiles.filter((tile) => !this.centroidTiles.has(tile.key));
        if (!missing.length) {
            this.refreshCentroidOverlay();
            return;
        }
        const requestId = ++this.centroidTileRequest;
        const shouldSpin = showSpinner || this.centroidFirstLoad;
        if (shouldSpin) {
            this.setLoading(true);
        }
        try {
            const payload = missing.map(({ x, y }) => ({ x, y }));
            const buffer = await this.dataLayer.getCentroidTiles(level, payload, this.centroidFilter, 50000);
            if (requestId !== this.centroidTileRequest || !buffer) return;
            const grouped = this.decodeCentroidTileBuffer(buffer, level);
            for (const tile of missing) {
                this.centroidTiles.set(tile.key, grouped.get(tile.key) || {
                    ids: new Uint32Array(0),
                    centers: new Float32Array(0),
                });
            }
            this.centroidFirstLoad = false;
            this.refreshCentroidOverlay();
        } finally {
            if (shouldSpin) {
                this.setLoading(false);
            }
        }
    }

    prepareLegacyCentroidCache() {
        const centers = this.centers || [];
        const ids = this.ids || [];
        const zoomScale = 2 ** (this.config.extraZoomLevels || 0);
        this.fullResolutionCenters = new Float32Array(centers.length);
        this.idToCenterOffset = new Map();
        // Coarse spatial bucket index (same floor-by-tile-span idea as
        // centroid_tiles.py's tile bucketing) so a full redraw only walks
        // points near the current viewport instead of every cell.
        this.legacyCentroidBucketSpan = Math.max(1, this.config.tileWidth || 512);
        this.legacyCentroidBuckets = new Map();
        for (let i = 0; i < centers.length; i += 2) {
            const x = centers[i] * zoomScale;
            const y = centers[i + 1] * zoomScale;
            this.fullResolutionCenters[i] = x;
            this.fullResolutionCenters[i + 1] = y;
            if (ids[i / 2] !== undefined) {
                this.idToCenterOffset.set(Number(ids[i / 2]), i);
            }
            const bucketKey = this.legacyCentroidBucketKey(x, y);
            let bucket = this.legacyCentroidBuckets.get(bucketKey);
            if (!bucket) {
                bucket = [];
                this.legacyCentroidBuckets.set(bucketKey, bucket);
            }
            bucket.push(i);
        }
    }

    legacyCentroidBucketKey(x, y) {
        const span = this.legacyCentroidBucketSpan;
        return `${Math.floor(x / span)}_${Math.floor(y / span)}`;
    }

    getVisibleCentroidTileState() {
        const item = this.viewer.world.getItemAt(0);
        if (!item) return null;
        const bounds = this.viewer.viewport.getBounds(true);
        const imageBounds = item.viewportToImageRectangle(bounds);
        const coordinateScale = 2 ** (this.config.extraZoomLevels || 0);
        const sourceBounds = {
            x: imageBounds.x / coordinateScale,
            y: imageBounds.y / coordinateScale,
            width: imageBounds.width / coordinateScale,
            height: imageBounds.height / coordinateScale,
        };
        const level = this.getCentroidLevel();
        const tileSpan = this.centroidManifest.tile_size * (2 ** level);
        const maxTx = Math.max(0, Math.ceil(this.centroidManifest.width / tileSpan) - 1);
        const maxTy = Math.max(0, Math.ceil(this.centroidManifest.height / tileSpan) - 1);
        const minX = Math.max(0, Math.floor(sourceBounds.x / tileSpan) - 1);
        const minY = Math.max(0, Math.floor(sourceBounds.y / tileSpan) - 1);
        const maxX = Math.min(maxTx, Math.floor((sourceBounds.x + sourceBounds.width) / tileSpan) + 1);
        const maxY = Math.min(maxTy, Math.floor((sourceBounds.y + sourceBounds.height) / tileSpan) + 1);
        const tiles = [];
        const keepKeys = new Set();
        for (let y = minY; y <= maxY; y += 1) {
            for (let x = minX; x <= maxX; x += 1) {
                const key = this.centroidTileKey(level, x, y);
                tiles.push({ level, x, y, key });
                keepKeys.add(key);
            }
        }
        return { level, tiles, keepKeys };
    }

    getCentroidLevel() {
        const item = this.viewer.world.getItemAt(0);
        let imageZoom = 1;
        try {
            imageZoom = item.viewportToImageZoom(this.viewer.viewport.getZoom(true));
        } catch (e) {
            imageZoom = 1;
        }
        const level = Math.floor(Math.max(0, Math.log2(1 / Math.max(imageZoom, 0.0001))));
        return Math.min(Math.max(0, level), Math.max(0, (this.centroidManifest?.level_count || 1) - 1));
    }

    centroidTileKey(level, x, y) {
        return `${level}/${x}/${y}`;
    }

    decodeCentroidTileBuffer(buffer, level) {
        const view = new DataView(buffer);
        const groups = new Map();
        const tileSpan = this.centroidManifest.tile_size * (2 ** level);
        const coordinateScale = 2 ** (this.config.extraZoomLevels || 0);
        for (let offset = 0; offset + 12 <= view.byteLength; offset += 12) {
            const id = view.getUint32(offset, true);
            const x = view.getFloat32(offset + 4, true);
            const y = view.getFloat32(offset + 8, true);
            const tx = Math.max(0, Math.floor(x / tileSpan));
            const ty = Math.max(0, Math.floor(y / tileSpan));
            const key = this.centroidTileKey(level, tx, ty);
            if (!groups.has(key)) {
                groups.set(key, { ids: [], centers: [] });
            }
            const group = groups.get(key);
            group.ids.push(id);
            group.centers.push(x * coordinateScale, y * coordinateScale);
        }
        const typedGroups = new Map();
        groups.forEach((group, key) => {
            typedGroups.set(key, {
                ids: new Uint32Array(group.ids),
                centers: new Float32Array(group.centers),
            });
        });
        return typedGroups;
    }

    async ensureSegmentationReady(showSpinner = false) {
        if (this.segmentationReady || this.noLabel) return;
        if (this.segmentationLoading) {
            await this.segmentationLoading;
            return;
        }
        this.segmentationLoading = (async () => {
            if (showSpinner) {
                this.setLoading(true);
            }
            try {
                if (!this.centers.length || !this.ids.length) {
                    const { ids, centers } = await this.numericData.loadCells();
                    this.ids = ids || [];
                    this.centers = centers || [];
                }
                this.bindSegmentationBuffers(this.ids, this.centers);
                this.viewerManagerVMain.load_label_image();
                this.segmentationReady = true;
                this.clearTileCache(true);
                await this.forceRepaint();
            } finally {
                if (showSpinner) {
                    this.setLoading(false);
                }
                this.segmentationLoading = null;
            }
        })();
        await this.segmentationLoading;
    }

    /**
     * @function updateSegmentationFilter - restrict one layer to a set of cells.
     *
     * @param name - which layer's gate to write. Defaults to the active layer;
     *   pass a name explicitly from a plugin's own gate flow so a tool the user
     *   has switched away from still updates its OWN cells rather than whichever
     *   layer happens to be active. Falls back to core's gate when nothing is
     *   registered at all.
     */
    async updateSegmentationFilter(filter = {}, showSpinner = false, name = undefined) {
        if (this.noLabel || !this.viewerManagerVMain?.sel_outlines) return;
        const target = name === undefined ? this._activeCellLayer : name;
        const layer = target ? this._cellLayers.get(target) : null;
        // A named layer that has since been removed: nothing to filter, and
        // nothing to fall through to -- writing core's gate here would apply a
        // dead tool's selection to every other layer on screen.
        if (target && !layer) return;
        const requestId = layer
            ? (layer.filterRequest += 1)
            : (this.segmentationFilterRequest += 1);
        const current = () => (layer ? layer.filterRequest : this.segmentationFilterRequest);
        const gates = filter || {};
        const hasGates = Object.keys(gates).length > 0;
        const provider = layer ? layer.provider : null;
        if (showSpinner) {
            this.setLoading(true);
        }
        try {
            await this.ensureSegmentationReady(false);
            let ids = null;
            if (hasGates && provider?.getSelectedIds) {
                const selected = await provider.getSelectedIds(gates);
                if (requestId !== current()) return;
                ids = selected instanceof Set ? selected : null;
            }
            if (layer) {
                layer.filterIds = ids;
            } else {
                this.segmentationFilterIds = ids;
            }
            this.rerenderSegmentationTiles(layer ? layer.name : null);
            this.viewer.forceRedraw();
        } finally {
            if (showSpinner) {
                this.setLoading(false);
            }
        }
    }

    /** Walk every loaded label tile. The tilesMatrix nesting is OpenSeadragon's,
     *  and three call sites needed the same four loops to reach through it. */
    forEachLabelTile(visit) {
        if (!this.viewer?.world) return;
        for (let i = 0; i < this.viewer.world.getItemCount(); i += 1) {
            const item = this.viewer.world.getItemAt(i);
            if (item?.source?.tileFormat != 32) continue;
            const matrix = item.tilesMatrix || {};
            Object.keys(matrix).forEach((level) => {
                Object.keys(matrix[level] || {}).forEach((x) => {
                    Object.keys(matrix[level][x] || {}).forEach((y) => {
                        const tile = matrix[level][x][y];
                        if (tile) visit(tile);
                    });
                });
            });
        }
    }

    /**
     * @function renderTileLayers - build one tile's canvas per drawn layer.
     *
     * One decoded array, N passes over it, N cached canvases -- which is the
     * whole reason several layers can be stacked without refetching a thing.
     *
     * @param tile - an OpenSeadragon label tile carrying `_array`
     * @param draw - the layers to draw, from maskDrawList()
     * @param only - rebuild just this layer's canvas, and CREATE any layer that
     *   has none yet (that is what repopulates a layer the user turned back on).
     *   Null rebuilds everything.
     */
    renderTileLayers(tile, draw = null, only = null) {
        if (!this.renderLabelTile || !tile?._array) return null;
        const width = tile._labelWidth;
        const height = tile._labelHeight;
        if (!width || !height) return tile._layerContexts || null;
        let contexts = tile._layerContexts;
        if (!contexts) {
            contexts = new Map();
            tile._layerContexts = contexts;
        }
        const layers = draw || this.maskDrawList();
        const wanted = new Set();
        for (const layer of layers) {
            wanted.add(layer.name);
            if (only && layer.name !== only && contexts.has(layer.name)) continue;
            contexts.set(layer.name,
                this.renderLabelTile(tile._array, width, height, layer));
        }
        // A layer that has left the stack releases its canvas now rather than
        // waiting for the tile itself to be evicted.
        for (const name of [...contexts.keys()]) {
            if (!wanted.has(name)) contexts.delete(name);
        }
        return contexts;
    }

    /** Release one layer's cached canvases across every loaded label tile. Its
     *  lookup table is deliberately kept -- see setCellLayerVisible. */
    dropLayerContexts(name) {
        this.forEachLabelTile((tile) => {
            tile._layerContexts?.delete(name);
        });
    }

    /**
     * @function rerenderSegmentationTiles - rebuild the label tiles' pixels.
     *
     * @param name - rebuild only this layer's canvases. Layers that are in the
     *   stack but have no canvas for a tile are still created, so this doubles
     *   as the "bring a re-shown layer back" path.
     */
    rerenderSegmentationTiles(name = null) {
        if (!this.renderLabelTile) return;
        const draw = this.maskDrawList();
        this.forEachLabelTile((tile) => {
            if (!tile._array || !tile._layerContexts) return;
            this.renderTileLayers(tile, draw, name);
        });
    }

    bindSegmentationBuffers(ids, centers) {
        if (!ids?.length || !centers?.length) return;
        const renderer = this.glRenderer;
        renderer.texture_mag = renderer.texture_mag || [renderer.gl.createTexture(), renderer.gl.createTexture(), renderer.gl.createTexture(), renderer.gl.createTexture()];
        renderer.texture_ids = renderer.texture_ids || renderer.gl.createTexture();
        renderer.texture_centers = renderer.texture_centers || renderer.gl.createTexture();
        renderer.texture_pickings = renderer.texture_pickings || renderer.gl.createTexture();
        renderer.texture_ranges = renderer.texture_ranges || renderer.gl.createTexture();
        this.bindCenters(renderer, centers);
        this.bindPickings(renderer, this.pickedIds || []);
        this.bindLabels(renderer, ids);
        this.idCount = ids.length;
    }

    initProjectLabel() {
        const wrapper = document.getElementById("openseadragon_wrapper");
        if (!wrapper || document.getElementById("viewer_project_label")) return;
        const label = document.createElement("div");
        label.id = "viewer_project_label";
        label.className = "viewer-project-label";
        label.textContent = datasource || "";
        wrapper.appendChild(label);
    }
    initLegend() {
        const wrapper = document.getElementById("openseadragon_wrapper");
        if (!wrapper || document.getElementById("viewer_channel_legend")) return;
        const legend = document.createElement("div");
        legend.id = "viewer_channel_legend";
        legend.className = "viewer-channel-legend";
        wrapper.appendChild(legend);
        this.eventHandler.bind(ChannelList.events.COLOR_TRANSFER_CHANGE, () => this.updateLegend());
        this.eventHandler.bind(ChannelList.events.CHANNELS_CHANGE, () => this.updateLegend());
        this.updateLegend();
    }

    getActiveLegendChannels() {
        return (window.__plexora?.viewerSidebar?.channelSlots || []).filter((slot) => slot.enabled && slot.name);
    }

    updateLegend() {
        const legend = document.getElementById("viewer_channel_legend");
        if (!legend) return;
        const active = this.getActiveLegendChannels();
        legend.innerHTML = active.map((slot) => '<span class="legend-row"><span class="legend-swatch" style="background:' + slot.colorHex + '"></span><span class="legend-name">' + slot.name + '</span></span>').join("");
        legend.style.display = active.length ? "flex" : "none";
    }

    drawLegendOnCanvas(ctx, width, height) {
        const active = this.getActiveLegendChannels();
        if (!active.length) return;
        const rowH = 26;
        const padding = 12;
        const swatchSize = 15;
        ctx.font = "15px sans-serif";
        const textWidths = active.map((slot) => ctx.measureText(slot.name).width);
        const boxW = Math.max.apply(null, textWidths) + swatchSize + padding * 2 + 8;
        const boxH = active.length * rowH + padding * 2;
        const x = width - boxW - 16;
        const y = height - boxH - 60;
        ctx.fillStyle = "rgba(17, 24, 39, 0.86)";
        ctx.fillRect(x, y, boxW, boxH);
        active.forEach((slot, i) => {
            const rowY = y + padding + i * rowH;
            ctx.fillStyle = slot.colorHex;
            ctx.fillRect(x + padding, rowY, swatchSize, swatchSize);
            ctx.fillStyle = "#f1f5f9";
            ctx.fillText(slot.name, x + padding + swatchSize + 8, rowY + swatchSize);
        });
    }

    // Maps a DOM overlay element's on-screen box into the coordinate space
    // of the full-resolution OSD drawer canvas, so PDF export can place
    // vector shapes/text exactly where the same overlay appears on screen
    // (canvas.width/height is the backing-store pixel size; getBoundingClientRect
    // is CSS layout size -- the ratio between them is the scale factor).
    getOverlayRectInCanvasSpace(el) {
        const canvasEl = this.viewer?.drawer?.canvas;
        if (!canvasEl || !el) return null;
        const canvasRect = canvasEl.getBoundingClientRect();
        const elRect = el.getBoundingClientRect();
        if (!canvasRect.width || !canvasRect.height) return null;
        const scaleX = canvasEl.width / canvasRect.width;
        const scaleY = canvasEl.height / canvasRect.height;
        return {
            x: (elRect.left - canvasRect.left) * scaleX,
            y: (elRect.top - canvasRect.top) * scaleY,
            width: elRect.width * scaleX,
            height: elRect.height * scaleY,
            scale: scaleX,
        };
    }

    // Vector (line + real text, not a rasterized image) redraw of the
    // on-screen scale bar for PDF export -- positioned/sized from the live
    // DOM element rather than recomputing the microscopy scalebar plugin's
    // own round-number sizing logic a second time.
    drawScalebarVector(pdf) {
        const instance = this.viewer?.scalebarInstance;
        const element = instance?.divElt;
        if (!element || element.style.display === "none") return;
        const rect = this.getOverlayRectInCanvasSpace(element);
        if (!rect || !rect.width) return;
        const barY = rect.y + rect.height;
        pdf.setDrawColor(255, 255, 255);
        pdf.setLineWidth(Math.max(1, (instance.barThickness || 3) * rect.scale));
        pdf.line(rect.x, barY, rect.x + rect.width, barY);
        pdf.setFont("helvetica", "normal");
        pdf.setFontSize(Math.max(6, (parseFloat(instance.fontSize) || 12) * rect.scale));
        pdf.setTextColor(255, 255, 255);
        pdf.text(element.textContent || "", rect.x + rect.width / 2, rect.y + rect.height / 2, {
            align: "center",
            baseline: "middle",
        });
    }

    // Vector redraw of the channel legend for PDF export: a real filled
    // rect per swatch and real text per name (editable in Illustrator),
    // positioned from the on-screen legend DOM so it matches item-for-item.
    drawLegendVector(pdf) {
        const legendEl = document.getElementById("viewer_channel_legend");
        const active = this.getActiveLegendChannels();
        if (!legendEl || !active.length || legendEl.style.display === "none") return;
        const rect = this.getOverlayRectInCanvasSpace(legendEl);
        if (!rect || !rect.width) return;

        pdf.saveGraphicsState();
        pdf.setGState(new pdf.GState({ opacity: 0.86 }));
        pdf.setFillColor(17, 24, 39);
        pdf.roundedRect(rect.x, rect.y, rect.width, rect.height, 3 * rect.scale, 3 * rect.scale, "F");
        pdf.restoreGraphicsState();

        const rows = legendEl.querySelectorAll(".legend-row");
        pdf.setFont("helvetica", "normal");
        rows.forEach((row, i) => {
            const slot = active[i];
            const swatchRect = this.getOverlayRectInCanvasSpace(row.querySelector(".legend-swatch"));
            if (!slot || !swatchRect) return;
            const { r, g, b } = hexToRgb(slot.colorHex);
            pdf.setFillColor(r, g, b);
            pdf.rect(swatchRect.x, swatchRect.y, swatchRect.width, swatchRect.height, "F");
            pdf.setFontSize(Math.max(6, 15 * rect.scale));
            pdf.setTextColor(241, 245, 249);
            pdf.text(slot.name, swatchRect.x + swatchRect.width + 8 * rect.scale, swatchRect.y + swatchRect.height / 2, {
                baseline: "middle",
            });
        });
    }

    // Vector redraw of the top-left project-name label for PDF export.
    drawProjectLabelVector(pdf) {
        const labelEl = document.getElementById("viewer_project_label");
        if (!labelEl) return;
        const rect = this.getOverlayRectInCanvasSpace(labelEl);
        if (!rect || !rect.width) return;

        pdf.saveGraphicsState();
        pdf.setGState(new pdf.GState({ opacity: 0.86 }));
        pdf.setFillColor(17, 24, 39);
        pdf.roundedRect(rect.x, rect.y, rect.width, rect.height, 3 * rect.scale, 3 * rect.scale, "F");
        pdf.restoreGraphicsState();

        pdf.setFont("helvetica", "bold");
        pdf.setFontSize(Math.max(6, 13 * rect.scale));
        pdf.setTextColor(241, 245, 249);
        pdf.text(labelEl.textContent || "", rect.x + rect.width / 2, rect.y + rect.height / 2, {
            align: "center",
            baseline: "middle",
        });
    }


    addScaleBar() {
        let pixelsPerMeter;
        if (this.imgMetadata) {
            if (this.show_scalebar) {
                let unitConvert;
                if (this.imgMetadata.physical_size_x_unit === "µm" || this.imgMetadata.physical_size_x_unit === "um") {
                    unitConvert = 1000000;
                } else if (this.imgMetadata.physical_size_x_unit === "nm") {
                    unitConvert = 1000000000;
                } else if (this.imgMetadata.physical_size_x_unit === "cm") {
                    unitConvert = 100;
                } else if (this.imgMetadata.physical_size_x_unit === "m") {
                    unitConvert = 1;
                } else {
                    unitConvert = 0;
                }
                pixelsPerMeter = unitConvert * this.imgMetadata.physical_size_x;
            } else {
                pixelsPerMeter = 0;
            }
            pixelsPerMeter = this.show_scalebar ? this.getPixelsPerMeter() : null;

            this.viewer.scalebar({
                location: OpenSeadragon.ScalebarLocation.BOTTOM_RIGHT,
                minWidth: "100px",
                type: OpenSeadragon.ScalebarType.MICROSCOPY,
                stayInsideImage: false,
                pixelsPerMeter: pixelsPerMeter,
                fontColor: "rgb(255, 255, 255)",
                color: "rgb(255, 255, 255)",
                backgroundColor: "rgba(0, 0, 0, 0.45)",
                barThickness: 3,
            });
            this.styleScaleBar();
        }
    }

    getPixelsPerMeter() {
        const physicalSizeX = Number(this.imgMetadata?.physical_size_x);
        if (!physicalSizeX) return null;
        const unitsPerMeter = {
            "\u00b5m": 1000000,
            "um": 1000000,
            "nm": 1000000000,
            "cm": 100,
            "m": 1,
        }[this.imgMetadata?.physical_size_x_unit];
        if (!unitsPerMeter) return null;
        return unitsPerMeter / physicalSizeX;
    }

    setScalebarVisible(visible) {
        this.show_scalebar = visible;
        if (!this.viewer?.scalebarInstance) return;
        this.viewer.scalebar({ pixelsPerMeter: visible ? this.getPixelsPerMeter() : null });
    }

    styleScaleBar() {
        const element = this.viewer?.scalebarInstance?.divElt;
        if (!element) return;
        element.style.zIndex = "220";
        element.style.position = "absolute";
        element.style.padding = "2px 4px";
        element.style.borderRadius = "3px";
    }

    /**
     * @function drawCentroids - the point representation of every visible layer.
     *
     * WHICH POINTS EXIST is not per layer, and cannot be with the tiled centroid
     * source: the gate is applied server-side when the tiles are fetched
     * (updateCentroidFilter), and there is one set of tiles. So a gating layer
     * that is visible but NOT active draws its colours over the point set the
     * ACTIVE layer asked for. Colour is per layer; membership is not. Splitting
     * that would mean a tile set per layer, which is a fetch per layer per pan.
     */
    drawCentroids(context, imageZoom = 1) {
        if (this.centroidMode === "legacy") {
            this.drawLegacyCentroids(context, imageZoom);
            return;
        }
        if (!this.centroidTiles.size || !this.viewer?.viewport) return;
        const item = this.viewer.world.getItemAt(0);
        if (!item) return;
        const layers = this.centroidDrawList();
        if (!layers.length) return;
        const bounds = this.viewer.viewport.getBounds(true);
        const imageBounds = item.viewportToImageRectangle(bounds);
        const minX = imageBounds.x;
        const minY = imageBounds.y;
        const maxX = imageBounds.x + imageBounds.width;
        const maxY = imageBounds.y + imageBounds.height;
        const safeImageZoom = Math.max(imageZoom, 0.0001);
        const radius = this.getCentroidScreenRadius(safeImageZoom) / safeImageZoom;
        // One pass per layer, bottom of the stack first, so the same drag that
        // reorders the mask layers reorders these. They all land on the same
        // overlay canvas, which is above every mask tile -- see centroidDrawList.
        for (const layer of layers) {
            const colored = Boolean(layer.lut);
            context.save();
            context.globalAlpha = 0.9 * this.layerAlpha(layer);
            context.fillStyle = "#ffdd55";
            context.strokeStyle = "rgba(0, 0, 0, 0.8)";
            context.lineWidth = 1.6 / safeImageZoom;
            // Only reassigned when the colour actually changes. Canvas state
            // changes are the expensive part of this loop, and a categorical
            // variable draws long runs of one colour.
            let currentFill = "#ffdd55";
            const drawPoint = (x, y, cellId) => {
                if (x < minX || x > maxX || y < minY || y > maxY) {
                    return;
                }
                if (colored) {
                    const style = this.cellColorStyle(cellId, layer);
                    if (!style) return;
                    if (style !== currentFill) {
                        context.fillStyle = style;
                        currentFill = style;
                    }
                }
                context.beginPath();
                context.arc(x, y, radius, 0, Math.PI * 2);
                context.fill();
                context.stroke();
            };
            this.centroidTiles.forEach((tile) => {
                const centers = tile.centers || [];
                // Parallel to `centers`, two coordinates per id -- see
                // decodeCentroidTileBuffer, which builds both from the same record.
                const ids = tile.ids;
                for (let i = 0; i < centers.length; i += 2) {
                    drawPoint(centers[i], centers[i + 1], ids ? ids[i >> 1] : 0);
                }
            });
            context.restore();
        }
    }

    drawLegacyCentroids(context, imageZoom = 1) {
        const centers = this.fullResolutionCenters || [];
        if (!centers.length || !this.viewer?.viewport) return;
        const item = this.viewer.world.getItemAt(0);
        if (!item) return;
        const layers = this.centroidDrawList();
        if (!layers.length) return;
        const bounds = this.viewer.viewport.getBounds(true);
        const imageBounds = item.viewportToImageRectangle(bounds);
        const minX = imageBounds.x;
        const minY = imageBounds.y;
        const maxX = imageBounds.x + imageBounds.width;
        const maxY = imageBounds.y + imageBounds.height;
        const safeImageZoom = Math.max(imageZoom, 0.0001);
        const radius = this.getCentroidScreenRadius(safeImageZoom) / safeImageZoom;
        for (const layer of layers) {
            const colored = Boolean(layer.lut);
            context.save();
            context.globalAlpha = 0.9 * this.layerAlpha(layer);
            context.fillStyle = "#ffdd55";
            context.strokeStyle = "rgba(0, 0, 0, 0.8)";
            context.lineWidth = 1.6 / safeImageZoom;
            let currentFill = "#ffdd55";
            const drawAtOffset = (i) => {
                const x = centers[i];
                const y = centers[i + 1];
                if (x < minX || x > maxX || y < minY || y > maxY) {
                    return;
                }
                if (colored) {
                    // this.ids is parallel to `centers` at two coordinates per
                    // id, the same relationship the tiled path has -- so one
                    // colour lookup works for both without either knowing about
                    // the other.
                    const style = this.cellColorStyle(this.ids[i >> 1], layer);
                    if (!style) return;
                    if (style !== currentFill) {
                        context.fillStyle = style;
                        currentFill = style;
                    }
                }
                context.beginPath();
                context.arc(x, y, radius, 0, Math.PI * 2);
                context.fill();
                context.stroke();
            };
            if (this.centroidIdSet instanceof Set) {
                this.centroidIdSet.forEach((id) => {
                    const offset = this.idToCenterOffset.get(Number(id));
                    if (offset !== undefined) {
                        drawAtOffset(offset);
                    }
                });
            } else if (this.legacyCentroidBuckets) {
                // Only walk buckets overlapping the current viewport instead of
                // every cell in the dataset.
                const span = this.legacyCentroidBucketSpan;
                const minTx = Math.floor(minX / span);
                const maxTx = Math.floor(maxX / span);
                const minTy = Math.floor(minY / span);
                const maxTy = Math.floor(maxY / span);
                for (let tx = minTx; tx <= maxTx; tx += 1) {
                    for (let ty = minTy; ty <= maxTy; ty += 1) {
                        const bucket = this.legacyCentroidBuckets.get(`${tx}_${ty}`);
                        if (!bucket) continue;
                        for (const offset of bucket) {
                            drawAtOffset(offset);
                        }
                    }
                }
            } else {
                for (let i = 0; i < centers.length; i += 2) {
                    drawAtOffset(i);
                }
            }
            context.restore();
        }
    }

    getCentroidScreenRadius(imageZoom) {
        const overviewLevel = Math.max(0, Math.log2(1 / imageZoom));
        // The clamp is applied to the adaptive part and the user's multiplier
        // to the result, so asking for bigger dots stays bigger at every zoom
        // rather than being flattened by the ceiling at the wide end.
        return this.centroidPointScale
            * Math.max(2.5, Math.min(7, 3.5 + overviewLevel * 0.8));
    }

    /**
     * @function setCentroidPointScale - how big centroid dots are drawn.
     *
     * A redraw and nothing else: the points, their positions and their colours
     * are all unchanged, so this costs one canvas pass over what is already in
     * view. Dragging the slider is as cheap as panning.
     *
     * @param value - multiplier, clamped to [MIN, MAX]_CENTROID_SCALE
     * @returns whether anything changed
     */
    setCentroidPointScale(value) {
        const asked = Number(value);
        if (!Number.isFinite(asked)) return false;
        const next = Math.max(ImageViewer.MIN_CENTROID_SCALE,
                              Math.min(ImageViewer.MAX_CENTROID_SCALE, asked));
        if (next === this.centroidPointScale) return false;
        this.centroidPointScale = next;
        this.viewer?.forceRedraw?.();
        return true;
    }

    downloadCurrentView(format = "png") {
        if (format === "pdf") {
            this.exportPdf();
            return;
        }

        // PNG has no vector concept, so the scale bar and legend are baked
        // in as raster pixels here, same as before.
        const baseCanvas = this.viewer?.scalebarInstance && this.show_scalebar
            ? this.viewer.scalebarInstance.getImageWithScalebarAsCanvas()
            : this.viewer?.drawer?.canvas;
        if (!baseCanvas) return;

        const canvas = document.createElement("canvas");
        canvas.width = baseCanvas.width;
        canvas.height = baseCanvas.height;
        const ctx = canvas.getContext("2d");
        ctx.drawImage(baseCanvas, 0, 0);
        this.drawLegendOnCanvas(ctx, canvas.width, canvas.height);

        const link = document.createElement("a");
        link.download = `${datasource || "plexora"}_current_view.png`;
        link.href = canvas.toDataURL("image/png");
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
    }

    // PDF export: the microscopy image itself stays a raster embed (it's a
    // bitmap by nature), but the scale bar, legend and project label are all
    // drawn as real vector shapes/text on top -- editable as separate
    // objects in Illustrator, not baked into the image pixels.
    exportPdf() {
        const baseCanvas = this.viewer?.drawer?.canvas;
        if (!baseCanvas) return;
        const width = baseCanvas.width;
        const height = baseCanvas.height;

        const pdf = new jsPDF({
            orientation: width >= height ? "landscape" : "portrait",
            unit: "px",
            format: [width, height],
        });
        pdf.addImage(baseCanvas.toDataURL("image/png"), "PNG", 0, 0, width, height);

        if (this.show_scalebar) this.drawScalebarVector(pdf);
        this.drawLegendVector(pdf);
        this.drawProjectLabelVector(pdf);

        pdf.save(`${datasource || "plexora"}_current_view.pdf`);
    }

    setLoading(isLoading) {
        const loader = document.getElementById("openseadragon_loader");
        if (loader) {
            loader.style.display = isLoading ? "flex" : "none";
        }
    }


    /**
     * @function clearTileCache - drop loaded tiles so they get refetched
     * @param onlySegmentation - if true, only clear the segmentation/label layer
     *   (tileFormat 32) instead of every channel's tile pyramid
     *
     * Uses TiledImage.reset(), OpenSeadragon 6's supported entry point, which
     * delegates to TileCache.clearTilesFor() and properly unloads each tile and
     * decrements _cachesLoadedCount.
     *
     * The previous hand-rolled version (and its evictLeastRecentlyUsedTiles
     * sibling, plus a 30s interval that called it) was written against
     * OpenSeadragon 2.x, where _tilesLoaded held {tile: ...} records. OSD 6
     * stores Tile objects directly, so `tileRecord.tile` was always undefined:
     * tile.unload() never fired and nothing was actually freed -- yet the
     * splice()/= [] calls still tore real tiles out of OSD's LRU list while
     * leaving _cachesLoadedCount untouched. That left _freeOldRecordRoutine
     * with no eviction candidates, so the cache grew without bound and the
     * resulting GC pauses showed up as stutter during panning.
     */
    clearTileCache(onlySegmentation = false) {
        if (!this.viewer || !this.viewer.world) {
            return;
        }
        for (let i = 0; i < this.viewer.world.getItemCount(); i++) {
            const item = this.viewer.world.getItemAt(i);
            if (onlySegmentation && item?.source?.tileFormat !== 32) continue;
            item?.reset?.();
        }
        this.viewer.forceRedraw();
    }
}

// Static vars
ImageViewer.events = {
    imageClickedMultiSel: "image_clicked_multi_selection",
    renderingMode: "renderingMode"
};

/**
 * @function toFloatColor - convert 0-255 rgb color to 0-1 float array
 * @param color - rgb object with values 0-255
 * @returns array
 */
function toFloatColor(color) {
    return [color.r / 255, color.g / 255, color.b / 255];
}
