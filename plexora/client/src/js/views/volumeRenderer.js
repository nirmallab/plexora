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
 * TEXTURES STAY ON THE GPU. Each is kept by what its bytes are -- channel,
 * frame, format, area -- in a byte-budgeted LRU (`GPU_BUDGET`), whether or not
 * its channel is on: switching a channel off and on again, leaving 3-D and
 * coming back to the same area, or going back to fast tiles after HD, is a
 * uniform, not a download and an upload. A channel switched on while others
 * hold textures is fetched at the budget its share allows; the others keep
 * the finer levels they have while the budget holds them. The level route
 * names its bytes (ETag, and `w=` for the window), so a reload revalidates
 * instead of downloading.
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
 * changed. `webglcontextlost` hands the viewer back to Plane mode with a toast.
 *
 * MOVING IT. A drag turns the stack under the hand, one frame per display
 * refresh (views/volumeCamera.js holds the orientation and the named views,
 * which match the plane viewer and the section panes). X, Y or Z locks the
 * turn to that axis of the IMAGE -- pressed again, or Esc, lets go -- and the
 * orientation gizmo in a corner (views/orientationGizmo.js) shows the axes,
 * the lock, and turns to look along an axis when one is pressed.
 * Let go while it is moving and it keeps turning, slowing the way a
 * real object does: the speed decays exponentially, per millisecond rather
 * than per frame (the same coast at 60 Hz and 120 Hz), so a flick carries as
 * far as it was fast and a slow, placed release does not move at all. Any
 * press stops it dead. The wheel's zoom eases to where it is going, and the
 * preset views turn there rather than cut. While anything moves, the picture
 * is drawn with half the samples and -- if frames still come slower than the
 * display -- at a lower resolution, learnt as it goes; the moment it rests it
 * is drawn again in full. With motion off (the system's reduced-motion
 * setting, or the Depth card's switch: services/viewerPrefs.js) nothing
 * coasts or eases: every change lands at once.
 */
(function (global) {
    "use strict";

    //: How long the clip range must rest before its contrast is asked for (ms).
    const HINT_SETTLE_MS = 220;
    const MAX_CHANNELS = 6;
    //: The camera's vertical field of view, in radians.
    const FOV_Y = 0.7;
    const DEFAULT_BUDGET = 48 * 2 ** 20;
    //: The camera at rest this long is looked at again for a finer detail.
    const DETAIL_REST_MS = 300;
    //: A detail only for a part of the stack under this share of its volume.
    const DETAIL_MAX_SHARE = 0.35;
    //: A new detail only when the part seen overlaps the last by less.
    const DETAIL_KEEP_OVERLAP = 0.7;
    //: Points per side of the lattice the part seen is found with.
    const REGION_LATTICE = 16;
    //: Bytes of a detail uploaded to the GPU per display frame.
    const UPLOAD_PER_FRAME = 4 * 2 ** 20;
    //: How close the camera may come (the stack's longest side is 1).
    const MIN_DIST = 0.15;
    //: What the textures may hold on the GPU in all, switched-off channels,
    //: details and other areas included; past it details go first, then the
    //: least recently drawn.
    const GPU_BUDGET = 1024 * 2 ** 20;

    const Camera = () => global.PlexoraVolumeCamera;

    /** Intersection over union of two {x0, x1, y0, y1, z0, z1} boxes, 0..1:
     *  the same part seen, not merely one inside the other (a part zoomed
     *  into is inside the last, and may be what a finer level fits). */
    function overlap(a, b) {
        const span = (lo0, hi0, lo1, hi1) => Math.max(0, Math.min(hi0, hi1) - Math.max(lo0, lo1));
        const inter = span(a.x0, a.x1, b.x0, b.x1) * span(a.y0, a.y1, b.y0, b.y1)
            * span(a.z0, a.z1 + 1, b.z0, b.z1 + 1);
        const size = (r) => (r.x1 - r.x0) * (r.y1 - r.y0) * (r.z1 - r.z0 + 1);
        const union = size(a) + size(b) - inter;
        return union > 0 ? inter / union : 0;
    }

    /** A number as Python's `f"{x:g}"` writes it, for the plain integers and
     *  short decimals a window is (the server's `w=` check; anything else
     *  only costs a revalidation). */
    function formatG(value) {
        const number = Number(value);
        return Number.isInteger(number) && Math.abs(number) < 1e6
            ? String(number) : String(Number(number.toPrecision(6)));
    }
    //: How long a released spin takes to lose 63 % of its speed. With
    //: SPIN_MAX this bounds a coast at SPIN_TAU_MS x SPIN_MAX radians (about
    //: 57 degrees for the hardest flick, 25 for an ordinary drag let go).
    const SPIN_TAU_MS = 220;
    //: Radians per millisecond: a release slower than RELEASE_MIN is a stop
    //: (placing the view precisely must not turn into a nudge), a spin that
    //: has slowed to SPIN_STOP ends, and no flick spins faster than SPIN_MAX.
    const RELEASE_MIN = 0.0004;
    const SPIN_STOP = 0.00008;
    const SPIN_MAX = 0.0045;
    //: A release's speed is the pointer's over its last VELOCITY_WINDOW_MS;
    //: a pointer held still for RELEASE_STILL_MS before letting go has none.
    const VELOCITY_WINDOW_MS = 90;
    const RELEASE_STILL_MS = 70;
    //: The wheel's zoom closes on its target with this time constant.
    const ZOOM_TAU_MS = 90;
    //: A preset view, or a keyboard turn, takes this long to arrive.
    const TURN_MS = 450;
    const KEY_TURN = Math.PI / 12;
    //: While moving: frames slower than SLOW_FRAME_MS on average lower the
    //: resolution, down to MIN_FAST_SCALE of full; at rest it is always full,
    //: drawn SETTLE_MS after the last movement.
    const SLOW_FRAME_MS = 24;
    const MIN_FAST_SCALE = 0.4;
    const SETTLE_MS = 90;

    function now() {
        return global.performance?.now?.() ?? Date.now();
    }


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
    function fragmentSource(count, detail = false) {
        const samplers = [];
        const sample = [];
        for (let c = 0; c < count; c += 1) {
            samplers.push(`uniform sampler3D u_volume${c};`);
            if (detail) {
                // The detail sub-volume where the sample falls in it, the
                // whole level everywhere else (see `detail` in the renderer).
                samplers.push(`uniform sampler3D u_detail${c};`);
                sample.push(`        v[${c}] = inDetail ? texture(u_detail${c}, q).r : texture(u_volume${c}, p).r;`);
            } else {
                sample.push(`        v[${c}] = texture(u_volume${c}, p).r;`);
            }
        }
        const locate = detail ? `
        bool inDetail = all(greaterThanEqual(p, u_detail_lo)) && all(lessThanEqual(p, u_detail_hi));
        vec3 q = (p - u_detail_lo) / max(u_detail_hi - u_detail_lo, vec3(1e-6));` : "";
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
${detail ? "uniform vec3 u_detail_lo;\nuniform vec3 u_detail_hi;" : ""}
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
        vec3 p = world / u_extent + 0.5;${locate}
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

    function program(gl, count, detail = false) {
        const built = gl.createProgram();
        gl.attachShader(built, compile(gl, gl.VERTEX_SHADER, VERTEX));
        gl.attachShader(built, compile(gl, gl.FRAGMENT_SHADER, fragmentSource(count, detail)));
        gl.bindAttribLocation(built, 0, "a_position");
        gl.linkProgram(built);
        if (!gl.getProgramParameter(built, gl.LINK_STATUS)) {
            throw new Error(`volume program: ${gl.getProgramInfoLog(built)}`);
        }
        return built;
    }

    /**
     * The hand's turning speed as it let go, in radians per millisecond, as
     * `{a, b}`: the two turns a drag makes (about the screen's x and y for a
     * free drag; the locked axis's angle in `a`). Over its last
     * VELOCITY_WINDOW_MS, none when it had come to rest first or was too slow
     * to mean a throw, and never past SPIN_MAX.
     */
    function releaseSpeed(samples, at) {
        if (!samples || samples.length < 2) return null;
        const last = samples[samples.length - 1];
        if (at - last.t > RELEASE_STILL_MS) return null;
        let first = last;
        for (let i = samples.length - 1; i >= 0; i -= 1) {
            if (last.t - samples[i].t > VELOCITY_WINDOW_MS) break;
            first = samples[i];
        }
        const span = last.t - first.t;
        if (span < 8) return null;
        let a = (last.a - first.a) / span;
        let b = (last.b - first.b) / span;
        const speed = Math.hypot(a, b);
        if (speed < RELEASE_MIN) return null;
        if (speed > SPIN_MAX) {
            a *= SPIN_MAX / speed;
            b *= SPIN_MAX / speed;
        }
        return { a, b };
    }

    function typing() {
        if (global.PlexoraShortcuts?.isTyping) return global.PlexoraShortcuts.isTyping();
        const active = document.activeElement;
        const tag = active?.tagName;
        return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT"
            || Boolean(active?.isContentEditable);
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
            //: program -> {uniform name -> location}
            this._uniforms = new WeakMap();
            //: `${name}|${t}|${format}|${box}` -> {texture, bytes, shape, covered,
            //: level, used}: kept across toggles, evicted by LRU past GPU_BUDGET.
            this.textures = new Map();
            this.textureBytes = 0;
            this.gpuBudget = GPU_BUDGET;
            global.PlexoraViewPerf?.provide?.("volumeTextureBytes", () => this.textureBytes);
            //: `q` takes the image's axes to the screen's (volumeCamera.js).
            this.camera = { q: Camera().VIEWS.iso.slice(), dist: 2.2, panX: 0, panY: 0 };
            //: The image axis turns are locked to ("x" | "y" | "z"), or null.
            this.lock = null;
            this.gizmo = null;
            this.composite = "mip";
            this.dirty = true;
            this.frame = 0;
            this.interacting = false;
            //: What moves on its own: a released spin -- {screen: [x, y]} radians
            //: per ms about the screen's axes, or {axis, rate} about a locked
            //: image axis -- a zoom target, a turn to a view {from, to, start, ms}.
            this.spin = null;
            this.zoomTarget = null;
            this.tween = null;
            this.lastStep = 0;
            //: Resolution while moving, as a share of full; learnt (see draw).
            this.fastScale = 1;
            this._fastFrames = { last: 0, average: 0, count: 0 };
            this._settleTimer = 0;
            this.prefs = global.PlexoraViewerPrefs || null;
            this.status = "idle";
            this.level = null;
            this.box = null;
            this.covered = null;
            this.shape = null;
            this._loads = new Map();
            //: name -> {hint, level, box, t, stamp}: the default contrast of
            //: the level each channel was last drawn from, over every plane.
            this.levelHints = new Map();
            //: The same over the clip range's planes, fetched once per
            //: `${name}|${level}|${box}|${t}|${stamp}|${z0},${z1}`.
            this._clipHints = new Map();
            this._hintTimer = 0;
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
            if (global.PlexoraOrientationGizmo) {
                this.gizmo = new global.PlexoraOrientationGizmo({
                    onAxis: (name, options) => this.lookAlong(name, options) });
                // Placed by the shared canvas layout (services/canvasLayout.js)
                // in a corner no other control holds; the wrapper otherwise.
                const placed = global.PlexoraCanvasLayout?.place?.(this.gizmo.root,
                    { corner: "top-right", id: "orientation", order: 10 });
                if (!placed) this.wrapper.appendChild(this.gizmo.root);
            }
            this._resizeObserver = typeof ResizeObserver === "function"
                ? new ResizeObserver(() => this.invalidate()) : null;
            this._resizeObserver?.observe(this.wrapper);
            return true;
        }

        destroy() {
            cancelAnimationFrame(this.frame);
            clearTimeout(this._settleTimer);
            this.spin = null;
            this.tween = null;
            this.zoomTarget = null;
            this._resizeObserver?.disconnect();
            for (const entry of this.textures.values()) this.gl?.deleteTexture(entry.texture);
            this.textures.clear();
            this.textureBytes = 0;
            this.detail = null;
            clearTimeout(this._detailTimer);
            this.canvas?.remove();
            this.gizmo?.root.remove();
            this.gizmo = null;
            this.canvas = null;
            this.gl = null;
        }

        show(visible) {
            if (this.canvas) this.canvas.hidden = !visible;
            this.gizmo?.show(visible);
            if (!visible) this.setLock(null);
            if (visible) this.invalidate();
        }

        /** Whether the user is working in the 3-D view: the pointer over it
         *  or its gizmo, or keyboard focus in either. Its letter keys are
         *  only its own then (attach). */
        engaged() {
            const within = (node) => Boolean(node) && (node.matches?.(":hover")
                || node === document.activeElement || node.contains?.(document.activeElement));
            return within(this.canvas) || within(this.gizmo?.root);
        }

        /** Lock turns to image axis `name`, or release with null. The same
         *  axis again releases it. */
        setLock(name) {
            const next = name && name !== this.lock ? name : null;
            if (next === this.lock) return;
            this.lock = next;
            // A spin about the old axis (or a free one) is not this lock's.
            this.spin = null;
            this.gizmo?.setLocked(this.lock);
            this.canvas?.setAttribute("aria-description",
                this.lock ? `Turning about the image's ${this.lock.toUpperCase()} axis` : "");
            this.invalidate();
        }

        /** Look along image axis `name` (the gizmo, the menu's views): from
         *  the far side when asked, or when already looking along it. */
        lookAlong(name, { reverse = false } = {}) {
            const C = Camera();
            const near = C.viewAlong(name);
            const already = C.quat.angleBetween(this.tween?.to.q || this.camera.q, near) < 0.02;
            this.turnTo({ q: C.viewAlong(name, { reverse: reverse || already }), panX: 0, panY: 0 });
        }

        lost() {
            this.textures.clear();
            this.textureBytes = 0;
            this.detail = null;
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
                // The window the level's bytes are quantized with, named in
                // the request (`w=`): an address only those bytes have. The
                // viewer's (services/volumeWindows.js) when it holds one.
                const packet = sidebar.quantWindow?.(slot.name, "volume");
                const held = global.__plexora?.volumeWindows?.peek?.(fullName);
                const stamp = held ? global.__plexora.volumeWindows.stamp(fullName)
                    : (packet && Number.isFinite(Number(packet.qmin))
                        ? `${formatG(packet.qmin)},${formatG(packet.qmax)}` : "");
                const levelWindow = held ? [held.qmin, held.qmax]
                    : (packet ? [Number(packet.qmin), Number(packet.qmax)] : null);
                // The slider's bytes are over the sidebar's window; a texture
                // made with another (the provisional one, say) is read in its own.
                if (!sidebar.isHdMode?.() && packet && levelWindow
                        && (levelWindow[0] !== Number(packet.qmin) || levelWindow[1] !== Number(packet.qmax))) {
                    const span = Math.max(1, Number(packet.qmax) - Number(packet.qmin));
                    const into = Math.max(1, levelWindow[1] - levelWindow[0]);
                    window = (slot.range || [0, 255]).map((value) => Math.min(1, Math.max(0,
                        (Number(packet.qmin) + (value / 255) * span - levelWindow[0]) / into)));
                }
                return {
                    name: fullName,
                    colour: hexToRgb(slot.color?.hex || slot.color),
                    window,
                    stamp,
                    opacity: this.opacityOf(fullName),
                };
            });
        }

        opacityOf(name) {
            const stored = this.viewState?.get?.()?.volume?.opacity?.[name];
            return Number.isFinite(stored) ? stored : 0.6;
        }

        /** The frame the view draws (the revision on screen). */
        frameT() {
            return (this.view || this.viewState?.get?.())?.t || 0;
        }

        /** Where a channel's texture is kept: what its bytes are (the window
         *  only for 8-bit ones; half floats are made with it too, but the
         *  window is the same unless it changed, and then so is `stamp`). */
        textureKey(name, t = this.frameT(), format = this.format(), box = this.boxKey(), stamp = "") {
            return `${name}|${t}|${format}|${box}|${stamp}`;
        }

        /** The texture `channel` draws from now, or null. */
        textureOf(channel) {
            return this.textures.get(this.textureKey(channel.name, undefined, undefined, undefined,
                                                     channel.stamp || "")) || null;
        }

        /** Fetch the level every visible channel needs and does not hold. */
        async ensureTextures(channels) {
            const t = this.frameT();
            const missing = channels.filter((channel) => !this.textureOf(channel));
            if (!missing.length) {
                if (this.status !== "ready") {
                    this.status = "ready";
                    this.paintStatus();
                }
                return;
            }
            this.status = "loading";
            this.paintStatus();
            const budget = Math.max(this.budget,
                                    Math.floor((4 * this.budget) / Math.max(1, channels.length)));
            await Promise.all(missing.map((channel) => this.loadChannel(channel.name, t, budget,
                                                                        channel.stamp)));
            this.evict(new Set(channels.map((channel) => this.textureKey(
                channel.name, undefined, undefined, undefined, channel.stamp || ""))));
            this.status = "ready";
            this.paintStatus();
        }

        /** Past the GPU budget, the least recently drawn textures go -- never
         *  one in `keep` (what the view draws now). */
        evict(keep = new Set()) {
            if (this.textureBytes <= this.gpuBudget) return;
            // Details first (a finer copy of what a whole level already
            // shows), then whatever was drawn least recently.
            const order = [...this.textures.entries()].sort((a, b) =>
                (Number(Boolean(b[1].detail)) - Number(Boolean(a[1].detail))) || (a[1].used - b[1].used));
            for (const [key, entry] of order) {
                if (this.textureBytes <= this.gpuBudget) break;
                if (keep.has(key)) continue;
                this.gl?.deleteTexture(entry.texture);
                this.textures.delete(key);
                this.textureBytes -= entry.bytes;
            }
        }

        /** The area the view draws: `{x, y, width, height}` in full-resolution
         *  pixels, or null for the whole plane. */
        boxKey() {
            const box = this.box;
            return box ? [box.x, box.y, box.width, box.height].map(Math.round).join(",") : "";
        }

        /** `f16` while HD tiles are on (16-bit data, half-float textures), else `u8`. */
        format() {
            return global.__plexora?.viewerSidebar?.isHdMode?.() ? "f16" : "u8";
        }

        async loadChannel(name, t, budget = this.budget, stamp = "") {
            const format = this.format();
            const area = this.boxKey();
            const key = this.textureKey(name, t, format, area, stamp);
            if (this._loads.has(key)) return this._loads.get(key);
            const ds = encodeURIComponent(this.datasource);
            const query = new URLSearchParams({ channel: name, t: String(t),
                                                budget: String(budget),
                                                max_side: String(this.maxTexture), format });
            if (area) query.set("box", area);
            if (stamp) query.set("w", stamp);
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
                const held = this.textures.get(key);
                if (held) {
                    gl.deleteTexture(held.texture);
                    this.textureBytes -= held.bytes;
                }
                // The area the texture covers, in full-resolution pixels: what
                // the box's proportions are measured from (extent()).
                const covered = String(response.headers.get("X-Plexora-Box") || "")
                    .split(",").map(Number);
                const entry = {
                    texture, t, format, box: area, name,
                    shape: [depth, height, width],
                    covered: covered.length === 4 && covered.every(Number.isFinite) ? covered : null,
                    level: Number(response.headers.get("X-Plexora-Level") || 0),
                    bytes: voxels.byteLength,
                    used: now(),
                };
                this.textures.set(key, entry);
                this.textureBytes += entry.bytes;
                this.covered = entry.covered;
                // The default contrast for what is drawn here (the level's own
                // maxima, in raw units): the sidebar levels auto channels by it
                // -- over the clip range's planes when that is narrower than
                // the stack (refreshHints).
                const hint = String(response.headers.get("X-Plexora-Hint") || "")
                    .split(",").map(Number);
                if (hint.length === 2 && hint.every(Number.isFinite)) {
                    this.levelHints.set(name, { hint, level: entry.level, box: area, t, stamp });
                    this.refreshHints({ now: true, only: name });
                }
                this.level = Number(response.headers.get("X-Plexora-Level") || 0);
                this.shape = [depth, height, width];
                this.invalidate();
            })().finally(() => this._loads.delete(key));
            this._loads.set(key, load);
            return load;
        }

        // -- the contrast for what is drawn ---------------------------------

        /** The clip range `[z0, z1]` (the strip's slab band), or null when it
         *  is the whole stack. */
        clipRange() {
            const state = this.view || this.viewState?.get?.();
            const depth = Math.max(1, this.viewState?.depth || 1);
            const z0 = Math.max(0, Number(state?.slab?.z0 ?? 0));
            const z1 = Math.min(depth - 1, Number(state?.slab?.z1 ?? depth - 1));
            return z0 <= 0 && z1 >= depth - 1 ? null : [z0, z1];
        }

        /**
         * Tell the sidebar the default contrast for each channel drawn: the
         * level's hint over every plane, or -- with a clip range narrower than
         * the stack -- the same numbers over the clipped planes only
         * (`/volume/<ds>/hint`, at the level drawn), so a thin range through a
         * dim part of a stack is not levelled by the brightest plane outside
         * it. Settles briefly while the range is dragged.
         */
        refreshHints({ now: soon = false, only = null } = {}) {
            clearTimeout(this._hintTimer);
            const run = () => this._sendHints(only);
            if (soon) run();
            else this._hintTimer = setTimeout(run, HINT_SETTLE_MS);
        }

        async _sendHints(only) {
            const clip = this.clipRange();
            const send = (name, hint) => global.dispatchEvent?.(new CustomEvent(
                "plexora:volume-hint", { detail: { name, hint } }));
            for (const [name, held] of this.levelHints) {
                if (only && name !== only) continue;
                if (!clip) {
                    send(name, held.hint);
                    continue;
                }
                const key = [name, held.level, held.box, held.t, held.stamp, clip.join(",")].join("|");
                let hint = this._clipHints.get(key);
                if (hint === undefined) {
                    const query = new URLSearchParams({ channel: name, t: String(held.t),
                                                        level: String(held.level),
                                                        z: clip.join(",") });
                    if (held.box) query.set("box", held.box);
                    if (held.stamp) query.set("w", held.stamp);
                    const ds = encodeURIComponent(this.datasource);
                    const address = typeof global.plexoraUrl === "function"
                        ? global.plexoraUrl(`volume/${ds}/hint?${query}`)
                        : `/volume/${ds}/hint?${query}`;
                    try {
                        const response = await this.fetch(address);
                        const body = response.ok ? await response.json() : null;
                        hint = Array.isArray(body?.hint) && body.hint.every(Number.isFinite)
                            ? body.hint : null;
                    } catch (error) {
                        hint = null;
                    }
                    this._clipHints.set(key, hint);
                    if (this._clipHints.size > 256) {
                        this._clipHints.delete(this._clipHints.keys().next().value);
                    }
                }
                // The range moved on while this was asked: the next call sends.
                const still = this.clipRange();
                if (String(still) !== String(clip)) return;
                send(name, hint || held.hint);
            }
        }

        // -- drawing -------------------------------------------------------

        invalidate() {
            this.dirty = true;
            this.schedule();
        }

        schedule() {
            if (this.frame) return;
            this.frame = requestAnimationFrame((time) => {
                this.frame = 0;
                this.onFrame(time);
            });
        }

        /** Whether planes may coast and ease (services/viewerPrefs.js). */
        smooth() {
            return this.prefs ? this.prefs.smoothMotion() : true;
        }

        /** True while something still moves on its own. */
        animating() {
            return Boolean(this.spin || this.tween || this.zoomTarget !== null);
        }

        /** True while the picture may be drawn at the lower quality. */
        moving() {
            return this.interacting || this.animating();
        }

        /** One display frame: move what is moving, draw, and ask for the next
         *  frame while anything still moves. */
        onFrame(time) {
            if (this.animating()) {
                const dt = this.lastStep ? Math.min(64, Math.max(0, time - this.lastStep)) : 16;
                this.lastStep = time;
                this.advance(dt, time);
                this.dirty = true;
            } else {
                this.lastStep = 0;
            }
            if (this.dirty) {
                this.learnFrame(time, this.animating());
                this.draw(time);
                if (!this.moving()) this.scheduleDetail();
            }
            if (this.animating()) {
                this.schedule();
            } else if (!this.interacting && this.drawnFast) {
                // At rest: the full picture, once the hand has had a moment
                // to start again (a frame it would not see).
                clearTimeout(this._settleTimer);
                this._settleTimer = setTimeout(() => this.invalidate(), SETTLE_MS);
            }
        }

        /** Move the camera on by `dt` milliseconds of whatever is moving. */
        advance(dt, time) {
            const camera = this.camera;
            if (this.spin) {
                // Exponential decay, integrated exactly over dt: the same
                // coast whatever the frame rate.
                const decay = Math.exp(-dt / SPIN_TAU_MS);
                const travel = SPIN_TAU_MS * (1 - decay);
                const C = Camera();
                const spin = this.spin;
                if (spin.axis) {
                    camera.q = C.turnAbout(camera.q, spin.axis, spin.rate * travel);
                    spin.rate *= decay;
                    if (Math.abs(spin.rate) < SPIN_STOP) this.spin = null;
                } else {
                    const [wx, wy] = spin.screen;
                    const rate = Math.hypot(wx, wy);
                    camera.q = C.quat.normalize(C.quat.multiply(
                        C.quat.axisAngle([wx, wy, 0], rate * travel), camera.q));
                    spin.screen = [wx * decay, wy * decay];
                    if (rate * decay < SPIN_STOP) this.spin = null;
                }
            }
            if (this.zoomTarget !== null) {
                const k = 1 - Math.exp(-dt / ZOOM_TAU_MS);
                camera.dist += (this.zoomTarget - camera.dist) * k;
                if (Math.abs(this.zoomTarget - camera.dist) < this.zoomTarget * 0.002) {
                    camera.dist = this.zoomTarget;
                    this.zoomTarget = null;
                }
            }
            if (this.tween) {
                const { from, to, start, ms } = this.tween;
                const f = Math.min(1, Math.max(0, (time - start) / ms));
                const eased = f < 0.5 ? 4 * f ** 3 : 1 - (-2 * f + 2) ** 3 / 2;
                for (const key of Object.keys(to)) {
                    camera[key] = key === "q"
                        ? Camera().quat.slerp(from.q, to.q, eased)
                        : from[key] + (to[key] - from[key]) * eased;
                }
                if (f >= 1) this.tween = null;
            }
        }

        /** Stop everything moving on its own (a press, a new view). */
        stopMotion() {
            this.spin = null;
            this.tween = null;
        }

        /** Turn (and pan) to `target` -- `{q, panX, panY, dist}`, any of
         *  them -- the short way round. */
        turnTo(target) {
            this.spin = null;
            const to = { ...target };
            if (to.q) to.q = to.q.slice();
            if (!this.smooth()) {
                this.tween = null;
                Object.assign(this.camera, to);
                this.invalidate();
                return;
            }
            const from = {};
            for (const key of Object.keys(to)) {
                from[key] = key === "q" ? this.camera.q.slice() : this.camera[key];
            }
            const start = now();
            this.tween = { from, to, start, ms: TURN_MS };
            if (!this.lastStep) this.lastStep = start;
            this.invalidate();
        }

        /** Zoom by `factor`, eased unless motion is off. */
        zoomBy(factor) {
            const base = this.zoomTarget ?? this.camera.dist;
            // Close enough to look inside the stack: a detail (considerDetail)
            // brings the finer levels in as the part seen gets smaller.
            const target = Math.max(MIN_DIST, Math.min(8, base * factor));
            if (!this.smooth()) {
                this.zoomTarget = null;
                this.camera.dist = target;
            } else {
                if (this.zoomTarget === null && !this.lastStep) this.lastStep = now();
                this.zoomTarget = target;
            }
            this.invalidate();
        }

        /**
         * Learn how fast moving frames come: when they average slower than
         * SLOW_FRAME_MS the resolution steps down. Only frames the view asks
         * for itself (a coast, a turn, an eased zoom) count: under a drag the
         * frames come as fast as the pointer moves, and a pointer's pause is
         * not a slow frame. What is learnt holds for the next drag.
         */
        learnFrame(time, fast) {
            const frames = this._fastFrames;
            if (!fast) {
                frames.last = 0;
                frames.count = 0;
                return;
            }
            const interval = frames.last ? time - frames.last : 0;
            frames.last = time;
            if (!(interval > 0 && interval < 100)) return;
            frames.average = frames.count ? frames.average * 0.7 + interval * 0.3 : interval;
            frames.count += 1;
            if (frames.count >= 6 && frames.average > SLOW_FRAME_MS) {
                this.fastScale = Math.max(MIN_FAST_SCALE, this.fastScale * 0.8);
                frames.count = 0;
            }
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
            // An area: its own size, not the plane's.
            const across = this.covered
                ? [this.covered[2] - this.covered[0], this.covered[3] - this.covered[1]]
                : [width * scale, height * scale];
            const physical = [across[0] * sx, across[1] * sx, planes * sz];
            const longest = Math.max(...physical) || 1;
            return physical.map((side) => side / longest);
        }

        matrices() {
            const canvas = this.canvas;
            const aspect = canvas.width / Math.max(1, canvas.height);
            const { q, dist, panX, panY } = this.camera;
            // The camera sits `dist` back along the screen's z; the stack is
            // turned by q (image axes -> screen axes) and shifted by the pan.
            const shift = new Float32Array([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0,
                                            -panX, -panY, -dist, 1]);
            const view = mat4Multiply(shift, Camera().quat.toMat4(q));
            const projection = mat4Perspective(FOV_Y, aspect, 0.01, 20);
            return mat4Invert(mat4Multiply(projection, view));
        }

        draw(time = now()) {
            this.dirty = false;
            const gl = this.gl;
            const canvas = this.canvas;
            if (!gl || !canvas || canvas.hidden) return;
            const fast = this.moving();
            this.drawnFast = fast;
            const ratio = Math.min(global.devicePixelRatio || 1, 1.5) * (fast ? this.fastScale : 1);
            const width = Math.max(1, Math.round(canvas.clientWidth * ratio));
            const height = Math.max(1, Math.round(canvas.clientHeight * ratio));
            if (canvas.width !== width || canvas.height !== height) {
                canvas.width = width;
                canvas.height = height;
            }
            gl.viewport(0, 0, width, height);
            const at = now();
            const channels = this.channels().map((channel) => ({ ...channel, entry: this.textureOf(channel) }))
                .filter((channel) => channel.entry);
            gl.clearColor(0, 0, 0, 1);
            gl.clear(gl.COLOR_BUFFER_BIT);
            if (!channels.length) return;
            // What is drawn sets the box's proportions and the step count:
            // channels may hold different levels (see ensureTextures).
            for (const channel of channels) channel.entry.used = at;
            const lead = channels[0].entry;
            this.covered = lead.covered;
            this.shape = channels.reduce((most, channel) => {
                const shape = channel.entry.shape;
                return shape[0] * shape[1] * shape[2] > most[0] * most[1] * most[2] ? shape : most;
            }, lead.shape);
            this.level = lead.level;
            // The detail where the camera looks, once every channel has it.
            const detail = this.detail;
            const details = detail
                ? channels.map((channel) => this.textures.get(this.detailKey(channel, detail)) || null)
                : [];
            const withDetail = Boolean(detail) && details.length > 0 && details.every(Boolean);
            const programKey = `${channels.length}|${withDetail ? 1 : 0}`;
            let built = this.programs.get(programKey);
            if (!built) {
                built = program(gl, channels.length, withDetail);
                this.programs.set(programKey, built);
            }
            gl.useProgram(built);
            // Looked up once per program: a lookup is a round trip into the
            // driver, and a coasting view draws every frame.
            let locations = this._uniforms.get(built);
            if (!locations) {
                locations = new Map();
                this._uniforms.set(built, locations);
            }
            const uniform = (name) => {
                if (!locations.has(name)) locations.set(name, gl.getUniformLocation(built, name));
                return locations.get(name);
            };
            channels.forEach((channel, index) => {
                gl.activeTexture(gl.TEXTURE0 + index);
                gl.bindTexture(gl.TEXTURE_3D, channel.entry.texture);
                gl.uniform1i(uniform(`u_volume${index}`), index);
            });
            if (withDetail) {
                details.forEach((entry, index) => {
                    entry.used = at;
                    gl.activeTexture(gl.TEXTURE0 + channels.length + index);
                    gl.bindTexture(gl.TEXTURE_3D, entry.texture);
                    gl.uniform1i(uniform(`u_detail${index}`), channels.length + index);
                });
                gl.uniform3fv(uniform("u_detail_lo"), detail.lo);
                gl.uniform3fv(uniform("u_detail_hi"), detail.hi);
            }
            gl.uniform3fv(uniform("u_color"), channels.flatMap((channel) => channel.colour));
            gl.uniform2fv(uniform("u_window"), channels.flatMap((channel) => channel.window));
            gl.uniform1fv(uniform("u_opacity"), channels.map((channel) => channel.opacity));
            gl.uniformMatrix4fv(uniform("u_inverse"), false, this.matrices());
            gl.uniform3fv(uniform("u_extent"), this.extent());
            // The revision on screen (attach's surface), not the one asked for.
            const state = this.view || this.viewState?.get?.();
            const depth = Math.max(1, this.viewState?.depth || 1);
            const clip = state ? [state.slab.z0 / depth, (state.slab.z1 + 1) / depth] : [0, 1];
            gl.uniform2fv(uniform("u_clip_z"), clip);
            gl.uniform1i(uniform("u_mode"), this.composite === "additive" ? 1 : 0);
            // As many samples as the finest voxels drawn: the detail's, spread
            // over the whole box, when there is one.
            let longest = this.shape ? Math.max(...this.shape) : 256;
            if (withDetail) {
                const shape = details[0].shape;   // (z, y, x)
                const span = [detail.hi[2] - detail.lo[2], detail.hi[1] - detail.lo[1],
                              detail.hi[0] - detail.lo[0]];
                longest = Math.max(longest, ...shape.map((side, i) => side / Math.max(1e-3, span[i])));
            }
            // Along the diagonal, so about one sample per texel along any axis.
            const steps = Math.min(1024, Math.max(96, Math.round(longest * 2)));
            gl.uniform1i(uniform("u_steps"), fast ? Math.round(steps / 2) : steps);
            gl.uniform3fv(uniform("u_ground"), [0, 0, 0]);
            gl.bindBuffer(gl.ARRAY_BUFFER, this.quad);
            gl.enableVertexAttribArray(0);
            gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
            gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
            this.paintGizmo(fast);
        }

        paintGizmo(fast = this.moving()) {
            if (!this.gizmo) return;
            const q = this.camera.q;
            this.gizmo.update((name) => Camera().axisOnScreen(q, name));
            this.gizmo.setActive(fast);
        }

        /** Where the stack's centre is on the canvas, in CSS pixels. */
        centreOnScreen() {
            const canvas = this.canvas;
            const width = canvas?.clientWidth || 1;
            const height = canvas?.clientHeight || 1;
            const f = 1 / Math.tan(FOV_Y / 2);
            const { dist, panX, panY } = this.camera;
            return { x: width / 2 + ((-panX * f) / (dist * (width / height))) * (width / 2),
                     y: height / 2 - ((-panY * f) / dist) * (height / 2) };
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

        /** A named view (volumeCamera.VIEWS): xy, xz, yz or iso. */
        preset(name) {
            const views = Camera().VIEWS;
            this.turnTo({ q: views[name] || views.iso, panX: 0, panY: 0 });
        }

        bindPointer(canvas) {
            let drag = null;
            //: The hand's turn so far and when, for the release's speed:
            //: {t, a, b} -- about the screen's x and y for a free drag, the
            //: locked axis's angle in `a` for a locked one.
            let samples = [];
            canvas.addEventListener("pointerdown", (event) => {
                canvas.setPointerCapture?.(event.pointerId);
                // A press stops a spin or a turn where it is.
                this.stopMotion();
                clearTimeout(this._settleTimer);
                const pan = event.shiftKey || event.button === 1;
                const box = canvas.getBoundingClientRect();
                drag = { x: event.clientX, y: event.clientY, a: 0, b: 0, pan, box,
                         // A locked drag's rule is fixed as it starts (volumeCamera).
                         locked: !pan && this.lock
                             ? Camera().lockedDrag(this.camera.q, this.lock) : null,
                         centre: this.centreOnScreen() };
                samples = [{ t: now(), a: 0, b: 0 }];
                this.interacting = true;
                // Probe back up a little each time, in case the last slow
                // frames were the machine being busy rather than this view.
                this.fastScale = Math.min(1, this.fastScale * 1.1);
            });
            canvas.addEventListener("pointermove", (event) => {
                if (!drag) return;
                const dx = event.clientX - drag.x;
                const dy = event.clientY - drag.y;
                drag.x = event.clientX;
                drag.y = event.clientY;
                if (!dx && !dy) return;
                const C = Camera();
                if (drag.pan) {
                    const scale = this.camera.dist / Math.max(1, canvas.clientHeight);
                    this.camera.panX -= dx * scale;
                    this.camera.panY += dy * scale;
                } else if (drag.locked) {
                    const pointer = { x: event.clientX - drag.box.left, y: event.clientY - drag.box.top };
                    const angle = drag.locked.angle(dx, dy, pointer, drag.centre);
                    this.camera.q = C.turnAbout(this.camera.q, this.lock, angle);
                    drag.a += angle;
                } else {
                    this.camera.q = C.turnFree(this.camera.q, dx, dy);
                    drag.a += dy * C.ROTATE_PER_PX;
                    drag.b += dx * C.ROTATE_PER_PX;
                }
                if (!drag.pan) {
                    const at = now();
                    samples.push({ t: at, a: drag.a, b: drag.b });
                    while (samples.length > 2 && at - samples[0].t > VELOCITY_WINDOW_MS * 2) {
                        samples.shift();
                    }
                }
                this.invalidate();
            });
            const end = (event) => {
                if (!drag) return;
                const turning = !drag.pan && event?.type === "pointerup";
                const locked = drag.locked ? this.lock : null;
                drag = null;
                this.interacting = false;
                const speed = turning && this.smooth() ? releaseSpeed(samples, now()) : null;
                samples = [];
                if (speed) {
                    // A locked turn coasts about the same axis; a free one
                    // about the screen axes it was turning on.
                    this.spin = locked ? { axis: locked, rate: speed.a }
                        : { screen: [speed.a, speed.b] };
                    this.lastStep = now();
                }
                this.invalidate();
            };
            canvas.addEventListener("pointerup", end);
            canvas.addEventListener("pointercancel", end);
            canvas.addEventListener("lostpointercapture", end);
            canvas.addEventListener("wheel", (event) => {
                if (event.shiftKey) return;
                event.preventDefault();
                const unit = event.deltaMode === 1 ? 16 : (event.deltaMode === 2 ? 400 : 1);
                this.zoomBy(Math.exp(event.deltaY * unit * 0.0012));
            }, { passive: false });
            canvas.addEventListener("dblclick", () => this.preset("iso"));
            // The keyboard, once the view has focus: arrows turn (about the
            // locked axis when there is one), + and - zoom, 0 goes back to the
            // three-quarter view.
            canvas.addEventListener("keydown", (event) => {
                if (event.metaKey || event.ctrlKey || event.altKey) return;
                const step = event.shiftKey ? KEY_TURN / 3 : KEY_TURN;
                const turns = { ArrowLeft: [0, -1], ArrowRight: [0, 1],
                                ArrowUp: [-1, 0], ArrowDown: [1, 0] };
                if (event.key in turns) {
                    event.preventDefault();
                    const C = Camera();
                    const base = this.tween ? this.tween.to.q || this.camera.q : this.camera.q;
                    const [ax, ay] = turns[event.key];
                    const q = this.lock
                        ? C.turnAbout(base, this.lock, (ax + ay) * step)
                        : C.quat.normalize(C.quat.multiply(C.quat.axisAngle([ax, ay, 0], step), base));
                    this.turnTo({ q });
                } else if (event.key === "+" || event.key === "=") {
                    event.preventDefault();
                    this.zoomBy(1 / 1.25);
                } else if (event.key === "-" || event.key === "_") {
                    event.preventDefault();
                    this.zoomBy(1.25);
                } else if (event.key === "0") {
                    event.preventDefault();
                    this.preset("iso");
                }
            });
        }

        // -- detail where the camera looks ----------------------------------------

        /**
         * The part of the stack the camera sees, in texture space ([0,1]^3 of
         * the whole-level texture), within the clip range: a lattice of points
         * through the clipped box projected into the view, the ones on screen
         * and in front of the camera kept, their box grown by one lattice step.
         * Null when it sees nothing.
         */
        visibleRegion() {
            const inverse = this.matrices();
            const forward = inverse ? mat4Invert(inverse) : null;
            if (!forward) return null;
            const extent = this.extent();
            const state = this.view || this.viewState?.get?.();
            const depth = Math.max(1, this.viewState?.depth || 1);
            const clip = state ? [state.slab.z0 / depth, (state.slab.z1 + 1) / depth] : [0, 1];
            const n = REGION_LATTICE;
            const min = [Infinity, Infinity, Infinity];
            const max = [-Infinity, -Infinity, -Infinity];
            const m = forward;
            for (let i = 0; i <= n; i += 1) {
                const px = i / n;
                for (let j = 0; j <= n; j += 1) {
                    const py = j / n;
                    for (let k = 0; k <= n; k += 1) {
                        const pz = clip[0] + (k / n) * (clip[1] - clip[0]);
                        const x = (px - 0.5) * extent[0];
                        const y = (py - 0.5) * extent[1];
                        const z = (pz - 0.5) * extent[2];
                        const w = m[3] * x + m[7] * y + m[11] * z + m[15];
                        if (!(w > 0)) continue;
                        const sx = (m[0] * x + m[4] * y + m[8] * z + m[12]) / w;
                        const sy = (m[1] * x + m[5] * y + m[9] * z + m[13]) / w;
                        if (Math.abs(sx) > 1 || Math.abs(sy) > 1) continue;
                        const p = [px, py, pz];
                        for (let a = 0; a < 3; a += 1) {
                            if (p[a] < min[a]) min[a] = p[a];
                            if (p[a] > max[a]) max[a] = p[a];
                        }
                    }
                }
            }
            if (!Number.isFinite(min[0])) return null;
            const step = [1 / n, 1 / n, (clip[1] - clip[0]) / n];
            return {
                lo: min.map((v, a) => Math.max(a === 2 ? clip[0] : 0, v - step[a])),
                hi: max.map((v, a) => Math.min(a === 2 ? clip[1] : 1, v + step[a])),
            };
        }

        /** Look again for a finer view of what the camera sees, once it rests. */
        scheduleDetail() {
            clearTimeout(this._detailTimer);
            if (this.status !== "ready" || !this.gl) return;
            this._detailTimer = setTimeout(() => this.considerDetail(), DETAIL_REST_MS);
        }

        /** The key a channel's detail texture is kept under. */
        detailKey(channel, detail) {
            const r = detail.region;
            return `${this.textureKey(channel.name, detail.t, detail.format, this.boxKey(),
                                      channel.stamp || "")}|D${r.x0},${r.y0},${r.x1},${r.y1},${r.z0},${r.z1}`;
        }

        /**
         * When the camera sees a small part of the stack, fetch that part at
         * a finer level -- the whole level stays as what is drawn around it,
         * and while the detail loads. Only when the camera rests, only when
         * the part is under DETAIL_MAX_SHARE of the stack, and only when the
         * server has a finer level for it within the budget.
         */
        async considerDetail() {
            if (this.moving() || this.status !== "ready" || !this.gl) return;
            const region = this.visibleRegion();
            const share = region ? region.hi.reduce((product, v, i) => product * (v - region.lo[i]), 1) : 1;
            if (!region || share > DETAIL_MAX_SHARE) {
                if (this.detail) {
                    this.detail = null;
                    this.invalidate();
                }
                return;
            }
            const channels = this.channels();
            const lead = channels.length ? this.textureOf(channels[0]) : null;
            if (!lead) return;
            // Texture space -> full-resolution pixels and planes.
            const info = this.viewState?.info || {};
            const covered = lead.covered || [0, 0, Number(info.width) || 1, Number(info.height) || 1];
            const depth = Math.max(1, this.viewState?.depth || 1);
            const spanX = covered[2] - covered[0];
            const spanY = covered[3] - covered[1];
            const pixels = {
                x0: Math.floor(covered[0] + region.lo[0] * spanX),
                x1: Math.ceil(covered[0] + region.hi[0] * spanX),
                y0: Math.floor(covered[1] + region.lo[1] * spanY),
                y1: Math.ceil(covered[1] + region.hi[1] * spanY),
                z0: Math.max(0, Math.floor(region.lo[2] * depth)),
                z1: Math.min(depth - 1, Math.ceil(region.hi[2] * depth) - 1),
            };
            if (pixels.x1 - pixels.x0 < 2 || pixels.y1 - pixels.y0 < 2 || pixels.z1 < pixels.z0) return;
            const t = this.frameT();
            const format = this.format();
            // Asked already, and nothing finer fitted: not again for this part.
            const tried = this._noFiner;
            if (tried && tried.t === t && tried.format === format
                    && overlap(tried.wanted, pixels) > DETAIL_KEEP_OVERLAP) {
                return;
            }
            const held = this.detail;
            if (held && held.t === t && held.format === format
                    && overlap(held.wanted, pixels) > DETAIL_KEEP_OVERLAP
                    && channels.every((channel) => this.textures.has(this.detailKey(channel, held)))) {
                return;
            }
            const request = { region: pixels, t, format, wanted: pixels };
            this._detailRequest = request;
            let level = null;
            let arrived = null;
            try {
                const answers = await Promise.all(channels.map((channel) => this.loadDetail(channel, request)));
                if (this._detailRequest !== request) return;
                if (answers.some((answer) => !answer)) return;
                level = answers[0].level;
                arrived = answers[0];
            } catch (error) {
                console.error("volumeRenderer: the detail could not be read", error);
                return;
            }
            // No finer than what is drawn already: nothing to add.
            if (!(level < lead.level)) {
                for (const channel of channels) this.dropTexture(this.detailKey(channel, request));
                this._noFiner = { t, format, wanted: pixels };
                if (this.detail) {
                    this.detail = null;
                    this.invalidate();
                }
                return;
            }
            this._noFiner = null;
            // Where the detail sits in the whole level's texture space, by
            // what the server actually covered (aligned to its level's grid).
            const box = arrived.covered || [pixels.x0, pixels.y0, pixels.x1, pixels.y1];
            const z = arrived.z || [pixels.z0, pixels.z1];
            request.lo = [(box[0] - covered[0]) / spanX, (box[1] - covered[1]) / spanY, z[0] / depth];
            request.hi = [(box[2] - covered[0]) / spanX, (box[3] - covered[1]) / spanY, (z[1] + 1) / depth];
            const previous = this.detail;
            this.detail = request;
            if (previous && previous !== request) {
                for (const channel of channels) this.dropTexture(this.detailKey(channel, previous));
            }
            global.PlexoraViewPerf?.mark?.("volume", "detail", null, level);
            this.invalidate();
        }

        /** One channel's detail texture for `request`: `{level, covered, z}`. */
        async loadDetail(channel, request) {
            const key = this.detailKey(channel, request);
            const held = this.textures.get(key);
            if (held) return { level: held.level, covered: held.covered, z: held.z };
            const r = request.region;
            const ds = encodeURIComponent(this.datasource);
            const count = Math.max(1, this.channels().length);
            const budget = Math.max(this.budget, Math.floor((4 * this.budget) / count));
            const query = new URLSearchParams({
                channel: channel.name, t: String(request.t), budget: String(budget),
                max_side: String(this.maxTexture), format: request.format,
                box: [r.x0, r.y0, r.x1 - r.x0, r.y1 - r.y0].join(","), z: `${r.z0},${r.z1}` });
            if (channel.stamp) query.set("w", channel.stamp);
            const address = typeof global.plexoraUrl === "function"
                ? global.plexoraUrl(`volume/${ds}/level?${query}`) : `/volume/${ds}/level?${query}`;
            const response = await this.fetch(address);
            if (!response.ok) throw new Error(`volume detail: HTTP ${response.status}`);
            const shape = String(response.headers.get("X-Plexora-Shape") || "").split(",").map(Number);
            const half = (response.headers.get("X-Plexora-Format") || "u8") === "f16";
            const buffer = await response.arrayBuffer();
            const voxels = half ? new Uint16Array(buffer) : new Uint8Array(buffer);
            const [depth, height, width] = shape;
            if (!(depth * height * width === voxels.length) || !this.gl) return null;
            const gl = this.gl;
            const texture = gl.createTexture();
            gl.bindTexture(gl.TEXTURE_3D, texture);
            gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
            gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
            gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
            gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
            gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_R, gl.CLAMP_TO_EDGE);
            // Uploaded a slab of planes per frame (UPLOAD_PER_FRAME): a detail
            // is a hundred megabytes, and one upload of it is a frame that
            // stops the view turning. It is drawn only once every plane is in.
            gl.texStorage3D(gl.TEXTURE_3D, 1, half ? gl.R16F : gl.R8, width, height, depth);
            const planeBytes = width * height * (half ? 2 : 1);
            const slab = Math.max(1, Math.floor(UPLOAD_PER_FRAME / Math.max(1, planeBytes)));
            for (let z = 0; z < depth; z += slab) {
                if (z) await new Promise((resolve) => global.requestAnimationFrame(resolve));
                if (!this.gl || this._detailRequest !== request) {
                    this.gl?.deleteTexture(texture);
                    return null;
                }
                const count = Math.min(slab, depth - z);
                gl.bindTexture(gl.TEXTURE_3D, texture);
                gl.texSubImage3D(gl.TEXTURE_3D, 0, 0, 0, z, width, height, count, gl.RED,
                                 half ? gl.HALF_FLOAT : gl.UNSIGNED_BYTE,
                                 voxels.subarray(z * width * height, (z + count) * width * height));
            }
            const covered = String(response.headers.get("X-Plexora-Box") || "").split(",").map(Number);
            const z = String(response.headers.get("X-Plexora-Z") || "").split(",").map(Number);
            const entry = {
                texture, name: channel.name, t: request.t, format: request.format, box: "detail",
                shape: [depth, height, width], detail: true,
                covered: covered.length === 4 && covered.every(Number.isFinite) ? covered : null,
                z: z.length === 2 && z.every(Number.isFinite) ? z : null,
                level: Number(response.headers.get("X-Plexora-Level") || 0),
                bytes: voxels.byteLength, used: now(),
            };
            this.textures.set(key, entry);
            this.textureBytes += entry.bytes;
            this.evict(new Set([key, ...this.channels().map((ch) => this.textureKey(
                ch.name, undefined, undefined, undefined, ch.stamp || ""))]));
            return { level: entry.level, covered: entry.covered, z: entry.z };
        }

        dropTexture(key) {
            const entry = this.textures.get(key);
            if (!entry) return;
            this.gl?.deleteTexture(entry.texture);
            this.textures.delete(key);
            this.textureBytes -= entry.bytes;
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
        let wasOn = false;
        let lastClip = "";
        /** The area on screen, when the plane is zoomed in on part of the image:
         *  the 3-D view draws that, finer, rather than the whole stack coarsely. */
        const areaOnScreen = () => {
            const viewer = global.__plexora?.seaDragonViewer?.viewer;
            const item = viewer?.world?.getItemAt?.(0);
            const info = viewState.info || {};
            if (!item || !info.width || !info.height) return null;
            const rect = item.viewportToImageRectangle(viewer.viewport.getBounds(true));
            const x0 = Math.max(0, rect.x);
            const y0 = Math.max(0, rect.y);
            const x1 = Math.min(info.width, rect.x + rect.width);
            const y1 = Math.min(info.height, rect.y + rect.height);
            if (x1 - x0 < 1 || y1 - y0 < 1) return null;
            if (x1 - x0 > 0.7 * info.width && y1 - y0 > 0.7 * info.height) return null;
            return { x: x0, y: y0, width: x1 - x0, height: y1 - y0 };
        };
        const controller = {
            get renderer() { return renderer; },
            sync(state) {
                if (renderer) renderer.view = state;
                const on = viewState.isVolume(state);
                const entering = on && !wasOn;
                wasOn = on;
                if (!on) {
                    renderer?.show(false);
                    document.getElementById("openseadragon_wrapper")
                        ?.classList.remove("is-volume-mode");
                    return;
                }
                if (!renderer) {
                    renderer = new VolumeRenderer({ viewState, datasource });
                    renderer.view = state;
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
                if (entering) renderer.box = areaOnScreen();
                // Only what is missing is fetched (ensureTextures): entering
                // again over the same area, with the same channels, is a draw.
                if (entering || lastT !== state.t) {
                    lastT = state.t;
                    renderer.refresh();
                } else {
                    renderer.invalidate();
                }
                // A new clip range: contrast for the planes it keeps.
                const clip = `${state.slab?.z0},${state.slab?.z1}`;
                if (clip !== lastClip) {
                    lastClip = clip;
                    if (!entering) renderer.refreshHints();
                }
            },
            channelsChanged() {
                if (renderer && viewState.isVolume()) renderer.refresh();
            },
        };
        // X, Y, Z lock turns to that image axis; the same key again, or Esc,
        // lets go. Bare letters other tools bind too (Thresholding's Z / X for
        // the previous / next marker, QC's for its flicker), so they are the
        // 3-D view's only while the user is ENGAGED with it -- the pointer
        // over it or its gizmo, or either focused -- and nobody is typing or in
        // a dialog. Then they are taken in the capture phase, so nothing
        // underneath also acts on them; anywhere else they pass straight on.
        global.addEventListener?.("keydown", (event) => {
            if (!renderer || !viewState.isVolume() || !renderer.canvas || renderer.canvas.hidden) return;
            if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey
                || event.shiftKey) return;
            if (typing() || document.querySelector("dialog[open]")
                || global.PlexoraConfirm?.modalOpen?.()) return;
            if (event.key === "Escape") {
                if (renderer.lock) renderer.setLock(null);
                return;
            }
            const key = String(event.key || "").toLowerCase();
            if ((key === "x" || key === "y" || key === "z") && renderer.engaged()) {
                event.preventDefault();
                event.stopPropagation();
                if (!event.repeat) renderer.setLock(key);
            }
        }, true);
        // HD swaps the textures for half floats (or back): the same refresh.
        global.addEventListener?.("plexora:hd-mode-changed", () => controller.channelsChanged());
        // A channel's exact window replacing its provisional one: its levels
        // are fetched again, made with it (services/volumeWindows.js).
        global.addEventListener?.("plexora:volume-window", () => controller.channelsChanged());
        if (typeof viewState.registerSurface === "function") {
            // As a navigation revision (services/viewState.js): the clip, the
            // composite and the mode change in the frame the plane, the panes
            // and the outlines do.
            viewState.registerSurface("volume", { order: 50,
                                                  prepare: (rev) => () => controller.sync(rev.state) });
        } else {
            viewState.subscribe((state) => controller.sync(state));
        }
        // `class ChannelList` in a classic script is a global LEXICAL binding,
        // never a property of window -- so it is reached by name.
        const lists = typeof ChannelList !== "undefined" ? ChannelList : global.ChannelList;
        if (eventHandler?.bind && lists?.events) {
            eventHandler.bind(lists.events.CHANNELS_CHANGE, () => controller.channelsChanged());
            eventHandler.bind(lists.events.COLOR_TRANSFER_CHANGE, () => renderer?.invalidate());
            // A contrast drag: windows are uniforms, read at draw -- a redraw
            // is all it takes, in the frame the drag moved.
            eventHandler.bind(lists.events.BRUSH_MOVE, () => {
                if (viewState.isVolume()) renderer?.invalidate();
            });
        }
        global.addEventListener?.("plexora:channels-applied", () => controller.channelsChanged());
        controller.sync(viewState.get());
        return controller;
    }

    VolumeRenderer.attach = attach;
    VolumeRenderer.releaseSpeed = releaseSpeed;
    VolumeRenderer.MOTION = { SPIN_TAU_MS, RELEASE_MIN, SPIN_STOP, SPIN_MAX, ZOOM_TAU_MS,
                              TURN_MS, VELOCITY_WINDOW_MS, RELEASE_STILL_MS };
    VolumeRenderer.fragmentSource = fragmentSource;
    VolumeRenderer.mat4 = { multiply: mat4Multiply, invert: mat4Invert,
                            perspective: mat4Perspective, lookAt: mat4LookAt };
    global.PlexoraVolumeRenderer = VolumeRenderer;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = VolumeRenderer;
    }
})(typeof window !== "undefined" ? window : globalThis);
