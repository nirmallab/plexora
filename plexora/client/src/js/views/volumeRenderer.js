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
 * the stack: a cutaway or a crop, over any of X, Y and Z. Both are uniforms
 * -- the box a ray is clipped to, the corner a cutaway takes out, whether
 * the cut faces are drawn solid -- read at every frame from
 * services/inspectState.js, so a cut follows the hand with no texture,
 * program or revision behind it.
 *
 * THE GRID. An optional reference grid (the depth popover's Grid row; a per-user
 * preference, services/viewerPrefs.js `grid`) is drawn by the ray caster
 * itself, not as geometry: each ray that crosses the box finds the face it
 * leaves by and draws lines there, at round values counted from the image's
 * origin. Those faces are the far walls, so the grid is always behind the
 * tissue, turns with it, and follows the slab band and setClip as the box
 * does; bright tissue covers it, and so does an opaque surface. Lines are in
 * microns only where the spacing is stated (metadata or typed): otherwise x
 * and y count pixels and z planes, and the numbers say so. Their numbers are
 * a 2-D canvas over this one, laid out each frame drawn from the same camera,
 * and baked into snapshot() -- not into renderFrame(), whose caller (Video
 * Studio) draws its own axes. With "Surfaces only" there is no volume pass
 * and so no grid.
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
    //: How close the camera may come (the stack's longest side is 1): near
    //: enough that a square placed on a zoomed-in plane opens at the plane's
    //: magnification (frameRegion); a detail brings the finer levels in.
    const MIN_DIST = 0.03;
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
    //: ... or when, of MISSED_WINDOW moving frames, MISSED_FRAMES came later
    //: than 1.5 display periods (the shortest interval seen): on a 120 Hz
    //: screen a full-step MIP coast could average under SLOW_FRAME_MS and
    //: still drop one frame in five.
    const MISSED_WINDOW = 12;
    const MISSED_FRAMES = 2;
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

    //: The fewest milliseconds between two moves of a slid area (a fetch each).
    const SLIDE_MS = 40;

    /** Image units moved along each axis for a pointer travel `(dx, dy)`:
     *  along one axis its projection, along two the pair whose screen steps
     *  add up to the travel (views/volumeHandles.js measures a grip's the
     *  same way). */
    function travel(axes, dx, dy) {
        if (axes.length === 1) {
            const { step } = axes[0];
            const length = step.x * step.x + step.y * step.y;
            return { [axes[0].axis]: length > 1e-9 ? (dx * step.x + dy * step.y) / length : 0 };
        }
        if (!axes.length) return {};
        const [a, b] = axes;
        const det = a.step.x * b.step.y - a.step.y * b.step.x;
        if (Math.abs(det) < 1e-6) return travel([a], dx, dy);
        return { [a.axis]: (dx * b.step.y - dy * b.step.x) / det,
                 [b.axis]: (a.step.x * dy - a.step.y * dx) / det };
    }

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
                sample.push(`        v[${c}] = inDetail ? texture(u_detail${c}, q).r : baseRead(u_volume${c}, p);`);
            } else {
                sample.push(`        v[${c}] = baseRead(u_volume${c}, p);`);
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
uniform vec3 u_tex_lo;         // where the box's [0,1] lies in the base textures
uniform vec3 u_tex_span;       // (the area framed, against what they cover)
uniform int u_cut;             // 0 nothing more, 1 the cut faces drawn solid
uniform vec3 u_color[NCH];
uniform vec2 u_window[NCH];    // [lo, hi] in 0..1 texture units
uniform float u_cofactor[NCH]; // the asinh cofactor in the same units, 0 linear
uniform float u_opacity[NCH];
uniform int u_mode;            // 0 MIP, 1 additive
uniform int u_steps;
uniform vec3 u_ground;
uniform float u_grid;          // the grid's opacity, 0 for none
uniform int u_grid_faces;      // 0 every far face, 1 the far z face only
uniform vec3 u_grid_scale;     // grid lines per box unit, per axis
uniform vec3 u_grid_offset;    // grid lines from the origin to the box's centre
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

float level(float value, vec2 window, float c) {
    float span = max(window.y - window.x, 1e-4);
    if (c > 0.0) {
        // The display curve (services/displayCurve.js): asinh above c.
        return clamp(asinh((value - window.x) / c) / asinh(span / c), 0.0, 1.0);
    }
    return clamp((value - window.x) / span, 0.0, 1.0);
}

// A base texture at box point p. The box is the area framed; the texture may
// cover a little more (its level's grid) or, while the area's own loads,
// another area (entryFor): read where p lies in it, nothing outside it.
float baseRead(sampler3D tex, vec3 p) {
    vec3 s = u_tex_lo + p * u_tex_span;
    float inside = step(0.0, s.x) * step(s.x, 1.0) * step(0.0, s.y) * step(s.y, 1.0);
    return texture(tex, s).r * inside;
}
${cut}

// The reference grid, on the face each ray leaves the box by -- the far
// walls only, so it is always behind the tissue -- as premultiplied light.
// Lines are a pixel wide at any zoom (fwidth), fade where they would crowd
// closer than a few pixels, and the faces' own edges are drawn stronger so
// the box reads. exitT is where the ray leaves the box.
vec4 gridBehind(vec3 origin, vec3 dir, vec3 lo, vec3 hi, out float exitT) {
    vec3 inv = 1.0 / dir;
    vec3 t0 = (lo - origin) * inv;
    vec3 t1 = (hi - origin) * inv;
    vec3 tmax = max(t0, t1);
    vec3 tmin = min(t0, t1);
    float tIn = max(max(tmin.x, tmin.y), tmin.z);
    exitT = min(min(tmax.x, tmax.y), tmax.z);
    vec3 p = origin + dir * exitT;
    vec3 f = u_grid_offset + p * u_grid_scale;
    // Derivatives before any branch: neighbouring pixels may leave by
    // another face, or miss the box.
    vec3 wf = max(fwidth(f), vec3(1e-6));
    vec3 wp = max(fwidth(p), vec3(1e-6));
    if (tIn > exitT || exitT < 0.0) return vec4(0.0);
    vec3 across = vec3(equal(tmax, vec3(exitT)));
    if (u_grid_faces == 1 && across.z < 0.5) return vec4(0.0);
    vec3 inFace = 1.0 - across;
    vec3 lines = (1.0 - smoothstep(0.5, 1.5, abs(fract(f + 0.5) - 0.5) / wf))
        * (1.0 - smoothstep(0.15, 0.4, wf)) * inFace;
    vec3 edges = (1.0 - smoothstep(0.75, 1.75, min(p - lo, hi - p) / wp)) * inFace;
    float a = max(max(max(lines.x, lines.y), lines.z) * u_grid,
                  max(max(edges.x, edges.y), edges.z) * min(1.0, u_grid * 2.2));
    return vec4(vec3(0.75, 0.8, 0.88) * a, a);
}

void main() {
    vec4 nearPoint = u_inverse * vec4(v_ndc, -1.0, 1.0);
    vec4 farPoint = u_inverse * vec4(v_ndc, 1.0, 1.0);
    vec3 origin = nearPoint.xyz / nearPoint.w;
    vec3 dir = normalize(farPoint.xyz / farPoint.w - origin);
    vec3 lo = u_box_lo * u_extent - 0.5 * u_extent;
    vec3 hi = u_box_hi * u_extent - 0.5 * u_extent;
    float gridExit = 0.0;
    vec4 grid = u_grid > 0.0 ? gridBehind(origin, dir, lo, hi, gridExit) : vec4(0.0);
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
${atlas ? ATLAS_MARCH(chunk.sample) : `    for (int i = 0; i < 4096; i++) {
        if (i >= count) break;
        float t = t0 + (float(i) + jitter) * stepLength;
        vec3 world = origin + dir * t;
        vec3 p = world / u_extent + 0.5;${locate}
${sample.join("\n")}`}
        if (u_mode == 0) {
            for (int c = 0; c < NCH; c++) peak[c] = max(peak[c], v[c]);
        } else {
            vec3 emit = vec3(0.0);
            float a = 0.0;
            for (int c = 0; c < NCH; c++) {
                float s = level(v[c], u_window[c], u_cofactor[c]);
                emit += u_color[c] * s;
                a = max(a, s * u_opacity[c]);
            }
            a = 1.0 - pow(1.0 - clamp(a, 0.0, 0.999), stepLength * 120.0);
            colour += (1.0 - alpha) * emit * a;
            alpha += (1.0 - alpha) * a;
            if (alpha > 0.97) break;
        }${atlas ? "\n        t += stepLength;" : ""}
    }
    // [inspect] The cut face the ray meets, drawn solid.
    float cutT = 1e20;
    vec4 cut = vec4(0.0);
    if (u_cut == 1) cut = cutFace(origin, dir, lo, hi, t0, hit.y, cutT);
    if (u_mode == 0) {
        for (int c = 0; c < NCH; c++) colour += u_color[c] * level(peak[c], u_window[c], u_cofactor[c]);
        outColor = vec4(max(min(colour, vec3(1.0)), u_ground), 1.0);
    } else {
        outColor = vec4(min(colour + (1.0 - alpha) * u_ground, vec3(1.0)), 1.0);
    }
${GRID_UNDER}
    outColor.rgb = min(cut.rgb + (1.0 - cut.a) * outColor.rgb, vec3(1.0));
}`;
    }

    /** The grid under the picture: bright tissue covers it (MIP), and what
     *  the rays have gathered hides it (additive). surfaceRenderer.js swaps
     *  this line for its own (behind the back part, never past a surface). */
    const GRID_UNDER = `    outColor.rgb = min(outColor.rgb + grid.rgb * (u_mode == 0
        ? 1.0 - clamp(2.0 * max(max(outColor.r, outColor.g), outColor.b), 0.0, 1.0)
        : 1.0 - alpha), vec3(1.0));`;

    /**
     * [inspect] What a cut draws beyond the box (services/inspectGeometry.js):
     * a point sampled anywhere in the stack (`sampleAt`, the same textures
     * the march reads, bricks included), and from it the cut faces, drawn
     * solid.
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

vec3 cutColour(float v[NCH]) {
    vec3 c = vec3(0.0);
    for (int i = 0; i < NCH; i++) c += u_color[i] * level(v[i], u_window[i], u_cofactor[i]);
    return c;
}

// Where the ray first meets kept voxels across a cut -- a cropped or cut
// side of the box, not the area's own -- the voxel there, drawn solid.
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
    if (tFace < 0.0 || tFace > tFar) return vec4(0.0);
    tFirst = tFace;
    float v[NCH];
    Brick brick = noBrick();
    sampleAt((origin + dir * (tFace + 1e-4)) / u_extent + 0.5, brick, v);
    return vec4(cutColour(v), 1.0);
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
        if (t > hit.y) break;
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

    // -- the reference grid -------------------------------------------------
    //
    // Pure helpers, so the probe can test them without a GL context: what the
    // grid's lines are in each axis's units, where they fall in the box, and
    // where their numbers go on screen. The shader half is `gridBehind`.

    //: The grid as the user leaves it (services/viewerPrefs.js `grid`).
    const GRID_DEFAULTS = { on: false, faces: "walls", step: "auto", opacity: 0.18, labels: true };
    //: Sources a length may be printed from (server/volume/axes.py
    //: TRUSTED_SOURCES): "assumed" is a ratio for display, never a micron.
    const TRUSTED_SPACING = ["metadata", "manual"];
    //: How many microns one of each length unit is (axes.py `_MICRONS`).
    const MICRONS = { "µm": 1, "μm": 1, um: 1, nm: 1e-3, mm: 1e3 };
    //: An automatic step gives about this many lines across the longest side;
    //: a chosen one is never finer than MAX_GRID_LINES across it.
    const GRID_LINES = 6;
    const MAX_GRID_LINES = 40;
    //: Tick numbers: at most this many per axis, this far off their edge (CSS
    //: px), and an edge shorter than MIN_LABEL_EDGE on screen gets none.
    const MAX_LABELS = 12;
    const LABEL_OFFSET = 9;
    const TITLE_OFFSET = 24;
    const MIN_LABEL_EDGE = 40;

    /** The grid preference, repaired: anything that does not parse reads as
     *  its default (the server's `normalize_viewer_prefs` says the same). */
    function gridPrefs(value) {
        const v = value && typeof value === "object" ? value : {};
        const step = Number(v.step);
        const opacity = Number(v.opacity);
        return {
            on: typeof v.on === "boolean" ? v.on : GRID_DEFAULTS.on,
            faces: v.faces === "floor" ? "floor" : "walls",
            step: v.step !== "auto" && typeof v.step !== "boolean" && Number.isFinite(step) && step > 0
                ? Math.min(1e6, Math.max(1e-3, step)) : "auto",
            opacity: v.opacity !== null && typeof v.opacity !== "boolean" && Number.isFinite(opacity)
                ? Math.min(1, Math.max(0.05, opacity)) : GRID_DEFAULTS.opacity,
            labels: typeof v.labels === "boolean" ? v.labels : GRID_DEFAULTS.labels,
        };
    }

    /** The 1-2-5 x 10^n value nearest `value` from above (`up`) or below. */
    function niceAt(value, { up = true } = {}) {
        if (!(value > 0)) return 1;
        const power = 10 ** Math.floor(Math.log10(value));
        const steps = [1, 2, 5, 10].map((m) => m * power);
        if (up) return steps.find((s) => s >= value * (1 - 1e-9)) || 10 * power;
        return [...steps].reverse().find((s) => s <= value * (1 + 1e-9)) || power;
    }

    /** The 1-2-5 step that puts closest to `lines` lines across `span`. */
    function niceStep(span, lines = GRID_LINES) {
        if (!(span > 0)) return 1;
        const target = span / lines;
        const below = niceAt(target, { up: false });
        const above = niceAt(target, { up: true });
        return Math.abs(Math.log(target / below)) <= Math.abs(Math.log(above / target)) ? below : above;
    }

    /** The next 1-2-5 step finer (`-1`) or coarser (`+1`) than `step`. */
    function niceNeighbour(step, direction) {
        return direction > 0 ? niceAt(step * 1.01, { up: true }) : niceAt(step * 0.99, { up: false });
    }

    /** A spacing axis's length per pixel or plane in microns, or null when it
     *  is not one a length may be printed from. */
    function micronsOf(axis) {
        const value = Number(axis?.value);
        const unit = MICRONS[String(axis?.unit || "µm").trim()];
        if (!TRUSTED_SPACING.includes(axis?.source) || !(value > 0) || !unit) return null;
        return value * unit;
    }

    /**
     * What the grid's lines are along each image axis (x, y, z), for a box of
     * `extent` (the renderer's, longest side 1) over full-resolution pixels
     * `covered` = [x0, y0, x1, y1] and `planes` planes:
     *
     *     units   "µm" where the spacing is stated, "px" for x and y without
     *             one, "planes" for z without one (or without x and y's)
     *     centre  the axis's value at the box's centre
     *     per     its value per box unit
     *     step    the distance between lines, in the axis's units
     *     auto    the automatic step for x and y
     *
     * Every axis is measured against the box as it is drawn -- y by x's
     * spacing, as the box is -- so the lines fall where the numbers say.
     * Values count from the image's origin; z from the first plane's centre.
     */
    function gridAxes({ spacing = {}, covered, planes, extent, step = "auto" } = {}) {
        const umX = micronsOf(spacing?.x);
        const umZ = umX ? micronsOf(spacing?.z) : null;
        const perPx = umX || 1;
        const perPlane = umZ || 1;
        const [x0, y0, x1, y1] = covered;
        const depth = Math.max(1, planes || 1);
        const spans = [(x1 - x0) * perPx, (y1 - y0) * perPx, depth * perPlane];
        const centre = [(x0 + x1) / 2 * perPx, (y0 + y1) / 2 * perPx, (depth / 2 - 0.5) * perPlane];
        const per = spans.map((span, i) => span / Math.max(1e-9, extent[i]));
        const units = [umX ? "µm" : "px", umX ? "µm" : "px", umZ ? "µm" : "planes"];
        // One step for every axis in the same unit, so a square is square.
        const shared = umZ ? Math.max(...spans) : Math.max(spans[0], spans[1]);
        const auto = niceStep(shared);
        const chosen = Number.isFinite(step) && step > 0 ? step : auto;
        const xy = Math.max(chosen, niceAt(shared / MAX_GRID_LINES));
        const z = umZ ? xy : Math.max(1, niceStep(depth));
        return { units, centre, per, step: [xy, xy, z], auto, spans };
    }

    /** A tick's number as it is printed: no trailing zeros, no float dust. */
    function formatTick(value) {
        const rounded = Math.abs(value) < 1e-9 ? 0 : Number(value.toPrecision(6));
        return String(rounded);
    }

    /** `point` (box space) on a `width` x `height` screen through `forward`
     *  (clip from box), or null behind the camera. */
    function project(forward, point, width, height) {
        const [x, y, z] = point;
        const m = forward;
        const w = m[3] * x + m[7] * y + m[11] * z + m[15];
        if (!(w > 1e-6)) return null;
        const cx = (m[0] * x + m[4] * y + m[8] * z + m[12]) / w;
        const cy = (m[1] * x + m[5] * y + m[9] * z + m[13]) / w;
        return { x: (cx + 1) / 2 * width, y: (1 - cy) / 2 * height };
    }

    /**
     * The grid's numbers on screen: along one edge of the box per axis, the
     * edge the camera sees the box's outline by (one of its faces turned
     * towards the camera, the other away) and lowest-leftmost of those, so
     * the numbers stand outside the box and never over the tissue. Each tick
     * at a line of `axes.step`; one that would crowd the last, or overlap a
     * name or a number already placed (two axes meet at a corner), is left
     * out -- the names are placed first.
     * `[{x, y, text, align, baseline, title}]` in the screen's pixels, the
     * axis's name (`title`) beside its edge's middle; `scale` multiplies the
     * offsets (a capture larger than the screen).
     * `floor`: only the far z face has lines (u_grid_faces 1), so X and Y are
     * numbered along that face's own edges, whether or not they are on the
     * outline, and Z, which has no lines there, is not numbered at all.
     */
    function gridLabels({ forward, eye, lo, hi, axes, width, height, scale = 1, floor = false }) {
        const out = [];
        const centre = project(forward, lo.map((v, i) => (v + hi[i]) / 2), width, height);
        if (!centre) return out;
        const facing = (axis, high) => (high ? eye[axis] > hi[axis] : eye[axis] < lo[axis]);
        //: The far z face, the floor: the one the eye is further from.
        const floorHigh = eye[2] < (lo[2] + hi[2]) / 2;
        const names = ["X", "Y", "Z"];
        //: What is placed so far, as boxes, for the overlap test.
        const placed = [];
        const boxOf = (label, size) => {
            const w = (label.text.length * 6.2 + 4) * scale;
            const h = size * scale;
            const x = label.align === "left" ? label.x : label.align === "right" ? label.x - w : label.x - w / 2;
            const y = label.baseline === "top" ? label.y : label.baseline === "bottom" ? label.y - h : label.y - h / 2;
            return { x0: x, y0: y, x1: x + w, y1: y + h };
        };
        const place = (label, size) => {
            const box = boxOf(label, size);
            if (placed.some((o) => box.x0 < o.x1 && o.x0 < box.x1 && box.y0 < o.y1 && o.y0 < box.y1)) {
                return false;
            }
            placed.push(box);
            out.push(label);
            return true;
        };
        //: Ticks wait for every axis's name to be placed.
        const ticks = [];
        for (let a = 0; a < 3; a += 1) {
            if (floor && a === 2) continue;
            const [b, c] = [0, 1, 2].filter((i) => i !== a);
            let best = null;
            for (const highB of [false, true]) {
                for (const highC of [false, true]) {
                    // An outline edge: one face beside it towards the camera.
                    const outline = facing(b, highB) !== facing(c, highC);
                    if (floor) {
                        // An edge of the floor (c is z for both X and Y), on
                        // the outline when it can be, so the numbers stay off
                        // the tissue.
                        if (highC !== floorHigh) continue;
                    } else if (!outline) {
                        continue;
                    }
                    const at = (value) => {
                        const p = [0, 0, 0];
                        p[a] = value;
                        p[b] = highB ? hi[b] : lo[b];
                        p[c] = highC ? hi[c] : lo[c];
                        return p;
                    };
                    const start = project(forward, at(lo[a]), width, height);
                    const end = project(forward, at(hi[a]), width, height);
                    if (!start || !end) continue;
                    const length = Math.hypot(end.x - start.x, end.y - start.y);
                    if (length < MIN_LABEL_EDGE * scale) continue;
                    const mid = { x: (start.x + end.x) / 2, y: (start.y + end.y) / 2 };
                    // Low on the screen first, then left.
                    const score = (mid.y - centre.y) - 0.35 * (mid.x - centre.x)
                        + (floor && outline ? 1e6 : 0);
                    if (!best || score > best.score) best = { score, at, start, end, mid, length };
                }
            }
            if (!best) continue;
            // Away from the box, square to the edge.
            const tx = (best.end.x - best.start.x) / best.length;
            const ty = (best.end.y - best.start.y) / best.length;
            let nx = -ty;
            let ny = tx;
            if (nx * (best.mid.x - centre.x) + ny * (best.mid.y - centre.y) < 0) {
                nx = -nx;
                ny = -ny;
            }
            const align = nx > 0.4 ? "left" : nx < -0.4 ? "right" : "center";
            const baseline = ny > 0.4 ? "top" : ny < -0.4 ? "bottom" : "middle";
            const step = axes.step[a];
            const value = (p) => axes.centre[a] + p * axes.per[a];
            const vLo = value(lo[a]);
            const vHi = value(hi[a]);
            let first = Math.ceil((vLo - 1e-9 * Math.abs(vLo)) / step);
            const last = Math.floor((vHi + 1e-9 * Math.abs(vHi)) / step);
            // Never more than MAX_LABELS: every second (fifth...) line then.
            let stride = 1;
            while ((last - first) / stride + 1 > MAX_LABELS) stride = niceAt(stride * 1.01);
            first = Math.ceil(first / stride) * stride;
            let previous = null;
            for (let k = first; k <= last; k += stride) {
                const tick = k * step;
                const point = project(forward, best.at((tick - axes.centre[a]) / axes.per[a]), width, height);
                if (!point) continue;
                const text = formatTick(tick);
                // Room for the text along the edge before the next one.
                const room = (text.length * 6.2 + 10) * scale;
                if (previous && Math.hypot(point.x - previous.x, point.y - previous.y) < room) continue;
                const x = point.x + nx * LABEL_OFFSET * scale;
                const y = point.y + ny * LABEL_OFFSET * scale;
                if (x < 0 || y < 0 || x > width || y > height) continue;
                ticks.push({ x, y, text, align, baseline, title: false });
                previous = point;
            }
            place({ x: best.mid.x + nx * TITLE_OFFSET * scale, y: best.mid.y + ny * TITLE_OFFSET * scale,
                    text: `${names[a]} (${axes.units[a]})`, align, baseline, title: true }, 13);
        }
        for (const tick of ticks) place(tick, 12);
        return out;
    }

    class VolumeRenderer {
        constructor({ viewState, datasource, wrapper = null, fetchImpl = null,
                      budget = DEFAULT_BUDGET, sidebar = null, windows = null } = {}) {
            this.viewState = viewState;
            this.datasource = datasource;
            //: The channel widget whose slots are drawn, and the per-stack
            //: windows their levels are made with: the viewer's own unless
            //: handed others (Figure Builder's Quick Edit runs a scoped
            //: sidebar of its own beside a live 3-D view).
            this.sidebarFor = () => sidebar || global.__plexora?.viewerSidebar;
            this.windowsFor = () => windows || global.__plexora?.volumeWindows;
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
            this._fastFrames = { last: 0, average: 0, count: 0, window: 0, missed: 0, period: 0 };
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
            // The grid's numbers: a 2-D canvas over this one, never a click.
            this.gridCanvas = document.createElement("canvas");
            this.gridCanvas.className = "viewer-volume-grid-labels";
            this.gridCanvas.setAttribute("aria-hidden", "true");
            this.gridCanvas.hidden = true;
            this.wrapper.appendChild(this.gridCanvas);
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
            this.gridCanvas?.remove();
            this.gridCanvas = null;
            this.note?.remove();
            this.note = null;
            this.gizmo?.root.remove();
            this.gizmo = null;
            this.canvas = null;
            this.gl = null;
        }

        show(visible) {
            if (this.canvas) this.canvas.hidden = !visible;
            if (this.gridCanvas && !visible) this.gridCanvas.hidden = true;
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
            // [inspect] The cut panel and the handles are part of the view.
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
            this.turnTo(this.centred(C.viewAlong(name, { reverse: reverse || already })));
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
            const sidebar = this.sidebarFor();
            const windows = this.windowsFor();
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
                const held = windows?.peek?.(fullName);
                const stamp = held ? windows.stamp(fullName)
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
                    // The slot's display curve, raw units (cofactorFor).
                    curve: slot.curve || null,
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
         * while that loads -- the one it was drawn from last. A window turning
         * exact, a frame step, HD on or off, the area slid: the picture keeps
         * the channel until its new texture can replace it, never a channel
         * gone black and faded back in. Another area's texture is read where
         * the new area overlaps it (the frame is the area's, `frameCovered`;
         * `baseRead` in the shader), so a slide never shows a blank frame.
         * Drawing only: `settled()`, `refine()` and the loads ask for the
         * exact texture.
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
            if (!shown) return null;
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
        /** A channel's asinh cofactor in the units of the texture it is
         *  drawn from (raw over the window it was made with); 0 linear. */
        cofactorFor(channel) {
            const made = channel.entry?.levelWindow || channel.levelWindow;
            const curves = global.PlexoraDisplayCurve;
            if (!channel.curve || !made || !curves) return 0;
            return curves.normalize(channel.curve, Math.max(1e-6, made[1] - made[0]));
        }

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
            // it is drawn from, when its bytes are the same kind and it is of
            // this area -- or of one the same size (a slide): same size, same
            // level, so the atlas made for it stays.
            const held = channels.map((channel) => this.textureOf(channel)
                || this.textures.get(this._shownKey.get(channel.name))).find((entry) => entry
                    && entry.format === this.format()
                    && (entry.box === this.boxKey() || this.sameSize(entry)));
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

        /** Whether texture `entry` covers an area the size of the one drawn
         *  now (to within one of its own voxels): another place, same level. */
        sameSize(entry) {
            const area = this.areaCovered();
            const c = entry?.covered;
            const shape = entry?.shape;
            if (!area || !c || !shape) return false;
            const vx = (c[2] - c[0]) / Math.max(1, shape[2]);
            const vy = (c[3] - c[1]) / Math.max(1, shape[1]);
            return Math.abs((c[2] - c[0]) - (area[2] - area[0])) <= vx + 1e-6
                && Math.abs((c[3] - c[1]) - (area[3] - area[1])) <= vy + 1e-6;
        }

        /** The stack's own extent (inspectGeometry.dims), with no area. */
        plainDims() {
            const G = global.PlexoraInspectGeometry;
            const vs = this.viewState;
            if (G) return G.dims(vs?.info || {}, vs?.depth);
            return { width: Number(vs?.info?.width) || 1, height: Number(vs?.info?.height) || 1,
                     depth: Math.max(1, vs?.depth || 1), area: null };
        }

        /**
         * Draw `area` (`{x, y, width, height}` full-resolution pixels, the
         * view state's `volume.area`) or, with null, the whole stack. Its
         * textures are fetched (`refresh`, unless `refresh: false`); until
         * they land the last ones stand in (entryFor). Under a hand, while
         * two loads a channel are still in flight, the fetch waits for the
         * next move or the release: a fast slide asks for where it is, not
         * for every place it passed. True when it changed.
         */
        setArea(area, { refresh = true } = {}) {
            const G = global.PlexoraInspectGeometry;
            const next = G ? G.cleanArea(area, this.plainDims()) : (area || null);
            const before = this.boxKey();
            this.box = next;
            const changed = this.boxKey() !== before;
            if (changed) this.invalidate();
            if (!refresh || (!changed && !this._areaStale)) return changed;
            const busy = this._loads.size >= 2 * Math.max(1, this.channels().length);
            if (this.interacting && busy) {
                this._areaStale = true;
            } else {
                this._areaStale = false;
                this.refresh();
            }
            return changed;
        }

        /** `[x0, y0, x1, y1]` the area covers, as the server rounds it, or
         *  null for the whole stack (inspectGeometry.areaCovered). */
        areaCovered() {
            const G = global.PlexoraInspectGeometry;
            if (!this.box || !G) return null;
            return G.areaCovered(this.box, this.plainDims());
        }

        /**
         * `[x0, y0, x1, y1]` the view FRAMES: the area, else what the drawn
         * textures cover (`lead`'s, else the last drawn). The box, the cut,
         * the grid, the handles and the surfaces are all measured on it.
         */
        frameCovered(lead = null) {
            return this.areaCovered() || lead?.covered || this.covered || null;
        }

        /** Where the frame's [0,1] lies in texture `lead`'s: `{lo, span}`
         *  vec3s for `u_tex_lo` / `u_tex_span` (identity when it covers the
         *  frame exactly, as a level-0 texture of the area does). */
        texTransform(lead) {
            const frame = this.frameCovered(lead);
            const c = lead?.covered;
            if (!frame || !c) return { lo: [0, 0, 0], span: [1, 1, 1] };
            const sx = Math.max(1e-9, c[2] - c[0]);
            const sy = Math.max(1e-9, c[3] - c[1]);
            return { lo: [(frame[0] - c[0]) / sx, (frame[1] - c[1]) / sy, 0],
                     span: [(frame[2] - frame[0]) / sx, (frame[3] - frame[1]) / sy, 1] };
        }

        /** The area the view draws: `{x, y, width, height}` in full-resolution
         *  pixels, or null for the whole plane. */
        boxKey() {
            const box = this.box;
            return box ? [box.x, box.y, box.width, box.height].map(Math.round).join(",") : "";
        }

        /** `f16` while HD tiles are on (16-bit data, half-float textures), else `u8`. */
        format() {
            return this.sidebarFor()?.isHdMode?.() ? "f16" : "u8";
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
                this.level = Number(response.headers.get("X-Plexora-Level") || 0);
                this.shape = [depth, height, width];
                this.invalidate();
            })().finally(() => this._loads.delete(key));
            this._loads.set(key, load);
            return load;
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
            return G.resolve(state.inspect, G.dims(this.viewState.info, this.viewState.depth,
                                                   state.volume?.area || null));
        }

        /**
         * The cut in the frame's space (`covered`, frameCovered), narrowed
         * by `setClip` (Video Studio's reveals): `{lo, hi, cut, face}`.
         */
        cutUniforms(covered = this.frameCovered()) {
            const G = global.PlexoraInspectGeometry;
            const info = this.viewState?.info || {};
            const area = covered || [0, 0, Number(info.width) || 1, Number(info.height) || 1];
            const depth = Math.max(1, this.viewState?.depth || 1);
            const resolved = G ? this.inspectNow() : null;
            const out = resolved ? G.toTexture(resolved, area, depth)
                : { lo: [0, 0, 0], hi: [1, 1, 1], cut: 0, face: 0 };
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
        bindCut(uniform, cut) {
            const gl = this.gl;
            gl.uniform3fv(uniform("u_box_lo"), cut.lo);
            gl.uniform3fv(uniform("u_box_hi"), cut.hi);
            gl.uniform1i(uniform("u_cut"), cut.cut);
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
                this.turnAboutPivot(axis === "screen"
                    ? C.quat.normalize(C.quat.multiply(C.quat.axisAngle([0, 1, 0], angle), camera.q))
                    : C.turnAbout(camera.q, axis, angle));
            }
            if (this.spin) {
                // Exponential decay, integrated exactly over dt: the same
                // coast whatever the frame rate.
                const decay = Math.exp(-dt / SPIN_TAU_MS);
                const travel = SPIN_TAU_MS * (1 - decay);
                const C = Camera();
                const spin = this.spin;
                if (spin.axis) {
                    this.turnAboutPivot(C.turnAbout(camera.q, spin.axis, spin.rate * travel));
                    spin.rate *= decay;
                    if (Math.abs(spin.rate) < SPIN_STOP) this.spin = null;
                } else {
                    const [wx, wy] = spin.screen;
                    const rate = Math.hypot(wx, wy);
                    this.turnAboutPivot(C.quat.normalize(C.quat.multiply(
                        C.quat.axisAngle([wx, wy, 0], rate * travel), camera.q)));
                    spin.screen = [wx * decay, wy * decay];
                    if (rate * decay < SPIN_STOP) this.spin = null;
                }
            }
            if (this.zoomTarget !== null) {
                const k = 1 - Math.exp(-dt / ZOOM_TAU_MS);
                camera.dist += (this.zoomTarget - camera.dist) * k;
                // Close enough, against the distance to the pivot (`dist`
                // itself may be below zero with a crop off the stack's centre).
                if (Math.abs(this.zoomTarget - camera.dist) < Math.max(1e-5, this.pivotDistance() * 0.002)) {
                    camera.dist = this.zoomTarget;
                    this.zoomTarget = null;
                }
            }
            if (this.tween) {
                const { from, to, start, ms, pivot } = this.tween;
                const f = Math.min(1, Math.max(0, (time - start) / ms));
                const eased = f < 0.5 ? 4 * f ** 3 : 1 - (-2 * f + 2) ** 3 / 2;
                for (const key of Object.keys(to)) {
                    if (key !== "q") camera[key] = from[key] + (to[key] - from[key]) * eased;
                    else if (pivot) this.turnAboutPivot(Camera().quat.slerp(from.q, to.q, eased));
                    else camera.q = Camera().quat.slerp(from.q, to.q, eased);
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
         *  them -- the short way round. A turn alone (no pan, no distance)
         *  turns about the pivot, as a drag does. */
        turnTo(target) {
            this.spin = null;
            this.setAutoRotate(false);
            const to = { ...target };
            if (to.q) to.q = to.q.slice();
            const pivot = Boolean(to.q) && !("panX" in to) && !("panY" in to) && !("dist" in to);
            if (!this.smooth()) {
                this.tween = null;
                if (pivot) this.turnAboutPivot(to.q);
                else Object.assign(this.camera, to);
                this.invalidate();
                return;
            }
            const from = {};
            for (const key of Object.keys(to)) {
                from[key] = key === "q" ? this.camera.q.slice() : this.camera[key];
            }
            const start = now();
            this.tween = { from, to, start, ms: TURN_MS, pivot };
            if (!this.lastStep) this.lastStep = start;
            this.invalidate();
        }

        /** Zoom by `factor`, eased unless motion is off. */
        zoomBy(factor) {
            const base = this.zoomTarget ?? this.camera.dist;
            // Closer to the pivot, or further: `factor` times the distance to
            // it, which is `dist` itself with no crop. Close enough to look
            // inside the stack: a detail (considerDetail) brings the finer
            // levels in as the part seen gets smaller.
            const along = this.pivotAlong();
            const target = along + Math.max(MIN_DIST, Math.min(8, (base - along) * factor));
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
                frames.window = 0;
                frames.missed = 0;
                return;
            }
            const interval = frames.last ? time - frames.last : 0;
            frames.last = time;
            if (!(interval > 0 && interval < 100)) return;
            // The display's period: no frame comes sooner (4 ms: 240 Hz).
            frames.period = Math.max(4, Math.min(frames.period || Infinity, interval));
            frames.average = frames.count ? frames.average * 0.7 + interval * 0.3 : interval;
            frames.count += 1;
            frames.window = (frames.window || 0) + 1;
            if (interval > 1.5 * frames.period) frames.missed = (frames.missed || 0) + 1;
            const slow = frames.count >= 6 && frames.average > SLOW_FRAME_MS;
            const missing = frames.window >= MISSED_WINDOW && frames.missed >= MISSED_FRAMES;
            if (slow || missing) {
                this.fastScale = Math.max(MIN_FAST_SCALE, this.fastScale * 0.8);
                frames.count = 0;
            }
            if (slow || missing || frames.window >= MISSED_WINDOW) {
                frames.window = 0;
                frames.missed = 0;
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
            const frame = this.frameCovered();
            const across = frame
                ? [frame[2] - frame[0], frame[3] - frame[1]]
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
            const frame = this.frameCovered(lead);
            const covered = frame || [0, 0, Number(info.width) || 1, Number(info.height) || 1];
            const own = lead.covered || covered;
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
                // A screen pixel's size at distance 1; a base voxel in
                // full-resolution pixels, pixels and planes.
                pixel: (2 * Math.tan(FOV_Y / 2)) / Math.max(1, this.canvas?.height || 1),
                // (by what the base texture covers, which may be another area's)
                baseVox: [(own[2] - own[0]) / Math.max(1, shape[2]),
                          (own[3] - own[1]) / Math.max(1, shape[1]),
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
                    const covered = this.frameCovered(lead) || [0, 0, Number(info.width) || 1, Number(info.height) || 1];
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
                // No volume pass, so no grid: it is drawn by the ray caster.
                this._grid = null;
                this.paintGridLabels();
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
            // [inspect] The cut: the box and the cut faces, in the frame.
            const frame = this.frameCovered(lead);
            const cut = this.cutUniforms(frame);
            const tex = this.texTransform(lead);
            gl.uniform3fv(uniform("u_tex_lo"), tex.lo);
            gl.uniform3fv(uniform("u_tex_span"), tex.span);
            gl.uniform3fv(uniform("u_color"), channels.flatMap(
                (channel, index) => channel.colour.map((value) => value * presence[index])));
            // Each channel in the window of the texture it is drawn from.
            gl.uniform2fv(uniform("u_window"), channels.flatMap((channel) => this.windowFor(channel)));
            gl.uniform1fv(uniform("u_cofactor"), channels.map((channel) => this.cofactorFor(channel)));
            gl.uniform1fv(uniform("u_opacity"), channels.map(
                (channel, index) => channel.opacity * presence[index]));
            gl.uniformMatrix4fv(uniform("u_inverse"), false, this.matrices());
            const extent = this.extent();
            gl.uniform3fv(uniform("u_extent"), extent);
            this.bindCut(uniform, cut);
            gl.uniform1i(uniform("u_mode"), this.composite === "additive" ? 1 : 0);
            this.bindGrid(gl, uniform, cut.lo, cut.hi);
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
            this.paintGridLabels();
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

        // -- the reference grid ----------------------------------------------

        /** The grid as the user leaves it (services/viewerPrefs.js). */
        gridPrefs() {
            return gridPrefs(this.prefs?.grid?.());
        }

        /** What the grid's lines are for the box drawn now (gridAxes), at
         *  `step` in x and y's units or the automatic one. */
        gridAxes(step = this.gridPrefs().step) {
            const vs = this.viewState;
            const info = vs?.info || {};
            const full = info.levels?.[0] || info;
            // The box extent() draws: the area framed, by every plane of the
            // stack.
            const covered = this.frameCovered()
                || [0, 0, Number(full.width || info.width) || 1, Number(full.height || info.height) || 1];
            const planes = Math.max(this.shape?.[0] || 1, Number(vs?.depth) || 0);
            return gridAxes({ spacing: info.spacing, covered, planes, extent: this.extent(), step });
        }

        /** The grid's uniforms for a box clipped to `lo`..`hi` (texture
         *  space): an opacity of 0 when it is off, and nothing else to do. */
        bindGrid(gl, uniform, lo, hi) {
            const grid = this.gridPrefs();
            if (!grid.on) {
                this._grid = null;
                gl.uniform1f(uniform("u_grid"), 0);
                return;
            }
            const axes = this.gridAxes(grid.step);
            const extent = this.extent();
            this._grid = { axes, labels: grid.labels, faces: grid.faces,
                           lo: lo.map((v, i) => (v - 0.5) * extent[i]),
                           hi: hi.map((v, i) => (v - 0.5) * extent[i]) };
            gl.uniform1f(uniform("u_grid"), grid.opacity);
            gl.uniform1i(uniform("u_grid_faces"), grid.faces === "floor" ? 1 : 0);
            gl.uniform3fv(uniform("u_grid_scale"), axes.per.map((per, i) => per / axes.step[i]));
            gl.uniform3fv(uniform("u_grid_offset"), axes.centre.map((c, i) => c / axes.step[i]));
        }

        /** The grid's numbers over the canvas (`.viewer-volume-grid-labels`),
         *  laid out again with every frame drawn; hidden with no grid. */
        paintGridLabels() {
            const overlay = this.gridCanvas;
            if (!overlay) return;
            const canvas = this.canvas;
            const shown = Boolean(this._grid?.labels) && Boolean(canvas) && !canvas.hidden;
            const context = overlay.getContext?.("2d");
            if (!shown || !context) {
                overlay.hidden = true;
                return;
            }
            const width = canvas.clientWidth || 1;
            const height = canvas.clientHeight || 1;
            const ratio = Math.min(global.devicePixelRatio || 1, 2);
            const w = Math.round(width * ratio);
            const h = Math.round(height * ratio);
            if (overlay.width !== w || overlay.height !== h) {
                overlay.width = w;
                overlay.height = h;
            }
            context.setTransform(1, 0, 0, 1, 0, 0);
            context.clearRect(0, 0, w, h);
            context.setTransform(ratio, 0, 0, ratio, 0, 0);
            this.drawGridLabels(context, width, height, 1);
            overlay.hidden = false;
        }

        /** The grid's numbers into `context` over a `width` x `height`
         *  picture of the current camera, offsets and type times `scale`. */
        drawGridLabels(context, width, height, scale = 1) {
            const grid = this._grid;
            if (!grid?.labels || !this.canvas) return;
            const camera = this.cameraMatrices();
            const labels = gridLabels({ forward: camera.forward, eye: camera.eye, lo: grid.lo,
                                        hi: grid.hi, axes: grid.axes, width, height, scale,
                                        floor: grid.faces === "floor" });
            context.save();
            context.shadowColor = "rgba(0, 0, 0, 0.9)";
            context.shadowBlur = 3 * scale;
            for (const label of labels) {
                context.font = `${label.title ? 600 : 400} ${(label.title ? 11 : 10) * scale}px `
                    + "system-ui, -apple-system, 'Segoe UI', sans-serif";
                context.fillStyle = label.title ? "rgba(214, 222, 235, 0.9)" : "rgba(190, 200, 216, 0.82)";
                context.textAlign = label.align;
                context.textBaseline = label.baseline;
                context.fillText(label.text, label.x, label.y);
            }
            context.restore();
        }

        /** `source` (the GL canvas) with the grid's numbers drawn in, as a
         *  copy; `source` itself when there are none to draw. */
        withGridLabels(source) {
            if (!this._grid?.labels || !source || typeof document === "undefined") return source;
            const copy = document.createElement("canvas");
            const context = copy.getContext?.("2d");
            if (!context) return source;
            copy.width = source.width;
            copy.height = source.height;
            context.drawImage(source, 0, 0);
            this.drawGridLabels(context, source.width, source.height,
                                source.width / Math.max(1, this.canvas?.clientWidth || source.width));
            return copy;
        }

        paintGizmo(fast = this.moving()) {
            if (!this.gizmo) return;
            const q = this.camera.q;
            this.gizmo.update((name) => Camera().axisOnScreen(q, name));
            this.gizmo.setActive(fast);
        }

        /** Where the stack turns about on the canvas, in CSS pixels: its
         *  centre, or a crop's (pivot). */
        centreOnScreen() {
            const canvas = this.canvas;
            const width = canvas?.clientWidth || 1;
            const height = canvas?.clientHeight || 1;
            const f = 1 / Math.tan(FOV_Y / 2);
            const { dist, panX, panY, q } = this.camera;
            const point = this.pivot();
            const r = point ? Camera().quat.rotate(q, point) : [0, 0, 0];
            const away = Math.max(1e-6, dist - r[2]);
            return { x: width / 2 + (((r[0] - panX) * f) / (away * (width / height))) * (width / 2),
                     y: height / 2 - (((r[1] - panY) * f) / away) * (height / 2) };
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
                await this.windowsFor()?.ensure?.(
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
            this.turnTo(this.centred(views[name] || views.iso));
        }

        // -- the pivot ---------------------------------------------------------

        /**
         * What the camera turns about, in the box's space (its centre the
         * origin, `extent()`): the middle of what a crop keeps, so a square
         * cropped out of the whole stack turns in place rather than swinging
         * round the stack's centre; null with no crop (the box's centre).
         * The camera itself stays `{q, dist, panX, panY}` -- what Video
         * Studio, the history and the saved view keep -- and a turn moves
         * the pan and the distance with it (turnAboutPivot).
         */
        pivot() {
            const G = global.PlexoraInspectGeometry;
            const resolved = G ? this.inspectNow() : null;
            if (resolved?.mode !== "crop") return null;
            const cut = this.cutUniforms();
            const extent = this.extent();
            return [0, 1, 2].map((i) => ((cut.lo[i] + cut.hi[i]) / 2 - 0.5) * extent[i]);
        }

        /** Turn to `q` with the pivot held where it is on screen. */
        turnAboutPivot(q) {
            const camera = this.camera;
            const point = this.pivot();
            if (point) {
                const C = Camera();
                const was = C.quat.rotate(camera.q, point);
                const now = C.quat.rotate(q, point);
                camera.panX += now[0] - was[0];
                camera.panY += now[1] - was[1];
                camera.dist += now[2] - was[2];
            }
            camera.q = q;
        }

        /** The pivot's depth in the turned box, toward the camera: what
         *  `dist` counts beyond the distance to it (0 with no crop). */
        pivotAlong() {
            const point = this.pivot();
            return point ? Camera().quat.rotate(this.camera.q, point)[2] : 0;
        }

        /** How far the camera is from the pivot, along its view. */
        pivotDistance() {
            return Math.max(MIN_DIST / 4, this.camera.dist - this.pivotAlong());
        }

        /** The camera facing `q` with the pivot in the middle of the view,
         *  as far from it as it is now. */
        centred(q) {
            const point = this.pivot();
            if (!point) return { q, panX: 0, panY: 0 };
            const away = this.pivotDistance();
            const [x, y, z] = Camera().quat.rotate(q, point);
            return { q, panX: x, panY: y, dist: away + z };
        }

        /** The pivot in the middle of the view, at once (a new frame). */
        recentre() {
            this.tween = null;
            Object.assign(this.camera, this.centred(this.camera.q.slice()));
            this.invalidate();
        }

        /**
         * Frame image region `box` ([x0, y0, x1, y1], full-resolution pixels)
         * at `perPixel` screen pixels per image pixel -- what the plane showed
         * it at -- centred, through the middle of the stack: looking straight
         * down first, as the plane did, then turning to the way the stack was
         * last seen. `height`, the view's height, while the canvas has none.
         */
        frameRegion({ box, perPixel, height = 0 } = {}) {
            const h = this.canvas?.clientHeight || Number(height) || 0;
            if (!Array.isArray(box) || !(perPixel > 0) || !(h > 0)) return false;
            const C = Camera();
            const info = this.viewState?.info || {};
            const covered = this.frameCovered() || [0, 0, Number(info.width) || 1, Number(info.height) || 1];
            const extent = this.extent();
            const spanX = Math.max(1e-9, covered[2] - covered[0]);
            const spanY = Math.max(1e-9, covered[3] - covered[1]);
            const point = this.pivot() || [
                (((box[0] + box[2]) / 2 - covered[0]) / spanX - 0.5) * extent[0],
                (((box[1] + box[3]) / 2 - covered[1]) / spanY - 0.5) * extent[1], 0];
            // A screen pixel at distance d spans 2 d tan(fov / 2) / h of the
            // box; an image pixel spans extent / span of it.
            const away = (h * (extent[0] / spanX)) / (2 * Math.tan(FOV_Y / 2) * perPixel);
            const goal = this.camera.q.slice();
            const down = C.VIEWS.xy.slice();
            const [x, y, z] = C.quat.rotate(down, point);
            this.stopMotion();
            this.zoomTarget = null;
            Object.assign(this.camera, { q: down, panX: x, panY: y,
                                         dist: Math.max(MIN_DIST, Math.min(8, away)) + z });
            this.invalidate();
            if (C.quat.angleBetween(goal, down) > 0.02) this.turnTo({ q: goal });
            return true;
        }

        /** An image point (x, y pixels, z planes, continuous) on screen,
         *  `{x, y, w}` CSS pixels, or null behind the camera. */
        projectImage(point, camera = this.cameraMatrices(), extent = this.extent()) {
            const canvas = this.canvas;
            const vs = this.viewState;
            const c = this.frameCovered() || [0, 0, Number(vs?.info?.width) || 1, Number(vs?.info?.height) || 1];
            const D = Math.max(1, vs?.depth || 1);
            const x = ((point[0] - c[0]) / Math.max(1e-9, c[2] - c[0]) - 0.5) * extent[0];
            const y = ((point[1] - c[1]) / Math.max(1e-9, c[3] - c[1]) - 0.5) * extent[1];
            const z = (point[2] / D - 0.5) * extent[2];
            const m = camera.forward;
            const w = m[3] * x + m[7] * y + m[11] * z + m[15];
            if (!(w > 1e-6)) return null;
            const nx = (m[0] * x + m[4] * y + m[8] * z + m[12]) / w;
            const ny = (m[1] * x + m[5] * y + m[9] * z + m[13]) / w;
            return { x: (nx * 0.5 + 0.5) * canvas.clientWidth, y: (0.5 - ny * 0.5) * canvas.clientHeight, w };
        }

        /** The screen's measure of one image pixel along x and y at the
         *  area's centre, mid-stack: `[{axis, step: {x, y}}]` CSS pixels,
         *  those that show on screen at all. */
        slideSteps() {
            const area = this.areaCovered();
            if (!area || !this.canvas) return [];
            const camera = this.cameraMatrices();
            const extent = this.extent();
            const centre = [(area[0] + area[2]) / 2, (area[1] + area[3]) / 2,
                            Math.max(1, this.viewState?.depth || 1) / 2];
            const at = this.projectImage(centre, camera, extent);
            if (!at) return [];
            const out = [];
            ["x", "y"].forEach((axis, i) => {
                const span = Math.max(1, area[i + 2] - area[i]) / 50;
                const ahead = centre.slice();
                ahead[i] += span;
                const b = this.projectImage(ahead, camera, extent);
                if (!b) return;
                const step = { x: (b.x - at.x) / span, y: (b.y - at.y) / span };
                if (Math.hypot(step.x, step.y) > 1e-4) out.push({ axis, step });
            });
            return out;
        }

        /**
         * The area slid by a pointer travel of (`dx`, `dy`) CSS pixels from
         * `start` (`{x, y, width, height}`): the tissue follows the hand, so
         * the area moves the other way. Kept inside the image, never shrunk.
         */
        slidTo(start, steps, dx, dy) {
            const moved = travel(steps, dx, dy);
            const G = global.PlexoraInspectGeometry;
            const next = { ...start, x: start.x - (moved.x || 0), y: start.y - (moved.y || 0) };
            return G ? G.inside(next, this.plainDims()) : next;
        }

        /**
         * A drag of the area under way, for whichever hand moves it --
         * Shift+drag on the canvas, the area's grips (views/volumeHandles.js).
         * `move(dx, dy)` with the pointer's travel since the press: at most
         * one change every SLIDE_MS, since each is a fetch; `end()` sends the
         * last one as final; `cancel()` puts the area back where it started.
         * `to(start, dx, dy)` makes the area from the travel (a slide's by
         * default: the tissue follows the hand), `send(area, {final})` hears
         * each (`onSlide` by default). Null with no area to drag.
         */
        dragArea({ to = null, send = null } = {}) {
            const area = this.box;
            const hear = send || this.onSlide;
            if (!area || typeof hear !== "function") return null;
            const steps = this.slideSteps();
            if (!steps.length) return null;
            const start = { ...area };
            const make = to || ((from, dx, dy) => this.slidTo(from, steps, dx, dy));
            let last = null;
            let lastSent = JSON.stringify(start);
            let sent = 0;
            let timer = 0;
            const flush = (final) => {
                clearTimeout(timer);
                timer = 0;
                sent = now();
                if (last && (final || lastSent !== JSON.stringify(last))) {
                    lastSent = JSON.stringify(last);
                    hear(last, { final });
                }
            };
            return {
                start,
                steps,
                move: (dx, dy) => {
                    last = make(start, dx, dy);
                    const wait = SLIDE_MS - (now() - sent);
                    if (wait <= 0) flush(false);
                    else if (!timer) timer = setTimeout(() => flush(false), wait);
                },
                end: () => {
                    if (last) flush(true);
                    else clearTimeout(timer);
                },
                cancel: () => {
                    clearTimeout(timer);
                    timer = 0;
                    if (lastSent !== JSON.stringify(start)) hear(start, { final: true });
                },
            };
        }

        bindPointer(canvas) {
            let drag = null;
            //: A slide of the area under way (`dragArea`), and where the
            //: press was: {session, x0, y0}.
            let slide = null;
            //: The hand's turn so far and when, for the release's speed:
            //: {t, a, b} -- about the screen's x and y for a free drag, the
            //: locked axis's angle in `a` for a locked one.
            let samples = [];
            canvas.addEventListener("pointerdown", (event) => {
                canvas.setPointerCapture?.(event.pointerId);
                // A press stops a spin or a turn where it is.
                this.stopMotion();
                clearTimeout(this._settleTimer);
                // Shift slides the area where there is one (the tissue under
                // a fixed frame); Alt, or the middle button, pans the camera
                // (Shift too, with no area to slide).
                const area = this.box;
                const session = event.button === 0 && event.shiftKey ? this.dragArea() : null;
                if (session) slide = { session, x0: event.clientX, y0: event.clientY };
                const pan = !slide && (event.button === 1 || (event.button === 0 && event.altKey)
                    || (event.button === 0 && event.shiftKey && !area));
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
                if (slide) {
                    slide.session.move(event.clientX - slide.x0, event.clientY - slide.y0);
                    return;
                }
                const C = Camera();
                if (drag.pan) {
                    const scale = this.pivotDistance() / Math.max(1, canvas.clientHeight);
                    this.camera.panX -= dx * scale;
                    this.camera.panY += dy * scale;
                } else if (drag.locked) {
                    const pointer = { x: event.clientX - drag.box.left, y: event.clientY - drag.box.top };
                    const angle = drag.locked.angle(dx, dy, pointer, drag.centre);
                    this.turnAboutPivot(C.turnAbout(this.camera.q, this.lock, angle));
                    drag.a += angle;
                } else {
                    this.turnAboutPivot(C.turnFree(this.camera.q, dx, dy));
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
                if (slide) {
                    // At rest before the last move is heard: it fetches.
                    this.interacting = false;
                    slide.session.end();
                    slide = null;
                    drag = null;
                    samples = [];
                    this.invalidate();
                    return;
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
            // [inspect] Within what the cut keeps.
            const cut = this.cutUniforms();
            const bounds = [0, 1, 2].map((a) => [cut.lo[a], cut.hi[a]]);
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
         *  and coarser -- the cut panel says so (volumeDock "refining"). */
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
            // The frame's space -> full-resolution pixels and planes.
            const info = this.viewState?.info || {};
            const covered = this.frameCovered(lead) || [0, 0, Number(info.width) || 1, Number(info.height) || 1];
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
            // Where the detail sits in the frame, by what the server actually
            // covered (aligned to its level's grid).
            const box = arrived.covered || [pixels.x0, pixels.y0, pixels.x1, pixels.y1];
            const z = arrived.z || [pixels.z0, pixels.z1];
            request.lo = [(box[0] - covered[0]) / spanX, (box[1] - covered[1]) / spanY, z[0] / depth];
            request.hi = [(box[2] - covered[0]) / spanX, (box[3] - covered[1]) / spanY, (z[1] + 1) / depth];
            const previous = this.detail;
            this.detail = request;
            if (previous && previous !== request) {
                // Keys are by region: a request for the region already shown
                // (a channel turned on, the camera still) holds the very
                // textures it just reused -- dropping those would lose them
                // and ask for every channel's detail again at the next rest.
                for (const channel of channels) {
                    const old = this.detailKey(channel, previous);
                    if (old !== this.detailKey(channel, request)) this.dropTexture(old);
                }
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
                return this.withGridLabels(this.canvas);
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
                const labelled = this.withGridLabels(this.canvas);
                if (labelled !== this.canvas) return labelled;
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
            // Not clamped to MIN_DIST: turned about a crop's middle, `dist`
            // (to the stack's centre) may rightly be less, or below zero.
            if (Number.isFinite(stored.dist)) this.camera.dist = Math.max(-8, Math.min(8, stored.dist));
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
        let lastCut = "";
        //: A region to frame when 3-D opens next, if it opens soon
        //: (frameNext), and what the frame last drawn was: going between the
        //: whole stack and an area (Crop to area, Back from it) centres the
        //: pivot again; a slide or a resize of the area does not.
        let pendingFrame = null;
        let lastFrameKind = "";
        const FRAME_WAIT_MS = 3000;
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
        /** A slid area, into the view state: through the cut's controller
         *  (services/inspectState.js), so a slide and the cut it carries
         *  undo together. */
        const slideArea = (area, { final = false } = {}) => {
            const cutter = global.PlexoraInspect;
            if (cutter?.viewState === viewState && typeof cutter.slideArea === "function") {
                cutter.slideArea(area, { final });
            } else {
                viewState.set({ volume: { area } }, { save: final });
            }
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
                    renderer.onSlide = slideArea;
                    global.dispatchEvent?.(new CustomEvent("plexora:volume-mounted",
                        { detail: { renderer } }));
                }
                renderer.surfaces?.setShow(state.volume?.show || {});
                document.getElementById("openseadragon_wrapper")?.classList.add("is-volume-mode");
                renderer.composite = state.volume?.composite === "additive" ? "additive" : "mip";
                renderer.show(true);
                // The area the view state says (`volume.area`), or the whole
                // stack: Crop to area's (services/inspectState.js).
                const moved = renderer.setArea(state.volume?.area || null, { refresh: false });
                const kind = state.volume?.area ? "area" : "whole";
                if (!entering && lastFrameKind && kind !== lastFrameKind) renderer.recentre();
                lastFrameKind = kind;
                // A square placed on the plane (views/areaTool.js): at the
                // plane's magnification, not the whole view.
                if (entering && pendingFrame && now() - pendingFrame.at < FRAME_WAIT_MS) {
                    renderer.frameRegion(pendingFrame.request);
                }
                pendingFrame = null;
                // Only what is missing is fetched (ensureTextures): entering
                // again over the same area, with the same channels, is a draw.
                if (entering || lastT !== state.t || moved) {
                    lastT = state.t;
                    renderer.refresh();
                } else {
                    renderer.invalidate();
                }
                lastCut = JSON.stringify(renderer.cutUniforms());
            },
            /** The next time 3-D opens, frame `request` (`{box, perPixel,
             *  height}`, VolumeRenderer.frameRegion) rather than keep the
             *  camera; dropped if 3-D does not open with it. */
            frameNext(request) {
                pendingFrame = request ? { request, at: now() } : null;
                if (pendingFrame && renderer && viewState.isVolume() && wasOn) {
                    renderer.frameRegion(request);
                    pendingFrame = null;
                }
            },
            /** [inspect] The cut changed -- the dock, the strip, a handle, an
             *  undo, the agent: drawn again from uniforms in the next frame
             *  (no revision), and a finer detail looked for once it rests. The
             *  contrast stays the channel's (views/viewerSidebar.js). */
            cutChanged() {
                if (!renderer || !viewState.isVolume()) return;
                const key = JSON.stringify(renderer.cutUniforms());
                if (key === lastCut) return;
                lastCut = key;
                renderer.invalidate();
                renderer.scheduleDetail();
            },
            /** The area moved (a slide, the Area tool, a restore, Video
             *  Studio): its textures fetched, the last drawn standing in. */
            areaChanged(state = viewState.get()) {
                if (!renderer || !viewState.isVolume(state)) return;
                renderer.setArea(state.volume?.area || null);
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
                // An open popover (the cog's) takes Escape first: this runs
                // before its own listener, so it stands aside.
                if (document.querySelector(".plx-popover")) return;
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
        // The grid is the user's (services/viewerPrefs.js): a uniform, a redraw.
        global.addEventListener?.("plexora:viewer-prefs-changed", () => {
            if (viewState.isVolume()) renderer?.invalidate();
        });
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
        // [inspect] Whoever changes the cut, and a preview under a hand: while
        // one is previewed the view is moving (as under a camera drag: no
        // crossfade, a finer detail only once it rests), and the commit or
        // the cancel that ends it is the rest.
        let cutting = false;
        viewState.subscribe((state) => {
            controller.areaChanged(state);
            controller.cutChanged();
        });
        global.addEventListener?.("plexora:inspect-preview", () => {
            if (renderer && !renderer.interacting) {
                renderer.interacting = true;
                cutting = true;
            }
            controller.cutChanged();
        });
        global.addEventListener?.("plexora:inspect-changed", () => {
            if (renderer && cutting) {
                cutting = false;
                renderer.interacting = false;
                renderer.invalidate();
            }
            controller.cutChanged();
        });
        controller.sync(viewState.get());
        return controller;
    }

    VolumeRenderer.attach = attach;
    VolumeRenderer.releaseSpeed = releaseSpeed;
    VolumeRenderer.travel = travel;
    VolumeRenderer.SLIDE_MS = SLIDE_MS;
    VolumeRenderer.MOTION = { SPIN_TAU_MS, RELEASE_MIN, SPIN_STOP, SPIN_MAX, ZOOM_TAU_MS,
                              TURN_MS, VELOCITY_WINDOW_MS, RELEASE_STILL_MS,
                              ROTATE_DEFAULT_DPS, ROTATE_MIN_DPS, ROTATE_MAX_DPS };
    VolumeRenderer.fragmentSource = fragmentSource;
    VolumeRenderer.grid = { DEFAULTS: GRID_DEFAULTS, prefs: gridPrefs, niceStep, niceNeighbour,
                            axes: gridAxes, labels: gridLabels, project, formatTick };
    VolumeRenderer.mat4 = { multiply: mat4Multiply, invert: mat4Invert,
                            perspective: mat4Perspective, lookAt: mat4LookAt };
    global.PlexoraVolumeRenderer = VolumeRenderer;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = VolumeRenderer;
    }
})(typeof window !== "undefined" ? window : globalThis);
