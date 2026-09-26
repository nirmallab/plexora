/**
 * The GPU cell layer: label tiles drawn by a fragment shader instead of a
 * per-pixel JavaScript pass, and a gate evaluated in the browser instead of a
 * cell-id list from the server.
 *
 * WHY. Use case 2 of the manuscript benchmark timed a live threshold drag. Per
 * drawn update, 55-73% of the time was renderLabelTile walking every pixel of
 * every loaded label tile in JavaScript, and most of the rest was the server
 * building, and the browser parsing, a JSON list of up to 23 MB of passing
 * cell ids. Here a gate change is one byte per cell uploaded once, and each
 * VISIBLE tile is one draw call and one blit, inside OpenSeadragon's own frame.
 *
 * WHAT IT DRAWS is exactly what renderLabelTile draws (labelTile.js stays the
 * reference, shaders/label.frag.glsl mirrors it). The alpha arithmetic is done
 * on the CPU by labelTile.alphaTables and looked up by the shader, so the two
 * paths cannot round a tie differently.
 *
 * HOW IT SITS IN THE PIPELINE. Same place and pattern as the channel tiles:
 * tileColorize.js's tile-drawing handler calls drawTile per layer, and the
 * shader's output is blitted into the tile's own persistent 2D context
 * (`e.rendered`), which OpenSeadragon then composites -- so export, the view
 * transform and stacking are unchanged. The program is a SECOND program on the
 * channel renderer's context; drawTile restores the channel program, the
 * viewport and the unpack state before it returns.
 *
 * WHEN IT IS NOT USED. ImageViewer decides (labelRenderer): the CPU path runs
 * when the program failed to build, the context was lost, a table would need
 * 2^24 or more cell ids, or the user asked for it (`?labelRenderer=cpu`, or
 * localStorage plexoraLabelRenderer = "cpu").
 *
 * Served as a classic script (see base.html): after glInit.js and labelTile.js,
 * before imageViewer.js.
 */

//: What a software WebGL renderer (SwiftShader, llvmpipe: a machine with no
//: usable GPU) defaults to. Set from the no-GPU measurement in
//: plexora_manuscript/usecase2_live_threshold (after_nogpu vs after_nogpu_cpu).
const LABEL_GPU_SOFTWARE_DEFAULT = "gpu";
//: The id-indexed tables stop here: 16.7 M cells is 64 MB of colour table per
//: layer. A project past it draws on the CPU, as every project did before.
const LABEL_GPU_MAX_IDS = 1 << 24;
//: Label tiles are 4 MB each on the GPU (1024 square, four bytes a pixel) and
//: get their own budget, so they never evict the channel textures.
const LABEL_GPU_TILE_BUDGET = 160 * 1024 * 1024;
//: Texture units 24-31 belong to the channel program (glInit.js); the tile
//: sampler there is unit 0. These four are this program's alone.
const LABEL_UNITS = { tile: 20, gate: 21, lut: 22, alpha: 23 };

function labelGpuPreference() {
    try {
        const fromUrl = new URLSearchParams(window.location.search).get("labelRenderer");
        if (fromUrl === "cpu" || fromUrl === "gpu") return fromUrl;
    } catch (e) { /* no window */ }
    try {
        const stored = window.localStorage.getItem("plexoraLabelRenderer");
        if (stored === "cpu" || stored === "gpu") return stored;
    } catch (e) { /* storage blocked */ }
    return null;
}

/**
 * The gate, evaluated here with the server's rules (data_model.apply_range_mask):
 * every key must pass, a pass is low < value < high compared in FLOAT32, NaN
 * never passes, and a key the table does not have is skipped.
 *
 * @param ids     - Uint32Array, the table's cell ids (numericData.loadCells order)
 * @param columns - {key: Float32Array aligned with ids}; keys absent are skipped
 * @param gates   - {key: [low, high]}
 * @returns {{mask: Uint8Array, count: number, maxId: number}} mask is indexed by
 *   cell id; count is the number of distinct ids that pass (the size the
 *   server's id list makes as a Set).
 */
function evaluateGateMask(ids, columns, gates) {
    let maxId = 0;
    for (let i = 0; i < ids.length; i += 1) {
        if (ids[i] > maxId) maxId = ids[i];
    }
    const tests = [];
    for (const key of Object.keys(gates || {})) {
        const column = columns[key];
        if (!column) continue;
        const range = gates[key];
        // numpy compares a float32 column against a Python float in float32:
        // the bound is rounded to float32 first. Math.fround is that rounding.
        tests.push([column, Math.fround(Number(range[0])), Math.fround(Number(range[1]))]);
    }
    const mask = new Uint8Array(maxId + 1);
    let count = 0;
    const n = ids.length;
    if (tests.length === 1) {
        const [column, lo, hi] = tests[0];
        for (let i = 0; i < n; i += 1) {
            const v = column[i];
            if (v > lo && v < hi) {
                const id = ids[i];
                if (!mask[id]) { mask[id] = 1; count += 1; }
            }
        }
    } else {
        for (let i = 0; i < n; i += 1) {
            let pass = true;
            for (let k = 0; k < tests.length; k += 1) {
                const t = tests[k];
                const v = t[0][i];
                if (!(v > t[1] && v < t[2])) { pass = false; break; }
            }
            if (pass) {
                const id = ids[i];
                if (!mask[id]) { mask[id] = 1; count += 1; }
            }
        }
    }
    return { mask, count, maxId };
}

/** A Set of passing ids (the server path's answer) as the same mask. */
function maskFromSet(set) {
    let maxId = 0;
    for (const id of set) if (id > maxId) maxId = id;
    const mask = new Uint8Array(maxId + 1);
    for (const id of set) if (id >= 0) mask[id] = 1;
    return mask;
}

/** A sparse LUT ({map: Map<id, [r,g,b,a]>}) as the dense shape. */
function denseFromMap(map) {
    let maxId = 0;
    for (const id of map.keys()) if (id > maxId) maxId = id;
    const colors = new Uint8Array(4 * (maxId + 1));
    for (const [id, entry] of map) {
        if (!entry || id < 0) continue;
        const o = id * 4;
        colors[o] = entry[0]; colors[o + 1] = entry[1]; colors[o + 2] = entry[2]; colors[o + 3] = entry[3];
    }
    return { colors, maxId };
}

/**
 * @param renderer   - the channel GLRenderer (glInit.createGLRenderer); its
 *                     context, quad buffer and `program` are shared
 * @param vShaderUrl, fShaderUrl - vert.glsl and label.frag.glsl
 * @param labelTile  - PlexoraLabelTile (alphaTables)
 * @param onChange   - called (deferred) when `active` flips
 */
function createLabelGpu({ renderer, vShaderUrl, fShaderUrl, labelTile, onChange = () => {} }) {
    const gl = renderer.gl;
    const state = {
        active: false,
        reason: "compiling",
        program: null,
        uniforms: {},
        layers: new Map(),
        tileKeys: new WeakMap(),
        nextTileKey: 1,
        tileCache: null,
        alphaTexture: null,
        alphaFill: null,
        dummies: null,
        tableWidth: 0,
        software: null,
        stats: { draws: 0, tileUploads: 0, tableUploads: 0 },
    };

    const notify = () => setTimeout(() => onChange(api), 0);

    function disable(reason) {
        if (!state.active && state.reason === reason) return;
        const was = state.active;
        state.active = false;
        state.reason = reason;
        console.warn(`GPU cell layer off: ${reason}. Drawing cells on the CPU.`);
        if (was) notify();
    }

    function newTexture() {
        const t = gl.createTexture();
        gl.bindTexture(gl.TEXTURE_2D, t);
        // Integer textures are INCOMPLETE under the default mipmapped minifying
        // filter, and texelFetch from an incomplete texture silently reads 0 --
        // which would draw nothing and say nothing. NEAREST on every one.
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
        return t;
    }

    function compile(kind, source) {
        const shader = gl.createShader(kind);
        gl.shaderSource(shader, source);
        gl.compileShader(shader);
        if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
            throw new Error(gl.getShaderInfoLog(shader) || "shader did not compile");
        }
        return shader;
    }

    async function build() {
        if (!gl) throw new Error("no WebGL2 context");
        const [vs, fs] = await Promise.all([vShaderUrl, fShaderUrl].map(async (url) => {
            const response = await fetch(url, { cache: "no-store" });
            if (!response.ok) throw new Error(`${url}: ${response.status}`);
            return response.text();
        }));
        const program = gl.createProgram();
        gl.attachShader(program, compile(gl.VERTEX_SHADER, vs));
        gl.attachShader(program, compile(gl.FRAGMENT_SHADER, fs));
        // The quad's attribute is context-global state set up by the channel
        // program (GLRenderer.toBuffers); pinning this program's a_uv to 0 is
        // what lets drawTile point attribute 0 at the same buffer.
        gl.bindAttribLocation(program, 0, "a_uv");
        gl.linkProgram(program);
        if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
            throw new Error(gl.getProgramInfoLog(program) || "program did not link");
        }
        const u = {};
        for (const name of ["u_tile", "u_gate", "u_lut", "u_alpha", "u_size", "u_table_width",
            "u_gate_present", "u_gate_max", "u_lut_present", "u_lut_max", "u_derive", "u_fill_on"]) {
            u[name] = gl.getUniformLocation(program, name);
        }
        const previous = gl.getParameter(gl.CURRENT_PROGRAM);
        gl.useProgram(program);
        gl.uniform1i(u.u_tile, LABEL_UNITS.tile);
        gl.uniform1i(u.u_gate, LABEL_UNITS.gate);
        gl.uniform1i(u.u_lut, LABEL_UNITS.lut);
        gl.uniform1i(u.u_alpha, LABEL_UNITS.alpha);
        gl.useProgram(previous);

        state.tableWidth = Math.min(gl.getParameter(gl.MAX_TEXTURE_SIZE) || 4096, 16384);
        state.tileCache = new PlexoraGL.GLTileTextureCache(gl, LABEL_GPU_TILE_BUDGET);
        // Created on this program's own unit: unit 0 is the channel tile's.
        gl.activeTexture(gl.TEXTURE0 + LABEL_UNITS.alpha);
        state.alphaTexture = newTexture();
        gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
        gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, 0);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.R8UI, 256, 2, 0, gl.RED_INTEGER, gl.UNSIGNED_BYTE, null);
        const dummyGate = newTexture();
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.R8UI, 1, 1, 0, gl.RED_INTEGER, gl.UNSIGNED_BYTE, new Uint8Array(1));
        const dummyLut = newTexture();
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8UI, 1, 1, 0, gl.RGBA_INTEGER, gl.UNSIGNED_BYTE, new Uint8Array(4));
        gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, 1);
        gl.activeTexture(gl.TEXTURE0);
        state.dummies = { gate: dummyGate, lut: dummyLut };
        state.program = program;
        state.uniforms = u;
        state.active = true;
        state.reason = null;
        notify();
    }

    function isSoftware() {
        if (state.software !== null) return state.software;
        let name = "";
        try {
            const info = gl.getExtension("WEBGL_debug_renderer_info");
            name = String(gl.getParameter(info ? info.UNMASKED_RENDERER_WEBGL : gl.RENDERER) || "");
        } catch (e) { /* ignore */ }
        state.rendererName = name;
        state.software = /SwiftShader|llvmpipe|softpipe|Basic Render|Microsoft Basic/i.test(name);
        return state.software;
    }

    /** Upload an id-indexed table as a W x H integer texture, no copy. */
    function uploadTable(texture, data, channels, count) {
        const W = state.tableWidth;
        const rows = Math.max(1, Math.ceil(count / W));
        const width = rows === 1 ? Math.max(1, count) : W;
        const [internal, format] = channels === 4
            ? [gl.RGBA8UI, gl.RGBA_INTEGER] : [gl.R8UI, gl.RED_INTEGER];
        gl.bindTexture(gl.TEXTURE_2D, texture);
        gl.texImage2D(gl.TEXTURE_2D, 0, internal, width, rows, 0, format, gl.UNSIGNED_BYTE, null);
        const full = Math.floor(count / width);
        if (full > 0) {
            gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, width, full, format, gl.UNSIGNED_BYTE,
                data.subarray(0, full * width * channels));
        }
        const rest = count - full * width;
        if (rest > 0) {
            gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, full, rest, 1, format, gl.UNSIGNED_BYTE,
                data.subarray(full * width * channels, count * channels));
        }
        state.stats.tableUploads += 1;
    }

    /** This layer's gate and colour tables, re-uploaded only when they changed. */
    function layerTables(layer) {
        let s = state.layers.get(layer.name);
        if (!s) {
            s = { gateSrc: undefined, gateTex: null, gateMax: 0,
                  lutSrc: undefined, lutVersion: -1, lutTex: null, lutMax: 0 };
            state.layers.set(layer.name, s);
        }
        const gateSrc = layer.gateMask || layer.filterIds || null;
        if (gateSrc !== s.gateSrc) {
            if (gateSrc) {
                const mask = gateSrc instanceof Uint8Array ? gateSrc : maskFromSet(gateSrc);
                if (mask.length > LABEL_GPU_MAX_IDS) { disable("cell ids reach 2^24"); return null; }
                s.gateTex = s.gateTex || newTexture();
                uploadTable(s.gateTex, mask, 1, mask.length);
                s.gateMax = mask.length - 1;
            }
            s.gateSrc = gateSrc;
        }
        const lut = layer.lut || null;
        const lutVersion = layer.lutVersion || 0;
        if (lut !== s.lutSrc || lutVersion !== s.lutVersion) {
            if (lut) {
                const dense = lut.colors ? lut : (lut.map ? denseFromMap(lut.map) : null);
                if (dense) {
                    const count = Math.min(dense.maxId + 1, Math.floor(dense.colors.length / 4));
                    if (count > LABEL_GPU_MAX_IDS) { disable("cell ids reach 2^24"); return null; }
                    s.lutTex = s.lutTex || newTexture();
                    uploadTable(s.lutTex, dense.colors, 4, count);
                    s.lutMax = count - 1;
                }
                s.lutDense = Boolean(dense);
            }
            s.lutSrc = lut;
            s.lutVersion = lutVersion;
        }
        return s;
    }

    function tileTexture(tile, w, h) {
        const pixels = tile._array;
        let key = state.tileKeys.get(pixels);
        if (!key) {
            // Keyed by the decoded array, not the tile's url: a reloaded mask
            // hangs new arrays on the same urls, and must not draw old ids.
            key = `label-${state.nextTileKey++}`;
            state.tileKeys.set(pixels, key);
        }
        const { texture, resident } = state.tileCache.acquire(key, w * h * 4);
        gl.activeTexture(gl.TEXTURE0 + LABEL_UNITS.tile);
        gl.bindTexture(gl.TEXTURE_2D, texture);
        if (!resident) {
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
            const view = pixels.length === w * h * 4 ? pixels : pixels.subarray(0, w * h * 4);
            gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8UI, w, h, 0, gl.RGBA_INTEGER, gl.UNSIGNED_BYTE, view);
            state.stats.tileUploads += 1;
        }
    }

    /**
     * Draw one layer of one label tile into `rendered` (the tile's 2D context),
     * at the context's current globalAlpha, over what is already there.
     * @returns whether it drew; false leaves `rendered` as it was.
     */
    function drawTile(tile, layer, rendered, w, h, segmentationMode) {
        if (!state.active) return false;
        const lw = tile._labelWidth;
        const lh = tile._labelHeight;
        if (!tile._array || !lw || !lh || tile._array.length < lw * lh * 4) return false;
        if (gl.isContextLost()) { disable("the WebGL context was lost"); return false; }
        if (lw > renderer.width || lh > renderer.height) {
            // Grow only, as the channel path does (tileColorize.js).
            renderer.updateShape(Math.max(lw, renderer.width), Math.max(lh, renderer.height));
        }
        const u = state.uniforms;
        try {
            gl.useProgram(state.program);
            gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
            gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, 0);
            gl.activeTexture(gl.TEXTURE0 + LABEL_UNITS.gate);
            const tables = layerTables(layer);
            if (!tables) return false;
            gl.bindTexture(gl.TEXTURE_2D, tables.gateSrc ? tables.gateTex : state.dummies.gate);
            gl.activeTexture(gl.TEXTURE0 + LABEL_UNITS.lut);
            const lutOn = Boolean(tables.lutSrc && tables.lutDense);
            gl.bindTexture(gl.TEXTURE_2D, lutOn ? tables.lutTex : state.dummies.lut);

            const fillMode = layer.mode === "filled" && segmentationMode === "filled";
            const derive = segmentationMode === "filled" && !fillMode;
            const fill = derive ? (tile._fillWeight || 0) : 0;
            gl.activeTexture(gl.TEXTURE0 + LABEL_UNITS.alpha);
            gl.bindTexture(gl.TEXTURE_2D, state.alphaTexture);
            if (derive && fill && state.alphaFill !== fill) {
                gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, 256, 2, gl.RED_INTEGER, gl.UNSIGNED_BYTE,
                    labelTile.alphaTables(fill));
                state.alphaFill = fill;
            }
            tileTexture(tile, lw, lh);

            gl.uniform2i(u.u_size, lw, lh);
            gl.uniform1i(u.u_table_width, state.tableWidth);
            gl.uniform1i(u.u_gate_present, tables.gateSrc ? 1 : 0);
            gl.uniform1ui(u.u_gate_max, tables.gateSrc ? tables.gateMax : 0);
            gl.uniform1i(u.u_lut_present, lutOn ? 1 : 0);
            gl.uniform1ui(u.u_lut_max, lutOn ? tables.lutMax : 0);
            gl.uniform1i(u.u_derive, derive ? 1 : 0);
            gl.uniform1i(u.u_fill_on, fill ? 1 : 0);

            gl.viewport(0, 0, lw, lh);
            gl.bindBuffer(gl.ARRAY_BUFFER, renderer.buffer);
            gl.enableVertexAttribArray(0);
            gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 2 * Float32Array.BYTES_PER_ELEMENT, 0);
            gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
            // The viewport is the canvas's bottom-left corner; in canvas
            // coordinates that is the last lh rows. 1:1 when the tile's canvas
            // is the label's size, which it is (no extra zoom levels).
            rendered.drawImage(gl.canvas, 0, renderer.height - lh, lw, lh, 0, 0, w, h);
            state.stats.draws += 1;
            return true;
        } catch (e) {
            console.warn("GPU cell layer draw failed:", e);
            return false;
        } finally {
            // The channel program, its viewport and its unpack state, exactly
            // as tileColorize and GLRenderer expect to find them.
            gl.useProgram(renderer.program || null);
            gl.viewport(0, 0, renderer.width, renderer.height);
            gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, 1);
            gl.activeTexture(gl.TEXTURE0);
        }
    }

    function dropLayer(name) {
        const s = state.layers.get(name);
        if (!s) return;
        if (s.gateTex) gl.deleteTexture(s.gateTex);
        if (s.lutTex) gl.deleteTexture(s.lutTex);
        state.layers.delete(name);
    }

    function clear() {
        for (const name of [...state.layers.keys()]) dropLayer(name);
        state.tileCache?.clear();
        state.tileKeys = new WeakMap();
    }

    if (gl?.canvas?.addEventListener) {
        gl.canvas.addEventListener("webglcontextlost", () => disable("the WebGL context was lost"));
    }

    const api = {
        get active() { return state.active; },
        get reason() { return state.reason; },
        get stats() { return state.stats; },
        get rendererName() { isSoftware(); return state.rendererName; },
        ready: null,
        isSoftware,
        drawTile,
        dropLayer,
        clear,
        disable,
    };
    api.ready = build().catch((e) => {
        disable(`the cell-layer shader did not build (${e.message || e})`);
    });
    return api;
}

const PLEXORA_LABEL_GPU = {
    createLabelGpu, evaluateGateMask, maskFromSet, denseFromMap, labelGpuPreference,
    SOFTWARE_DEFAULT: LABEL_GPU_SOFTWARE_DEFAULT, MAX_IDS: LABEL_GPU_MAX_IDS, UNITS: LABEL_UNITS,
};
if (typeof window !== "undefined") {
    window.PlexoraLabelGpu = PLEXORA_LABEL_GPU;
}
if (typeof globalThis !== "undefined" && !globalThis.PlexoraLabelGpu) {
    globalThis.PlexoraLabelGpu = PLEXORA_LABEL_GPU;
}
