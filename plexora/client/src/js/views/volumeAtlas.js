/**
 * volumeAtlas.js - full resolution wherever the 3-D view needs it: bricks of
 * the finer levels in a GPU atlas, found per sample through a page table.
 *
 * The renderer (volumeRenderer.js) draws one whole level as its base -- the
 * finest that fits its budget. Close up, that level's voxels are larger than
 * a screen pixel. The atlas holds the parts of the finer levels the view
 * needs, as bricks (server/volume/brick_serve.py: 32 x 64 x 64 voxels, planes
 * pooled so a voxel is about as deep as wide, one voxel of apron so the
 * filter shows no seams), and a page table says, for every brick of every
 * atlas level, whether it is
 *
 *   RESIDENT  in the atlas, at the slot its entry names;
 *   REFINED   wanted finer: the bricks one level down hold this region;
 *   EMPTY     all at or below the window's floor -- stored nowhere, skipped
 *             (unless REFINED: a finer level can hold what averaging hid).
 *
 * WHICH BRICKS (plan). A region is split into finer bricks while their
 * voxels would cover more than a screen pixel at its distance from the
 * camera, always where a voxel covers the most pixels first, until the atlas
 * has no room for the next split: the view gets the coarsest level that is
 * still sharp -- fine near the camera, coarse far off -- and, short of room,
 * the near parts sharp and the farthest from the base. Bricks off screen, or
 * outside the clip range or the area drawn, are not visited; known empty
 * ones are not fetched.
 *
 * WHAT A RAY DOES (shaderChunk). Each sample walks the page table from the
 * coarsest atlas level down -- a texel fetch a level -- while entries say
 * REFINED, remembering the last RESIDENT one, and samples that brick; with
 * none on the way, the base texture. The brick it ended in is remembered,
 * so the walk happens once per brick a ray crosses, not once per sample;
 * an EMPTY brick moves the ray on to where it leaves the brick.
 *
 * LOADING. Only at rest (the renderer calls `update` when the camera has
 * settled), nearest bricks first, a batch of bricks of one level and every
 * channel per request (`/volume/<ds>/bricks`), two requests at a time --
 * gzipped once the link has measured slower than compressing (`LINK`, which
 * the renderer's whole levels share). A
 * brick's slot is reused least recently wanted first, never one the current
 * cut draws. Everything is keyed by the channels, their windows, the frame
 * and the format; a change of any of them starts the atlas again.
 */
(function (global) {
    "use strict";

    const RESIDENT = 1;
    const REFINED = 2;
    const EMPTY = 4;
    //: Atlas levels a ray walks at most (the shader's loop bound).
    const MAXL = 12;
    //: What the atlas may hold on the GPU, all channels together.
    const ATLAS_BUDGET = 384 * 2 ** 20;
    //: The page table's largest size; the finest levels are dropped past it.
    const PAGE_BUDGET = 64 * 2 ** 20;
    //: Bricks a request carries, and requests in flight at once.
    const BATCH = 24;
    const IN_FLIGHT = 2;
    //: The cut may fill this share of the atlas; past it, it is made coarser.
    const FILL = 0.85;

    function now() {
        return global.performance?.now?.() ?? Date.now();
    }

    //: Under this, bytes per millisecond (30 MB/s), the link is slower than
    //: gzip at its fastest setting, and voxels come compressed (about five
    //: times fewer bytes); over it -- localhost -- compressing only costs.
    const SLOW_RATE = 30000;

    /**
     * How fast the 3-D view's bytes arrive, shared by the base levels and the
     * bricks: from the browser's own timing of each large response (request
     * sent to last byte, less the server's `Server-Timing` total). Once two
     * uncompressed responses have come slower than SLOW_RATE, every later
     * request asks for gzip (`z=gzip`) -- for the rest of the session.
     */
    const LINK = {
        rates: [],
        // A page served from another machine is never on a link faster than
        // compressing: gzip from the first level. Loopback (the desktop app,
        // and a tunnel to a node) is measured first.
        compress: !(/^(localhost|127(\.\d+){3}|\[::1\]|::1|)$/.test(global.location?.hostname || "")),
        note(url) {
            if (this.compress || !url) return;
            const entries = global.performance?.getEntriesByName?.(url) || [];
            const entry = entries[entries.length - 1];
            if (!entry || !(entry.responseEnd > 0) || !(entry.encodedBodySize > 2 ** 20)) return;
            const total = (entry.serverTiming || []).find((t) => t.name === "total");
            const ms = Math.max(1, entry.responseEnd - entry.requestStart - (total ? total.duration : 0));
            this.rates.push(entry.encodedBodySize / ms);
            if (this.rates.length >= 2) {
                const sorted = this.rates.slice().sort((a, b) => a - b);
                if (sorted[sorted.length >> 1] < SLOW_RATE) this.compress = true;
            }
        },
        /** `query` (URLSearchParams) asking for gzip when the link is slow. */
        ask(query) {
            if (this.compress) query.set("z", "gzip");
            return query;
        },
    };

    /** `{brick, apron, levels: [...]}` from the manifest route, the levels
     *  the atlas can use below `baseLevel`, finest first, as the page table
     *  and the shader see them: each with `vox` (full-resolution pixels,
     *  pixels, planes per voxel, x/y/z), `brickFull` (x, y, z), `grid`
     *  (x, y, z) and its page table offset in z. */
    function atlasLevels(manifest, baseLevel, maxTexture) {
        let chosen = (manifest?.levels || []).filter((entry) => entry.level < baseLevel)
            .sort((a, b) => a.level - b.level)
            .map((entry) => ({
                level: entry.level,
                pool: entry.pool,
                vox: [2 ** entry.level, 2 ** entry.level, entry.pool],
                brickFull: [entry.brick_full[2], entry.brick_full[1], entry.brick_full[0]],
                grid: [entry.grid[2], entry.grid[1], entry.grid[0]],
            }));
        // The coarsest MAXL; then the finest dropped while the page table
        // will not fit a texture or its budget.
        if (chosen.length > MAXL) chosen = chosen.slice(chosen.length - MAXL);
        const size = (levels) => {
            const x = Math.max(...levels.map((l) => l.grid[0]));
            const y = Math.max(...levels.map((l) => l.grid[1]));
            const z = levels.reduce((sum, l) => sum + l.grid[2], 0);
            return { x, y, z };
        };
        while (chosen.length) {
            const s = size(chosen);
            if (s.x <= maxTexture && s.y <= maxTexture && s.z <= maxTexture
                    && s.x * s.y * s.z * 4 <= PAGE_BUDGET) break;
            chosen = chosen.slice(1);
        }
        let offset = 0;
        for (const level of chosen) {
            level.pageZ = offset;
            offset += level.grid[2];
        }
        return chosen;
    }

    /** How many slots, laid out how, the atlas has for one channel. */
    function atlasLayout(brickTex, bytesPerVoxel, channels, maxTexture, budget = ATLAS_BUDGET) {
        const slotBytes = brickTex[0] * brickTex[1] * brickTex[2] * bytesPerVoxel;
        const want = Math.max(1, Math.floor(budget / Math.max(1, channels) / slotBytes));
        // Never more slots than the budget holds: a layer narrower than
        // 16 x 16 when the budget is small.
        const sx = Math.max(1, Math.min(Math.floor(maxTexture / brickTex[0]), 16,
                                        Math.ceil(Math.sqrt(want))));
        const sy = Math.max(1, Math.min(Math.floor(maxTexture / brickTex[1]), 16,
                                        Math.floor(want / sx)));
        const sz = Math.max(1, Math.min(Math.floor(maxTexture / brickTex[2]),
                                        Math.floor(want / (sx * sy))));
        return { slots: [sx, sy, sz], capacity: sx * sy * sz, slotBytes,
                 size: [sx * brickTex[0], sy * brickTex[1], sz * brickTex[2]] };
    }

    /** A response body: `{header, voxels}` (brick_serve.payload). */
    function parsePayload(buffer) {
        const view = new DataView(buffer);
        const length = view.getUint32(0, true);
        const text = new TextDecoder().decode(new Uint8Array(buffer, 4, length));
        return { header: JSON.parse(text), voxels: buffer.slice(4 + length) };
    }

    /** The GLSL the renderer adds for an atlas: declarations, and the walk. */
    function shaderChunk(count) {
        const samplers = [];
        for (let c = 0; c < count; c += 1) samplers.push(`uniform sampler3D u_atlas${c};`);
        return {
            declarations: `#define MAXL ${MAXL}
precision highp usampler3D;
uniform usampler3D u_pages;
${samplers.join("\n")}
uniform vec3 u_full_lo;            // the base texture's (0,0,0), in pixels, pixels, planes
uniform vec3 u_full_span;          // and its size in the same units
uniform int u_top;                 // the coarsest atlas level
uniform vec3 u_brick_full[MAXL];   // a brick's extent per level (x, y, z)
uniform vec3 u_vox_full[MAXL];     // a voxel's
uniform ivec3 u_page_off[MAXL];
uniform ivec3 u_page_grid[MAXL];
uniform vec3 u_brick_tex;          // a brick in the atlas, apron included
uniform vec3 u_atlas_dim;

// Where the walk for full-resolution point 'full' ends: the brick box
// [lo, hi) it stopped in, and how to draw inside it -- state 0 the base
// texture, 1 the atlas from the deepest resident brick on the way (its
// texel origin 'base', voxel size 'vox', box origin 'anchor'), 2 empty.
void locate(vec3 full, out vec3 lo, out vec3 hi, out int state, out vec3 base,
            out vec3 vox, out vec3 anchor) {
    state = 0;
    lo = vec3(-1e9);
    hi = vec3(1e9);
    base = vec3(0.0);
    vox = vec3(1.0);
    anchor = vec3(0.0);
    for (int i = 0; i < MAXL; i++) {
        int k = u_top - i;
        if (k < 0) break;
        ivec3 b = clamp(ivec3(floor(full / u_brick_full[k])), ivec3(0), u_page_grid[k] - 1);
        uvec4 e = texelFetch(u_pages, u_page_off[k] + b, 0);
        lo = vec3(b) * u_brick_full[k];
        hi = lo + u_brick_full[k];
        // Refined first: a coarse brick can be empty where a finer one is
        // not (one bright pixel averaged away), and the walk goes on down.
        bool refined = (e.a & ${REFINED}u) != 0u;
        if (!refined && (e.a & ${EMPTY}u) != 0u) {
            state = 2;
            return;
        }
        if ((e.a & ${RESIDENT}u) != 0u) {
            state = 1;
            base = vec3(e.xyz) * u_brick_tex + 1.0;
            vox = u_vox_full[k];
            anchor = lo;
        }
        if (!refined) return;
    }
}`,
            sample: Array.from({ length: count }, (_, c) =>
                `        v[${c}] = cState == 1 ? texture(u_atlas${c}, a).r : texture(u_volume${c}, p).r;`)
                .join("\n"),
        };
    }

    class VolumeAtlas {
        constructor({ gl, datasource, maxTexture, fetchImpl = null, onChange = null } = {}) {
            this.gl = gl;
            this.datasource = datasource;
            this.maxTexture = maxTexture || 2048;
            this.fetch = fetchImpl || ((...args) => global.fetch(...args));
            this.onChange = onChange;
            //: "idle" | "loading" | "ready" | "unavailable"
            this.status = "idle";
            this.manifest = null;
            this.key = "";
            this.levels = [];
            this.textures = [];
            this.pages = null;
            this.mirror = null;
            this.pageSize = null;
            this.layout = null;
            this.slotKey = [];
            this.slotUsed = null;
            this.slotOf = new Map();
            this.free = [];
            this.empties = new Set();
            this.refined = new Set();
            this.inflight = new Set();
            this.requests = 0;
            this.generation = 0;
            //: Bumped whenever the page table changes (a brick placed or
            //: dropped, the atlas started again): what is drawn changed.
            this.pagesVersion = 0;
            this.cut = null;
            this.bytes = 0;
            this.finest = null;
        }

        address(path) {
            return typeof global.plexoraUrl === "function" ? global.plexoraUrl(path) : `/${path}`;
        }

        /** Ask once whether this image is served as bricks. */
        async load() {
            if (this.status !== "idle") return this.status === "ready";
            this.status = "loading";
            try {
                const ds = encodeURIComponent(this.datasource);
                const response = await this.fetch(this.address(`volume/${ds}/bricks/manifest`));
                if (!response.ok) {
                    this.status = "unavailable";
                    return false;
                }
                this.manifest = await response.json();
                this.status = "ready";
                return true;
            } catch (error) {
                this.status = "unavailable";
                return false;
            }
        }

        /**
         * Make the atlas fit what is drawn: `channels` ({name, stamp}), frame
         * `t`, `format` and the base texture's level. Starts it again when
         * any of them changed. False when there is nothing finer than the
         * base to hold.
         */
        configure(channels, t, format, baseLevel) {
            if (this.status !== "ready" || !this.gl || !channels.length) return false;
            const key = [channels.map((c) => `${c.name}@${c.stamp || ""}`).join("|"), t, format,
                         baseLevel].join("#");
            if (key === this.key) return this.levels.length > 0;
            this.reset();
            this.key = key;
            this.channels = channels.map((c) => ({ name: c.name, stamp: c.stamp || "" }));
            this.t = t;
            this.format = format;
            this.levels = atlasLevels(this.manifest, baseLevel, this.maxTexture);
            if (!this.levels.length) return false;
            const gl = this.gl;
            const brick = this.manifest.brick;               // (z, y, x)
            const apron = this.manifest.apron || 0;
            this.brickTex = [brick[2] + 2 * apron, brick[1] + 2 * apron, brick[0] + 2 * apron];
            const half = format === "f16";
            let layout = atlasLayout(this.brickTex, half ? 2 : 1, channels.length, this.maxTexture);
            // A driver that cannot find the room says so; half the room then.
            for (let attempt = 0; attempt < 4; attempt += 1) {
                const textures = [];
                let failed = false;
                for (let c = 0; c < channels.length && !failed; c += 1) {
                    const texture = gl.createTexture();
                    gl.bindTexture(gl.TEXTURE_3D, texture);
                    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
                    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
                    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
                    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
                    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_R, gl.CLAMP_TO_EDGE);
                    gl.texStorage3D(gl.TEXTURE_3D, 1, half ? gl.R16F : gl.R8, ...layout.size);
                    textures.push(texture);
                    if (gl.getError() === gl.OUT_OF_MEMORY) failed = true;
                }
                if (!failed) {
                    this.textures = textures;
                    break;
                }
                for (const texture of textures) gl.deleteTexture(texture);
                layout = atlasLayout(this.brickTex, half ? 2 : 1, channels.length,
                                     this.maxTexture, ATLAS_BUDGET / 2 ** (attempt + 1));
            }
            if (!this.textures.length) {
                this.levels = [];
                return false;
            }
            this.layout = layout;
            this.bytes = layout.capacity * layout.slotBytes * channels.length;
            this.slotKey = new Array(layout.capacity).fill(null);
            this.slotUsed = new Float64Array(layout.capacity);
            this.free = Array.from({ length: layout.capacity }, (_, i) => layout.capacity - 1 - i);
            const x = Math.max(...this.levels.map((l) => l.grid[0]));
            const y = Math.max(...this.levels.map((l) => l.grid[1]));
            const z = this.levels.reduce((sum, l) => sum + l.grid[2], 0);
            this.pageSize = [x, y, z];
            this.mirror = new Uint8Array(x * y * z * 4);
            this.pages = gl.createTexture();
            gl.bindTexture(gl.TEXTURE_3D, this.pages);
            gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
            gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
            gl.texStorage3D(gl.TEXTURE_3D, 1, gl.RGBA8UI, x, y, z);
            this.uploadPages();
            return true;
        }

        /** Whether the next draw should walk the atlas. */
        active() {
            return this.levels.length > 0 && this.textures.length > 0 && Boolean(this.pages);
        }

        reset() {
            const gl = this.gl;
            for (const texture of this.textures) gl?.deleteTexture(texture);
            if (this.pages) gl?.deleteTexture(this.pages);
            this.textures = [];
            this.pages = null;
            this.mirror = null;
            this.levels = [];
            this.slotOf.clear();
            this.empties.clear();
            this.refined.clear();
            this.inflight.clear();
            this.free = [];
            this.slotKey = [];
            this.cut = null;
            this.bytes = 0;
            this.finest = null;
            this.key = "";
            this.generation += 1;
            this.pagesVersion += 1;
        }

        /** The graphics context went: everything on it is gone. */
        lost() {
            this.textures = [];
            this.pages = null;
            this.reset();
        }

        destroy() {
            this.reset();
            this.gl = null;
        }

        entryIndex(k, x, y, z) {
            const level = this.levels[k];
            const [sx, sy] = this.pageSize;
            return (((level.pageZ + z) * sy + y) * sx + x) * 4;
        }

        uploadPages() {
            const gl = this.gl;
            if (!gl || !this.pages) return;
            this.pagesVersion += 1;
            gl.bindTexture(gl.TEXTURE_3D, this.pages);
            gl.texSubImage3D(gl.TEXTURE_3D, 0, 0, 0, 0, ...this.pageSize, gl.RGBA_INTEGER,
                             gl.UNSIGNED_BYTE, this.mirror);
        }

        /**
         * Which bricks the view needs: `view` = {eye, forward (clip from
         * world), extent, fullLo, fullSpan, clip [z0, z1] planes, pixel
         * (world size of a screen pixel at distance 1), baseVox (a base
         * voxel in full-resolution pixels, pixels, planes)}. Returns
         * `{leaves: [{k, x, y, z, key, d}], refined: Set}`, nearest first,
         * within the atlas's room.
         */
        plan(view) {
            if (!this.active()) return null;
            return this.cutAt(view, Math.floor(FILL * this.layout.capacity));
        }

        /**
         * The cut, greedily: always refine next where a voxel covers the
         * most screen pixels -- a region still drawn from the base, or a
         * brick whose level is still coarser than a pixel there -- until
         * every voxel drawn is under a pixel or the atlas has no room for
         * the next step. Within the room, the parts nearest the camera are
         * sharpest and the farthest keep the base; a uniform coarsening
         * would give up the near parts to spare the far ones.
         */
        cutAt(view, room = Infinity) {
            const levels = this.levels;
            const top = levels.length - 1;
            const world = (axis, value) =>
                ((value - view.fullLo[axis]) / view.fullSpan[axis] - 0.5) * view.extent[axis];
            // World size of one full-resolution step along each axis.
            const step = [0, 1, 2].map((a) => view.extent[a] / view.fullSpan[a]);
            const lo = [view.fullLo[0], view.fullLo[1], view.clip[0]];
            const hi = [view.fullLo[0] + view.fullSpan[0], view.fullLo[1] + view.fullSpan[1],
                        view.clip[1] + 1];
            /** A brick inside what is drawn and on screen, or null. */
            const make = (k, x, y, z) => {
                const level = levels[k];
                const b0 = [x * level.brickFull[0], y * level.brickFull[1], z * level.brickFull[2]];
                const box0 = [0, 1, 2].map((a) => Math.max(lo[a], b0[a]));
                const box1 = [0, 1, 2].map((a) => Math.min(hi[a], b0[a] + level.brickFull[a]));
                if (box0.some((v, a) => v >= box1[a])) return null;
                const w0 = [0, 1, 2].map((a) => world(a, box0[a]));
                const w1 = [0, 1, 2].map((a) => world(a, box1[a]));
                if (!onScreen(view.forward, w0, w1)) return null;
                return { k, x, y, z, b0, key: `${k}/${x}/${y}/${z}`,
                         d: Math.max(1e-3, distanceToBox(view.eye, w0, w1)) };
            };
            // Screen pixels a voxel covers, along an axis the next finer
            // level resolves better: x/y always (each level halves them); z
            // only where it pools fewer planes -- a stack's planes are often
            // deeper than its pixels at every level.
            const coarseness = (vox, finerVox, d) => {
                const pixel = d * view.pixel;
                let error = (vox[0] * step[0]) / pixel;
                if (finerVox[2] < vox[2]) error = Math.max(error, (vox[2] * step[2]) / pixel);
                return error;
            };
            const children = (node) => {
                const level = levels[node.k];
                const finer = levels[node.k - 1];
                const range = (a) => [Math.floor(node.b0[a] / finer.brickFull[a]),
                                      Math.min(finer.grid[a] - 1,
                                               Math.ceil((node.b0[a] + level.brickFull[a]) / finer.brickFull[a]) - 1)];
                const [x0, x1] = range(0);
                const [y0, y1] = range(1);
                const [z0, z1] = range(2);
                const out = [];
                for (let cz = z0; cz <= z1; cz += 1) {
                    for (let cy = y0; cy <= y1; cy += 1) {
                        for (let cx = x0; cx <= x1; cx += 1) {
                            const child = make(node.k - 1, cx, cy, cz);
                            if (child) out.push(child);
                        }
                    }
                }
                return out;
            };
            const cost = (node) => (this.empties.has(node.key) ? 0 : 1);
            const heap = new MaxHeap();
            const offer = (node, from) => {
                // `from`: the voxel the region is drawn with now.
                if (node.k < 0) return;
                const error = from === "base"
                    ? coarseness(view.baseVox, levels[node.k].vox, node.d)
                    : (node.k > 0 ? coarseness(levels[node.k].vox, levels[node.k - 1].vox, node.d) : 0);
                if (error > 1) heap.push(error, { node, from });
            };
            const root = levels[top];
            const span = (a) => [Math.floor(lo[a] / root.brickFull[a]),
                                 Math.min(root.grid[a] - 1, Math.ceil(hi[a] / root.brickFull[a]) - 1)];
            const [x0, x1] = span(0);
            const [y0, y1] = span(1);
            const [z0, z1] = span(2);
            for (let z = z0; z <= z1; z += 1) {
                for (let y = y0; y <= y1; y += 1) {
                    for (let x = x0; x <= x1; x += 1) {
                        const node = make(top, x, y, z);
                        if (node) offer(node, "base");
                    }
                }
            }
            const leaves = new Map();
            const refined = new Set();
            let used = 0;
            while (heap.size) {
                const { node, from } = heap.pop();
                if (from === "base") {
                    // The base -> this brick of the coarsest atlas level.
                    if (used + cost(node) > room) continue;
                    used += cost(node);
                    leaves.set(node.key, node);
                    offer(node, "atlas");
                    continue;
                }
                // This brick -> its children one level finer. A coarse brick
                // known empty is split too: averaging can hide what is there.
                const kids = children(node);
                const delta = kids.reduce((sum, kid) => sum + cost(kid), 0) - cost(node);
                if (used + delta > room) continue;
                used += delta;
                leaves.delete(node.key);
                refined.add(node.key);
                for (const kid of kids) {
                    leaves.set(kid.key, kid);
                    offer(kid, "atlas");
                }
            }
            // Known empty: nothing to fetch or draw (its rays skip it).
            const out = [...leaves.values()].filter((node) => !this.empties.has(node.key))
                .map(({ k, x, y, z, key, d }) => ({ k, x, y, z, key, d }));
            out.sort((a, b) => a.d - b.d);
            return { leaves: out, refined };
        }

        /** Put `cut`'s refinement into the page table. */
        applyCut(cut) {
            const parse = (key) => key.split("/").map(Number);
            for (const key of this.refined) {
                if (cut.refined.has(key)) continue;
                const [k, x, y, z] = parse(key);
                this.mirror[this.entryIndex(k, x, y, z) + 3] &= ~REFINED;
            }
            for (const key of cut.refined) {
                const [k, x, y, z] = parse(key);
                this.mirror[this.entryIndex(k, x, y, z) + 3] |= REFINED;
            }
            this.refined = cut.refined;
            this.cut = cut;
            const at = now();
            let finest = null;
            for (const leaf of cut.leaves) {
                const slot = this.slotOf.get(leaf.key);
                if (slot !== undefined) this.slotUsed[slot] = at;
                finest = finest === null ? leaf.k : Math.min(finest, leaf.k);
            }
            this.finest = finest;
            this.uploadPages();
        }

        /**
         * At rest: plan the cut for `view`, show it, and fetch what it lacks,
         * nearest first. `view.generation` stale -> later batches not sent.
         */
        async update(view) {
            const cut = this.plan(view);
            if (!cut) return;
            const generation = ++this.generation;
            this.applyCut(cut);
            this.onChange?.();
            const missing = cut.leaves.filter((leaf) => !this.slotOf.has(leaf.key)
                && !this.empties.has(leaf.key) && !this.inflight.has(leaf.key));
            // Batches of one level, nearest first.
            const batches = [];
            const byLevel = new Map();
            for (const leaf of missing) {
                let open = byLevel.get(leaf.k);
                if (!open || open.length >= BATCH) {
                    open = [];
                    byLevel.set(leaf.k, open);
                    batches.push(open);
                }
                open.push(leaf);
            }
            const keep = new Set(cut.leaves.map((leaf) => leaf.key));
            const run = async () => {
                while (batches.length && generation === this.generation) {
                    const batch = batches.shift();
                    for (const leaf of batch) this.inflight.add(leaf.key);
                    try {
                        await this.fetchBatch(batch, keep);
                    } catch (error) {
                        console.error("volumeAtlas: bricks could not be read", error);
                        return;
                    } finally {
                        for (const leaf of batch) this.inflight.delete(leaf.key);
                    }
                }
            };
            await Promise.all(Array.from({ length: IN_FLIGHT }, run));
        }

        async fetchBatch(batch, keep) {
            const level = this.levels[batch[0].k];
            const key = this.key;
            const query = new URLSearchParams({ level: String(level.level), t: String(this.t),
                                                format: this.format });
            for (const channel of this.channels) {
                query.append("channel", channel.name);
                if (channel.stamp) query.append("w", channel.stamp);
            }
            query.set("at", batch.map((leaf) => `${leaf.z}.${leaf.y}.${leaf.x}`).join(","));
            LINK.ask(query);
            const ds = encodeURIComponent(this.datasource);
            this.requests += 1;
            const url = this.address(`volume/${ds}/bricks?${query}`);
            const response = await this.fetch(url);
            if (!response.ok) throw new Error(`bricks: HTTP ${response.status}`);
            const buffer = await response.arrayBuffer();
            if (typeof URL === "function" && global.location?.href) {
                LINK.note(new URL(url, global.location.href).href);
            }
            if (key !== this.key || !this.gl) return;   // started again meanwhile
            const { header, voxels } = parsePayload(buffer);
            this.store(batch[0].k, header, voxels, keep);
        }

        /** One response's bricks into the atlas and the page table. */
        store(k, header, voxels, keep) {
            const gl = this.gl;
            const half = header.format === "f16";
            const size = header.bytes_per_brick;
            const [bz, by, bx] = header.brick;
            let changed = false;
            for (const brick of header.bricks) {
                const [z, y, x] = brick.at;
                const key = `${k}/${x}/${y}/${z}`;
                const entry = this.entryIndex(k, x, y, z);
                if (brick.offset === null) {
                    this.empties.add(key);
                    this.mirror[entry + 3] = (this.mirror[entry + 3] & REFINED) | EMPTY;
                    changed = true;
                    continue;
                }
                const slot = this.allocate(key, keep);
                if (slot < 0) break;
                const [sx, sy] = this.layout.slots;
                const at = [slot % sx, Math.floor(slot / sx) % sy, Math.floor(slot / (sx * sy))];
                for (let c = 0; c < this.textures.length; c += 1) {
                    const start = brick.offset + c * size;
                    const data = half ? new Uint16Array(voxels, start, size / 2)
                        : new Uint8Array(voxels, start, size);
                    gl.bindTexture(gl.TEXTURE_3D, this.textures[c]);
                    gl.texSubImage3D(gl.TEXTURE_3D, 0, at[0] * bx, at[1] * by, at[2] * bz,
                                     bx, by, bz, gl.RED, half ? gl.HALF_FLOAT : gl.UNSIGNED_BYTE,
                                     data);
                }
                this.mirror[entry] = at[0];
                this.mirror[entry + 1] = at[1];
                this.mirror[entry + 2] = at[2];
                this.mirror[entry + 3] = (this.mirror[entry + 3] & REFINED) | RESIDENT;
                changed = true;
            }
            if (changed) {
                this.uploadPages();
                this.onChange?.();
            }
        }

        /** A slot for brick `key`: a free one, else the least recently
         *  wanted one not in `keep`; -1 when every slot is wanted. */
        allocate(key, keep) {
            let slot = this.free.pop();
            if (slot === undefined) {
                let oldest = -1;
                for (let i = 0; i < this.slotKey.length; i += 1) {
                    if (this.slotKey[i] === null) {
                        oldest = i;             // never used: free after all
                        break;
                    }
                    if (keep.has(this.slotKey[i])) continue;
                    if (oldest < 0 || this.slotUsed[i] < this.slotUsed[oldest]) oldest = i;
                }
                if (oldest < 0) return -1;
                slot = oldest;
                if (this.slotKey[slot] !== null) {
                    const [k, x, y, z] = this.slotKey[slot].split("/").map(Number);
                    const entry = this.entryIndex(k, x, y, z);
                    this.mirror[entry + 3] &= ~RESIDENT;
                    this.slotOf.delete(this.slotKey[slot]);
                }
            }
            this.slotKey[slot] = key;
            this.slotUsed[slot] = now();
            this.slotOf.set(key, slot);
            return slot;
        }

        /** Bind the atlas for a draw: textures from `unit` on, and its uniforms. */
        bind(gl, uniform, unit, view) {
            this.textures.forEach((texture, c) => {
                gl.activeTexture(gl.TEXTURE0 + unit + c);
                gl.bindTexture(gl.TEXTURE_3D, texture);
                gl.uniform1i(uniform(`u_atlas${c}`), unit + c);
            });
            const pagesUnit = unit + this.textures.length;
            gl.activeTexture(gl.TEXTURE0 + pagesUnit);
            gl.bindTexture(gl.TEXTURE_3D, this.pages);
            gl.uniform1i(uniform("u_pages"), pagesUnit);
            gl.uniform3fv(uniform("u_full_lo"), view.fullLo);
            gl.uniform3fv(uniform("u_full_span"), view.fullSpan);
            gl.uniform1i(uniform("u_top"), this.levels.length - 1);
            const pad = (list, width, fill) => {
                const out = list.flat();
                while (out.length < MAXL * width) out.push(fill);
                return out;
            };
            gl.uniform3fv(uniform("u_brick_full"), pad(this.levels.map((l) => l.brickFull), 3, 1));
            gl.uniform3fv(uniform("u_vox_full"), pad(this.levels.map((l) => l.vox), 3, 1));
            gl.uniform3iv(uniform("u_page_off"), pad(this.levels.map((l) => [0, 0, l.pageZ]), 3, 0));
            gl.uniform3iv(uniform("u_page_grid"), pad(this.levels.map((l) => l.grid), 3, 1));
            gl.uniform3fv(uniform("u_brick_tex"), this.brickTex);
            gl.uniform3fv(uniform("u_atlas_dim"), this.layout.size);
        }

        /** The finest voxel the cut draws, in full-resolution units (x, y, z). */
        finestVox() {
            return this.finest === null ? null : this.levels[this.finest].vox;
        }
    }

    /** A max-heap of (priority, value). */
    class MaxHeap {
        constructor() {
            this.items = [];
        }

        get size() {
            return this.items.length;
        }

        push(priority, value) {
            const items = this.items;
            items.push({ priority, value });
            let i = items.length - 1;
            while (i > 0) {
                const parent = (i - 1) >> 1;
                if (items[parent].priority >= items[i].priority) break;
                [items[parent], items[i]] = [items[i], items[parent]];
                i = parent;
            }
        }

        pop() {
            const items = this.items;
            const top = items[0];
            const last = items.pop();
            if (items.length) {
                items[0] = last;
                let i = 0;
                for (;;) {
                    const l = 2 * i + 1;
                    const r = l + 1;
                    let best = i;
                    if (l < items.length && items[l].priority > items[best].priority) best = l;
                    if (r < items.length && items[r].priority > items[best].priority) best = r;
                    if (best === i) break;
                    [items[best], items[i]] = [items[i], items[best]];
                    i = best;
                }
            }
            return top.value;
        }
    }

    /** Whether any of box [w0, w1]'s corners can be on screen. */
    function onScreen(m, w0, w1) {
        let left = 0, right = 0, below = 0, above = 0, behind = 0;
        for (let i = 0; i < 8; i += 1) {
            const x = i & 1 ? w1[0] : w0[0];
            const y = i & 2 ? w1[1] : w0[1];
            const z = i & 4 ? w1[2] : w0[2];
            const cx = m[0] * x + m[4] * y + m[8] * z + m[12];
            const cy = m[1] * x + m[5] * y + m[9] * z + m[13];
            const cw = m[3] * x + m[7] * y + m[11] * z + m[15];
            if (cw <= 0) {
                behind += 1;
                continue;
            }
            if (cx < -cw) left += 1;
            if (cx > cw) right += 1;
            if (cy < -cw) below += 1;
            if (cy > cw) above += 1;
        }
        if (behind === 8) return false;
        if (behind) return true;     // straddles the camera: conservatively seen
        return !(left === 8 || right === 8 || below === 8 || above === 8);
    }

    function distanceToBox(p, w0, w1) {
        let sum = 0;
        for (let a = 0; a < 3; a += 1) {
            const d = Math.max(w0[a] - p[a], 0, p[a] - w1[a]);
            sum += d * d;
        }
        return Math.sqrt(sum);
    }

    VolumeAtlas.LINK = LINK;
    VolumeAtlas.atlasLevels = atlasLevels;
    VolumeAtlas.atlasLayout = atlasLayout;
    VolumeAtlas.parsePayload = parsePayload;
    VolumeAtlas.shaderChunk = shaderChunk;
    VolumeAtlas.onScreen = onScreen;
    VolumeAtlas.STATE = { RESIDENT, REFINED, EMPTY };
    VolumeAtlas.MAXL = MAXL;
    global.PlexoraVolumeAtlas = VolumeAtlas;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = VolumeAtlas;
    }
})(typeof window !== "undefined" ? window : globalThis);
