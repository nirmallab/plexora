/**
 * volumeRenderer.js - a z-stack drawn as a volume: WebGL2 ray casting over 3-D textures.
 *
 * Plexora's own raycaster rather than a framework's (the plan's decision G):
 * a few hundred lines in the same idiom as glRenderer.js, no deck.gl or vtk.js
 * in a classic-script codebase, and every budget decided here rather than
 * inside somebody else's loader.
 *
 * WHAT IT DRAWS. One 3-D texture per visible channel (`R8`), each a whole
 * pyramid level fetched from `/volume/<ds>/level` -- the finest level whose
 * voxels fit the GPU budget and whose every side fits `MAX_3D_TEXTURE_SIZE`.
 * The bytes are quantized with the same window as the tiles
 * (server/volume/windows.py), so a channel's contrast slider means the same
 * thing here as on the plane underneath: the per-channel window and colour
 * come straight from the sidebar's slots. In HD the textures are `R16F`
 * (`format=f16`): the same window as 0..1 half floats, so a dim stack whose
 * brightest voxel is far above its tissue keeps its grey levels here too. The
 * budget is shared by the visible channels -- one channel may use four times
 * `DEFAULT_BUDGET`, never more in all than eight channels at that budget.
 *
 * Two composites: maximum-intensity projection along each ray (MIP -- what
 * fluorescence people mean by "3-D view"), and front-to-back additive
 * emission-absorption with early termination. The box is drawn in physical
 * proportion: x, y and z are scaled by the stack's spacing, so a 40-plane
 * stack 2 µm apart over 0.5 µm pixels is a slab four times thicker than its
 * plane count suggests. With no stated z step the planes are drawn one pixel
 * deep, and the badge says "assumed".
 *
 * THE Z RANGE. The depth strip's slab band doubles as the clip range in this
 * mode: what is rendered is planes z0..z1, so cutting the stack down to the
 * planes of interest is the same gesture as choosing a projection's range.
 *
 * Draws on demand -- a frame only after the camera, the channels or the clip
 * changed -- and at half the step count while the user is dragging.
 * `webglcontextlost` hands the viewer back to Plane mode with a toast.
 */
(function (global) {
    "use strict";

    const MAX_CHANNELS = 6;
    const DEFAULT_BUDGET = 48 * 2 ** 20;

    // -- small 4x4 matrix helpers (column-major, like WebGL) --------------

    function mat4Multiply(a, b) {
        const out = new Float32Array(16);
        for (let col = 0; col < 4; col += 1) {
            for (let row = 0; row < 4; row += 1) {
                let sum = 0;
                for (let k = 0; k < 4; k += 1) sum += a[k * 4 + row] * b[col * 4 + k];
                out[col * 4 + row] = sum;
            }
        }
        return out;
    }

    function mat4Perspective(fovy, aspect, near, far) {
        const f = 1 / Math.tan(fovy / 2);
        const nf = 1 / (near - far);
        return new Float32Array([f / aspect, 0, 0, 0, 0, f, 0, 0, 0, 0, (far + near) * nf, -1,
                                 0, 0, 2 * far * near * nf, 0]);
    }

    function mat4LookAt(eye, target, up) {
        let zx = eye[0] - target[0], zy = eye[1] - target[1], zz = eye[2] - target[2];
        let len = Math.hypot(zx, zy, zz) || 1;
        zx /= len; zy /= len; zz /= len;
        let xx = up[1] * zz - up[2] * zy, xy = up[2] * zx - up[0] * zz, xz = up[0] * zy - up[1] * zx;
        len = Math.hypot(xx, xy, xz) || 1;
        xx /= len; xy /= len; xz /= len;
        const yx = zy * xz - zz * xy, yy = zz * xx - zx * xz, yz = zx * xy - zy * xx;
        return new Float32Array([
            xx, yx, zx, 0, xy, yy, zy, 0, xz, yz, zz, 0,
            -(xx * eye[0] + xy * eye[1] + xz * eye[2]),
            -(yx * eye[0] + yy * eye[1] + yz * eye[2]),
            -(zx * eye[0] + zy * eye[1] + zz * eye[2]), 1]);
    }

    function mat4Invert(m) {
        const inv = new Float32Array(16);
        inv[0] = m[5] * m[10] * m[15] - m[5] * m[11] * m[14] - m[9] * m[6] * m[15] + m[9] * m[7] * m[14] + m[13] * m[6] * m[11] - m[13] * m[7] * m[10];
        inv[4] = -m[4] * m[10] * m[15] + m[4] * m[11] * m[14] + m[8] * m[6] * m[15] - m[8] * m[7] * m[14] - m[12] * m[6] * m[11] + m[12] * m[7] * m[10];
        inv[8] = m[4] * m[9] * m[15] - m[4] * m[11] * m[13] - m[8] * m[5] * m[15] + m[8] * m[7] * m[13] + m[12] * m[5] * m[11] - m[12] * m[7] * m[9];
        inv[12] = -m[4] * m[9] * m[14] + m[4] * m[10] * m[13] + m[8] * m[5] * m[14] - m[8] * m[6] * m[13] - m[12] * m[5] * m[10] + m[12] * m[6] * m[9];
        inv[1] = -m[1] * m[10] * m[15] + m[1] * m[11] * m[14] + m[9] * m[2] * m[15] - m[9] * m[3] * m[14] - m[13] * m[2] * m[11] + m[13] * m[3] * m[10];
        inv[5] = m[0] * m[10] * m[15] - m[0] * m[11] * m[14] - m[8] * m[2] * m[15] + m[8] * m[3] * m[14] + m[12] * m[2] * m[11] - m[12] * m[3] * m[10];
        inv[9] = -m[0] * m[9] * m[15] + m[0] * m[11] * m[13] + m[8] * m[1] * m[15] - m[8] * m[3] * m[13] - m[12] * m[1] * m[11] + m[12] * m[3] * m[9];
        inv[13] = m[0] * m[9] * m[14] - m[0] * m[10] * m[13] - m[8] * m[1] * m[14] + m[8] * m[2] * m[13] + m[12] * m[1] * m[10] - m[12] * m[2] * m[9];
        inv[2] = m[1] * m[6] * m[15] - m[1] * m[7] * m[14] - m[5] * m[2] * m[15] + m[5] * m[3] * m[14] + m[13] * m[2] * m[7] - m[13] * m[3] * m[6];
        inv[6] = -m[0] * m[6] * m[15] + m[0] * m[7] * m[14] + m[4] * m[2] * m[15] - m[4] * m[3] * m[14] - m[12] * m[2] * m[7] + m[12] * m[3] * m[6];
        inv[10] = m[0] * m[5] * m[15] - m[0] * m[7] * m[13] - m[4] * m[1] * m[15] + m[4] * m[3] * m[13] + m[12] * m[1] * m[7] - m[12] * m[3] * m[5];
        inv[14] = -m[0] * m[5] * m[14] + m[0] * m[6] * m[13] + m[4] * m[1] * m[14] - m[4] * m[2] * m[13] - m[12] * m[1] * m[6] + m[12] * m[2] * m[5];
        inv[3] = -m[1] * m[6] * m[11] + m[1] * m[7] * m[10] + m[5] * m[2] * m[11] - m[5] * m[3] * m[10] - m[9] * m[2] * m[7] + m[9] * m[3] * m[6];
        inv[7] = m[0] * m[6] * m[11] - m[0] * m[7] * m[10] - m[4] * m[2] * m[11] + m[4] * m[3] * m[10] + m[8] * m[2] * m[7] - m[8] * m[3] * m[6];
        inv[11] = -m[0] * m[5] * m[11] + m[0] * m[7] * m[9] + m[4] * m[1] * m[11] - m[4] * m[3] * m[9] - m[8] * m[1] * m[7] + m[8] * m[3] * m[5];
        inv[15] = m[0] * m[5] * m[10] - m[0] * m[6] * m[9] - m[4] * m[1] * m[10] + m[4] * m[2] * m[9] + m[8] * m[1] * m[6] - m[8] * m[2] * m[5];
        let det = m[0] * inv[0] + m[1] * inv[4] + m[2] * inv[8] + m[3] * inv[12];
        if (!det) return null;
        det = 1 / det;
        for (let i = 0; i < 16; i += 1) inv[i] *= det;
        return inv;
    }

    // -- shaders -------------------------------------------------------------

    const VERTEX = `#version 300 es
in vec2 a_position;
out vec2 v_ndc;
void main() {
    v_ndc = a_position;
    gl_Position = vec4(a_position, 0.0, 1.0);
}`;

    /**
     * One fragment program per channel count: GLSL ES 3.00 cannot index an
     * array of samplers with a loop variable, so the channel loop is unrolled
     * into NCH explicit samples.
     */
    function fragmentSource(count) {
        const samplers = [];
        const sample = [];
        for (let c = 0; c < count; c += 1) {
            samplers.push(`uniform sampler3D u_volume${c};`);
            sample.push(`        v[${c}] = texture(u_volume${c}, p).r;`);
        }
        return `#version 300 es
precision highp float;
precision highp sampler3D;
#define NCH ${count}
${samplers.join("\n")}
uniform mat4 u_inverse;        // clip -> model (box in [0,1]^3, then scaled)
uniform vec3 u_extent;         // physical box, longest side 1
uniform vec2 u_clip_z;         // texture z range shown, [0,1]
uniform vec3 u_color[NCH];
uniform vec2 u_window[NCH];    // [lo, hi] in 0..1 texture units
uniform float u_opacity[NCH];
uniform int u_mode;            // 0 MIP, 1 additive
uniform int u_steps;
uniform vec3 u_ground;
in vec2 v_ndc;
out vec4 outColor;

vec2 hitBox(vec3 origin, vec3 dir, vec3 lo, vec3 hi) {
    vec3 inv = 1.0 / dir;
    vec3 t0 = (lo - origin) * inv;
    vec3 t1 = (hi - origin) * inv;
    vec3 tmin = min(t0, t1);
    vec3 tmax = max(t0, t1);
    return vec2(max(max(tmin.x, tmin.y), tmin.z), min(min(tmax.x, tmax.y), tmax.z));
}

float level(float value, vec2 window) {
    return clamp((value - window.x) / max(window.y - window.x, 1e-4), 0.0, 1.0);
}

void main() {
    vec4 nearPoint = u_inverse * vec4(v_ndc, -1.0, 1.0);
    vec4 farPoint = u_inverse * vec4(v_ndc, 1.0, 1.0);
    vec3 origin = nearPoint.xyz / nearPoint.w;
    vec3 dir = normalize(farPoint.xyz / farPoint.w - origin);
    vec3 lo = vec3(0.0, 0.0, u_clip_z.x) * u_extent - 0.5 * u_extent;
    vec3 hi = vec3(1.0, 1.0, u_clip_z.y) * u_extent - 0.5 * u_extent;
    vec2 hit = hitBox(origin, dir, lo, hi);
    if (hit.x > hit.y || hit.y < 0.0) {
        outColor = vec4(u_ground, 1.0);
        return;
    }
    float t0 = max(hit.x, 0.0);
    float len = hit.y - t0;
    float stepLength = length(u_extent) / float(u_steps);
    int count = int(ceil(len / stepLength));
    // Jitter the first step by the pixel's position, so the regular sampling
    // grid does not show as wood-grain rings.
    float jitter = fract(sin(dot(gl_FragCoord.xy, vec2(12.9898, 78.233))) * 43758.5453);
    float v[NCH];
    float peak[NCH];
    for (int c = 0; c < NCH; c++) peak[c] = 0.0;
    vec3 colour = vec3(0.0);
    float alpha = 0.0;
    for (int i = 0; i < 4096; i++) {
        if (i >= count) break;
        float t = t0 + (float(i) + jitter) * stepLength;
        vec3 world = origin + dir * t;
        vec3 p = world / u_extent + 0.5;
${sample.join("\n")}
        if (u_mode == 0) {
            for (int c = 0; c < NCH; c++) peak[c] = max(peak[c], v[c]);
        } else {
            vec3 emit = vec3(0.0);
            float a = 0.0;
            for (int c = 0; c < NCH; c++) {
                float s = level(v[c], u_window[c]);
                emit += u_color[c] * s;
                a = max(a, s * u_opacity[c]);
            }
            a = 1.0 - pow(1.0 - clamp(a, 0.0, 0.999), stepLength * 120.0);
            colour += (1.0 - alpha) * emit * a;
            alpha += (1.0 - alpha) * a;
            if (alpha > 0.97) break;
        }
    }
    if (u_mode == 0) {
        for (int c = 0; c < NCH; c++) colour += u_color[c] * level(peak[c], u_window[c]);
        outColor = vec4(max(min(colour, vec3(1.0)), u_ground), 1.0);
    } else {
        outColor = vec4(min(colour + (1.0 - alpha) * u_ground, vec3(1.0)), 1.0);
    }
}`;
    }

    function compile(gl, type, text) {
        const shader = gl.createShader(type);
        gl.shaderSource(shader, text);
        gl.compileShader(shader);
        if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
            const log = gl.getShaderInfoLog(shader);
            gl.deleteShader(shader);
            throw new Error(`volume shader: ${log}`);
        }
        return shader;
    }

    function program(gl, count) {
        const built = gl.createProgram();
        gl.attachShader(built, compile(gl, gl.VERTEX_SHADER, VERTEX));
        gl.attachShader(built, compile(gl, gl.FRAGMENT_SHADER, fragmentSource(count)));
        gl.bindAttribLocation(built, 0, "a_position");
        gl.linkProgram(built);
        if (!gl.getProgramParameter(built, gl.LINK_STATUS)) {
            throw new Error(`volume program: ${gl.getProgramInfoLog(built)}`);
        }
        return built;
    }

    function hexToRgb(colour) {
        if (colour && typeof colour === "object" && "r" in colour) {
            return [colour.r / 255, colour.g / 255, colour.b / 255];
        }
        const text = String(colour || "#ffffff").replace(/^#/, "");
        const full = text.length === 3 ? text.split("").map((c) => c + c).join("") : text;
        const value = parseInt(full, 16);
        if (!Number.isFinite(value)) return [1, 1, 1];
        return [((value >> 16) & 255) / 255, ((value >> 8) & 255) / 255, (value & 255) / 255];
    }

    class VolumeRenderer {
        constructor({ viewState, datasource, wrapper = null, fetchImpl = null,
                      budget = DEFAULT_BUDGET } = {}) {
            this.viewState = viewState;
            this.datasource = datasource;
            this.wrapper = wrapper || document.getElementById("openseadragon_wrapper");
            this.fetch = fetchImpl || ((...args) => global.fetch(...args));
            this.budget = budget;
            this.canvas = null;
            this.gl = null;
            this.programs = new Map();
            this.textures = new Map();
            this.camera = { yaw: -0.6, pitch: 0.55, dist: 2.2, panX: 0, panY: 0 };
            this.composite = "mip";
            this.dirty = true;
            this.frame = 0;
            this.interacting = false;
            this.status = "idle";
            this.level = null;
            this.shape = null;
            this._loads = new Map();
        }

        /** Whether this browser can draw a volume at all. */
        static supported() {
            try {
                const probe = document.createElement("canvas");
                const gl = probe.getContext("webgl2");
                if (!gl) return false;
                const size = gl.getParameter(gl.MAX_3D_TEXTURE_SIZE);
                gl.getExtension("WEBGL_lose_context")?.loseContext();
                return size >= 256;
            } catch (error) {
                return false;
            }
        }

        mount() {
            if (this.canvas) return true;
            if (!this.wrapper) return false;
            const canvas = document.createElement("canvas");
            canvas.className = "viewer-volume-canvas";
            canvas.setAttribute("aria-label", "Volume rendering");
            canvas.tabIndex = 0;
            const gl = canvas.getContext("webgl2", { antialias: false, premultipliedAlpha: false,
                                                     preserveDrawingBuffer: true });
            if (!gl) return false;
            this.canvas = canvas;
            this.gl = gl;
            this.maxTexture = gl.getParameter(gl.MAX_3D_TEXTURE_SIZE) || 256;
            gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
            this.quad = gl.createBuffer();
            gl.bindBuffer(gl.ARRAY_BUFFER, this.quad);
            gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]),
                          gl.STATIC_DRAW);
            canvas.addEventListener("webglcontextlost", (event) => {
                event.preventDefault();
                this.lost();
            });
            this.bindPointer(canvas);
            this.wrapper.appendChild(canvas);
            this._resizeObserver = typeof ResizeObserver === "function"
                ? new ResizeObserver(() => this.invalidate()) : null;
            this._resizeObserver?.observe(this.wrapper);
            return true;
        }

        destroy() {
            cancelAnimationFrame(this.frame);
            this._resizeObserver?.disconnect();
            for (const entry of this.textures.values()) this.gl?.deleteTexture(entry.texture);
            this.textures.clear();
            this.canvas?.remove();
            this.canvas = null;
            this.gl = null;
        }

        show(visible) {
            if (this.canvas) this.canvas.hidden = !visible;
            if (visible) this.invalidate();
        }

        lost() {
            this.textures.clear();
            this.programs.clear();
            this.status = "lost";
            global.PlexoraToast?.show?.({
                title: "The volume view was lost by the graphics driver; back to planes.",
                tone: "warning",
            });
            this.viewState?.set?.({ mode: "plane" });
        }

        // -- channels ------------------------------------------------------

        /** The sidebar's visible channels: name, colour, window in 0..1. */
        channels() {
            const sidebar = global.__plexora?.viewerSidebar;
            const slots = (sidebar?.channelSlots || []).filter((slot) => slot.enabled && slot.name)
                .slice(0, MAX_CHANNELS);
            return slots.map((slot) => {
                const fullName = sidebar.dataLayer?.getFullChannelName?.(slot.name) || slot.name;
                let window = (slot.range || [0, 255]).map((value) => value / 255);
                if (sidebar.isHdMode?.()) {
                    // Raw units onto the texture's 0..1, unrounded: a half-float
                    // texture resolves far finer than one byte step.
                    const packet = sidebar.quantWindow?.(slot.name, "volume");
                    if (packet) {
                        const span = Math.max(packet.qmax - packet.qmin, 1);
                        window = (slot.range || [0, 0]).map((value) => Math.min(1, Math.max(0,
                            (value - packet.qmin) / span)));
                    }
                }
                return {
                    name: fullName,
                    colour: hexToRgb(slot.color?.hex || slot.color),
                    window,
                    opacity: this.opacityOf(fullName),
                };
            });
        }

        opacityOf(name) {
            const stored = this.viewState?.get?.()?.volume?.opacity?.[name];
            return Number.isFinite(stored) ? stored : 0.6;
        }

        /** Fetch (once) the level every visible channel needs. */
        async ensureTextures(channels) {
            const wanted = new Set(channels.map((channel) => channel.name));
            for (const [name, entry] of this.textures) {
                if (!wanted.has(name)) {
                    this.gl.deleteTexture(entry.texture);
                    this.textures.delete(name);
                }
            }
            const t = this.viewState?.get?.()?.t || 0;
            const missing = channels.filter((channel) => {
                const held = this.textures.get(channel.name);
                return !held || held.t !== t || held.format !== this.format();
            });
            if (!missing.length) return;
            this.status = "loading";
            this.paintStatus();
            const budget = Math.max(this.budget,
                                    Math.floor((4 * this.budget) / Math.max(1, channels.length)));
            await Promise.all(missing.map((channel) => this.loadChannel(channel.name, t, budget)));
            this.status = "ready";
            this.paintStatus();
        }

        /** `f16` while HD tiles are on (16-bit data, half-float textures), else `u8`. */
        format() {
            return global.__plexora?.viewerSidebar?.isHdMode?.() ? "f16" : "u8";
        }

        async loadChannel(name, t, budget = this.budget) {
            const format = this.format();
            const key = `${name}|${t}|${format}`;
            if (this._loads.has(key)) return this._loads.get(key);
            const ds = encodeURIComponent(this.datasource);
            const query = new URLSearchParams({ channel: name, t: String(t),
                                                budget: String(budget),
                                                max_side: String(this.maxTexture), format });
            const address = typeof global.plexoraUrl === "function"
                ? global.plexoraUrl(`volume/${ds}/level?${query}`)
                : `/volume/${ds}/level?${query}`;
            const load = (async () => {
                const response = await this.fetch(address);
                if (!response.ok) throw new Error(`volume level: HTTP ${response.status}`);
                const shape = String(response.headers.get("X-Plexora-Shape") || "")
                    .split(",").map(Number);
                const half = (response.headers.get("X-Plexora-Format") || "u8") === "f16";
                const buffer = await response.arrayBuffer();
                const voxels = half ? new Uint16Array(buffer) : new Uint8Array(buffer);
                const [depth, height, width] = shape;
                if (!(depth * height * width === voxels.length)) {
                    throw new Error("volume level: the shape does not match the bytes");
                }
                const gl = this.gl;
                if (!gl) return;
                const texture = gl.createTexture();
                gl.bindTexture(gl.TEXTURE_3D, texture);
                gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
                gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
                gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
                gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
                gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_R, gl.CLAMP_TO_EDGE);
                if (half) {
                    gl.texImage3D(gl.TEXTURE_3D, 0, gl.R16F, width, height, depth, 0, gl.RED,
                                  gl.HALF_FLOAT, voxels);
                } else {
                    gl.texImage3D(gl.TEXTURE_3D, 0, gl.R8, width, height, depth, 0, gl.RED,
                                  gl.UNSIGNED_BYTE, voxels);
                }
                const held = this.textures.get(name);
                if (held) gl.deleteTexture(held.texture);
                this.textures.set(name, { texture, t, format, shape: [depth, height, width] });
                // The default contrast for what is drawn here (the level's own
                // maxima, in raw units): the sidebar levels auto channels by it.
                const hint = String(response.headers.get("X-Plexora-Hint") || "")
                    .split(",").map(Number);
                if (hint.length === 2 && hint.every(Number.isFinite)) {
                    global.dispatchEvent?.(new CustomEvent("plexora:volume-hint",
                        { detail: { name, hint } }));
                }
                this.level = Number(response.headers.get("X-Plexora-Level") || 0);
                this.shape = [depth, height, width];
                this.invalidate();
            })().finally(() => this._loads.delete(key));
            this._loads.set(key, load);
            return load;
        }

        // -- drawing -------------------------------------------------------

        invalidate() {
            this.dirty = true;
            if (this.frame) return;
            this.frame = requestAnimationFrame(() => {
                this.frame = 0;
                if (this.dirty) this.draw();
            });
        }

        /** The box's physical extent, longest side 1. */
        extent() {
            const vs = this.viewState;
            const info = vs?.info || {};
            const [depth, height, width] = this.shape
                || [vs?.depth || 1, info.height || 1, info.width || 1];
            const full = info.levels?.[0] || { width: info.width, height: info.height };
            const scale = (full.width || width) / width;
            const sx = Number(info.spacing?.x?.value) || 1;
            const sz = Number(info.spacing?.z?.value) || sx;
            // The stack's own plane count: a texture's z may be max-pooled
            // (`X-Plexora-ZPool`), which changes its depth and not the stack's.
            const planes = Math.max(depth, Number(vs?.depth) || 0);
            const physical = [width * scale * sx, height * scale * sx, planes * sz];
            const longest = Math.max(...physical) || 1;
            return physical.map((side) => side / longest);
        }

        matrices() {
            const canvas = this.canvas;
            const aspect = canvas.width / Math.max(1, canvas.height);
            const { yaw, pitch, dist, panX, panY } = this.camera;
            const eye = [dist * Math.cos(pitch) * Math.sin(yaw),
                         dist * Math.sin(pitch),
                         dist * Math.cos(pitch) * Math.cos(yaw)];
            const target = [panX, panY, 0];
            const view = mat4LookAt([eye[0] + panX, eye[1] + panY, eye[2]], target, [0, 1, 0]);
            const projection = mat4Perspective(0.7, aspect, 0.01, 20);
            // Image y runs down and z runs into the tissue; flip both so the
            // first plane sits on top and the picture is not mirrored.
            const model = new Float32Array([1, 0, 0, 0, 0, -1, 0, 0, 0, 0, -1, 0, 0, 0, 0, 1]);
            return mat4Invert(mat4Multiply(projection, mat4Multiply(view, model)));
        }

        draw() {
            this.dirty = false;
            const gl = this.gl;
            const canvas = this.canvas;
            if (!gl || !canvas || canvas.hidden) return;
            const ratio = Math.min(global.devicePixelRatio || 1, 1.5);
            const width = Math.max(1, Math.round(canvas.clientWidth * ratio));
            const height = Math.max(1, Math.round(canvas.clientHeight * ratio));
            if (canvas.width !== width || canvas.height !== height) {
                canvas.width = width;
                canvas.height = height;
            }
            gl.viewport(0, 0, width, height);
            const channels = this.channels().filter((channel) => this.textures.has(channel.name));
            gl.clearColor(0, 0, 0, 1);
            gl.clear(gl.COLOR_BUFFER_BIT);
            if (!channels.length) return;
            let built = this.programs.get(channels.length);
            if (!built) {
                built = program(gl, channels.length);
                this.programs.set(channels.length, built);
            }
            gl.useProgram(built);
            const uniform = (name) => gl.getUniformLocation(built, name);
            channels.forEach((channel, index) => {
                gl.activeTexture(gl.TEXTURE0 + index);
                gl.bindTexture(gl.TEXTURE_3D, this.textures.get(channel.name).texture);
                gl.uniform1i(uniform(`u_volume${index}`), index);
            });
            gl.uniform3fv(uniform("u_color"), channels.flatMap((channel) => channel.colour));
            gl.uniform2fv(uniform("u_window"), channels.flatMap((channel) => channel.window));
            gl.uniform1fv(uniform("u_opacity"), channels.map((channel) => channel.opacity));
            gl.uniformMatrix4fv(uniform("u_inverse"), false, this.matrices());
            gl.uniform3fv(uniform("u_extent"), this.extent());
            const state = this.viewState?.get?.();
            const depth = Math.max(1, this.viewState?.depth || 1);
            const clip = state ? [state.slab.z0 / depth, (state.slab.z1 + 1) / depth] : [0, 1];
            gl.uniform2fv(uniform("u_clip_z"), clip);
            gl.uniform1i(uniform("u_mode"), this.composite === "additive" ? 1 : 0);
            const longest = this.shape ? Math.max(...this.shape) : 256;
            // Along the diagonal, so about one sample per texel along any axis.
            const steps = Math.min(1024, Math.max(96, Math.round(longest * 2)));
            gl.uniform1i(uniform("u_steps"), this.interacting ? Math.round(steps / 2) : steps);
            gl.uniform3fv(uniform("u_ground"), [0, 0, 0]);
            gl.bindBuffer(gl.ARRAY_BUFFER, this.quad);
            gl.enableVertexAttribArray(0);
            gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
            gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
        }

        async refresh() {
            if (!this.gl) return;
            const channels = this.channels();
            try {
                await this.ensureTextures(channels);
            } catch (error) {
                console.error("volumeRenderer:", error);
                this.status = "error";
                this.paintStatus();
            }
            this.invalidate();
        }

        paintStatus() {
            global.dispatchEvent?.(new CustomEvent("plexora:volume-progress",
                { detail: { status: this.status, level: this.level, shape: this.shape } }));
        }

        preset(name) {
            const presets = { xy: [0, Math.PI / 2 - 0.001], xz: [0, 0], yz: [Math.PI / 2, 0],
                              iso: [-0.6, 0.55] };
            const [yaw, pitch] = presets[name] || presets.iso;
            Object.assign(this.camera, { yaw, pitch, panX: 0, panY: 0 });
            this.invalidate();
        }

        bindPointer(canvas) {
            let drag = null;
            canvas.addEventListener("pointerdown", (event) => {
                canvas.setPointerCapture?.(event.pointerId);
                drag = { x: event.clientX, y: event.clientY,
                         pan: event.shiftKey || event.button === 1 };
                this.interacting = true;
            });
            canvas.addEventListener("pointermove", (event) => {
                if (!drag) return;
                const dx = event.clientX - drag.x;
                const dy = event.clientY - drag.y;
                drag.x = event.clientX;
                drag.y = event.clientY;
                if (drag.pan) {
                    const scale = this.camera.dist / Math.max(1, canvas.clientHeight);
                    this.camera.panX -= dx * scale;
                    this.camera.panY += dy * scale;
                } else {
                    this.camera.yaw -= dx * 0.008;
                    this.camera.pitch = Math.max(-1.55, Math.min(1.55, this.camera.pitch + dy * 0.008));
                }
                this.invalidate();
            });
            const end = () => {
                drag = null;
                this.interacting = false;
                this.invalidate();
            };
            canvas.addEventListener("pointerup", end);
            canvas.addEventListener("pointercancel", end);
            canvas.addEventListener("wheel", (event) => {
                if (event.shiftKey) return;
                event.preventDefault();
                const factor = Math.exp(event.deltaY * 0.0012);
                this.camera.dist = Math.max(0.4, Math.min(8, this.camera.dist * factor));
                this.invalidate();
            }, { passive: false });
            canvas.addEventListener("dblclick", () => this.preset("iso"));
        }

        /** The current picture as a canvas (captures). */
        snapshot() {
            if (!this.canvas) return null;
            this.draw();
            return this.canvas;
        }
    }

    /**
     * Wire a renderer to the viewer: built the first time Volume mode is
     * chosen, shown only in it, redrawn when the channels, the clip range or
     * the composite change, refetched when the frame changes. Returns the
     * controller, or null where WebGL2 3-D textures are not available (the
     * strip then offers no Volume button at all).
     */
    function attach(viewState, { datasource, eventHandler = null } = {}) {
        if (!viewState?.enabled?.() || viewState.depth < 2 || !VolumeRenderer.supported()) {
            return null;
        }
        let renderer = null;
        let lastT = null;
        const controller = {
            get renderer() { return renderer; },
            sync(state) {
                const on = viewState.isVolume(state);
                if (!on) {
                    renderer?.show(false);
                    document.getElementById("openseadragon_wrapper")
                        ?.classList.remove("is-volume-mode");
                    return;
                }
                if (!renderer) {
                    renderer = new VolumeRenderer({ viewState, datasource });
                    if (!renderer.mount()) {
                        renderer = null;
                        global.PlexoraToast?.show?.({
                            title: "Volume rendering needs WebGL2 3-D textures, which this "
                                + "browser did not provide.", tone: "warning" });
                        viewState.set({ mode: "plane" });
                        return;
                    }
                }
                document.getElementById("openseadragon_wrapper")?.classList.add("is-volume-mode");
                renderer.composite = state.volume?.composite === "additive" ? "additive" : "mip";
                renderer.show(true);
                if (lastT !== state.t) {
                    lastT = state.t;
                    renderer.refresh();
                } else {
                    renderer.invalidate();
                }
            },
            channelsChanged() {
                if (renderer && viewState.isVolume()) renderer.refresh();
            },
        };
        // HD swaps the textures for half floats (or back): the same refresh.
        global.addEventListener?.("plexora:hd-mode-changed", () => controller.channelsChanged());
        viewState.subscribe((state) => controller.sync(state));
        // `class ChannelList` in a classic script is a global LEXICAL binding,
        // never a property of window -- so it is reached by name.
        const lists = typeof ChannelList !== "undefined" ? ChannelList : global.ChannelList;
        if (eventHandler?.bind && lists?.events) {
            eventHandler.bind(lists.events.CHANNELS_CHANGE, () => controller.channelsChanged());
            eventHandler.bind(lists.events.COLOR_TRANSFER_CHANGE, () => renderer?.invalidate());
        }
        global.addEventListener?.("plexora:channels-applied", () => controller.channelsChanged());
        controller.sync(viewState.get());
        return controller;
    }

    VolumeRenderer.attach = attach;
    VolumeRenderer.fragmentSource = fragmentSource;
    VolumeRenderer.mat4 = { multiply: mat4Multiply, invert: mat4Invert,
                            perspective: mat4Perspective, lookAt: mat4LookAt };
    global.PlexoraVolumeRenderer = VolumeRenderer;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = VolumeRenderer;
    }
})(typeof window !== "undefined" ? window : globalThis);
