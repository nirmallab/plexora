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
 * hold textures is fetched at the level they are drawn at when that level, for
 * every visible channel, still fits the shared budget -- one picture at one
 * resolution; otherwise at the budget its share allows, the others keeping
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
 * CUTTING IT. The view state's `inspect` (services/inspectGeometry.js) cuts
 * the stack: a cutaway, sections, slabs or a crop, over any of X, Y and Z.
 * Every one of them is uniforms -- the box a ray is clipped to, the corner a
 * cutaway takes out, the planes or slabs and how a slab's voxels combine --
 * read at every frame from services/inspectState.js, so a cut follows the
 * hand with no texture, program or revision behind it. A section is sampled
 * where each ray crosses its plane, never between two steps of the march.
 *
 * Draws on demand -- a frame only after the camera, the channels or the clip
 * changed. A channel new to the picture (switched on, the view just entered)
 * comes in over a quarter of a second rather than landing at once: its colour
 * and opacity are scaled up from nothing, at full quality, and at once with
 * motion off. `webglcontextlost` hands the viewer back to Plane mode with a
 * toast.
 *
 * NEVER A BLANK OR WRONGLY BRIGHT FRAME WHILE DATA LOADS. A channel whose
 * texture is being replaced -- its window turning exact, a frame step, HD on
 * or off -- keeps drawing the texture it was drawn from (same area only), read
 * in that texture's own window, until the new one lands (entryFor). What the
 * picture is DRAWN with changing under a still camera -- the full frame after
 * a turn stops, a texture, a finer detail or a batch of bricks landing, a
 * channel switched off -- dissolves rather than cuts: the frame on screen is
 * held on an opaque ghost over the canvas (planeMotion.js VolumeGhost) and
 * faded out over the new one. A moving camera ends any ghost at once, a
 * capture never has one, and with motion off there is none. MIP is drawn
 * with the same number of samples moving as at rest -- fewer would miss thin
 * peaks and the stack would dim while it turns -- and the sampling jitter
 * follows the screen, not the render target, so it holds still when the
 * resolution drops.
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
 * press stops it dead. Play in 3-D (the depth strip) turns the stack on its
 * own (setAutoRotate): smoothly, paced by the frame time, about the screen's
 * vertical or an image axis, from the camera as it is -- until a press, a
 * preset, a lock, Esc or Play again stops it. The wheel's zoom eases to where it is going, and the
 * preset views turn there rather than cut. While anything moves, the picture
 * is drawn at a lower resolution if frames come slower than the display,
 * learnt as it goes (and, additive only, with half the samples); the moment it
 * rests it is drawn again in full. With motion off (the system's reduced-motion
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
    //: A channel drawn that was not drawn the frame before comes in over
    //: this long, eased out, rather than all at once.
    const FADE_IN_MS = 240;
    //: Play in 3-D turns the stack this many degrees a second: the default
    //: and the bounds (services/viewerPrefs.js `rotate`).
    const ROTATE_DEFAULT_DPS = 30;
    const ROTATE_MIN_DPS = 5;
    const ROTATE_MAX_DPS = 90;
    const ROTATE_AXES = ["screen", "x", "y", "z"];

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
    function fragmentSource(count, detail = false, atlas = false) {
        const samplers = [];
        const sample = [];
        const chunk = atlas ? global.PlexoraVolumeAtlas.shaderChunk(count) : null;
        for (let c = 0; c < count; c += 1) {
            samplers.push(`uniform sampler3D u_volume${c};`);
            if (atlas) continue;
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
        const cut = cutSource(count, sample, locate, atlas ? chunk.sample : null);
        return `#version 300 es
precision highp float;
precision highp sampler3D;
#define NCH ${count}
${samplers.join("\n")}
uniform mat4 u_inverse;        // clip -> model (box in [0,1]^3, then scaled)
uniform vec3 u_extent;         // physical box, longest side 1
uniform vec3 u_box_lo;         // the box kept, texture space [0,1] (the cut, setClip)
uniform vec3 u_box_hi;
uniform int u_hole_on;         // a cutaway's corner taken out of the box
uniform vec3 u_hole_lo;
uniform vec3 u_hole_hi;
uniform int u_cut;             // 0 nothing more, 1 planes, 2 slabs, 3 the cut faces
uniform vec3 u_plane;          // each section's or slab's centre, texture space
uniform vec3 u_plane_on;       // 1.0 along each axis that has one
uniform vec3 u_slab_half;      // each slab's half thickness, texture space
uniform int u_slab_op;         // 1 max, 2 mean, 3 min, 4 sum
uniform float u_context;       // the surrounding volume beside a section or slab
uniform float u_cut_alpha;     // a section plane's, or a cut face's, opacity
uniform vec3 u_cut_color[NCH]; // the channels' colours, never dimmed by u_context
uniform vec3 u_vox;            // one full-resolution voxel, box units (a slab's sum)
uniform vec3 u_color[NCH];
uniform vec2 u_window[NCH];    // [lo, hi] in 0..1 texture units
uniform float u_opacity[NCH];
uniform int u_mode;            // 0 MIP, 1 additive
uniform int u_steps;
uniform vec3 u_ground;
uniform vec2 u_jitter_res;     // the full-quality target's size, in its pixels
${detail ? "uniform vec3 u_detail_lo;\nuniform vec3 u_detail_hi;" : ""}
${chunk ? chunk.declarations : ""}
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
${cut}
void main() {
    vec4 nearPoint = u_inverse * vec4(v_ndc, -1.0, 1.0);
    vec4 farPoint = u_inverse * vec4(v_ndc, 1.0, 1.0);
    vec3 origin = nearPoint.xyz / nearPoint.w;
    vec3 dir = normalize(farPoint.xyz / farPoint.w - origin);
    vec3 lo = u_box_lo * u_extent - 0.5 * u_extent;
    vec3 hi = u_box_hi * u_extent - 0.5 * u_extent;
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
    // grid does not show as wood-grain rings -- the position on the
    // full-quality grid, not the fragment's, so the pattern holds still when
    // a moving view is drawn at a lower resolution.
    vec2 cell = floor((v_ndc * 0.5 + 0.5) * u_jitter_res);
    float jitter = fract(sin(dot(cell, vec2(12.9898, 78.233))) * 43758.5453);
    float v[NCH];
    float peak[NCH];
    for (int c = 0; c < NCH; c++) peak[c] = 0.0;
    vec3 colour = vec3(0.0);
    float alpha = 0.0;
    // [inspect] A section or slab with no surroundings: nothing to march.
    bool skipMarch = (u_cut == 1 || u_cut == 2) && u_context <= 0.0;
${atlas ? ATLAS_MARCH(chunk.sample) : `    for (int i = 0; i < 4096; i++) {
        if (i >= count || skipMarch) break;
        float t = t0 + (float(i) + jitter) * stepLength;
        vec3 world = origin + dir * t;
        vec3 p = world / u_extent + 0.5;${locate}
${sample.join("\n")}`}
        // [inspect] What a cut leaves out is not part of the march: a
        // cutaway's corner, and the slabs, which are combined on their own.
        if ((u_hole_on == 1 && inHole(p)) || (u_cut == 2 && inSlab(p))) {
            for (int c = 0; c < NCH; c++) v[c] = 0.0;
        }
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
        }${atlas ? "\n        t += stepLength;" : ""}
    }
    // [inspect] The sections, the slabs or the cut faces the ray meets.
    float cutT = 1e20;
    vec4 cut = vec4(0.0);
    if (u_cut == 1) cut = cutPlanes(origin, dir, t0, hit.y, cutT);
    else if (u_cut == 2) cut = cutSlabs(origin, dir, t0, hit.y, jitter, stepLength, cutT);
    else if (u_cut == 3) cut = cutFace(origin, dir, lo, hi, t0, hit.y, cutT);
    if (u_mode == 0) {
        for (int c = 0; c < NCH; c++) colour += u_color[c] * level(peak[c], u_window[c]);
        outColor = vec4(max(min(colour, vec3(1.0)), u_ground), 1.0);
    } else {
        outColor = vec4(min(colour + (1.0 - alpha) * u_ground, vec3(1.0)), 1.0);
    }
    outColor.rgb = min(cut.rgb + (1.0 - cut.a) * outColor.rgb, vec3(1.0));
}`;
    }

    /**
     * [inspect] What a cut draws beyond the box (services/inspectGeometry.js):
     * a point sampled anywhere in the stack (`sampleAt`, the same textures
     * the march reads, bricks included), and from it the section planes a
     * ray meets -- where it crosses them, exactly, so a plane one voxel thick
     * is never missed between two steps -- the slabs, their voxels combined
     * along the ray, and the cut faces, drawn solid.
     */
    function cutSource(count, sample, locate, atlasSample) {
        let body;
        if (atlasSample) {
            body = `    vec3 full = u_full_lo + p * u_full_span;
    if (any(lessThan(full, b.lo)) || any(greaterThanEqual(full, b.hi))) {
        locate(full, b.lo, b.hi, b.state, b.base, b.vox, b.anchor);
    }
    if (b.state == 2) {
        for (int c = 0; c < NCH; c++) v[c] = 0.0;
        return;
    }
    vec3 a = (b.base + (full - b.anchor) / b.vox) / u_atlas_dim;
    int cState = b.state;
${atlasSample}`;
        } else {
            body = `${locate}
${sample.join("\n")}`;
        }
        return `
struct Brick { vec3 lo; vec3 hi; vec3 base; vec3 vox; vec3 anchor; int state; };

Brick noBrick() {
    return Brick(vec3(1.0), vec3(0.0), vec3(0.0), vec3(1.0), vec3(0.0), 0);
}

// One point of the stack, texture space; 'b' remembers the brick it was in.
void sampleAt(vec3 p, inout Brick b, out float v[NCH]) {
${body}
}

bool inHole(vec3 p) {
    return all(greaterThan(p, u_hole_lo)) && all(lessThan(p, u_hole_hi));
}

bool inSlab(vec3 p) {
    vec3 off = abs(p - u_plane) - u_slab_half;
    return (u_plane_on.x > 0.5 && off.x <= 0.0) || (u_plane_on.y > 0.5 && off.y <= 0.0)
        || (u_plane_on.z > 0.5 && off.z <= 0.0);
}

vec3 cutColour(float v[NCH]) {
    vec3 c = vec3(0.0);
    for (int i = 0; i < NCH; i++) c += u_cut_color[i] * level(v[i], u_window[i]);
    return c;
}

// The planes met in [tNear, tFar], nearest first, each at u_cut_alpha.
vec4 cutPlanes(vec3 origin, vec3 dir, float tNear, float tFar, out float tFirst) {
    float ts[3];
    ts[0] = 1e20; ts[1] = 1e20; ts[2] = 1e20;
    int n = 0;
    for (int a = 0; a < 3; a++) {
        if (u_plane_on[a] < 0.5 || abs(dir[a]) < 1e-7) continue;
        float t = ((u_plane[a] - 0.5) * u_extent[a] - origin[a]) / dir[a];
        if (t < tNear || t > tFar) continue;
        ts[n] = t;
        n++;
    }
    if (ts[1] < ts[0]) { float s = ts[0]; ts[0] = ts[1]; ts[1] = s; }
    if (ts[2] < ts[1]) { float s = ts[1]; ts[1] = ts[2]; ts[2] = s; }
    if (ts[1] < ts[0]) { float s = ts[0]; ts[0] = ts[1]; ts[1] = s; }
    tFirst = ts[0];
    vec4 acc = vec4(0.0);
    Brick brick = noBrick();
    for (int i = 0; i < 3; i++) {
        if (i >= n) break;
        float v[NCH];
        sampleAt((origin + dir * ts[i]) / u_extent + 0.5, brick, v);
        acc.rgb += (1.0 - acc.a) * cutColour(v) * u_cut_alpha;
        acc.a += (1.0 - acc.a) * u_cut_alpha;
    }
    return acc;
}

// The slabs met in [tNear, tFar]: their intervals along the ray, merged
// where they overlap (the union is one region), sampled at half the march's
// step -- a slab thinner than that still gets one sample -- and combined.
vec4 cutSlabs(vec3 origin, vec3 dir, float tNear, float tFar, float jitter, float stepLength,
              out float tFirst) {
    float lo[3];
    float hi[3];
    lo[0] = 1e20; lo[1] = 1e20; lo[2] = 1e20;
    hi[0] = 1e20; hi[1] = 1e20; hi[2] = 1e20;
    int n = 0;
    for (int a = 0; a < 3; a++) {
        if (u_plane_on[a] < 0.5) continue;
        float s0 = (u_plane[a] - u_slab_half[a] - 0.5) * u_extent[a];
        float s1 = (u_plane[a] + u_slab_half[a] - 0.5) * u_extent[a];
        float enter = tNear;
        float leave = tFar;
        if (abs(dir[a]) < 1e-7) {
            if (origin[a] < s0 || origin[a] > s1) continue;
        } else {
            float ta = (s0 - origin[a]) / dir[a];
            float tb = (s1 - origin[a]) / dir[a];
            enter = max(min(ta, tb), tNear);
            leave = min(max(ta, tb), tFar);
        }
        if (enter >= leave) continue;
        lo[n] = enter;
        hi[n] = leave;
        n++;
    }
    tFirst = 1e20;
    if (n == 0) return vec4(0.0);
    for (int sweep = 0; sweep < 2; sweep++) {
        for (int i = 0; i < 2; i++) {
            if (lo[i + 1] < lo[i]) {
                float s = lo[i]; lo[i] = lo[i + 1]; lo[i + 1] = s;
                s = hi[i]; hi[i] = hi[i + 1]; hi[i + 1] = s;
            }
        }
    }
    float sMax[NCH];
    float sMin[NCH];
    float sSum[NCH];
    for (int c = 0; c < NCH; c++) { sMax[c] = 0.0; sMin[c] = 1e9; sSum[c] = 0.0; }
    float weight = 0.0;
    float end = -1e20;
    Brick brick = noBrick();
    tFirst = lo[0];
    for (int k = 0; k < 3; k++) {
        if (k >= n) break;
        // Merged: the part of this interval past the end of the last.
        float start = max(lo[k], end);
        float stop = hi[k];
        end = max(end, stop);
        float len = stop - start;
        if (len <= 0.0) continue;
        int cnt = int(clamp(ceil(len / (stepLength * 0.5)), 1.0, 512.0));
        float dt = len / float(cnt);
        for (int i = 0; i < 512; i++) {
            if (i >= cnt) break;
            float t = start + (float(i) + jitter) * dt;
            float v[NCH];
            sampleAt((origin + dir * t) / u_extent + 0.5, brick, v);
            for (int c = 0; c < NCH; c++) {
                sMax[c] = max(sMax[c], v[c]);
                sMin[c] = min(sMin[c], v[c]);
                sSum[c] += v[c] * dt;
            }
            weight += dt;
        }
    }
    // A sum is the voxels added along the ray: dt in voxels, not box units.
    float perVoxel = length(dir / max(u_vox, vec3(1e-9)));
    vec3 colour = vec3(0.0);
    float alpha = 0.0;
    for (int c = 0; c < NCH; c++) {
        float value = u_slab_op == 1 ? sMax[c]
            : (u_slab_op == 2 ? sSum[c] / max(weight, 1e-9)
            : (u_slab_op == 3 ? sMin[c] : sSum[c] * perVoxel));
        float s = level(value, u_window[c]);
        colour += u_cut_color[c] * s;
        alpha = max(alpha, s);
    }
    return vec4(colour, clamp(alpha, 0.0, 1.0));
}

// Where the ray first meets kept voxels across a cut -- a cropped side of
// the box, not the stack's own, or the face of a cutaway's corner -- the
// voxel there, drawn solid.
vec4 cutFace(vec3 origin, vec3 dir, vec3 lo, vec3 hi, float tNear, float tFar, out float tFirst) {
    tFirst = 1e20;
    float tFace = -1.0;
    vec3 inv = 1.0 / dir;
    vec3 tmin = min((lo - origin) * inv, (hi - origin) * inv);
    float entry = max(max(tmin.x, tmin.y), tmin.z);
    if (entry > 0.0) {
        int a = entry == tmin.x ? 0 : (entry == tmin.y ? 1 : 2);
        bool fromLow = dir[a] > 0.0;
        if (fromLow ? u_box_lo[a] > 0.0005 : u_box_hi[a] < 0.9995) tFace = entry;
    }
    if (u_hole_on == 1) {
        vec2 h = hitBox(origin, dir, u_hole_lo * u_extent - 0.5 * u_extent,
                        u_hole_hi * u_extent - 0.5 * u_extent);
        if (h.x <= h.y && h.x <= tNear + 1e-5 && h.y > tNear && h.y < tFar) tFace = h.y;
    }
    if (tFace < 0.0 || tFace > tFar) return vec4(0.0);
    tFirst = tFace;
    float v[NCH];
    Brick brick = noBrick();
    sampleAt((origin + dir * (tFace + 1e-4)) / u_extent + 0.5, brick, v);
    return vec4(cutColour(v) * u_cut_alpha, u_cut_alpha);
}
`;
    }

    /**
     * The atlas variant's march (views/volumeAtlas.js): the ray advances by
     * its own `t`, walks the page table once per brick it enters (the brick
     * it ended in is remembered in cLo..cHi), and leaves an empty brick at
     * once rather than sampling it. `full` is the sample in full-resolution
     * pixels and planes, an affine function of t (A + B t).
     */
    function ATLAS_MARCH(sample) {
        return `    vec3 A = u_full_lo + (origin / u_extent + 0.5) * u_full_span;
    vec3 B = dir / u_extent * u_full_span;
    B = mix(B, vec3(1e-9), equal(B, vec3(0.0)));
    vec3 cLo = vec3(1.0), cHi = vec3(0.0), cBase = vec3(0.0), cVox = vec3(1.0), cAnchor = vec3(0.0);
    int cState = 0;
    float t = t0 + jitter * stepLength;
    for (int i = 0; i < 8192; i++) {
        if (t > hit.y || skipMarch) break;
        vec3 world = origin + dir * t;
        vec3 p = world / u_extent + 0.5;
        vec3 full = A + B * t;
        if (any(lessThan(full, cLo)) || any(greaterThanEqual(full, cHi))) {
            locate(full, cLo, cHi, cState, cBase, cVox, cAnchor);
        }
        if (cState == 2) {
            t = max(t + stepLength * 0.5, hitBox(A, B, cLo, cHi).y + stepLength * 0.01);
            continue;
        }
        vec3 a = (cBase + (full - cAnchor) / cVox) / u_atlas_dim;
${sample}`;
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

    function program(gl, count, detail = false, atlas = false, surf = false) {
        const built = gl.createProgram();
        // [surfaces] The ray caster that stops at surfaces and splits at the
        // translucent ones (views/surfaceRenderer.js), made from this source.
        const text = surf ? global.PlexoraSurfaceRenderer.volumeFragment(
            fragmentSource(count, detail, atlas)) : fragmentSource(count, detail, atlas);
        gl.attachShader(built, compile(gl, gl.VERTEX_SHADER, VERTEX));
        gl.attachShader(built, compile(gl, gl.FRAGMENT_SHADER, text));
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
            //: Play in 3-D: {axis: "screen" | "x" | "y" | "z", rate: signed
            //: radians per ms}, or null (setAutoRotate).
            this.autoRotate = null;
            this.lastStep = 0;
            //: Resolution while moving, as a share of full; learnt (see draw).
            this.fastScale = 1;
            this._fastFrames = { last: 0, average: 0, count: 0 };
            this._settleTimer = 0;
            //: The channels the last frame drew, and those still coming in:
            //: name -> when they were first drawn (presence).
            this._drawn = new Set();
            this._fades = new Map();
            //: name -> the texture key the channel was last drawn from: what
            //: it keeps drawing while its own texture loads (entryFor).
            this._shownKey = new Map();
            //: name -> the texture key it is waiting for (one mark per miss).
            this._missed = new Map();
            this._textureIds = 0;
            this._detailIds = 0;
            //: The camera and the composition the last frame was drawn with
            //: (cameraKey, draw), and the crossfade between two compositions.
            this._lastCam = null;
            this._lastComp = null;
            this._stepKey = "";
            this.ghost = null;
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
            //: Finer levels where the camera needs them (views/volumeAtlas.js),
            //: made the first time the view is drawn; the detail sub-volume
            //: (considerDetail) stands in where bricks are not served.
            this.atlas = null;
            global.PlexoraViewPerf?.provide?.("volumeAtlasBytes", () => this.atlas?.bytes || 0);
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
            // The crossfade between two pictures at one camera (draw).
            const Ghost = global.PlexoraPlaneMotion?.VolumeGhost;
            this.ghost = Ghost ? new Ghost(this, { prefs: this.prefs }) : null;
            // [surfaces] Meshes drawn on this context (views/surfaceRenderer.js).
            this.surfaces = global.PlexoraSurfaceRenderer
                ? new global.PlexoraSurfaceRenderer(this) : null;
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
            this.setAutoRotate(false);
            this.spin = null;
            this.tween = null;
            this.zoomTarget = null;
            this._resizeObserver?.disconnect();
            for (const entry of this.textures.values()) this.gl?.deleteTexture(entry.texture);
            this.textures.clear();
            this.textureBytes = 0;
            this.detail = null;
            clearTimeout(this._detailTimer);
            this.atlas?.destroy();
            this.atlas = null;
            this.surfaces?.destroy();
            this.surfaces = null;
            this.ghost?.cancel();
            this.ghost?.ghost?.remove();
            this.ghost = null;
            this._shownKey.clear();
            this.canvas?.remove();
            this.note?.remove();
            this.note = null;
            this.gizmo?.root.remove();
            this.gizmo = null;
            this.canvas = null;
            this.gl = null;
        }

        show(visible) {
            if (this.canvas) this.canvas.hidden = !visible;
            // The sentence goes with the canvas; refresh says it again on return.
            if (this.note && !visible) this.note.hidden = true;
            this.gizmo?.show(visible);
            this.notifyDraw();
            if (!visible) {
                // Leaving 3-D by any path stops Play's turn: nothing turns
                // unseen, and coming back never finds it still going.
                this.setAutoRotate(false);
                this.setLock(null);
                // Back in 3-D, what is drawn comes in again.
                this._drawn.clear();
                this._fades.clear();
                this.ghost?.cancel();
                this._lastCam = null;
            }
            if (visible) this.invalidate();
        }

        /** Whether the user is working in the 3-D view: the pointer over it
         *  or its gizmo, or keyboard focus in either. Its letter keys are
         *  only its own then (attach). */
        engaged() {
            const within = (node) => Boolean(node) && (node.matches?.(":hover")
                || node === document.activeElement || node.contains?.(document.activeElement));
            // [inspect] The 3-D dock and the handles are part of the view.
            return within(this.canvas) || within(this.gizmo?.root) || within(global.PlexoraVolumeDock?.root)
                || within(this.handles?.root);
        }

        /** Lock turns to image axis `name`, or release with null. The same
         *  axis again releases it. */
        setLock(name) {
            const next = name && name !== this.lock ? name : null;
            if (next === this.lock) return;
            this.lock = next;
            // A spin about the old axis (or a free one) is not this lock's,
            // and nor is Play's turn.
            this.spin = null;
            this.setAutoRotate(false);
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
            this._shownKey.clear();
            this.ghost?.cancel();
            this.setAutoRotate(false);
            this.detail = null;
            this.atlas?.lost();
            this.surfaces?.lost();
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
                // The slider in raw units: what a texture made with another
                // window is read with (windowFor).
                let raw = null;
                if (sidebar.isHdMode?.()) {
                    raw = slot.range ? slot.range.map(Number) : null;
                } else if (packet && Number.isFinite(Number(packet.qmin))) {
                    const span = Math.max(1, Number(packet.qmax) - Number(packet.qmin));
                    raw = (slot.range || [0, 255]).map((value) => Number(packet.qmin) + (value / 255) * span);
                }
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
                    raw: raw && raw.every(Number.isFinite) ? raw : null,
                    levelWindow,
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

        /** The texture made for `channel` as it is now, or null. */
        textureOf(channel) {
            return this.textures.get(this.textureKey(channel.name, undefined, undefined, undefined,
                                                     channel.stamp || "")) || null;
        }

        /**
         * The texture `channel` is DRAWN from: its own when held, else --
         * while that loads -- the one it was drawn from last, when that is of
         * the same area. A window turning exact, a frame step, HD on or off:
         * the picture keeps the channel until its new texture can replace it,
         * never a channel gone black and faded back in. A different area is a
         * different picture, and is never stood in for. Drawing only:
         * `settled()`, `refine()` and the loads ask for the exact texture.
         */
        entryFor(channel) {
            const key = this.textureKey(channel.name, undefined, undefined, undefined, channel.stamp || "");
            const exact = this.textures.get(key);
            if (exact) {
                this._shownKey.set(channel.name, key);
                this._missed.delete(channel.name);
                return exact;
            }
            const shown = this.textures.get(this._shownKey.get(channel.name));
            if (!shown || shown.box !== this.boxKey()) return null;
            if (this._missed.get(channel.name) !== key) {
                this._missed.set(channel.name, key);
                global.PlexoraViewPerf?.mark?.("volume", "texmiss", null, channel.name);
            }
            return shown;
        }

        /**
         * `channel`'s window in the units of the texture it is drawn from.
         * Its own texture's is `channel.window`; an older one, made with
         * another window (the provisional one, while the exact one loads; 8-bit
         * while HD's arrives), reads the same raw range in its own window, so
         * the picture keeps its contrast across the handover.
         */
        windowFor(channel) {
            const entry = channel.entry;
            const made = entry?.levelWindow;
            const own = channel.levelWindow;
            if (!made || !channel.raw) return channel.window;
            if (entry.format === this.format() && own && own[0] === made[0] && own[1] === made[1]) {
                return channel.window;
            }
            const span = Math.max(1e-6, made[1] - made[0]);
            return channel.raw.map((value) => Math.min(1, Math.max(0, (value - made[0]) / span)));
        }

        /** The textures eviction must leave: what each channel is made with
         *  now, what it is drawn from, and the details for both. */
        keepKeys(channels = this.channels()) {
            const keep = new Set(channels.map((channel) => this.textureKey(
                channel.name, undefined, undefined, undefined, channel.stamp || "")));
            for (const key of this._shownKey.values()) keep.add(key);
            for (const detail of [this.detail, this._detailRequest]) {
                if (!detail?.region) continue;
                for (const channel of channels) {
                    keep.add(this.detailKey(channel, detail));
                    const shown = this.textures.get(this._shownKey.get(channel.name));
                    if (shown) keep.add(this.detailKey({ name: channel.name, stamp: shown.stamp }, detail));
                }
            }
            return keep;
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
            // The level already drawn, when every visible channel at it still
            // fits what they share (eight channels at the default budget).
            // A channel being replaced (entryFor) counts as drawn at the level
            // it is drawn from, when its bytes are the same kind.
            const held = channels.map((channel) => this.textureOf(channel)
                || this.textures.get(this._shownKey.get(channel.name))).find((entry) => entry
                    && entry.format === this.format() && entry.box === this.boxKey());
            const level = held && held.bytes * channels.length <= 8 * this.budget
                ? held.level : null;
            await Promise.all(missing.map((channel) => this.loadChannel(channel.name, t, budget,
                                                                        channel.stamp, level)));
            this.evict(this.keepKeys(channels));
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

        /** One channel's texture: at `level` when given (the level the other
         *  channels are drawn at), else the finest the budget allows. */
        async loadChannel(name, t, budget = this.budget, stamp = "", level = null) {
            const format = this.format();
            const area = this.boxKey();
            const key = this.textureKey(name, t, format, area, stamp);
            if (this._loads.has(key)) return this._loads.get(key);
            const ds = encodeURIComponent(this.datasource);
            const query = new URLSearchParams({ channel: name, t: String(t),
                                                budget: String(budget),
                                                max_side: String(this.maxTexture), format });
            if (Number.isInteger(level)) query.set("level", String(level));
            if (area) query.set("box", area);
            if (stamp) query.set("w", stamp);
            global.PlexoraVolumeAtlas?.LINK?.ask(query);
            const address = typeof global.plexoraUrl === "function"
                ? global.plexoraUrl(`volume/${ds}/level?${query}`)
                : `/volume/${ds}/level?${query}`;
            const load = (async () => {
                const response = await this.fetch(address);
                if (response.status === 400) {
                    // The server's sentence: no level of this stack fits a 3-D
                    // texture, say. Shown as it is (see refresh).
                    let said = "";
                    try {
                        said = String((await response.json())?.error || "");
                    } catch (error) {
                        said = "";
                    }
                    const refused = new Error(said || "volume level: HTTP 400");
                    refused.refused = true;
                    throw refused;
                }
                if (!response.ok) throw new Error(`volume level: HTTP ${response.status}`);
                const shape = String(response.headers.get("X-Plexora-Shape") || "")
                    .split(",").map(Number);
                const half = (response.headers.get("X-Plexora-Format") || "u8") === "f16";
                const buffer = await response.arrayBuffer();
                // How fast the link is: a slow one gets the next levels gzipped.
                if (global.location?.href) {
                    global.PlexoraVolumeAtlas?.LINK?.note(new URL(address, global.location.href).href);
                }
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
                const window = String(stamp || "").split(",").map(Number);
                const entry = {
                    texture, t, format, box: area, name, stamp,
                    id: ++this._textureIds,
                    //: The window its bytes are quantized with (`w=`), raw units.
                    levelWindow: window.length === 2 && window.every(Number.isFinite) ? window : null,
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

        /**
         * The planes the cut keeps, `[z0, z1]`, or null for the whole stack:
         * what the contrast is levelled over. A Z crop's or cutaway's range;
         * a Z section's plane, or slab's planes, when it is drawn alone.
         */
        clipRange() {
            const depth = Math.max(1, this.viewState?.depth || 1);
            const resolved = this.inspectNow();
            if (!resolved) return null;
            let z0 = Math.max(0, Math.floor(resolved.box.z[0]));
            let z1 = Math.min(depth - 1, Math.ceil(resolved.box.z[1]) - 1);
            const plane = resolved.planes.length === 1 ? resolved.planes[0] : null;
            if (plane?.axis === "z" && resolved.context <= 0) {
                z0 = Math.max(0, Math.floor(plane.lo));
                z1 = Math.min(depth - 1, Math.max(z0, Math.ceil(plane.hi) - 1));
            }
            return z0 <= 0 && z1 >= depth - 1 ? null : [z0, z1];
        }

        // -- the cut (services/inspectGeometry.js) -------------------------

        /** What the 3-D view keeps now -- the cut controller's answer, a
         *  preview included (services/inspectState.js), else the view
         *  state's -- or null with no cut to read. */
        inspectNow() {
            const controller = global.PlexoraInspect;
            if (controller && controller.viewState === this.viewState) return controller.resolved();
            const G = global.PlexoraInspectGeometry;
            const state = this.viewState?.get?.();
            if (!G || !state?.inspect) return null;
            const d = G.dims(this.viewState.info, this.viewState.depth);
            return G.resolve(state.inspect, G.positions(state, d), d);
        }

        /**
         * The cut in texture space for textures covering `covered`, narrowed
         * by `setClip` (Video Studio's reveals): `{lo, hi, hole, plane,
         * planeOn, half, cut, op, context, opacity, face}`.
         */
        cutUniforms(covered = this.covered) {
            const G = global.PlexoraInspectGeometry;
            const info = this.viewState?.info || {};
            const area = covered || [0, 0, Number(info.width) || 1, Number(info.height) || 1];
            const depth = Math.max(1, this.viewState?.depth || 1);
            const resolved = G ? this.inspectNow() : null;
            const out = resolved ? G.toTexture(resolved, area, depth)
                : { lo: [0, 0, 0], hi: [1, 1, 1], hole: null, plane: [0.5, 0.5, 0.5], planeOn: [0, 0, 0],
                    half: [0, 0, 0], cut: 0, op: 0, context: 1, opacity: 1, face: 0 };
            const clip = this.clipXY;
            if (clip) {
                ["x", "y", "z"].forEach((axis, i) => {
                    const range = clip[axis];
                    if (!range) return;
                    out.lo[i] = Math.max(out.lo[i], range[0]);
                    out.hi[i] = Math.max(out.lo[i], Math.min(out.hi[i], range[1]));
                });
            }
            return out;
        }

        /** Set the cut's uniforms on the program in use. */
        bindCut(uniform, cut, covered, extent) {
            const gl = this.gl;
            const info = this.viewState?.info || {};
            const area = covered || [0, 0, Number(info.width) || 1, Number(info.height) || 1];
            const depth = Math.max(1, this.viewState?.depth || 1);
            gl.uniform3fv(uniform("u_box_lo"), cut.lo);
            gl.uniform3fv(uniform("u_box_hi"), cut.hi);
            gl.uniform1i(uniform("u_hole_on"), cut.hole ? 1 : 0);
            gl.uniform3fv(uniform("u_hole_lo"), cut.hole ? cut.hole.lo : [0, 0, 0]);
            gl.uniform3fv(uniform("u_hole_hi"), cut.hole ? cut.hole.hi : [0, 0, 0]);
            gl.uniform1i(uniform("u_cut"), cut.cut);
            gl.uniform3fv(uniform("u_plane"), cut.plane);
            gl.uniform3fv(uniform("u_plane_on"), cut.planeOn);
            gl.uniform3fv(uniform("u_slab_half"), cut.half);
            gl.uniform1i(uniform("u_slab_op"), cut.op || 1);
            gl.uniform1f(uniform("u_context"), cut.context);
            gl.uniform1f(uniform("u_cut_alpha"), cut.cut === 3 ? 1 : cut.opacity);
            const span = [Math.max(1e-9, area[2] - area[0]), Math.max(1e-9, area[3] - area[1]), depth];
            gl.uniform3fv(uniform("u_vox"), extent.map((side, i) => side / span[i]));
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
            return Boolean(this.spin || this.tween || this.zoomTarget !== null || this.autoRotate);
        }

        /**
         * Everything that places the picture on screen: the camera, the
         * canvas's size, the clip and the area. Two frames with the same key
         * line up pixel for pixel, which is when one may dissolve into the
         * other (VolumeGhost).
         */
        cameraKey() {
            const c = this.camera;
            const pinned = this._pinnedSize;
            return [c.q.map((v) => v.toFixed(6)).join(","), c.dist.toFixed(6), c.panX.toFixed(6),
                    c.panY.toFixed(6), this.canvas?.clientWidth || 0, this.canvas?.clientHeight || 0,
                    // [inspect] A cut is a camera move: it never dissolves.
                    JSON.stringify(this.cutUniforms()), this.boxKey(),
                    pinned ? `${pinned.width}x${pinned.height}` : ""].join("|");
        }

        /** True while the picture may be drawn at the lower quality. */
        moving() {
            return this.interacting || this.animating();
        }

        /** True while a channel is still coming in (presence): drawn every
         *  frame, but in full -- nothing is moving. */
        fading() {
            return this._fades.size > 0 && Boolean(this.canvas) && !this.canvas.hidden;
        }

        /**
         * How far in each of `channels` is, 0..1, at `time`: a channel the
         * last frame did not draw -- switched on, its level just arrived, the
         * view just entered -- starts from nothing and comes in over
         * FADE_IN_MS. All at once with motion off, and for a capture
         * (`settled`).
         */
        presence(channels, time, { settled = false } = {}) {
            const shown = new Set(channels.map((channel) => channel.name));
            const fade = !settled && this.smooth();
            for (const name of shown) {
                if (fade && !this._drawn.has(name)) this._fades.set(name, time);
            }
            for (const name of this._fades.keys()) {
                if (!fade || !shown.has(name)) this._fades.delete(name);
            }
            this._drawn = shown;
            return channels.map((channel) => {
                const start = this._fades.get(channel.name);
                if (start === undefined) return 1;
                const f = Math.min(1, Math.max(0, (time - start) / FADE_IN_MS));
                if (f >= 1) this._fades.delete(channel.name);
                return 1 - (1 - f) ** 3;
            });
        }

        /** One display frame: move what is moving, draw, and ask for the next
         *  frame while anything still moves. */
        onFrame(time) {
            const wasAnimating = this.animating();
            if (wasAnimating) {
                const dt = this.lastStep ? Math.min(64, Math.max(0, time - this.lastStep)) : 16;
                this.lastStep = time;
                this.advance(dt, time);
                this.dirty = true;
            } else {
                this.lastStep = 0;
            }
            if (this.fading()) this.dirty = true;
            if (this.dirty) {
                this.learnFrame(time, this.animating());
                // The frame a coast, a turn or an eased zoom ends in is drawn
                // as the moving ones were: the full picture follows at rest,
                // at this same camera, and so dissolves in (draw).
                this._motionTail = wasAnimating && !this.animating();
                try {
                    this.draw(time);
                } finally {
                    this._motionTail = false;
                }
                if (!this.moving()) this.scheduleDetail();
            }
            // [surfaces] The camera at rest is remembered (attach's onRest).
            if (!this.moving()) this.onRest?.();
            if (this.animating() || this.fading()) {
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
            if (this.autoRotate) {
                // Play's turn: the same angle per millisecond whatever the
                // frame rate (dt is capped, so a tab coming back does not
                // leap), about the screen's vertical as a sideways drag
                // turns it, or about an image axis held still on screen as
                // a locked drag does. The zoom and the pan are left alone.
                const C = Camera();
                const { axis, rate } = this.autoRotate;
                const angle = rate * dt;
                camera.q = axis === "screen"
                    ? C.quat.normalize(C.quat.multiply(C.quat.axisAngle([0, 1, 0], angle), camera.q))
                    : C.turnAbout(camera.q, axis, angle);
            }
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
            this.setAutoRotate(false);
        }

        /**
         * Play in 3-D: turn the stack `speed` degrees a second (within
         * ROTATE_MIN_DPS..ROTATE_MAX_DPS) about `axis` -- "screen" (the
         * screen's vertical) or an image axis -- `direction` 1 or -1, from the
         * camera as it is; called again while on, it takes the new speed and
         * axis without a jump. Off with `on` false. It is the user's action,
         * so it turns with motion off too (smooth() governs what moves of its
         * own accord); `stopMotion` (any press), `turnTo`, `setLock`,
         * `restoreCamera`, `renderFrame` and a sized `snapshot` end it.
         * Announced as `plexora:volume-rotate` ({on}) when it starts or stops.
         */
        setAutoRotate(on, { speed = ROTATE_DEFAULT_DPS, axis = "screen", direction = 1 } = {}) {
            const was = Boolean(this.autoRotate);
            if (on) {
                const dps = Math.min(ROTATE_MAX_DPS,
                                     Math.max(ROTATE_MIN_DPS, Number(speed) || ROTATE_DEFAULT_DPS));
                this.spin = null;
                this.tween = null;
                this.autoRotate = {
                    axis: ROTATE_AXES.includes(axis) ? axis : "screen",
                    rate: ((dps * Math.PI) / 180 / 1000) * (Number(direction) < 0 ? -1 : 1),
                };
                if (!this.lastStep) this.lastStep = now();
                this.invalidate();
            } else {
                this.autoRotate = null;
            }
            if (was === Boolean(this.autoRotate)) return;
            this.onAutoRotate?.(Boolean(this.autoRotate));
            try {
                global.dispatchEvent?.(new CustomEvent("plexora:volume-rotate",
                    { detail: { on: Boolean(this.autoRotate) } }));
            } catch (error) {
                // No CustomEvent in a bare runtime.
            }
        }

        /** Turn (and pan) to `target` -- `{q, panX, panY, dist}`, any of
         *  them -- the short way round. */
        turnTo(target) {
            this.spin = null;
            this.setAutoRotate(false);
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
            // [inspect] Pixels are square unless the image says otherwise.
            const sy = Number(info.spacing?.y?.value) || sx;
            const sz = Number(info.spacing?.z?.value) || sx;
            // The stack's own plane count: a texture's z may be max-pooled
            // (`X-Plexora-ZPool`), which changes its depth and not the stack's.
            const planes = Math.max(depth, Number(vs?.depth) || 0);
            // An area: its own size, not the plane's.
            const across = this.covered
                ? [this.covered[2] - this.covered[0], this.covered[3] - this.covered[1]]
                : [width * scale, height * scale];
            const physical = [across[0] * sx, across[1] * sy, planes * sz];
            const longest = Math.max(...physical) || 1;
            return physical.map((side) => side / longest);
        }

        matrices() {
            return this.cameraMatrices().inverse;
        }

        /** `{view, forward (clip from box), inverse, eye (in the box's space)}`. */
        cameraMatrices() {
            const canvas = this.canvas;
            const aspect = canvas.width / Math.max(1, canvas.height);
            const { q, dist, panX, panY } = this.camera;
            // The camera sits `dist` back along the screen's z; the stack is
            // turned by q (image axes -> screen axes) and shifted by the pan.
            const shift = new Float32Array([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0,
                                            -panX, -panY, -dist, 1]);
            const view = mat4Multiply(shift, Camera().quat.toMat4(q));
            const projection = mat4Perspective(FOV_Y, aspect, 0.01, 20);
            const forward = mat4Multiply(projection, view);
            const back = mat4Invert(view);
            return { view, forward, inverse: mat4Invert(forward),
                     eye: back ? [back[12], back[13], back[14]] : [0, 0, dist] };
        }

        /** What the atlas draws and plans with, for base texture `lead`. */
        atlasView(lead) {
            const info = this.viewState?.info || {};
            const covered = lead.covered || [0, 0, Number(info.width) || 1, Number(info.height) || 1];
            const depth = Math.max(1, this.viewState?.depth || 1);
            const extent = this.extent();
            const shape = lead.shape;                       // (z, y, x)
            const camera = this.cameraMatrices();
            // [inspect] What the cut keeps, in full-resolution pixels and planes.
            const cut = this.cutUniforms(covered);
            const span = [covered[2] - covered[0], covered[3] - covered[1], depth];
            const full = (axis, v) => (axis < 2 ? covered[axis] : 0) + v * span[axis];
            return {
                fullLo: [covered[0], covered[1], 0],
                fullSpan: span,
                extent,
                eye: camera.eye,
                forward: camera.forward,
                clip: [Math.max(0, Math.floor(cut.lo[2] * depth)),
                       Math.min(depth - 1, Math.ceil(cut.hi[2] * depth) - 1)],
                box: { lo: [0, 1, 2].map((a) => full(a, cut.lo[a])), hi: [0, 1, 2].map((a) => full(a, cut.hi[a])) },
                // Sections and slabs drawn alone: only the bricks they cross.
                only: (cut.cut === 1 || cut.cut === 2) && cut.context <= 0
                    ? [0, 1, 2].filter((a) => cut.planeOn[a]).map((a) => ({
                        axis: a,
                        lo: full(a, Math.max(0, cut.plane[a] - cut.half[a])) - (a < 2 ? 1 : 0.5),
                        hi: full(a, Math.min(1, cut.plane[a] + cut.half[a])) + (a < 2 ? 1 : 0.5) }))
                    : null,
                // A screen pixel's size at distance 1; a base voxel in
                // full-resolution pixels, pixels and planes.
                pixel: (2 * Math.tan(FOV_Y / 2)) / Math.max(1, this.canvas?.height || 1),
                baseVox: [(covered[2] - covered[0]) / Math.max(1, shape[2]),
                          (covered[3] - covered[1]) / Math.max(1, shape[1]),
                          depth / Math.max(1, shape[0])],
            };
        }

        /** The atlas made to fit what `channels` draw now; false when it is
         *  not served, not loaded yet, or has nothing finer than the base.
         *  Keyed by the textures DRAWN (entryFor): a channel still drawn
         *  from its last texture keeps the bricks made with the same window
         *  and frame until its new texture lands. */
        atlasFits(channels, lead) {
            const atlas = this.atlas;
            if (!atlas || atlas.status !== "ready" || !lead) return false;
            return atlas.configure(channels.map((c) => ({ name: c.name, stamp: (c.entry ? c.entry.stamp : c.stamp) || "" })),
                                   lead.t ?? this.frameT(), lead.format || this.format(), lead.level);
        }

        draw(time = now(), { settled = false } = {}) {
            this.dirty = false;
            const gl = this.gl;
            const canvas = this.canvas;
            if (!gl || !canvas || (canvas.hidden && !this._drawHidden)) return;
            const fast = this.moving() || Boolean(this._motionTail);
            this.drawnFast = fast;
            const full = Math.min(global.devicePixelRatio || 1, 1.5);
            const ratio = full * (fast ? this.fastScale : 1);
            // A pinned size wins over the canvas's CSS size: a frame rendered
            // for a video is the video's size (pinSize), and so is an export's
            // (snapshot), whatever the window.
            const pinned = this._pinnedSize;
            const width = pinned ? pinned.width : Math.max(1, Math.round(canvas.clientWidth * ratio));
            const height = pinned ? pinned.height : Math.max(1, Math.round(canvas.clientHeight * ratio));
            const at = now();

            // 1. What this frame is drawn with, decided before anything is
            //    written: the canvas still holds the last frame (see 2).
            const channels = this.channels().map((channel) => ({ ...channel, entry: this.entryFor(channel) }))
                .filter((channel) => channel.entry);
            // [surfaces] "Surfaces only" draws no channel; surfaces draw their
            // passes first and leave the volume two outputs to fill.
            if (this.surfaces?.hideVolume) channels.length = 0;
            const lead = channels.length ? channels[0].entry : null;
            const detail = lead ? this.detail : null;
            // A detail made with the window and frame each channel is drawn in.
            const details = detail
                ? channels.map((channel) => this.textures.get(this.detailKey(
                    { name: channel.name, stamp: channel.entry.stamp ?? channel.stamp }, detail)) || null)
                : [];
            const withAtlas = Boolean(lead) && this.atlasFits(channels, lead) && this.atlas.active();
            const withDetail = !withAtlas && Boolean(detail) && details.length > 0
                && details.every(Boolean);
            if (withAtlas && !this._atlasEngaged) {
                // The bricks cost more per sample than one texture: start the
                // moving resolution a step down rather than learn it from a
                // run of dropped frames (learnFrame goes on from here).
                this._atlasEngaged = true;
                this.fastScale = Math.min(this.fastScale, 0.85);
            }
            let shape = lead ? lead.shape : null;
            let steps = 0;
            let finest = null;
            if (lead) {
                shape = channels.reduce((most, channel) => {
                    const s = channel.entry.shape;
                    return s[0] * s[1] * s[2] > most[0] * most[1] * most[2] ? s : most;
                }, lead.shape);
                // As many samples as the finest voxels drawn: the detail's,
                // spread over the whole box, when there is one.
                let longest = Math.max(...shape);
                if (withDetail) {
                    const dshape = details[0].shape;   // (z, y, x)
                    const span = [detail.hi[2] - detail.lo[2], detail.hi[1] - detail.lo[1],
                                  detail.hi[0] - detail.lo[0]];
                    longest = Math.max(longest, ...dshape.map((side, i) => side / Math.max(1e-3, span[i])));
                }
                // The finest bricks the atlas draws: as many samples as their
                // voxels -- at rest. Moving, the base's count: the bricks are
                // still read, and a turning view is drawn again in full (and
                // dissolves in) the moment it stops.
                finest = withAtlas && !fast ? this.atlas.finestVox() : null;
                if (finest) {
                    const info = this.viewState?.info || {};
                    const covered = lead.covered || [0, 0, Number(info.width) || 1, Number(info.height) || 1];
                    const fullSpan = [covered[2] - covered[0], covered[3] - covered[1],
                                      Math.max(1, this.viewState?.depth || 1)];
                    longest = Math.max(longest, ...fullSpan.map((side, i) => side / finest[i]));
                }
                // Along the diagonal, so about one sample per texel along any
                // axis. Halved while moving for the additive composite only:
                // it is step-corrected and just softens, where MIP with fewer
                // samples misses thin peaks and the stack dims as it turns.
                steps = Math.min(finest ? 2048 : 1024, Math.max(96, Math.round(longest * 2)));
                if (fast && this.composite === "additive") steps = Math.round(steps / 2);
            }
            const stepKey = `${steps}|${fast ? 1 : 0}|${this.fastScale.toFixed(3)}`;
            if (stepKey !== this._stepKey) {
                this._stepKey = stepKey;
                global.PlexoraViewPerf?.mark?.("volume", "steps", null, stepKey);
            }

            // 2. The crossfade. With the camera where the last frame had it and
            //    what is drawn changed -- the full frame after a turn, a texture,
            //    a detail or bricks landing, a channel gone -- the frame on
            //    screen is held on the ghost before the canvas is touched
            //    (resizing it clears it), and fades out over this one. A camera
            //    that moved ends a fade at once; a capture never has one; a
            //    channel coming in has its own (presence).
            const cam = this.cameraKey();
            const comp = [channels.map((channel) => `${channel.name}:${channel.entry.id}`).join(","),
                          withDetail ? detail.id : "",
                          withAtlas ? this.atlas.pagesVersion : "",
                          steps, `${width}x${height}`, this.composite];
            const ghost = this.ghost;
            let held = false;
            if (ghost) {
                if (settled) {
                    ghost.cancel();
                } else {
                    ghost.sync(cam);
                    const last = this._lastComp;
                    const changed = last ? comp.findIndex((part, i) => String(part) !== String(last[i])) : -1;
                    const arriving = channels.some((channel) => !this._drawn.has(channel.name));
                    if (!fast && cam === this._lastCam && changed >= 0 && !arriving) {
                        held = ghost.capture(cam);
                        if (held) {
                            global.PlexoraViewPerf?.mark?.("volume", "ghost", null,
                                ["channels", "detail", "pages", "steps", "size", "composite"][changed]);
                        }
                    }
                }
            }
            this._lastCam = settled ? null : cam;
            this._lastComp = settled ? null : comp;

            // 3. The frame.
            if (canvas.width !== width || canvas.height !== height) {
                canvas.width = width;
                canvas.height = height;
            }
            gl.viewport(0, 0, width, height);
            const surf = this.surfaces?.begin(channels, { fast }) || null;
            // A channel new to the picture comes in rather than lands: its
            // colour and opacity scaled by how far in it is.
            const presence = this.presence(channels, time, { settled });
            gl.clearColor(0, 0, 0, surf ? 0 : 1);
            gl.clear(gl.COLOR_BUFFER_BIT);
            if (!channels.length) {
                if (surf) {
                    surf.finish(false);
                    this.paintGizmo(fast);
                }
                this.notifyDraw();
                if (held) ghost.release();
                return;
            }
            // What is drawn sets the box's proportions and the step count:
            // channels may hold different levels (see ensureTextures).
            for (const channel of channels) channel.entry.used = at;
            this.covered = lead.covered;
            this.shape = shape;
            this.level = lead.level;
            const programKey = `${channels.length}|${withDetail ? 1 : 0}|${withAtlas ? 1 : 0}${surf ? "|s" : ""}`;
            let built = this.programs.get(programKey);
            if (!built) {
                built = program(gl, channels.length, withDetail, withAtlas, Boolean(surf));
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
            // [inspect] How a texture is read between voxels (volume.interpolation).
            const filter = (this.view || this.viewState?.get?.())?.volume?.interpolation === "nearest"
                ? gl.NEAREST : gl.LINEAR;
            const filtered = (entry) => {
                if (entry.filter === filter) return;
                gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MIN_FILTER, filter);
                gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MAG_FILTER, filter);
                entry.filter = filter;
            };
            channels.forEach((channel, index) => {
                gl.activeTexture(gl.TEXTURE0 + index);
                gl.bindTexture(gl.TEXTURE_3D, channel.entry.texture);
                filtered(channel.entry);
                gl.uniform1i(uniform(`u_volume${index}`), index);
            });
            if (withDetail) {
                details.forEach((entry, index) => {
                    entry.used = at;
                    gl.activeTexture(gl.TEXTURE0 + channels.length + index);
                    gl.bindTexture(gl.TEXTURE_3D, entry.texture);
                    filtered(entry);
                    gl.uniform1i(uniform(`u_detail${index}`), channels.length + index);
                });
                gl.uniform3fv(uniform("u_detail_lo"), detail.lo);
                gl.uniform3fv(uniform("u_detail_hi"), detail.hi);
            }
            const atlasView = withAtlas ? this.atlasView(lead) : null;
            if (withAtlas) this.atlas.bind(gl, uniform, channels.length, atlasView);
            // [inspect] The cut: the box, the corner, the planes or slabs. Beside
            // a section or slab the rest of the stack is drawn at u_context.
            const cut = this.cutUniforms(lead.covered);
            const dim = cut.cut === 1 || cut.cut === 2 ? cut.context : 1;
            gl.uniform3fv(uniform("u_color"), channels.flatMap(
                (channel, index) => channel.colour.map((value) => value * presence[index] * dim)));
            gl.uniform3fv(uniform("u_cut_color"), channels.flatMap(
                (channel, index) => channel.colour.map((value) => value * presence[index])));
            // Each channel in the window of the texture it is drawn from.
            gl.uniform2fv(uniform("u_window"), channels.flatMap((channel) => this.windowFor(channel)));
            gl.uniform1fv(uniform("u_opacity"), channels.map(
                (channel, index) => channel.opacity * presence[index] * dim));
            gl.uniformMatrix4fv(uniform("u_inverse"), false, this.matrices());
            const extent = this.extent();
            gl.uniform3fv(uniform("u_extent"), extent);
            this.bindCut(uniform, cut, lead.covered, extent);
            gl.uniform1i(uniform("u_mode"), this.composite === "additive" ? 1 : 0);
            gl.uniform1i(uniform("u_steps"), steps);
            gl.uniform3fv(uniform("u_ground"), [0, 0, 0]);
            gl.uniform2fv(uniform("u_jitter_res"), pinned ? [pinned.width, pinned.height]
                : [Math.max(1, Math.round(canvas.clientWidth * full)),
                   Math.max(1, Math.round(canvas.clientHeight * full))]);
            surf?.bindDepth(uniform);
            gl.bindBuffer(gl.ARRAY_BUFFER, this.quad);
            gl.enableVertexAttribArray(0);
            gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
            gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
            surf?.finish(true);
            this.paintGizmo(fast);
            this.notifyDraw();
            if (held) ghost.release();
        }

        /** [inspect] Call `fn(renderer)` after every frame drawn (the
         *  handles follow the camera); returns the undo. */
        addDrawListener(fn) {
            this._drawListeners = this._drawListeners || new Set();
            this._drawListeners.add(fn);
            return () => this._drawListeners.delete(fn);
        }

        notifyDraw() {
            for (const fn of this._drawListeners || []) {
                try {
                    fn(this);
                } catch (error) {
                    console.error("volumeRenderer: a draw listener failed", error);
                }
            }
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
            if (!this.atlas && global.PlexoraVolumeAtlas) {
                this.atlas = new global.PlexoraVolumeAtlas({
                    gl: this.gl, datasource: this.datasource, maxTexture: this.maxTexture,
                    fetchImpl: this.fetch, onChange: () => this.invalidate() });
                this.atlas.load().then(() => this.scheduleDetail());
            }
            try {
                // The window before the address, as the plane does
                // (viewerManager.addChannelItems): a level is kept under the
                // window it is made with, and a channel switched on for the
                // first time has none yet. Its first answer is announced to
                // nobody (volumeWindows.adopt), so a level fetched before it
                // was looked for under another window and never drawn.
                await global.__plexora?.volumeWindows?.ensure?.(
                    this.channels().map((channel) => channel.name));
                const channels = this.channels();
                await this.ensureTextures(channels);
                this.say("");
            } catch (error) {
                if (error?.refused) {
                    // Not a fault: this stack has nothing a GPU can hold.
                    this.status = "unavailable";
                    this.say(error.message);
                } else {
                    console.error("volumeRenderer:", error);
                    this.status = "error";
                }
                this.paintStatus();
            }
            this.invalidate();
        }

        paintStatus() {
            global.dispatchEvent?.(new CustomEvent("plexora:volume-progress",
                { detail: { status: this.status, level: this.level, shape: this.shape,
                            message: this.note?.textContent || "" } }));
        }

        /** One sentence over the canvas -- why there is nothing to draw -- or
         *  none for "". */
        say(text) {
            if (!text) {
                if (this.note) this.note.hidden = true;
                return;
            }
            if (!this.note && this.wrapper && typeof document !== "undefined") {
                this.note = document.createElement("p");
                this.note.className = "viewer-volume-note";
                this.note.setAttribute("role", "status");
                this.wrapper.appendChild(this.note);
            }
            if (!this.note) return;
            this.note.textContent = text;
            this.note.hidden = false;
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
                         x0: event.clientX, y0: event.clientY,
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
                // [surfaces] A click (no drag) on a surface picks its object.
                if (event?.type === "pointerup" && this.surfaces?.active()
                        && Math.hypot(event.clientX - drag.x0, event.clientY - drag.y0) < 4) {
                    const box = canvas.getBoundingClientRect();
                    const hit = this.surfaces.pick(event.clientX - box.left, event.clientY - box.top);
                    global.dispatchEvent?.(new CustomEvent("plexora:surface-pick", {
                        detail: { hit, clientX: event.clientX, clientY: event.clientY,
                                  additive: Boolean(event.shiftKey || event.metaKey) } }));
                }
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
            // [inspect] Within what the cut keeps; a section or slab drawn
            // alone along one axis is all that is seen along it.
            const cut = this.cutUniforms();
            const bounds = [0, 1, 2].map((a) => [cut.lo[a], cut.hi[a]]);
            const alone = (cut.cut === 1 || cut.cut === 2) && cut.context <= 0;
            const along = cut.planeOn.filter(Boolean).length === 1 ? cut.planeOn.indexOf(1) : -1;
            if (alone && along >= 0) {
                const pad = along === 2 ? 0.5 / Math.max(1, this.viewState?.depth || 1) : 0.002;
                bounds[along] = [Math.max(bounds[along][0], cut.plane[along] - cut.half[along] - pad),
                                 Math.min(bounds[along][1], cut.plane[along] + cut.half[along] + pad)];
            }
            const xy = { x: bounds[0], y: bounds[1] };
            const clip = bounds[2];
            const n = REGION_LATTICE;
            const min = [Infinity, Infinity, Infinity];
            const max = [-Infinity, -Infinity, -Infinity];
            const m = forward;
            for (let i = 0; i <= n; i += 1) {
                const px = xy.x[0] + (i / n) * (xy.x[1] - xy.x[0]);
                for (let j = 0; j <= n; j += 1) {
                    const py = xy.y[0] + (j / n) * (xy.y[1] - xy.y[0]);
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
            const step = [(xy.x[1] - xy.x[0]) / n, (xy.y[1] - xy.y[0]) / n, (clip[1] - clip[0]) / n];
            return {
                lo: min.map((v, a) => Math.max(bounds[a][0], v - step[a])),
                hi: max.map((v, a) => Math.min(bounds[a][1], v + step[a])),
            };
        }

        /** Look again for a finer view of what the camera sees, once it rests. */
        scheduleDetail() {
            clearTimeout(this._detailTimer);
            if (this.status !== "ready" || !this.gl) return;
            this._detailTimer = setTimeout(() => this.refine(), DETAIL_REST_MS);
        }

        /**
         * Finer where the camera needs it: the atlas's bricks wherever this
         * image is served as bricks (every visible part at the level its
         * distance asks for), else one finer area where the camera looks.
         */
        refine() {
            if (this.moving() || this.status !== "ready" || !this.gl) return;
            const atlas = this.atlas;
            if (atlas?.status === "loading") return;          // load() schedules again
            // A channel still drawn from its last texture (entryFor): finer
            // views of it wait until its own has landed.
            if (this.channels().some((channel) => !this.textureOf(channel))) return;
            if (atlas?.status === "ready") {
                const channels = this.channels();
                const lead = channels.length ? this.textureOf(channels[0]) : null;
                if (this.atlasFits(channels, lead)) {
                    if (this.detail) {
                        this.detail = null;
                        this.invalidate();
                    }
                    this.trackFill(atlas.update(this.atlasView(lead)).catch((error) =>
                        console.error("volumeRenderer: the atlas could not be filled", error)));
                    return;
                }
                // Nothing finer than the base (a small stack), or no room on
                // the GPU: the base is all there is to draw.
                if (lead && lead.level === 0) return;
            }
            this.trackFill(this.considerDetail());
        }

        /** [inspect] Whether what the cut shows has arrived: no level, brick
         *  or detail in flight. Until then the base level is drawn, z-pooled
         *  and coarser -- the dock says so (volumeDock "refining"). */
        resolved() {
            if (this._loads.size || this._filling) return false;
            return !(this.atlas && (this.atlas.status === "loading" || this.atlas.inflight?.size > 0));
        }

        /** Remember a detail fill in flight until it ends (settled()). */
        trackFill(promise) {
            this._filling = promise;
            Promise.resolve(promise).finally(() => {
                if (this._filling === promise) this._filling = null;
            });
        }

        /** The texture z range drawn: the cut's, narrowed by setClip's z. */
        clipRangeZ() {
            const cut = this.cutUniforms();
            return [cut.lo[2], cut.hi[2]];
        }

        /**
         * Clip the box to `{x: [a, b], y: [a, b], z?: [a, b]}` (texture space,
         * [0, 1] along the stack's own axes), or null for none -- inside the
         * cut (cutUniforms), for a caller that cuts the stack open on its own
         * clock (Video Studio's reveals). Not saved, like the camera.
         */
        setClip(clip) {
            const range = (r) => (Array.isArray(r) && r.length === 2
                ? [Math.max(0, Math.min(1, Math.min(r[0], r[1]))), Math.max(0, Math.min(1, Math.max(r[0], r[1])))]
                : [0, 1]);
            const next = clip ? { x: range(clip.x), y: range(clip.y), ...(clip.z ? { z: range(clip.z) } : {}) } : null;
            const whole = next && next.x[0] === 0 && next.x[1] === 1 && next.y[0] === 0 && next.y[1] === 1
                && (!next.z || (next.z[0] === 0 && next.z[1] === 1));
            const value = whole ? null : next;
            if (JSON.stringify(value) === JSON.stringify(this.clipXY || null)) return false;
            this.clipXY = value;
            this.invalidate();
            return true;
        }

        // -- frame by frame (Video Studio) -----------------------------------
        //
        // A caller that renders a video drives the camera itself, one frame at
        // a time on its own clock: it pins the drawing size to the output,
        // sets `camera`, asks for detail at once (`refine`, rather than after
        // the resting delay), waits for `whenSettled()` and takes
        // `renderFrame()`. Nothing here runs unless a caller asks.

        /** Draw at exactly `{width, height}` device pixels from now on (null
         *  goes back to following the canvas's CSS size). */
        pinSize(size) {
            this._pinnedSize = size && size.width > 0 && size.height > 0
                ? { width: Math.round(size.width), height: Math.round(size.height) } : null;
            this.invalidate();
        }

        /** Whether everything the current camera can show has arrived: no
         *  level loading, no atlas bricks in flight, no detail being read. */
        settled() {
            if (this.status !== "ready") return this.status === "error";
            // Not on screen yet: the 3-D view's revision has not committed, so
            // the picture would be whatever was drawn last time it was shown.
            if (!this.canvas || this.canvas.hidden) return false;
            if (this._loads.size) return false;
            // A texture for every channel at its CURRENT window: when a
            // provisional window turns exact the textures reload under the new
            // stamp a moment later, and until then the old one is still drawn.
            if (this.channels().some((channel) => !this.textureOf(channel))) return false;
            const atlas = this.atlas;
            if (atlas && (atlas.status === "loading" || atlas.inflight?.size > 0)) return false;
            // [surfaces] A visible surface's mesh still on its way.
            if (global.PlexoraSurfaceLayers?.loading?.()) return false;
            return !this._filling;
        }

        /** Resolves once `settled()` holds (or the renderer is gone). */
        async whenSettled({ pollMs = 16 } = {}) {
            while (this.gl && !this.settled()) {
                await new Promise((resolve) => setTimeout(resolve, pollMs));
            }
        }

        /** The current camera drawn now, at full quality (never the fast
         *  path), at the pinned size, every channel in full (never partway
         *  in, presence). Stops any motion of its own first. */
        renderFrame() {
            if (!this.canvas) return null;
            this.spin = null;
            this.tween = null;
            this.zoomTarget = null;
            this.setAutoRotate(false);
            this.interacting = false;
            // Always a real draw: never the canvas's last picture returned as
            // if it were this frame's.
            this._drawHidden = true;
            try {
                this.draw(now(), { settled: true });
            } finally {
                this._drawHidden = false;
            }
            return this.canvas;
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
            const request = { region: pixels, t, format, wanted: pixels, id: ++this._detailIds };
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
            global.PlexoraVolumeAtlas?.LINK?.ask(query);
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
            this.evict(new Set([key, ...this.keepKeys()]));
            return { level: entry.level, covered: entry.covered, z: entry.z };
        }

        dropTexture(key) {
            const entry = this.textures.get(key);
            if (!entry) return;
            this.gl?.deleteTexture(entry.texture);
            this.textures.delete(key);
            this.textureBytes -= entry.bytes;
        }

        /** The current picture as a canvas (captures): at the canvas's own
         *  size, or drawn at `{width, height}` device pixels for an export
         *  (copied out, then the canvas goes back to its size). Every channel
         *  in full, never partway in. A plain capture records the pose of a
         *  turning stack and leaves it turning; a sized export stops it. */
        snapshot({ width = null, height = null } = {}) {
            if (!this.canvas) return null;
            if (!(width > 0 && height > 0)) {
                this.draw(now(), { settled: true });
                return this.canvas;
            }
            // A video's pinned size (pinSize) comes back afterwards.
            const pinned = this._pinnedSize;
            this._pinnedSize = { width: Math.round(width), height: Math.round(height) };
            try {
                this.spin = null;
                this.tween = null;
                this.zoomTarget = null;
                this.setAutoRotate(false);
                this.interacting = false;
                this.draw(now(), { settled: true });
                const copy = document.createElement("canvas");
                copy.width = this.canvas.width;
                copy.height = this.canvas.height;
                copy.getContext("2d").drawImage(this.canvas, 0, 0);
                return copy;
            } finally {
                this._pinnedSize = pinned || null;
                this.invalidate();
            }
        }

        /** The camera as the view state stores it (volume_routes). */
        cameraState() {
            const c = this.camera;
            return { q: c.q.slice(), dist: c.dist, pan: [c.panX, c.panY] };
        }

        /** Put a stored camera back (`{q, dist, pan}`); false when unusable. */
        restoreCamera(stored) {
            const q = stored?.q;
            if (!Array.isArray(q) || q.length !== 4 || !q.every(Number.isFinite)) return false;
            this.setAutoRotate(false);
            this.camera.q = q.slice();
            if (Number.isFinite(stored.dist)) this.camera.dist = Math.max(MIN_DIST, Math.min(8, stored.dist));
            if (Array.isArray(stored.pan) && stored.pan.every(Number.isFinite)) {
                [this.camera.panX, this.camera.panY] = stored.pan;
            }
            this.invalidate();
            return true;
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
        let lastCut = "";
        // [surfaces] The camera, remembered per project like the plane
        // (`volume.camera` of the view state), once it has rested a moment.
        let savedCamera = "";
        let cameraTimer = 0;
        const rememberCamera = () => {
            clearTimeout(cameraTimer);
            cameraTimer = setTimeout(() => {
                if (!renderer || !viewState.isVolume()) return;
                const camera = renderer.cameraState();
                const text = JSON.stringify(camera);
                if (text === savedCamera) return;
                savedCamera = text;
                viewState.set({ volume: { ...(viewState.get().volume || {}), camera } });
            }, 800);
        };
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
            /** The 3-D view's surfaces (views/surfaceRenderer.js), once it exists. */
            get surfaces() { return renderer?.surfaces || null; },
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
                    // [surfaces] Back where the camera was, and say the view
                    // exists (services/surfaceLayers.js loads meshes into it).
                    if (renderer.restoreCamera(state.volume?.camera)) {
                        savedCamera = JSON.stringify(renderer.cameraState());
                    }
                    renderer.onRest = rememberCamera;
                    global.dispatchEvent?.(new CustomEvent("plexora:volume-mounted",
                        { detail: { renderer } }));
                }
                renderer.surfaces?.setShow(state.volume?.show || {});
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
                // A new cut: contrast for the planes it keeps.
                const clip = String(renderer.clipRange());
                if (clip !== lastClip) {
                    lastClip = clip;
                    if (!entering) renderer.refreshHints();
                }
                lastCut = JSON.stringify(renderer.cutUniforms());
            },
            /** [inspect] The cut changed -- the dock, the strip, a handle, an
             *  undo, the agent: drawn again from uniforms in the next frame
             *  (no revision), a finer detail looked for once it rests, and the
             *  contrast asked again for the planes it keeps. */
            cutChanged() {
                if (!renderer || !viewState.isVolume()) return;
                const key = JSON.stringify(renderer.cutUniforms());
                if (key === lastCut) return;
                lastCut = key;
                renderer.invalidate();
                renderer.scheduleDetail();
                const clip = String(renderer.clipRange());
                if (clip !== lastClip) {
                    lastClip = clip;
                    renderer.refreshHints();
                }
            },
            channelsChanged() {
                if (renderer && viewState.isVolume()) renderer.refresh();
            },
            /** Play in 3-D: turn the stack (`{speed, axis, direction}`,
             *  services/viewerPrefs.js `rotate`), or stop. */
            rotate(on, options = {}) {
                if (!renderer || !viewState.isVolume()) return false;
                renderer.setAutoRotate(Boolean(on), options);
                return Boolean(renderer.autoRotate);
            },
            get rotating() { return Boolean(renderer?.autoRotate); },
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
                if (renderer.autoRotate) renderer.setAutoRotate(false);
                else if (renderer.lock) renderer.setLock(null);
                return;
            }
            const key = String(event.key || "").toLowerCase();
            if ((key === "x" || key === "y" || key === "z") && renderer.engaged()) {
                event.preventDefault();
                event.stopPropagation();
                if (!event.repeat) renderer.setLock(key);
            } else if (key === "s" && renderer.engaged()) {
                // [surfaces] S opens or closes the Quick Surface dock.
                event.preventDefault();
                event.stopPropagation();
                if (!event.repeat) global.dispatchEvent?.(new CustomEvent("plexora:surface-studio-toggle"));
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
        // [inspect] Whoever changes the cut, and a preview under a hand.
        viewState.subscribe(() => controller.cutChanged());
        global.addEventListener?.("plexora:inspect-preview", () => controller.cutChanged());
        global.addEventListener?.("plexora:inspect-changed", () => controller.cutChanged());
        controller.sync(viewState.get());
        return controller;
    }

    VolumeRenderer.attach = attach;
    VolumeRenderer.releaseSpeed = releaseSpeed;
    VolumeRenderer.MOTION = { SPIN_TAU_MS, RELEASE_MIN, SPIN_STOP, SPIN_MAX, ZOOM_TAU_MS,
                              TURN_MS, VELOCITY_WINDOW_MS, RELEASE_STILL_MS,
                              ROTATE_DEFAULT_DPS, ROTATE_MIN_DPS, ROTATE_MAX_DPS };
    VolumeRenderer.fragmentSource = fragmentSource;
    VolumeRenderer.mat4 = { multiply: mat4Multiply, invert: mat4Invert,
                            perspective: mat4Perspective, lookAt: mat4LookAt };
    global.PlexoraVolumeRenderer = VolumeRenderer;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = VolumeRenderer;
    }
})(typeof window !== "undefined" ? window : globalThis);
