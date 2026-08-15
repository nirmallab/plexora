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

class ImageViewer {
    // Vars
    viewerManagers = [];

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
        // Optional add-on module hook: { getSelectedIds(filter), supportsColorCoding(),
        // getColorCodedRanges(), eval_mode } -- see init() and the modeFlags/
        // colorCodedKeys/colorCodedRanges getters below. Null when no module is active.
        this.selectionProvider = null;
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

        // list_lassos stays permanently empty now that lasso drawing has been
        // removed; kept so saveGatingList/downloadGatingCSV keep a stable signature.
        this.list_lassos = {};
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
            maxImageCacheCount: 100,
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

        // Flexible use of textures
        const constantTextures = ["ids", "centers", "gatings", "pickings"];
        const otherOffset = 32 - constantTextures.length;
        const via = new GLRenderer();
        const nMarkers = 4;
        const markerOffset = otherOffset - nMarkers;
        const nTiles = markerOffset;
        const tileTextureKeys = [...Array(nTiles).keys()];
        const markerTextureKeys = [...Array(nMarkers).keys()];
        via._otherOffset = otherOffset;
        via._markerOffset = markerOffset;
        via._tileTextures = tileTextureKeys.map(() => "");
        via._markerTextures = markerTextureKeys.map(() => "");
        via._constantTextures = constantTextures;
        via._activeMarkerTexture = 0;
        via._nextMarkerTexture = 0;
        via._activeTileTexture = 0;
        via._nextTileTexture = 0;
        this.viaGL = via;

        const getTileTexture = this.getTileTexture.bind(this);
        const indexOfTexture = this.indexOfTexture.bind(this);
        const selectTexture = this.selectTexture.bind(this);
        const resolveGLReady = this.resolveGLReady;

        via.loadArray = function (e, w, h) {
            // Allow for custom drawing in webGL
            var gl = this.gl;
            const { source } = e.tiledImage;
            const tileArgs = [e.tile.level, e.tile.x, e.tile.y];
            const format = e.tile._format || `u${source.format}`;
            const okFormat = ["u16", "u32", "u8"].includes(format);
            const tKey = source.getTileKey(...tileArgs);
            const pixels = e.tile._array;

            // Clear before starting all the draw calls
            gl.clearColor(0, 0, 0, 0);
            gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);

            // Reset texture for GLSL
            const oldKey = getTileTexture();
            // Only transfer texture if needed
            if (oldKey != tKey && okFormat) {
                this._activeTileTexture = indexOfTexture(tKey, "T");
                selectTexture(gl, this.texture, this._activeTileTexture);
                const textureArgs = {
                    u16: [gl.RG8UI, w, h, 0, gl.RG_INTEGER],
                    u32: [gl.RGBA8UI, w, h, 0, gl.RGBA_INTEGER],
                    u8: [gl.R8UI, w, h, 0, gl.RED_INTEGER],
                }[format];

                // Send the tile into the texture.
                gl.texImage2D(gl.TEXTURE_2D, 0, ...textureArgs, gl.UNSIGNED_BYTE, pixels);
            }

            this.gl_arguments.tile_shape_2fv = new Float32Array([w, h]);

            // Call gl-drawing after loading
            this["gl-drawing"].call(this);
            gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
            return gl.canvas;
        };

        via.vShader = plexoraUrl("client/src/shaders/vert.glsl");
        via.fShader = plexoraUrl("client/src/shaders/frag.glsl");

        // Default tile-drawing behavior (invoked as the "callback" from the
        // custom handler below, mirroring viaWebGL's io/default dispatch shape)
        function tileDrawingDefault(e) {
            var w = e.rendered.canvas.width;
            var h = e.rendered.canvas.height;
            var gl_w = via.width;
            var gl_h = via.height;

            // Render a webGL canvas to an input canvas
            var output = via.loadArray(e, w, h);
            e.rendered.drawImage(output, 0, 0, gl_w, gl_h, 0, 0, w, h);
        }

        const { floatRange } = this.numericData;
        const findCurrentChannel = this.findCurrentChannel.bind(this);
        const selectCenterProps = this.selectCenterProps.bind(this);
        const labelOutlinesEnabled = () => !!this.viewerManagerVMain?.sel_outlines;
        // Custom tile-drawing handler
        const tileDrawingCustom = async (callback, e) => {
            // Read parameters from each tile
            const { source } = e.tiledImage;
            const { tileFormat } = source;
            const group = e.tile.getUrl().split("/");
            const sub_url = group[group.length - 3];
            const centerProps = selectCenterProps(e.tile, source);

            // Clear the rendered tile up front so every early return below
            // (missing data, outlines disabled, ...) leaves a properly blank
            // tile instead of whatever pixels were last drawn into this
            // canvas -- previously the clear happened after these guards,
            // so a skipped tile kept showing stale (or, before the
            // gl.texImage2D(undefined) bug below was fixed, garbage) content
            // from its last successful draw. Label/segmentation tiles must
            // stay transparent outside outlines so image channels underneath
            // remain visible.
            const w = e.rendered.canvas.width;
            const h = e.rendered.canvas.height;
            if (tileFormat == 32) {
                e.rendered.clearRect(0, 0, w, h);
            } else {
                e.rendered.fillStyle = "black";
                e.rendered.fillRect(0, 0, w, h);
            }

            if (tileFormat == 32 && e.tile._renderedContext) {
                if (labelOutlinesEnabled()) {
                    e.rendered.drawImage(e.tile._renderedContext.canvas, 0, 0, w, h);
                }
                return;
            }

            if (tileFormat != 32) {
                if (!e.tile._array) {
                    // Not loaded yet -- e.g. right after the HD toggle forces
                    // every visible tile to redraw immediately, before the
                    // freshly-invalidated tile's fetch/decode has finished.
                    // Falling through with pixels=undefined would still reach
                    // gl.texImage2D below, which allocates the texture with
                    // whatever GPU memory happened to be there -- rendered as
                    // solid static instead of skipping this frame like the
                    // segmentation branch below already does.
                    console.warn("Missing Array for tile:", e.tile.getUrl(), "- skipping rendering");
                    return;
                }
                const channel = findCurrentChannel(sub_url);
                const range = _.get(channel, "range", floatRange);
                const color = _.get(channel, "color", d3.color("white"));
                const floatColor = toFloatColor(color);
                // The fast/default tile path quantizes 16-bit -> 8-bit
                // server-side, linear against the channel's true max (see
                // get_channel_gmm's qmin/qmax). The shader now works
                // directly in that same [0, 255] byte domain -- u_tile_range
                // is expressed in byte units too in this mode (see
                // viewerSidebar.js's getImageRange/toImageConnectorRange),
                // so no reconstruction back into 16-bit units is needed here.
                const tileFmt = e.tile._format === "u8" ? 8 : 16;
                // Store channel color and range to send to shader
                via.gl_arguments = {
                    ...centerProps,
                    centers: [],
                    id_end_1i: 0,
                    picked_end_1i: 0,
                    color_3fv: new Float32Array(floatColor),
                    range_2fv: new Float32Array(range),
                    fmt_1i: tileFmt,
                };
            } else {
                if (!e.tile._array) {
                    console.warn("Missing Array for tile:", e.tile.getUrl(), "- skipping rendering");
                    // Skip rendering this tile by returning early
                    return;
                }
                // Use new parameters for this tile
                via.gl_arguments = {
                    ...centerProps,
                    color_3fv: new Float32Array([1, 1, 1]),
                    range_2fv: new Float32Array([0, 1]),
                    fmt_1i: 32,
                };
            }

            // Start webGL rendering
            callback(e);
        };

        via["gl-drawing"] = function () {
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

        via["gl-loaded"] = function (program) {
            // Uniform variables for coloring
            this.u_ids_shape = this.gl.getUniformLocation(program, "u_ids_shape");
            this.u_tile_shape = this.gl.getUniformLocation(program, "u_tile_shape");
            this.u_gating_shape = this.gl.getUniformLocation(program, "u_gating_shape");
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
            const u_gatings = this.gl.getUniformLocation(program, "u_gatings");
            const u_centers = this.gl.getUniformLocation(program, "u_centers");
            const u_pickings = this.gl.getUniformLocation(program, "u_pickings");
            this.gl.uniform1i(u_ids, indexOfTexture("ids", null));
            this.gl.uniform1i(u_gatings, indexOfTexture("gatings", null));
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

        const renderLabelTile = (tileArray, width, height) => {
            const allowedIds = this.segmentationFilterIds;
            const canvas = document.createElement("canvas");
            canvas.width = width;
            canvas.height = height;
            const context = canvas.getContext("2d");
            const imageData = context.createImageData(width, height);
            const output = imageData.data;
            for (let i = 0; i < tileArray.length; i += 4) {
                const cellId = tileArray[i]
                    + tileArray[i + 1] * 256
                    + tileArray[i + 2] * 65536
                    + tileArray[i + 3] * 16777216;
                if (cellId && (!allowedIds || allowedIds.has(cellId))) {
                    output[i] = 255;
                    output[i + 1] = 255;
                    output[i + 2] = 255;
                    output[i + 3] = 220;
                }
            }
            context.putImageData(imageData, 0, 0);
            return context;
        };
        this.renderLabelTile = renderLabelTile;

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
                        e.tile._renderedContext = renderLabelTile(decoded.data, decoded.width, decoded.height);
                    }
                    if (e.tile?._renderedContext) {
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
                        const blob = new Blob([responseArray], { type: "image/webp" });
                        const bitmap = await createImageBitmap(blob, { premultiplyAlpha: "none", colorSpaceConversion: "none" });
                        const { width, height } = bitmap; // capture before close() -- closing zeroes these
                        const canvas = new OffscreenCanvas(width, height);
                        const ctx = canvas.getContext("2d", { willReadFrequently: true });
                        ctx.drawImage(bitmap, 0, 0);
                        const rgba = ctx.getImageData(0, 0, width, height).data;
                        bitmap.close();
                        const u8 = new Uint8Array(width * height);
                        for (let i = 0; i < u8.length; i++) {
                            u8[i] = rgba[i * 4];
                        }
                        e.tile._array = u8;
                        e.tile._format = "u8";
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
            } catch (err) {
                console.log("Load Error, Refreshing", err, e.tile.getUrl());
                forceRepaint();
            }
        };


        this.viewer.addHandler("tile-drawn", (e) => {
            let count = _.size(e.tiledImage._tileCache._tilesLoaded);
            e.tiledImage._tileCache._imagesLoadedCount = count;
            const canvas = e.eventSource.drawer.canvas;
            const context = canvas.getContext("2d");
            context.mozImageSmoothingEnabled = false;
            context.webkitImageSmoothingEnabled = false;
            context.msImageSmoothingEnabled = false;
            context.imageSmoothingEnabled = false;
        });

        this.viewer.addHandler("tile-unloaded", (e) => {
            delete e.tile._array;
        });

        // Equivalent of viaWebGL's openSeadragonGL.init(): on 'open', size the
        // GL canvas, compile shaders, then wire the real tile-loaded/tile-drawing
        // handlers and force existing items to redraw. viewerManager.js manually
        // re-raises 'open' after adding the label tiled image, so this can run
        // more than once by design (matches the original behavior).
        const initGL = () => {
            via.width = via.width || this.config.tileWidth;
            via.height = via.height || this.config.tileHeight;
            via.updateShape(via.width, via.height);
            via.init().then(() => {
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


        // Add automatic tile cache monitoring, evicting least-recently-used
        // tiles per channel instead of clearing every channel's pyramid at
        // once (see evictLeastRecentlyUsedTiles).
        setInterval(() => {
            if (this.viewer && this.viewer.world) {
                const itemCount = this.viewer.world.getItemCount();
                if (itemCount === 0) return;
                let totalTiles = 0;
                // Use the correct OpenSeadragon API method
                for (let i = 0; i < itemCount; i++) {
                    const item = this.viewer.world.getItemAt(i);
                    if (item && item._tileCache && item._tileCache._tilesLoaded) {
                        totalTiles += item._tileCache._tilesLoaded.length || 0;
                    }
                }

                if (totalTiles > 1000) {
                    const perItemBudget = Math.max(150, Math.floor(1000 / itemCount));
                    console.warn(`Large tile cache detected: ${totalTiles} tiles. Evicting least-recently-used tiles (budget ${perItemBudget}/channel)...`);
                    this.evictLeastRecentlyUsedTiles(perItemBudget);
                }
            }
        }, 30000); // Check every 30 seconds

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
     * @param selectionProvider - optional active module instance implementing
     *   { getSelectedIds(filter), supportsColorCoding(), getColorCodedRanges() } --
     *   e.g. CSVGatingList. Null when no add-on module is active.
     * @param centers - List of image pixel coordinates per cell
     * @param ids - List of integer ids per cell
     */
    async init(viewerManager, channelList, selectionProvider, centers, ids) {
        this.channelList = channelList;
        this.selectionProvider = selectionProvider || null;
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
            const via = this.viaGL;
            via.texture_mag = [via.gl.createTexture(), via.gl.createTexture(), via.gl.createTexture(), via.gl.createTexture()];
            via.texture_ids = via.gl.createTexture();
            via.texture_mask = via.gl.createTexture();
            via.texture_gatings = via.gl.createTexture();
            via.texture_centers = via.gl.createTexture();
            via.texture_pickings = via.gl.createTexture();
            this.bindPickings(via, []);
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
     * @function indexOfTexture - return integer for named texture
     * @param label - the texture key label
     * @param scope - type of texture
     * @returns number
     */
    indexOfTexture(label, scope = null) {
        const via = this.viaGL;
        // image tiles
        if (scope == "T") {
            const index = via._tileTextures.indexOf(label);
            if (index > -1) {
                return index;
            }
            const newIndex = via._nextTileTexture;
            const maximum = via._tileTextures.length;
            via._nextTileTexture = (newIndex + 1) % maximum;
            via._tileTextures[newIndex] = label;
            return newIndex;
        }
        // magnitudes
        if (scope == "M") {
            const index = via._markerTextures.indexOf(label);
            if (index > -1) {
                return index;
            }
            const newIndex = via._nextMarkerTexture;
            const maximum = via._markerTextures.length;
            via._nextMarkerTexture = (newIndex + 1) % maximum;
            via._markerTextures[newIndex] = label;
            return newIndex + via._markerOffset;
        }
        // other
        const index = via._constantTextures.indexOf(label);
        if (index > -1) {
            return index + via._otherOffset;
        }
        return -1;
    }

    /**
     * @function getTileTexture - Most recently bound tile texture label.
     * @returns string
     */
    getTileTexture() {
        const via = this.viaGL;
        return via._tileTextures[via._activeTileTexture];
    }

    /**
     * @function findMarkerTexture - Check if texture label is active.
     * @param label - marker texture label
     * @returns number
     */
    findMarkerTexture(label) {
        const via = this.viaGL;
        return via._markerTextures.indexOf(label);
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
            or: this.selectionProvider?.supportsColorCoding?.() ? this.selectionProvider.eval_mode == "or" : false,
        };
    }

    /**
     * Color-coded (multi-range gate) keys for webGL rendering. Empty unless
     * the active selection provider supports color coding (see the scope
     * note on colorCodedRanges below) -- kept gating-owned by design, not a
     * generalized concept every add-on module needs to implement.
     *
     * @type {Array}
     */
    get colorCodedKeys() {
        const keys = Object.keys(this.colorCodedRanges);
        return [...keys.sort()];
    }

    /**
     * Color-coded (multi-range gate) selections. This is deliberately not a
     * generalized "selection" concept -- it's the gating module's per-channel
     * threshold-range rendering path (u_gating_shape/texture_gatings), only
     * ever populated when the active provider opts in via supportsColorCoding().
     *
     * @type {Array}
     */
    get colorCodedRanges() {
        if (!this.selectionProvider?.supportsColorCoding?.()) {
            return {};
        }
        return this.selectionProvider.getColorCodedRanges?.() || {};
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
     * Cache key for gating webGL buffer.
     *
     * @type {string}
     */

    get gatingCacheKey() {
        return this._cacheKeys.gating;
    }

    set gatingCacheKey(key) {
        this._cacheKeys.gating = key;
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
        const gatingLists = this.selectGatings(keys);
        const changes = this.updateCache(keys, gatingLists);
        const { markersChanged, gatingChanged } = changes;

        // Bind picked ids 
        if (this.pickingChanged || gatingChanged) {
            this.bindPickings(this.viaGL, this.pickedIds);
            this.pickingChanged = false;
        }
        // Bind buffers per-channel
        if (gatingChanged) {
            const gatings = [];
            for (const gating of gatingLists) {
                for (const gatingValue of gating) {
                    gatings.push(gatingValue);
                }
            }
            this.bindGatings(this.viaGL, gatings, 5);
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
                    this.bindMagnitudes(this.viaGL, mk, k);
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
        const via = this.viaGL;
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
        const magnitude_2iv = this.toTextureShape(via.gl, this.idCount);
        const markerSamples = [0, 1, 2, 3].map((i) => {
            const label = via._markerTextures[i];
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
     * @function selectGatings - select gating ranges
     * @param keys - active marker channels
     * @returns - lists of min, max, r, g, b gating values
     */
    selectGatings(keys) {
        const gatingLists = [];
        const selections = this.colorCodedRanges;
        for (const key of keys) {
            const range = selections[key].map((x) => parseFloat(x));
            const color = this.selectMaskColor(key);
            const floatColor = toFloatColor(color);
            const gating = range.concat(floatColor);
            gatingLists.push(gating);
        }

        return gatingLists;
    }

    /**
     * @function updateCache - update cache keys
     * @param keys - active marker channels
     * @param gatingLists - lists of min, max, r, g, b gating values
     * @typedef {object} Changes
     * @property {boolean} markersChanged - if marker lists have changed
     * @property {boolean} gatingChanged - if gating parameters changed
     * @returns Changes
     */
    updateCache(keys, gatingLists) {
        const markerCacheKey = this.toCacheKey(keys, []);
        const markersChanged = this.markerCacheKey !== markerCacheKey;
        if (markersChanged) {
            this.markerCacheKey = markerCacheKey;
        }
        const gatingCacheKey = this.toCacheKey(keys, gatingLists);
        const gatingChanged = this.gatingCacheKey !== gatingCacheKey;
        if (gatingChanged) {
            this.gatingCacheKey = gatingCacheKey;
        }
        return { markersChanged, gatingChanged };
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
        const width = gl.getParameter(gl.MAX_TEXTURE_SIZE);
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
     * @param via - the viaGL context
     * @param values - the texture data as 2d array
     */
    bindLabels(via, values) {
        // Add id mask map
        const idx = this.indexOfTexture("ids", null);
        const ids_2iv = this.toTextureShape(via.gl, values.length);
        via.gl.uniform2iv(via.u_ids_shape, ids_2iv);
        this.setIntegerTexture(via.gl, idx, via.texture_ids, values);
    }

    /**
     * @function bindMagnitudes - bind segmentation mask magnitudes
     * @param via - the viaGL context
     * @param values - the texture data as 2d array
     * @param key - the marker label
     */
    bindMagnitudes(via, values, key) {
        // Add a mask magnitude map
        const idx = this.indexOfTexture(key, "M");
        const texture = via.texture_mag[idx - via._markerOffset];
        const [width, height] = this.toTextureShape(via.gl, values.length);
        this.setFloatTexture(via.gl, idx, texture, values, width, height);
    }

    /**
     * @function bindCenters - bind segmentation mask centers
     * @param via - the viaGL context
     * @param values - the texture data as 2d array
     */
    bindCenters(via, values) {
        // Add a mask center map
        const idx = this.indexOfTexture("centers", null);
        const [width, height] = this.toTextureShape(via.gl, values.length);
        via.gl.uniform3iv(via.u_center_shape, [width, height, 2]);
        this.setFloatTexture(via.gl, idx, via.texture_centers, values, width, height);
    }

    /**
     * @function bindGatings - bind segmentation mask gating
     * @param via - the viaGL context
     * @param values - the texture data as 2d array
     * @param width - the texture width
     */
    bindGatings(via, values, width) {
        // Add a mask gating map
        const idx = this.indexOfTexture("gatings", null);
        const height = Math.floor(values.length / width);
        const gating_2iv = [width, height];
        via.gl.uniform2iv(via.u_gating_shape, gating_2iv);
        this.setFloatTexture(via.gl, idx, via.texture_gatings, values, width, height);
    }

    /**
     * @function bindPickings - bind segmentation mask pickings
     * @param via - the viaGL context
     * @param values - the texture data as 2d array
     */
    bindPickings(via, values) {
        // Add a mask gating map
        const idx = this.indexOfTexture("pickings", null);
        const picking_2iv = this.toTextureShape(via.gl, values.length);
        via.gl.uniform2iv(via.u_picking_shape, picking_2iv);
        this.setIntegerTexture(via.gl, idx, via.texture_pickings, values);
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
        this.forceRepaint();
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
        this.forceRepaint();
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
        // checkbox's checked state agree from the start -- previously this set
        // a separate force_centroids flag that shouldDrawCentroids() OR'd in
        // permanently, so unchecking the box afterward couldn't actually turn
        // centroids off again.
        const checkbox = document.querySelector("#gating_controls_centroids");
        if (checkbox && isFallback) {
            checkbox.checked = true;
            // Covers every updateCentroidFallback(true) caller in one place for
            // navbarControls.js's mirrored View > Show Centroids checkbox.
            window.dispatchEvent(new CustomEvent("plexora:centroids-changed", { detail: { enabled: true } }));
        }
        if (isFallback) {
            await this.updateCentroidVisibility(true);
        }
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
            const idsPromise = (hasFilter && this.selectionProvider)
                ? this.selectionProvider.getSelectedIds(this.centroidFilter)
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

    async updateSegmentationFilter(filter = {}, showSpinner = false) {
        if (this.noLabel || !this.viewerManagerVMain?.sel_outlines) return;
        const requestId = ++this.segmentationFilterRequest;
        const gates = filter || {};
        const hasGates = Object.keys(gates).length > 0;
        if (showSpinner) {
            this.setLoading(true);
        }
        try {
            await this.ensureSegmentationReady(false);
            if (!hasGates || !this.selectionProvider) {
                this.segmentationFilterIds = null;
            } else {
                const ids = await this.selectionProvider.getSelectedIds(gates);
                if (requestId !== this.segmentationFilterRequest) return;
                this.segmentationFilterIds = ids instanceof Set ? ids : null;
            }
            this.rerenderSegmentationTiles();
            this.viewer.forceRedraw();
        } finally {
            if (showSpinner) {
                this.setLoading(false);
            }
        }
    }

    rerenderSegmentationTiles() {
        if (!this.renderLabelTile || !this.viewer?.world) return;
        for (let i = 0; i < this.viewer.world.getItemCount(); i += 1) {
            const item = this.viewer.world.getItemAt(i);
            if (item?.source?.tileFormat != 32) continue;
            const matrix = item.tilesMatrix || {};
            Object.keys(matrix).forEach((level) => {
                Object.keys(matrix[level] || {}).forEach((x) => {
                    Object.keys(matrix[level][x] || {}).forEach((y) => {
                        const tile = matrix[level][x][y];
                        if (!tile?._array || !tile._renderedContext) return;
                        const { width, height } = tile._renderedContext.canvas;
                        tile._renderedContext = this.renderLabelTile(tile._array, width, height);
                    });
                });
            });
        }
    }

    bindSegmentationBuffers(ids, centers) {
        if (!ids?.length || !centers?.length) return;
        const via = this.viaGL;
        via.texture_mag = via.texture_mag || [via.gl.createTexture(), via.gl.createTexture(), via.gl.createTexture(), via.gl.createTexture()];
        via.texture_ids = via.texture_ids || via.gl.createTexture();
        via.texture_centers = via.texture_centers || via.gl.createTexture();
        via.texture_pickings = via.texture_pickings || via.gl.createTexture();
        via.texture_gatings = via.texture_gatings || via.gl.createTexture();
        this.bindCenters(via, centers);
        this.bindPickings(via, this.pickedIds || []);
        this.bindLabels(via, ids);
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

    drawCentroids(context, imageZoom = 1) {
        if (this.centroidMode === "legacy") {
            this.drawLegacyCentroids(context, imageZoom);
            return;
        }
        if (!this.centroidTiles.size || !this.viewer?.viewport) return;
        const item = this.viewer.world.getItemAt(0);
        if (!item) return;
        const bounds = this.viewer.viewport.getBounds(true);
        const imageBounds = item.viewportToImageRectangle(bounds);
        const minX = imageBounds.x;
        const minY = imageBounds.y;
        const maxX = imageBounds.x + imageBounds.width;
        const maxY = imageBounds.y + imageBounds.height;
        const safeImageZoom = Math.max(imageZoom, 0.0001);
        const radius = this.getCentroidScreenRadius(safeImageZoom) / safeImageZoom;
        context.save();
        context.globalAlpha = 0.9;
        context.fillStyle = "#ffdd55";
        context.strokeStyle = "rgba(0, 0, 0, 0.8)";
        context.lineWidth = 1.6 / safeImageZoom;
        const drawPoint = (x, y) => {
            if (x < minX || x > maxX || y < minY || y > maxY) {
                return;
            }
            context.beginPath();
            context.arc(x, y, radius, 0, Math.PI * 2);
            context.fill();
            context.stroke();
        };
        this.centroidTiles.forEach((tile) => {
            const centers = tile.centers || [];
            for (let i = 0; i < centers.length; i += 2) {
                drawPoint(centers[i], centers[i + 1]);
            }
        });
        context.restore();
    }

    drawLegacyCentroids(context, imageZoom = 1) {
        const centers = this.fullResolutionCenters || [];
        if (!centers.length || !this.viewer?.viewport) return;
        const item = this.viewer.world.getItemAt(0);
        if (!item) return;
        const bounds = this.viewer.viewport.getBounds(true);
        const imageBounds = item.viewportToImageRectangle(bounds);
        const minX = imageBounds.x;
        const minY = imageBounds.y;
        const maxX = imageBounds.x + imageBounds.width;
        const maxY = imageBounds.y + imageBounds.height;
        const safeImageZoom = Math.max(imageZoom, 0.0001);
        const radius = this.getCentroidScreenRadius(safeImageZoom) / safeImageZoom;
        context.save();
        context.globalAlpha = 0.9;
        context.fillStyle = "#ffdd55";
        context.strokeStyle = "rgba(0, 0, 0, 0.8)";
        context.lineWidth = 1.6 / safeImageZoom;
        const drawAtOffset = (i) => {
            const x = centers[i];
            const y = centers[i + 1];
            if (x < minX || x > maxX || y < minY || y > maxY) {
                return;
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

    getCentroidScreenRadius(imageZoom) {
        const overviewLevel = Math.max(0, Math.log2(1 / imageZoom));
        return Math.max(2.5, Math.min(7, 3.5 + overviewLevel * 0.8));
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
     * @function evictLeastRecentlyUsedTiles - per-item LRU tile eviction,
     *   using OpenSeadragon's own tile.lastTouchTime, instead of clearing
     *   every channel's whole pyramid when the shared tile budget is hit.
     * @param perItemBudget - max tiles to keep loaded per TiledImage
     */
    evictLeastRecentlyUsedTiles(perItemBudget) {
        if (!this.viewer || !this.viewer.world) return;
        for (let i = 0; i < this.viewer.world.getItemCount(); i++) {
            const item = this.viewer.world.getItemAt(i);
            const loaded = item?._tileCache?._tilesLoaded;
            if (!loaded || loaded.length <= perItemBudget) continue;
            const oldestFirst = loaded.slice().sort(
                (a, b) => (a.tile?.lastTouchTime || 0) - (b.tile?.lastTouchTime || 0)
            );
            const excess = oldestFirst.length - perItemBudget;
            for (let k = 0; k < excess; k += 1) {
                const tileRecord = oldestFirst[k];
                if (tileRecord.tile && tileRecord.tile.unload) {
                    tileRecord.tile.unload();
                }
                const idx = loaded.indexOf(tileRecord);
                if (idx !== -1) {
                    loaded.splice(idx, 1);
                }
                tileRecord.tile = null;
            }
        }
        this.viewer.forceRedraw();
    }



    /**
     * @function clearTileCache - Clears the tile cache to free memory
     * @param onlySegmentation - if true, only clear the segmentation/label layer
     *   (tileFormat 32) instead of every channel's tile pyramid
     */
    clearTileCache(onlySegmentation = false) {
        if (this.viewer && this.viewer.world) {
            // Use the correct OpenSeadragon API method
            for (let i = 0; i < this.viewer.world.getItemCount(); i++) {
                const item = this.viewer.world.getItemAt(i);
                if (onlySegmentation && item?.source?.tileFormat !== 32) continue;
                if (item && item._tileCache && item._tileCache._tilesLoaded) {
                    // Clear loaded tiles
                    item._tileCache._tilesLoaded.forEach(tileRecord => {
                        if (tileRecord.tile && tileRecord.tile.unload) {
                            tileRecord.tile.unload();
                        }
                        tileRecord.tile = null;
                        // delete tile record
                        tileRecord = null;
                    });
                    item._tileCache._tilesLoaded = [];
                }
            }

            // Force a redraw to reload visible tiles
            this.viewer.forceRedraw();
        }
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
