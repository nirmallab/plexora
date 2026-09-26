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
 * A scale bar counted in image pixels, for an image nobody has calibrated.
 *
 * The OSD scalebar plugin hides itself when `pixelsPerMeter` is falsy, so an
 * uncalibrated image used to get no bar at all -- and no way to tell a screen
 * pixel from an image pixel at an arbitrary zoom. Passing `pixelsPerMeter: 1`
 * makes "one meter" mean "one image pixel", and this renderer then labels the
 * bar in px instead of running the metric ladder over it, which would say
 * things like "2 kpx".
 *
 * The rounding is the plugin's own -- its `normalize`/`roundSignificand` are
 * private to its IIFE, so they are reimplemented rather than reached for. Both
 * pick the nearest 1/2/4/5 x 10^n below the minimum width, which is what makes
 * the bar land on a number somebody can read off it.
 */
function pixelScaleSizeAndText(pixelsPerScreenPixel, minSize) {
    const significand = (x) => x * Math.pow(10, Math.ceil(-Math.log10(x)));
    let value = significand(significand(pixelsPerScreenPixel) / significand(minSize));
    if (value >= 5) value /= 5;
    if (value >= 4) value /= 4;
    if (value >= 2) value /= 2;

    const raw = value / pixelsPerScreenPixel * minSize;
    // Whole image pixels: a bar labelled "512.3 px" claims a precision the
    // thing being counted does not have.
    const factor = Math.max(1, Math.round(raw));
    return {
        size: value * minSize,
        text: `${factor.toLocaleString()} px`,
    };
}




/**
 * How many channel planes OpenSeadragon's ONE shared tile cache has to hold.
 *
 * The reference image's channels used to be the whole answer, and were while a
 * registered layer was one world item. A layer with channel controls is N world
 * items like any other channel stack, drawing from the same cache -- so a
 * project with a 19-channel reference and a 15-channel layer registered over it
 * would evict tiles it was about to redraw, which is the tile-popping stutter
 * this budget exists to avoid.
 *
 * Counted at construction, from the layer list `/config` already carries. A
 * layer adopted mid-session is not counted, and deliberately: OSD reads this
 * once, and the ceiling below is the real bound anyway.
 *
 * Capped per layer at the sidebar's own maximum, because that is the most
 * channels of one layer that can be on at once.
 */
function tileCachePlanes(config) {
    const reference = (config["imageData"] || []).length;
    const layers = (config["layers"] || []).reduce((total, layer) => {
        if (!layer || layer.kind !== "image") return total;
        if ((layer.render || {}).rgb) return total + 1;
        const channels = (layer.channels || []).length;
        return total + Math.min(Math.max(channels, 1), 15);
    }, 0);
    return reference + layers;
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
        // What the viewer draws, in the order it draws it. See layerStack.js:
        // the image channels, the mask, the centroids and anything registered
        // alongside them are one list, and this is it. The viewer is attached
        // below, once OSD exists.
        this.layerStack = new PlexoraLayerStack.LayerStack({
            onChange: () => this.viewer?.forceRedraw?.(),
        });
        // Everything drawn ON TOP of the image, on core's one overlay canvas.
        // A plugin used to make its own CanvasOverlayHd -- which leaves an
        // unremovable update-viewport handler behind, and has to rediscover the
        // once-per-channel guard and the image-pixel line width for itself.
        // See views/layerStack.js.
        this.overlays = new PlexoraLayerStack.OverlayHost({
            stack: this.layerStack,
            repaint: () => this.repaintOverlay(),
        });
        // Every plugin currently colouring cells, keyed by name. Each entry is
        // one SUB-LAYER of the mask -- see registerCellLayer for the record's
        // shape -- and several may be live at once, which is what lets a
        // phenotype map and a gate be looked at together rather than one
        // replacing the other.
        //
        // A sub-stack rather than a layer of its own because there is exactly
        // one set of boundaries on screen: a plugin's cell layer is a STYLING of
        // the segmentation mask, not a second mask. Order is held separately
        // from membership because it is the z-order they composite in (bottom
        // first) and the user sets it by dragging cards; the two change
        // independently. Which one the shared controls, picking and gating act
        // on is the stack's `active`: exactly one, or none -- "visible" is a
        // property of every sub-layer, "active" is a property of the session,
        // and conflating the two is what made opening a second tool silently
        // take the first one's colours away.
        this._cellStack = new PlexoraLayerStack.SubLayerStack({
            makeRecord: (name) => ({
                name,
                provider: null,
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
                //: The same gate evaluated in the browser: one byte per cell id
                //: (1 = passes) and how many ids pass. Set instead of filterIds
                //: when the provider's gate can be evaluated here -- see
                //: updateSegmentationFilter.
                gateMask: null,
                gateCount: null,
                //: Bumped whenever this layer's label pixels change (gate,
                //: colours, visibility): the GPU cell layer redraws a tile when
                //: it moves. lutVersion moves with the colour table alone.
                renderVersion: 0,
                lutVersion: 0,
                //: Distinct fill strings, memoized so the centroid path does not
                //: build a colour string per point per frame. Keyed on the packed
                //: RGB, so it is bounded by the number of colours in use rather
                //: than the cell count.
                styleCache: new Map(),
            }),
        });
        // Which of the three representations the cell layer draws when NO plugin
        // has registered one. Core's own mode, kept so a plain viewer -- or one
        // running only Thresholding before it registers -- draws exactly what it
        // always did. Once layers exist, each carries its own mode.
        this.cellDisplayMode = "outlines";
        //: Selected cells hidden at COMPOSITE time, keeping every pixel that
        //: was built for them. See setOverlayMuted -- this is what the overlay
        //: key toggles, and the reason it is not `selectMode("none")`.
        this.overlayMuted = false;
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
            gateMask: null,
            gateCount: null,
            renderVersion: 0,
            lutVersion: 0,
            styleCache: new Map(),
        };
        // Centroid dot size, as a multiplier -- see setCentroidPointScale.
        this.centroidPointScale = ImageViewer.DEFAULT_CENTROID_SCALE;
        // A spot's radius in image pixels when the table says -- see syncLayers.
        this.centroidImageRadius = null;
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
        this.segmentationGateMask = null;
        this.segmentationGateCount = null;
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

        // Explicit "something is happening" claims, as releases waiting to be
        // called. See setLoading below: a stack rather than a boolean.
        this._loaderHolds = [];

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
            maxImageCacheCount: Math.min(512, Math.max(200, tileCachePlanes(config) * 40)),
            // Left at OSD's default of 0 (unlimited), OSD opens every queued
            // tile request at once; the browser then serves them ~6 at a time
            // per origin in issue order, so tiles for where the viewport USED to
            // be block the ones where it is now. Capping the in-flight set keeps
            // OSD's own ordering in charge.
            imageLoaderLimit: 10,
            timeout: 90000,
            // A tile that failed is otherwise never asked for again: it stays
            // a hole until the page is reloaded. The first tiles of an image
            // read from the web can fail or time out while the server is still
            // fetching what they need, and a later ask is answered from cache.
            tileRetryMax: 3,
            tileRetryDelay: 3000,
            collectionMode: false,
            preload: false,
            homeFillsViewer: true,
            visibilityRatio: 0,
            // Force the canvas drawer: our per-tile WebGL colorize pass needs
            // the 'tile-drawing' event's 2D `rendered` context, which is only
            // guaranteed under the canvas drawer (OSD 6's WebGL drawer has no
            // documented custom-shader hook as of this writing).
            drawer: "canvas",
            // Brightfield only, and it fixes a seam that is drawing rather
            // than data. A tile whose edge lands on a fractional device pixel
            // is drawn antialiased; the next tile's edge is drawn over it with
            // `source-over`, so the two partial coverages do not add up to one
            // and the canvas's own transparency shows through as a pale
            // hairline down every tile boundary. Invisible on black, which is
            // where every other Plexora layer is drawn. Very visible as a grid
            // over pink tissue -- and the tile bytes themselves join exactly,
            // measured against the source, so there is nothing to fix upstream
            // of here. Rounding tile placement to whole pixels removes the
            // fractional edge and the seam with it.
            subPixelRoundingForTransparency:
                config.image_kind === "brightfield"
                    ? OpenSeadragon.SUBPIXEL_ROUNDING_OCCURRENCES.ALWAYS
                    : null,
        };

        // Instantiate the real OpenSeadragon viewer
        this.viewer = OpenSeadragon(viewer_config);
        this.layerStack.setViewer(this.viewer);
        // How the view is turned and mirrored -- core's Rotate and Flip, saved
        // with the image. Built here, before a single item is added, so main.js
        // can adopt the saved orientation and the first tiles draw oriented.
        // Everything below that draws or picks in screen space goes through
        // its helpers rather than the viewport. See services/viewTransform.js.
        this.viewTransform = typeof PlexoraViewTransform === "function"
            ? new PlexoraViewTransform(this.viewer, { datasource: window.flaskVariables?.datasource || "" })
            : null;
        // OSD's own r / R (rotate) and f (flip) canvas keys. They turned the
        // view unsaved, behind the Rotate card's back, and collided with ROI's
        // R and F whenever the canvas had focus. The tools are the one writer.
        // Arrow keys, +/- and 0 (home) are left to OSD.
        this.viewer.addHandler("canvas-key", (event) => {
            const code = event.originalEvent?.keyCode;
            if (code === 82 || code === 70) event.preventDefaultAction = true;
        });
        // The scale bar keeps inside the image by clamping to where OSD says
        // the image's bottom-right corner is -- a corner OSD computes turned
        // but not mirrored, and which on a turned view is not the bottom-right
        // of anything. So a turned or mirrored view pins the bar to the
        // viewer's corner instead, and upright keeps exactly today's placement.
        this.viewTransform?.subscribe((state) => {
            if (!this.viewer?.scalebarInstance) return;
            this.viewer.scalebar({ stayInsideImage: PlexoraViewTransform.isIdentity(state) });
        });
        // Lets the navbar status indicator report tiles that are still
        // streaming in -- see appStatus.js watchViewer(), which tracks each
        // TiledImage rather than the viewer's own aggregate.
        window.PlexoraStatus?.watchViewer(this.viewer);
        // And lets the spinner in the middle of the image stand down at the
        // moment there is actually something to look at -- see viewerLoader.js.
        // Until this point the page's own markup is what is on screen.
        window.PlexoraViewerLoader?.watch(this.viewer);
        this.initProjectLabel();
        this.initLegend();
        this.initMiniMap();
        this.addScaleBar();
        this.selectionPolygonToDraw = [];

        // OSD's own full-page button only resizes the #openseadragon element
        // itself (it reparents that element to <body>), leaving the sidebar
        // behind. Redirect it to a native Fullscreen API toggle instead.
        //
        // The document element, not #bodyDiv: the Fullscreen API draws an
        // opaque ::backdrop over everything that is not the fullscreen element
        // or a descendant of it, and the navbar is a sibling of #bodyDiv --
        // it lives in base.html while #bodyDiv is inside that page's content
        // block. Fullscreening the shell therefore took File, Tools, View and
        // Settings off the screen for as long as fullscreen lasted. Going
        // fullscreen on the root keeps the page exactly as laid out and drops
        // only the browser's own chrome, which is what the button is for.
        this.viewer.addHandler("pre-full-page", (event) => {
            event.preventDefaultAction = true;
            // The desktop app's window goes full screen as a window (menu bar
            // and Dock handled by the OS), which HTML fullscreen in a WebView
            // does not do.
            if (window.PlexoraDesktop) {
                window.PlexoraDesktop.toggleFullscreen();
                return;
            }
            if (document.fullscreenElement) {
                document.exitFullscreen();
            } else {
                document.documentElement.requestFullscreen();
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

        // The GLRenderer and its three hooks. See views/glInit.js -- the texture
        // unit layout, the tile texture cache and the uniform plumbing all live
        // there; what stays here is only this viewer's half of the wiring.
        const renderer = PlexoraGL.createGLRenderer({
            indexOfTexture: this.indexOfTexture.bind(this),
            selectTexture: this.selectTexture.bind(this),
            resolveGLReady: this.resolveGLReady,
        });
        this.glRenderer = renderer;

        // The GPU cell layer (views/labelGpu.js): the label tiles drawn by a
        // second shader on this renderer's context, when it builds and nothing
        // asks for the CPU path. `_labelRenderer` is what the tiles are drawn
        // with NOW; it only changes through applyLabelRenderer, which moves
        // every loaded tile across in one step.
        this._labelRenderer = "cpu";
        this._labelRendererPref = typeof PlexoraLabelGpu !== "undefined"
            ? PlexoraLabelGpu.labelGpuPreference() : null;
        this.labelGpu = typeof PlexoraLabelGpu !== "undefined" && renderer.gl
            ? PlexoraLabelGpu.createLabelGpu({
                renderer,
                vShaderUrl: plexoraUrl("client/src/shaders/vert.glsl"),
                fShaderUrl: plexoraUrl("client/src/shaders/label.frag.glsl"),
                labelTile: PlexoraLabelTile,
                onChange: () => this.applyLabelRenderer(),
            })
            : null;

        // The per-tile colorize pass. See views/tileColorize.js -- what reaches it
        // is this viewer's renderer plus the handful of lookups it needs per tile.
        const { tileDrawingDefault, tileDrawingCustom } = PlexoraTileColorize.createTileDrawing({
            renderer,
            floatRange: this.numericData.floatRange,
            findCurrentChannel: this.findCurrentChannel.bind(this),
            selectCenterProps: this.selectCenterProps.bind(this),
            // `overlayMuted` is a DRAW-TIME gate and nothing else: the mask item
            // stays loaded and every tile keeps its layer canvases, so a muted
            // tile simply is not blitted. See setOverlayMuted.
            labelOutlinesEnabled: () => !!this.viewerManagerVMain?.sel_outlines
                && !this.overlayMuted,
            modeFlags: () => this.modeFlags,
            maskDrawList: () => this.maskDrawList(),
            labelGpu: this.labelGpu,
            labelGpuMode: () => this.labelGpuMode(),
            segmentationMode: () => this.config?.segmentationMode,
        });

        // One decoded label tile -> one canvas, for one cell layer. The drawing
        // itself lives in views/labelTile.js; the only thing bound here is the
        // datasource's segmentationMode, which decides whether boundaries have to
        // be derived per tile or already are the stored pixels.
        const renderLabelTile = (tileArray, width, height, layer) =>
            PlexoraLabelTile.renderLabelTile(
                tileArray, width, height, layer, this.config?.segmentationMode);
        this.renderLabelTile = renderLabelTile;
        // Decode workers. Capped at 4: decode is memory-bandwidth bound, and
        // more workers than that mostly adds ~3 MB of live intermediates each
        // without decoding faster.
        const decoderPool = PlexoraTileDecode.TileDecoderPool.create(
            plexoraUrl("client/src/js/workers/tileDecoder.js"),
            Math.max(2, Math.min(4, navigator.hardwareConcurrency || 4)),
        );
        this.decoderPool = decoderPool;

        // tile-loaded handling: decode the raw tile bytes ourselves rather than
        // letting OSD turn them into an image. See views/tileDecode.js for the
        // decoders and for why the handler is registered as an async function.
        const handleTileLoaded = PlexoraTileDecode.createTileLoadedHandler({
            decoderPool,
            renderTileLayers: (tile) => this.renderTileLayers(tile),
            forceRepaint: this.forceRepaint.bind(this),
        });

        // Disable canvas image smoothing once per drawer canvas, not once per
        // tile per frame. This used to live in a "tile-drawn" handler that also
        // ran _.size() over the entire shared tile cache and re-fetched the 2D
        // context for every tile of every channel on every frame -- O(tiles^2)
        // work per frame whose only lasting effect was these four flags. (The
        // _imagesLoadedCount it maintained is an OpenSeadragon 2.x field that
        // OSD 6 no longer reads.)
        //
        // On for a brightfield slide, and only there. A fluorescence channel
        // is signal against nothing, and interpolating it invents intensities
        // between the pixels that were measured -- which is why this is off
        // everywhere else. A transmitted-light slide is a photograph: nearest
        // neighbour makes it blocky between pyramid levels, and it leaves a
        // hairline seam along every tile edge wherever the tile lands on a
        // fractional device pixel. The tile bytes themselves join exactly
        // (measured against the source: the step across a boundary is the same
        // in the served WebP as in the file), so the seam is drawing, and this
        // is where it is fixed.
        const brightfield = config.image_kind === "brightfield";
        const disableSmoothing = () => {
            const canvas = this.viewer?.drawer?.canvas;
            const context = canvas?.getContext?.("2d");
            if (!context) {
                return;
            }
            context.mozImageSmoothingEnabled = brightfield;
            context.webkitImageSmoothingEnabled = brightfield;
            context.msImageSmoothingEnabled = brightfield;
            context.imageSmoothingEnabled = brightfield;
            if (brightfield) {
                context.imageSmoothingQuality = "high";
            }
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
            delete e.tile._fillWeight;
        });

        // GL initialization: on 'open', size the GL canvas, compile shaders,
        // then wire the real tile-loaded/tile-drawing handlers and force
        // existing items to redraw. viewerManager.js manually re-raises 'open'
        // after adding the label tiled image, so this runs more than once by
        // design. See views/glInit.js.
        const initGL = PlexoraGL.createGLInit({
            viewer: this.viewer,
            renderer,
            config: this.config,
            handleTileLoaded,
            tileDrawingCustom,
            tileDrawingDefault,
        });
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
            fontColor: "rgb(255, 255, 255)",
            color: "rgb(255, 255, 255)",
            backgroundColor: "rgba(0, 0, 0, 0.45)",
            barThickness: 3,
            // Through the shared builder, or an uncalibrated image would get a
            // bar here and lose it at the addScaleBar() call, depending only
            // on which ran last.
            ...this.scalebarScaleOptions(),
        });
        this.styleScaleBar();

        // Add event mouse handler (cell selection)
        this.viewer.addHandler("canvas-nonprimary-press", (e) => {
            // Right click (cell selection)
            if (event.button === 2) {
                const { numericData } = this;
                const { source } = e.eventSource;
                const tiledImage = this.referenceItem();
                if (!tiledImage) return undefined;
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
                    // Convert that to viewport coordinates, the lingua franca
                    // of OpenSeadragon coordinates. Through the view transform:
                    // OSD's pointFromPixel undoes a rotation but not a flip.
                    const viewportPoint = PlexoraViewTransform.pointFromPixel(that.viewer, webPoint);
                    // Convert from viewport coordinates to image coordinates.
                    const anchor = that.referenceItem();
                    if (!anchor) return undefined;
                    let imagePoint = anchor.viewportToImageCoordinates(viewportPoint);
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

        // ONE pass, not one per world item.
        //
        // CanvasOverlayHd calls onRedraw once per item in the world, each time
        // with the context pre-transformed into THAT item's image space. Nothing
        // here ever claimed a pass, so at seven active channels the selection
        // polygon was stroked seven times and every centroid was filled seven
        // times at globalAlpha 0.9 -- which is why a "0.9" dot reads as opaque.
        // It has been invisible only because every item currently shares one
        // transform, so the seven passes landed on top of each other. The moment
        // a layer carries its own it becomes N ghosts at N positions.
        //
        // The guard is the stack's anchor rather than `index !== 0`: item 0 is
        // the RGB base for a brightfield project, whichever channel was added
        // first for a fluorescence one, and whatever was last dragged after any
        // setItemIndex. See LayerStack.anchorIndex.
        //
        // This is the one change in this phase that moves pixels, and it moves
        // them towards what the code always said: centroid alpha is now the 0.9
        // drawCentroids asks for instead of 1 - 0.1^N.
        this.canvasOverlay = new OpenSeadragon.CanvasOverlayHd(this.viewer, {
            onRedraw: function (opts) {
                if (opts.index !== that.layerStack.anchorIndex()) return;
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
                // Plugin overlays last, so they sit over core's own two. Each
                // one is drawn inside a save()/restore() and through its layer's
                // transform -- see OverlayHost.drawAll.
                that.overlays.drawAll(opts);
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
            // Deliberately does NOT wait for GL here, though waitForGLReady is
            // right below and reads like it belongs. `glReady` resolves inside
            // GL init, which runs on OSD's `open`, which viewerManager raises
            // only after a channel has been added -- and channels are added by
            // viewerSidebar.init, which main.js runs AFTER awaiting this
            // method. So the wait could never be satisfied; it always ran its
            // 5000 ms timeout out and then continued in exactly the state it
            // would have had with no wait at all (the textures below come from
            // renderer.gl, which exists from construction). Five seconds of
            // blank viewer on every project with a mask, for nothing.
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

    /**
     * Adopt the layer list the server computed for this project.
     *
     * The list arrives with `/config` (see serve_config) and already describes
     * what this viewer draws today: the reference image, the mask when there is
     * one, the centroids when the table has coordinates, then anything
     * registered alongside them. Adopting it is therefore a no-op on screen --
     * which is the point. The stack is being given a model of what is already
     * happening before anything is asked to draw differently.
     *
     * Registering is idempotent and keeps whatever a layer already holds, so a
     * channel claimed by ViewerManager before this ran is not reset by it.
     *
     * @param layers - `config.layers`, server-ordered bottom first
     */
    syncLayers(layers) {
        const list = Array.isArray(layers) ? layers : [];
        const MASK_LAYER_ID = PlexoraLayerStack.MASK_LAYER_ID;
        for (const spec of list) {
            if (!spec?.id) continue;
            //: Saved state only on the way in. A re-sync (adoptLayers, the
            //: /config poll) must not undo the eye the user just clicked, so
            //: `visible` and `opacity` are passed on first registration and
            //: never again -- the stack is authoritative from then on.
            const first = !this.layerStack.has(spec.id);
            const saved = first ? {
                visible: spec.visible !== false,
                opacity: Number.isFinite(Number(spec.render?.opacity))
                    ? Number(spec.render.opacity) : 1,
                pinned: spec.id === MASK_LAYER_ID,
            } : {};
            this.layerStack.register(spec.id, {
                kind: spec.kind,
                label: spec.label || spec.id,
                src: spec.src || null,
                channelIndex: spec.channelIndex ?? null,
                //: Kept whole so a per-kind fact nobody has modelled yet (a
                //: mask's segmentationMode, a points layer's manifest url) is
                //: reachable without this method growing a field for it.
                spec,
                transform: spec.transform || null,
                ...saved,
            });
        }
        if (list.length) this.layerStack.setOrder(list.map((spec) => spec.id));
        // A Visium table's centroids are spots of a stated size, in reference
        // pixels -- see Project.visium_spot_radius. Null draws dots.
        const centroids = list.find(
            (spec) => spec?.id === PlexoraLayerStack.CENTROID_LAYER_ID);
        const radius = Number(centroids?.render?.radius);
        this.centroidImageRadius = Number.isFinite(radius) && radius > 0
            ? radius : null;
        return this.layerStack;
    }

    /**
     * The image-pixel rectangle currently on screen.
     *
     * In FULL-RESOLUTION reference-layer pixels, which is the space ROIs are
     * stored in, centroids are drawn in, and `figureSceneSnapshot` records
     * viewports in. Every plugin that culls by viewport needs this, and ROI had
     * to work it out for itself -- including the extraZoomLevels divide, which
     * is the part that is wrong by a power of two if it is forgotten.
     *
     * @param pad - image pixels of slack, so a shape whose centre is just off
     *   screen but whose edge is on it still counts as visible
     * @returns { minX, minY, maxX, maxY } or null before the world has an item
     */
    /**
     * The world item every screen<->image conversion is done against.
     *
     * THE REFERENCE IMAGE, found by asking, not `getItemAt(0)`. Six places
     * used to take item 0 on the reasoning that the reference image is at the
     * bottom of the world and therefore first. It is not, necessarily: its
     * card can be dragged now, and a registered layer at index 0 carries its
     * OWN affine -- so a click would be converted through another slide's
     * registration and land somewhere else on the tissue, and every centroid
     * would be drawn at that offset. Nothing throws; the picture is simply
     * wrong by however far the two slides are apart.
     *
     * `anchorIndex` is the existing answer to exactly this question, which is
     * why it is asked here rather than answered again.
     */
    referenceItem() {
        const index = this.layerStack?.anchorIndex?.() ?? -1;
        const world = this.viewer?.world;
        if (!world) return null;
        //: A world with items but no stack to rank them is every test harness
        //: and the moment before `syncLayers` has run.
        if (index < 0) return world.getItemCount() ? world.getItemAt(0) : null;
        return world.getItemAt(index) || null;
    }

    viewportImageBounds(pad = 0) {
        try {
            const item = this.referenceItem();
            if (!item) return null;
            // The BOUNDING BOX of the view: under a rotation getBounds is a
            // turned rectangle whose x/y is a rotated corner, and reading it
            // as an axis-aligned box culls the wrong part of the image.
            // Identical to getBounds when upright (getBoundingBox clones).
            const rect = item.viewportToImageRectangle(
                this.viewer.viewport.getBounds(true).getBoundingBox());
            const scale = 2 ** (this.config?.extraZoomLevels || 0);
            return {
                minX: rect.x / scale - pad,
                minY: rect.y / scale - pad,
                maxX: (rect.x + rect.width) / scale + pad,
                maxY: (rect.y + rect.height) / scale + pad,
            };
        } catch (error) {
            return null;
        }
    }

    /**
     * The cell ids currently on screen.
     *
     * Nearly free, and that is the point: `centroidTiles` already holds exactly
     * the cells in view, because the viewport is what decided which tiles to
     * fetch. A plugin asking "which cells can the user see" would otherwise
     * either request them again or walk the whole table.
     *
     * Empty when centroids have never been loaded -- which is a real state, not
     * an error: a project with no feature table has no per-cell positions at all.
     */
    visibleCellIds() {
        const seen = new Set();
        for (const tile of this.centroidTiles.values()) {
            const ids = tile?.ids;
            if (!ids) continue;
            for (let i = 0; i < ids.length; i += 1) seen.add(ids[i]);
        }
        return Uint32Array.from(seen);
    }

    /**
     * Be told when the view moved, at most once per frame.
     *
     * Throttled HERE rather than in each plugin, because OSD raises `animation`
     * once per pointer move during a drag -- so the naive handler runs dozens of
     * times between two paints, and every plugin that ever listens has to
     * discover that and write its own rAF gate.
     *
     * @returns a function that stops the listening.
     */
    onViewportChange(fn) {
        if (typeof fn !== "function") return () => {};
        if (!this._viewportListeners) {
            this._viewportListeners = new Set();
            this._viewportFrame = null;
            const fire = () => {
                this._viewportFrame = null;
                const bounds = this.viewportImageBounds();
                for (const listener of this._viewportListeners) {
                    try {
                        listener(bounds);
                    } catch (error) {
                        console.error("viewport listener failed", error);
                    }
                }
            };
            const schedule = () => {
                if (this._viewportFrame) return;
                this._viewportFrame = typeof requestAnimationFrame === "function"
                    ? requestAnimationFrame(fire)
                    : setTimeout(fire, 0);
            };
            this.viewer?.addHandler?.("animation", schedule);
            this.viewer?.addHandler?.("animation-finish", schedule);
            this.viewer?.addHandler?.("resize", schedule);
        }
        this._viewportListeners.add(fn);
        return () => this._viewportListeners.delete(fn);
    }

    /**
     * Draw something over the image.
     *
     * The rendering primitive a plugin gets instead of building its own overlay:
     * core owns the canvas, the transform, the anchor guard, the frame
     * coalescing and the save()/restore() around every pass. The plugin owns
     * what is drawn.
     *
     * @param spec - { id, draw, hitTest, layerId, order, visible }
     * @returns { id, invalidate, remove, setVisible, setOrder }
     */
    addOverlay(spec) {
        return this.overlays.add(spec);
    }

    removeOverlay(id) {
        return this.overlays.remove(id);
    }

    /**
     * Repaint the overlay canvas without redrawing a single tile.
     *
     * CanvasOverlayHd repaints on `update-viewport`, which a pan raises and a
     * hover does not -- so something has to be able to say "the geometry
     * changed, the view did not". Cheaper than forceRedraw by exactly the tile
     * work it skips, which at seven channels is most of the frame.
     */
    repaintOverlay() {
        const overlay = this.canvasOverlay;
        if (!overlay) return;
        overlay.resize();
        overlay.clear();
        overlay._updateCanvas();
    }

    /**
     * Ask what is under a point, topmost overlay first.
     *
     * @param x / y - full-resolution image pixels in the REFERENCE layer's space
     */
    overlayAt(x, y, opts = {}) {
        return this.overlays.hitTest(x, y, opts);
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
        return this._cellStack.get(this._cellStack.active)?.provider || null;
    }

    /**
     * The name of the active cell layer's plugin, or null.
     */
    get cellLayerOwner() {
        return this._cellStack.active;
    }

    /** One layer's record by name, or null. */
    getCellLayer(name) {
        return this._cellStack.get(name);
    }

    /** Every registered layer, bottom of the stack first. */
    cellLayers() {
        return this._cellStack.all();
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
        view.gateMask = this.segmentationGateMask || null;
        view.gateCount = this.segmentationGateCount ?? null;
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
        if (!this._cellStack.size) {
            return [this.coreLayerView()];
        }
        return this._cellStack.all().filter(
            (layer) => layer.visible && ImageViewer.MASK_MODES.includes(layer.mode));
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
        if (!this._cellStack.size) {
            return [this.coreLayerView()];
        }
        return this._cellStack.all().filter(
            (layer) => layer.visible && layer.mode === "centroids");
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
        const existed = this._cellStack.has(name);
        const layer = this._cellStack.register(name);
        if (!existed || provider) layer.provider = provider || null;
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
        // `undefined` means there was nothing under that name. The stack also
        // hands the active layer over to the topmost survivor, so removing the
        // tool being looked at does not strand the shared controls while other
        // layers are still on screen.
        if (this._cellStack.unregister(name) === undefined) return false;
        this.labelGpu?.dropLayer?.(name);
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
        return this._cellStack.setActive(name);
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
        const layer = this._cellStack.get(name);
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
        const layer = this._cellStack.get(name);
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
        const layer = this._cellStack.get(name);
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
        if (!this._cellStack.setOrder(names)) return false;
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
        const layer = this._cellStack.get(name);
        if (!layer) return false;
        layer.lut = lut || null;
        layer.lutVersion = (layer.lutVersion || 0) + 1;
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
     * Every layer's own `opacity`, core's included. It used to be 1 for any
     * layer with no colour table, on the reasoning that the control belonged to
     * whichever plugin owned the colours -- which made the shared Opacity
     * slider a control that did nothing for the two cases it was most often on
     * screen for: Thresholding, whose cell layer carries no LUT, and a viewer
     * with no plugin at all, where the slider was not offered. A slider reading
     * 70% over a mask drawn at 100% is worse than either answer.
     *
     * The numbers are unchanged where they were already honoured: a registered
     * layer still starts at DEFAULT_CELL_LAYER_OPACITY and core's own layer
     * still starts at 1, so a plain viewer draws exactly what it always did.
     */
    layerAlpha(layer) {
        const value = Number(layer?.opacity);
        return Number.isFinite(value) ? value : 1;
    }

    /**
     * @function setCellDisplayOpacity - how strongly core's OWN cell layer
     * sits over the tissue, for a viewer with no plugin layers.
     *
     * The counterpart of setLayerOpacity, and the other half of what makes the
     * Opacity control canvas-level rather than a plugin's: a segmentation mask
     * can be turned on with no tool open at all, and fading it against the
     * tissue is the same wish whoever turned it on had.
     *
     * Composite-time, like setLayerOpacity: a redraw, never a re-render.
     */
    setCellDisplayOpacity(value) {
        const next = Math.max(0, Math.min(1, Number(value)));
        if (!Number.isFinite(next) || next === this._coreLayerView.opacity) return false;
        this._coreLayerView.opacity = next;
        this.viewer?.forceRedraw?.();
        return true;
    }

    /** What that slider should read with no plugin layer active. */
    get cellDisplayOpacity() {
        return this._coreLayerView.opacity;
    }

    /**
     * @function setOverlayMuted - take the selected cells off the picture, and
     * put the SAME pixels back.
     *
     * HIDING IS NOT THE SAME QUESTION AS TURNING OFF, and conflating them is
     * what made this slow in one direction. `selectMode("none")` and the card's
     * eye both mean "I am done with this": the mask item is unloaded, the
     * layers' per-tile canvases are dropped (`dropLayerContexts`) and the point
     * overlay's manifest work is abandoned. Going back then costs a pyramid
     * read, a filter round trip and a boundary re-render for every tile in
     * view -- the better part of a second on a real slide, against the
     * instant, free teardown that preceded it.
     *
     * Looking at the tissue under the cells is a different wish, and it is the
     * commonest one: it is asked and un-asked several times a minute. So this
     * changes one boolean that two draw-time gates consult -- the label blit
     * (`labelOutlinesEnabled`) and the point overlay (`shouldDrawCentroids`) --
     * and repaints. Nothing is unloaded, nothing is refetched, nothing is
     * re-rendered: the tile canvases still hold the right pixels, so both
     * directions cost exactly one frame.
     *
     * Deliberately NOT a mode, and deliberately invisible to the Cells control:
     * what comes back has to be what was taken away, down to which layers were
     * showing and how each one was drawn.
     */
    setOverlayMuted(muted) {
        const next = Boolean(muted);
        if (next === this.overlayMuted) return false;
        this.overlayMuted = next;
        // Cheap either way: clearing the overlay canvas, or drawing it again
        // from the centroid tiles already in `centroidTiles`. The catch-up
        // pass is for a viewer that was panned while the cells were hidden --
        // it fetches only tiles that are actually missing, and none are when
        // the view has not moved.
        this.refreshCentroidOverlay();
        if (!next) this.scheduleCentroidTileUpdate(0);
        this.viewer?.forceRedraw?.();
        return true;
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

        // A no-op while the lens is closed, which is what lets this sit on a
        // channel mutator at all. Only this one may need to fetch: a colour or
        // range change is served from the mini-map's cached greyscale.
        this.miniMap?.invalidate({ refetch: true });
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
        this.miniMap?.invalidate();
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
        this.miniMap?.invalidate();
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
        // The same draw-time gate the label blit takes: muted means "do not
        // paint them", never "forget them" -- see setOverlayMuted.
        return this.show_centroids && !this.overlayMuted;
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
        const item = this.referenceItem();
        if (!item) return null;
        // Bounding box, not the turned rectangle -- see viewportImageBounds.
        const bounds = this.viewer.viewport.getBounds(true).getBoundingBox();
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
        const item = this.referenceItem();
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
        const target = name === undefined ? this._cellStack.active : name;
        const layer = target ? this._cellStack.get(target) : null;
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
            let gate = null;
            // In the browser first, when the provider says its gate is plain
            // column ranges (the gating plugin): the columns are fetched once
            // per marker and every later tick is a pass over them here -- no
            // request, no id list. Anything that stops that (no table, a
            // column the server will not give, ids past 2^24) falls through to
            // the provider's own answer, exactly as before.
            if (hasGates && provider?.localRangeGate) {
                gate = await this.evaluateGateLocally(gates);
                if (requestId !== current()) return;
            }
            if (hasGates && !gate && provider?.getSelectedIds) {
                const selected = await provider.getSelectedIds(gates);
                if (requestId !== current()) return;
                ids = selected instanceof Set ? selected : null;
            }
            if (layer) {
                layer.filterIds = ids;
                layer.gateMask = gate ? gate.mask : null;
                layer.gateCount = gate ? gate.count : null;
            } else {
                this.segmentationFilterIds = ids;
                this.segmentationGateMask = gate ? gate.mask : null;
                this.segmentationGateCount = gate ? gate.count : null;
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
        if (this.labelGpuMode?.()) {
            // The GPU draws this tile when it is on screen (tileColorize.js);
            // all that is worth doing once per tile is the fill weight.
            this.prepareGpuTile(tile);
            return null;
        }
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
        if (this.labelGpuMode?.()) {
            this.bumpLabelVersions(name);
            return;
        }
        if (!this.renderLabelTile) return;
        const draw = this.maskDrawList();
        this.forEachLabelTile((tile) => {
            if (!tile._array || !tile._layerContexts) return;
            this.renderTileLayers(tile, draw, name);
        });
    }

    /** Whether the label tiles are drawn by the GPU cell layer right now. */
    labelGpuMode() {
        return this._labelRenderer === "gpu" && Boolean(this.labelGpu?.active);
    }

    /** "gpu" | "cpu": what the label tiles are drawn with now. */
    get labelRenderer() {
        return this.labelGpuMode() ? "gpu" : "cpu";
    }

    /** What they should be drawn with: the GPU when it is up, unless the user
     *  asked for the CPU, or the renderer is software and the measured default
     *  for software renderers says otherwise. */
    desiredLabelRenderer() {
        if (!this.labelGpu?.active) return "cpu";
        const pref = this._labelRendererPref;
        if (pref === "cpu" || pref === "gpu") return pref;
        return this.labelGpu.isSoftware() ? PlexoraLabelGpu.SOFTWARE_DEFAULT : "gpu";
    }

    /**
     * @function setLabelRenderer - draw the cell layer on the GPU or the CPU.
     * @param mode - "gpu", "cpu", or null for the default. Not persisted: set
     *   localStorage plexoraLabelRenderer, or ?labelRenderer=, for that.
     * @returns what the tiles are drawn with afterwards
     */
    setLabelRenderer(mode) {
        this._labelRendererPref = mode === "gpu" || mode === "cpu" ? mode : null;
        return this.applyLabelRenderer();
    }

    /**
     * Move every loaded label tile to the renderer desiredLabelRenderer names.
     * To the GPU: the per-layer canvases are dropped (the GPU draws from the
     * decoded ids). To the CPU: they are built again, for every layer drawn.
     * Also the context-loss path: labelGpu calls this when it turns itself off.
     */
    applyLabelRenderer() {
        const next = this.desiredLabelRenderer();
        if (next === this._labelRenderer) return next;
        this._labelRenderer = next;
        if (next === "gpu") {
            this.forEachLabelTile((tile) => {
                tile._layerContexts?.clear();
                delete tile._layerContexts;
                if (tile._array) this.prepareGpuTile(tile);
            });
            this.bumpLabelVersions(null);
        } else {
            this.labelGpu?.clear?.();
            const draw = this.maskDrawList();
            this.forEachLabelTile((tile) => {
                if (tile._array) this.renderTileLayers(tile, draw);
            });
        }
        this.viewer?.forceRedraw?.();
        return next;
    }

    /** The one per-tile quantity the GPU path takes from the CPU: how far the
     *  tile has gone from outlines to fill (labelTile.smallCellWeight). Only a
     *  datasource storing whole labels derives outlines, so only it needs it. */
    prepareGpuTile(tile) {
        if (!tile?._array || tile._fillWeight !== undefined) return;
        if (this.config?.segmentationMode !== "filled") return;
        const width = tile._labelWidth;
        const height = tile._labelHeight;
        if (!width || !height) return;
        tile._fillWeight = PlexoraLabelTile.fillWeightOf(tile._array, width, height);
    }

    /** The GPU path's re-render: mark the layer's pixels changed, so every
     *  tile on screen redraws it at the next frame. Null marks them all. */
    bumpLabelVersions(name = null) {
        const bump = (layer) => { layer.renderVersion = (layer.renderVersion || 0) + 1; };
        const layer = name ? this._cellStack.get(name) : null;
        if (layer) {
            bump(layer);
        } else {
            this._cellStack.all().forEach(bump);
            bump(this._coreLayerView);
        }
        this.viewer?.forceRedraw?.();
    }

    /**
     * The gate, evaluated here: the gated columns (numericData.getColumn,
     * cached per marker) against the server's rules
     * (PlexoraLabelGpu.evaluateGateMask). Null when it cannot be -- the caller
     * then asks the provider, as it always did.
     */
    async evaluateGateLocally(gates) {
        const numericData = this.numericData;
        if (typeof PlexoraLabelGpu === "undefined" || !numericData?.getColumn
            || !numericData.hasCellTable?.()) return null;
        try {
            if (!this.ids?.length) {
                const { ids, centers } = await numericData.loadCells();
                this.ids = ids || [];
                if (!this.centers?.length) this.centers = centers || [];
            }
            const ids = this.ids;
            if (!ids.length) return null;
            const keys = Object.keys(gates);
            const values = await Promise.all(keys.map((key) => numericData.getColumn(key)));
            const columns = {};
            keys.forEach((key, i) => {
                if (!values[i] || values[i].length !== ids.length) {
                    throw new Error(`column ${key} has ${values[i]?.length} values for ${ids.length} cells`);
                }
                columns[key] = values[i];
            });
            const gate = PlexoraLabelGpu.evaluateGateMask(ids, columns, gates);
            if (gate.maxId + 1 > PlexoraLabelGpu.MAX_IDS) return null;
            return gate;
        } catch (e) {
            console.warn("Gate evaluated on the server instead:", e.message || e);
            return null;
        }
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

    /**
     * The top-left corner of the canvas: the sample's name, and under it the
     * one key worth knowing about what is drawn over it.
     *
     * The hint is built here, empty and hidden, rather than by the control it
     * belongs to: ViewerControls fills it once it knows whether this project
     * can draw cells at all (paintOverlayHint), and a project that cannot
     * never sees it. Built here because this is what owns the overlays on
     * #openseadragon_wrapper -- the label, the legend and the mini-map.
     *
     * The column carries the positioning now; the label is a block inside it.
     * The RGB quick view builds the label on its own with no column, which is
     * why `.viewer-project-label` still positions itself and viewer.css undoes
     * that for the one inside a caption.
     */
    initProjectLabel() {
        const wrapper = document.getElementById("openseadragon_wrapper");
        if (!wrapper || document.getElementById("viewer_project_label")) return;
        const caption = document.createElement("div");
        caption.id = "viewer_canvas_caption";
        caption.className = "viewer-canvas-caption";
        const label = document.createElement("div");
        label.id = "viewer_project_label";
        label.className = "viewer-project-label";
        label.textContent = datasource || "";
        const hint = document.createElement("div");
        hint.id = "viewer_overlay_hint";
        hint.className = "viewer-overlay-hint";
        hint.hidden = true;
        // A key cap and a sentence, and the cap is a real <kbd>: over an image,
        // a bare letter in a line of text reads as a legend marker or a panel
        // label -- which is exactly what the letters on this canvas usually
        // ARE. The outline is what says "press this" without a word spent
        // saying it. Same treatment as Figure Builder's shutter key.
        const key = document.createElement("kbd");
        key.className = "viewer-overlay-hint-key";
        const text = document.createElement("span");
        text.setAttribute("data-role", "label");
        hint.append(key, text);
        caption.append(label, hint);
        wrapper.appendChild(caption);
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

    /**
     * The overview lens in the bottom-left corner. Built here alongside the
     * project label and the channel legend because it is the same kind of
     * thing: a DOM overlay on #openseadragon_wrapper that core owns.
     *
     * Guarded on `typeof` rather than window.MiniMap -- `class` in a classic
     * script creates a global LEXICAL binding, which never becomes a property
     * of window, so window.MiniMap is undefined even when the script loaded.
     *
     * Everything expensive about the mini-map is deferred to the first time
     * the user opens it; constructing it here costs a few DOM nodes.
     */
    initMiniMap() {
        if (typeof MiniMap === "undefined") {
            return;
        }
        this.miniMap = new MiniMap(this);
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
        // No longer gated on `imgMetadata` being present: an image whose file
        // said nothing still gets a bar, counted in pixels. The old body
        // computed a pixelsPerMeter from the unit and then overwrote it with
        // getPixelsPerMeter() on the very next line, so that arithmetic is
        // deleted rather than moved.
        this.viewer.scalebar({
            location: OpenSeadragon.ScalebarLocation.BOTTOM_RIGHT,
            minWidth: "100px",
            type: OpenSeadragon.ScalebarType.MICROSCOPY,
            stayInsideImage: false,
            fontColor: "rgb(255, 255, 255)",
            color: "rgb(255, 255, 255)",
            backgroundColor: "rgba(0, 0, 0, 0.45)",
            barThickness: 3,
            ...this.scalebarScaleOptions(),
        });
        this.styleScaleBar();
    }

    /**
     * Which of the two things the bar measures, as scalebar() options.
     *
     * One builder for both construction sites and for every later refresh, so
     * a calibration that arrives after the page did cannot leave the bar in
     * the mode it was built in. `pixelsPerMeter: 1` is what keeps an
     * uncalibrated bar VISIBLE at all -- the plugin hides itself on a falsy
     * one (see its refresh()) -- and makes "one meter" mean "one image pixel",
     * which is what pixelScaleSizeAndText then labels.
     */
    scalebarScaleOptions() {
        if (!this.show_scalebar) return { pixelsPerMeter: null };
        const perMeter = this.getPixelsPerMeter();
        if (perMeter) {
            return {
                pixelsPerMeter: perMeter,
                sizeAndTextRenderer:
                    OpenSeadragon.ScalebarSizeAndTextRenderer.METRIC_LENGTH,
            };
        }
        return { pixelsPerMeter: 1, sizeAndTextRenderer: pixelScaleSizeAndText };
    }

    getPixelsPerMeter() {
        const physicalSizeX = Number(this.imgMetadata?.physical_size_x);
        if (!physicalSizeX) return null;
        const unitsPerMeter = {
            "µm": 1000000,
            "um": 1000000,
            "nm": 1000000000,
            "cm": 100,
            "m": 1,
        }[this.imgMetadata?.physical_size_x_unit];
        if (!unitsPerMeter) return null;
        return unitsPerMeter / physicalSizeX;
    }

    /** Whether the bar is currently measuring a physical length rather than
     *  counting pixels. Read by the calibration control to word itself. */
    get isCalibrated() {
        return Boolean(this.getPixelsPerMeter());
    }

    /**
     * Take on a calibration that arrived after the page did.
     *
     * `metadata` is a fresh `/get_ome_metadata` payload rather than a bare
     * number: the server is what decides whether a value is the file's or the
     * user's, and re-reading it is what stops the bar and the control that set
     * it from being able to disagree. A payload with no physical size puts the
     * bar back to counting pixels, which is what clearing one means.
     */
    applyPixelSize(metadata) {
        this.imgMetadata = metadata || {};
        if (!this.viewer?.scalebarInstance) return;
        this.viewer.scalebar(this.scalebarScaleOptions());
        this.styleScaleBar();
    }

    setScalebarVisible(visible) {
        this.show_scalebar = visible;
        if (!this.viewer?.scalebarInstance) return;
        this.viewer.scalebar(this.scalebarScaleOptions());
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
        const item = this.referenceItem();
        if (!item) return;
        const layers = this.centroidDrawList();
        if (!layers.length) return;
        // Bounding box, not the turned rectangle -- see viewportImageBounds.
        const bounds = this.viewer.viewport.getBounds(true).getBoundingBox();
        const imageBounds = item.viewportToImageRectangle(bounds);
        const minX = imageBounds.x;
        const minY = imageBounds.y;
        const maxX = imageBounds.x + imageBounds.width;
        const maxY = imageBounds.y + imageBounds.height;
        const safeImageZoom = Math.max(imageZoom, 0.0001);
        const radius = this.centroidRadius(safeImageZoom);
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
        const item = this.referenceItem();
        if (!item) return;
        const layers = this.centroidDrawList();
        if (!layers.length) return;
        // Bounding box, not the turned rectangle -- see viewportImageBounds.
        const bounds = this.viewer.viewport.getBounds(true).getBoundingBox();
        const imageBounds = item.viewportToImageRectangle(bounds);
        const minX = imageBounds.x;
        const minY = imageBounds.y;
        const maxX = imageBounds.x + imageBounds.width;
        const maxY = imageBounds.y + imageBounds.height;
        const safeImageZoom = Math.max(imageZoom, 0.0001);
        const radius = this.centroidRadius(safeImageZoom);
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

    /**
     * @function centroidRadius - a centroid's radius in IMAGE pixels.
     *
     * A spot of stated size keeps that size at every zoom, scaled by the
     * user's multiplier, but never shrinks below a dot one can see: at the
     * overview a 55 micron spot is a pixel across.
     */
    centroidRadius(imageZoom) {
        const dot = this.getCentroidScreenRadius(imageZoom) / imageZoom;
        if (!this.centroidImageRadius) return dot;
        return Math.max(this.centroidImageRadius * this.centroidPointScale,
            1.5 / imageZoom);
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
        this.fillExportGround(ctx, canvas.width, canvas.height);
        ctx.drawImage(baseCanvas, 0, 0);
        this.drawLegendOnCanvas(ctx, canvas.width, canvas.height);

        const pngName = `${datasource || "plexora"}_current_view.png`;
        // The desktop app's window cannot follow a download link to a data
        // URL (WKWebView ignores it), so it saves through a native dialog.
        if (window.PlexoraDesktop) {
            canvas.toBlob((blob) => { if (blob) window.PlexoraDesktop.saveBlob(blob, pngName); },
                          "image/png");
            return;
        }
        const link = document.createElement("a");
        link.download = pngName;
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
        // Behind the raster embed, not over it: the drawer canvas is
        // transparent wherever no tile is drawn, and a PDF page's own ground is
        // white -- which for a fluorescence composite means the black the
        // viewer showed goes missing at every edge.
        pdf.setFillColor(...this.exportGroundRgb());
        pdf.rect(0, 0, width, height, "F");
        pdf.addImage(baseCanvas.toDataURL("image/png"), "PNG", 0, 0, width, height);

        if (this.show_scalebar) this.drawScalebarVector(pdf);
        this.drawLegendVector(pdf);
        this.drawProjectLabelVector(pdf);

        const pdfName = `${datasource || "plexora"}_current_view.pdf`;
        if (window.PlexoraDesktop) {
            window.PlexoraDesktop.saveBlob(pdf.output("blob"), pdfName);
        } else {
            pdf.save(pdfName);
        }
    }

    /**
     * The colour behind an exported view.
     *
     * A transmitted-light slide's own background is white and its stains are
     * dark; a fluorescence composite's background is black and its signal is
     * bright. Getting this wrong does not merely look different -- it inverts
     * which part of the picture reads as "nothing here".
     *
     * WHICHEVER THE USER CHOSE, first. Those two defaults used to be the whole
     * answer, and they were the same two viewer.css falls back to -- which was
     * fine while the ground was not settable and is a second, disagreeing
     * answer now that it is. Somebody who set the ground to white to read a
     * composite against a slide would export it on black.
     */
    exportGroundRgb() {
        const chosen = this.layerStack
            ?.get(PlexoraLayerStack.REFERENCE_LAYER_ID)?.spec?.render?.background;
        const parsed = /^#([0-9a-fA-F]{6})$/.exec(String(chosen || ""));
        if (parsed) {
            const value = parseInt(parsed[1], 16);
            return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
        }
        return this.config.image_kind === "brightfield"
            ? [251, 251, 252]
            : [0, 0, 0];
    }

    fillExportGround(ctx, width, height) {
        const [r, g, b] = this.exportGroundRgb();
        ctx.fillStyle = `rgb(${r}, ${g}, ${b})`;
        ctx.fillRect(0, 0, width, height);
    }

    /**
     * Claim, or release, the viewer's centre spinner for a piece of work.
     *
     * Ref-counted through viewerLoader.js rather than a bare show/hide. This
     * used to write `display` on the element directly, which meant two
     * overlapping true/false pairs -- auto-contrast inside a channel load, say
     * -- cancelled each other: the inner `false` hid the spinner while the
     * outer work was still going. Every call site is a balanced try/finally
     * pair, so a stack is enough; an unmatched `false` is a no-op rather than
     * somebody else's spinner going out.
     */
    setLoading(isLoading) {
        if (isLoading) {
            const release = window.PlexoraViewerLoader?.hold();
            if (release) this._loaderHolds.push(release);
            return;
        }
        this._loaderHolds.pop()?.();
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
