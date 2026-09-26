/**
 * The WebGL side of the tile path: the texture cache, the GLRenderer wiring, and
 * the `open` handler that compiles the shaders and puts the real tile handlers on
 * the viewer.
 *
 * Extracted verbatim from imageViewer.js, where all of this lived as closures in
 * the constructor. Nothing about what it draws changed in the move -- the values
 * each closure read off `this` are now named dependencies, which is the whole of
 * the difference.
 *
 * Served as a classic script (see base.html) and must load BEFORE imageViewer.js.
 */

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
 * Build the GLRenderer and wire its three hooks.
 *
 * @param indexOfTexture - ImageViewer.indexOfTexture, bound
 * @param selectTexture  - ImageViewer.selectTexture, bound
 * @param resolveGLReady - resolves ImageViewer's glReady promise, once shaders
 *                         have compiled and the uniform locations are known
 */
function createGLRenderer({ indexOfTexture, selectTexture, resolveGLReady }) {
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
        this.gl.uniform1i(this.u_alpha_mode, args.alpha_mode_1i || 0);
        this.gl.uniform1i(this.u_id_end, args.id_end_1i);
    };

    renderer["gl-loaded"] = function (program) {
        // Kept so a second program on this context (the GPU cell layer,
        // labelGpu.js) can hand the context back to this one. Re-set on every
        // `open`, because init() builds a new program each time.
        this.program = program;
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
        this.u_alpha_mode = this.gl.getUniformLocation(program, "u_alpha_mode");
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

    return renderer;
}


/**
 * The `open` handler that brings GL up.
 *
 * Runs more than once by design: viewerManager.js manually re-raises `open` after
 * adding the label tiled image and after every channel add. renderer.init() is
 * idempotent enough for that, and what the re-raise is for is the redraw at the
 * end -- the handler registrations are one-shot, see `wired` below.
 */
function createGLInit({ viewer, renderer, config, handleTileLoaded, tileDrawingCustom, tileDrawingDefault }) {
    //: The registrations below happen ONCE, however many times `open` is
    //: re-raised. Every channel add raises it (viewerManager.channel_add), and
    //: OpenSeadragon's addHandler does not dedupe -- so a 7-channel project
    //: hung seven copies of the decode and colorize handlers on the viewer,
    //: and `tile-drawing` is re-raised for every visible tile of every channel
    //: on EVERY frame. The duplicates were invisible because both handlers are
    //: idempotent (the decode guards on `tile._array`, the colorize pass
    //: returns early on its signature), so all they ever did was multiply the
    //: per-frame bookkeeping by the channel count.
    let wired = false;
    return () => {
        renderer.width = renderer.width || config.tileWidth;
        renderer.height = renderer.height || config.tileHeight;
        renderer.updateShape(renderer.width, renderer.height);
        renderer.init().then(() => {
            if (!wired) {
                wired = true;
                viewer.addHandler("tile-loaded", handleTileLoaded);
                viewer.addHandler("tile-drawing", (e) => tileDrawingCustom(tileDrawingDefault, e));
            }

            const world = viewer.world;
            for (let i = 0; i < world.getItemCount(); i++) {
                world.getItemAt(i)._needsDraw = true;
            }
            world.update();
        });
    };
}


if (typeof window !== "undefined") {
    window.PlexoraGL = { GLTileTextureCache, createGLRenderer, createGLInit };
}
if (typeof globalThis !== "undefined" && !globalThis.PlexoraGL) {
    globalThis.PlexoraGL = { GLTileTextureCache, createGLRenderer, createGLInit };
}
