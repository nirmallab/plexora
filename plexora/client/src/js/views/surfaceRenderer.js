/**
 * surfaceRenderer.js - triangle meshes in the 3-D view, depth-composited with the volume.
 *
 * Surfaces (plexora/server/surface) are drawn on the volume renderer's own
 * WebGL2 context, so a surface and the voxels around it are one picture: a
 * cell's surface sits INSIDE the MIP of its own stain, and the volume in front
 * of a surface is drawn over it, the volume behind it is hidden by it.
 *
 * THE FRAME, when any surface is shown (otherwise the volume draws exactly as
 * it always has -- `begin` answers null and nothing here runs):
 *
 *   1. Opaque surfaces into buffer A: colour, and a depth texture.
 *   2. Translucent surfaces: their nearest depth into F (the volume is split
 *      there), then order-independent blending into W -- weighted blended OIT,
 *      accumulation RGBA16F with revealage in its alpha and the weights in an
 *      R16F, ONE blend state for both (WebGL2 has no per-attachment blending):
 *      `blendFuncSeparate(ONE, ONE, ZERO, ONE_MINUS_SRC_ALPHA)`. Without
 *      EXT_color_buffer_float, translucent objects are sorted back to front
 *      and blended over instead (`capabilities().oit` says which).
 *   3. The volume's ray caster, in its surface variant (`volumeFragment`):
 *      each ray stops at the opaque depth, and accumulates what lies in front
 *      of the translucent depth and what lies behind it separately, into two
 *      outputs of buffer V.
 *   4. The composite, onto the canvas: front OVER translucent OVER back OVER
 *      (opaque or the ground). Where there is no surface this is the volume's
 *      own picture, byte for byte the formula it always used.
 *
 * WHAT NEVER TOUCHES A BUFFER. A mesh is uploaded once -- positions, normals,
 * object ids, triangles -- and every change of colour, opacity, style,
 * material, lighting, hidden or isolated objects or camera is a uniform or the
 * small per-object colour table (a texture). `bufferData` runs only on load,
 * and once more the first time a mesh is drawn as a wireframe (its edges) or
 * compared with its unsmoothed self.
 *
 * COORDINATES. Vertices are level-0 voxels, (x, y, z), a voxel's centre on
 * whole numbers. The volume's box is the area its textures cover (`covered`,
 * full-resolution pixels) by every plane, scaled to the stack's physical
 * proportions (`extent()`): a vertex goes to the box as
 * `(p + 0.5 - origin) * scale - extent / 2`, normals by the inverse transpose of
 * that scale, so an anisotropic stack shades as the tissue would. Fragments
 * outside the slab band's planes, the area drawn, or the clipping plane are
 * discarded -- the same cut as the voxels.
 *
 * Classic script: `window.PlexoraSurfaceRenderer`.
 */
(function (global) {
    "use strict";

    //: What surfaces may hold on the GPU in all; past it the least recently
    //: drawn hidden surface goes first.
    const SURFACE_BUDGET = 512 * 2 ** 20;
    //: The per-object colour table is this many ids wide.
    const LUT_WIDTH = 2048;
    const PREVIEW_ID = "__preview__";

    const MATERIALS = {
        //               ambient diffuse specular shininess rim
        matte: { ambient: 0.38, diffuse: 0.72, specular: 0.04, shininess: 8, rim: 0.0 },
        soft: { ambient: 0.32, diffuse: 0.7, specular: 0.22, shininess: 28, rim: 0.12 },
        glossy: { ambient: 0.2, diffuse: 0.85, specular: 0.6, shininess: 72, rim: 0.08 },
        glass: { ambient: 0.5, diffuse: 0.45, specular: 0.55, shininess: 48, rim: 0.75 },
    };
    const STYLES = ["solid", "glass", "wireframe", "solid+wire", "outline", "illustration"];

    // -- the mesh payload (server/surface/mesh.py) ----------------------------------

    /**
     * Read a `PXMS` payload into typed-array views over the same buffer (no
     * copy): `{header, positions, normals, objectId, triangles, objects,
     * positionsRaw}`.
     */
    function parse(buffer) {
        // A view's buffer, or the buffer itself (`isView` rather than
        // `instanceof`: a buffer from another realm is still one).
        const bytes = ArrayBuffer.isView(buffer) ? buffer.buffer : buffer;
        const view = new DataView(bytes);
        const magic = String.fromCharCode(view.getUint8(0), view.getUint8(1), view.getUint8(2),
                                          view.getUint8(3));
        if (magic !== "PXMS") throw new Error("not a Plexora mesh");
        const length = view.getUint32(4, true);
        const text = new TextDecoder().decode(new Uint8Array(bytes, 8, length));
        const header = JSON.parse(text);
        const arrays = {};
        const types = { f4: Float32Array, i1: Int8Array, u4: Uint32Array };
        for (const entry of header.arrays || []) {
            const Type = types[entry.dtype];
            if (!Type) continue;
            const count = (entry.shape || []).reduce((a, b) => a * b, 1);
            arrays[entry.name] = new Type(bytes, entry.offset, count);
        }
        return {
            header,
            positions: arrays.positions || new Float32Array(0),
            normals: arrays.normals || new Int8Array(0),
            objectId: arrays.object_id || new Uint32Array(0),
            triangles: arrays.triangles || new Uint32Array(0),
            objects: arrays.objects || new Uint32Array(0),
            positionsRaw: arrays.positions_raw || null,
        };
    }

    function hexToRgb(colour) {
        const text = String(colour || "#d8b45a").replace(/^#/, "");
        const full = text.length === 3 ? text.split("").map((c) => c + c).join("") : text;
        const value = parseInt(full, 16);
        if (!Number.isFinite(value)) return [0.85, 0.7, 0.35];
        return [((value >> 16) & 255) / 255, ((value >> 8) & 255) / 255, (value & 255) / 255];
    }

    /** A stable, well-spread colour for object `id` (golden-angle hues). */
    function idColour(id) {
        const hue = ((id * 137.508) % 360) / 360;
        const s = 0.55 + ((id * 7) % 5) * 0.06;
        const l = 0.52 + ((id * 11) % 3) * 0.05;
        const f = (n) => {
            const k = (n + hue * 12) % 12;
            const a = s * Math.min(l, 1 - l);
            return l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1));
        };
        return [f(0), f(8), f(4)];
    }

    // -- shaders ----------------------------------------------------------------

    const MESH_VERTEX = `#version 300 es
precision highp float;
precision highp int;
layout(location = 0) in vec3 a_position;
layout(location = 1) in vec4 a_normal;
layout(location = 2) in uint a_object;
uniform mat4 u_projection;
uniform mat4 u_view;
uniform vec3 u_origin;
uniform vec3 u_scale;
uniform vec3 u_extent;
uniform float u_inflate;       // view-space units per unit of depth (outline hull)
uniform bool u_useLut;
uniform highp sampler2D u_lut;
out vec3 v_viewPos;
out vec3 v_normal;
out vec3 v_world;
flat out vec4 v_lut;
flat out uint v_object;
void main() {
    vec3 world = (a_position + 0.5 - u_origin) * u_scale - 0.5 * u_extent;
    vec3 n = normalize(a_normal.xyz / u_scale);
    vec4 view = u_view * vec4(world, 1.0);
    vec3 vn = normalize(mat3(u_view) * n);
    if (u_inflate > 0.0) view.xyz += vn * u_inflate * max(0.05, -view.z);
    v_viewPos = view.xyz;
    v_normal = vn;
    v_world = world;
    v_object = a_object;
    if (u_useLut) {
        int id = int(a_object);
        v_lut = texelFetch(u_lut, ivec2(id % ${LUT_WIDTH}, id / ${LUT_WIDTH}), 0);
    } else {
        v_lut = vec4(1.0);
    }
    gl_Position = u_projection * view;
}`;

    function meshFragment(pass) {
        const outputs = {
            opaque: "out vec4 outColor;",
            sorted: "out vec4 outColor;",
            wboit: "layout(location = 0) out vec4 outAccum;\nlayout(location = 1) out vec4 outWeight;",
            depth: "out vec4 outColor;",
            pick: "out uvec4 outId;",
        }[pass];
        return `#version 300 es
precision highp float;
precision highp int;
#define PASS_${pass.toUpperCase()} 1
in vec3 v_viewPos;
in vec3 v_normal;
in vec3 v_world;
flat in vec4 v_lut;
flat in uint v_object;
uniform vec3 u_extent;
uniform vec2 u_clip_z;
uniform vec4 u_clip_xy;        // x0, x1, y0, y1: the volume's setClip, [0,1]
uniform vec4 u_clip_plane;
uniform bool u_use_clip_plane;
uniform bool u_useLut;
uniform bool u_lutColour;
uniform vec3 u_color;
uniform float u_opacity;
uniform vec4 u_material;       // ambient, diffuse, specular, shininess
uniform float u_rim;
uniform vec3 u_light;          // view space, towards the light
uniform bool u_flat;
uniform bool u_flatColour;     // outline hull and wire lines: one colour, no shading
uniform vec3 u_flatRgb;
uniform uint u_selected;
uniform uint u_surfaceIndex;
#ifdef PASS_DEPTH
uniform highp sampler2D u_opaqueDepth;
#endif
${outputs}
void main() {
    vec3 p = v_world / u_extent + 0.5;
    if (p.z < u_clip_z.x || p.z > u_clip_z.y || p.x < u_clip_xy.x - 0.001 || p.x > u_clip_xy.y + 0.001
            || p.y < u_clip_xy.z - 0.001 || p.y > u_clip_xy.w + 0.001) discard;
    if (u_use_clip_plane && dot(vec4(v_world, 1.0), u_clip_plane) < 0.0) discard;
    if (u_useLut && v_lut.a < 0.002) discard;
#ifdef PASS_PICK
    outId = uvec4(v_object, u_surfaceIndex + 1u, floatBitsToUint(gl_FragCoord.z), 1u);
    return;
#else
#ifdef PASS_DEPTH
    if (gl_FragCoord.z >= texelFetch(u_opaqueDepth, ivec2(gl_FragCoord.xy), 0).r) discard;
    outColor = vec4(0.0);
    return;
#else
    vec3 base = (u_useLut && u_lutColour) ? v_lut.rgb : u_color;
    float alpha = u_opacity * (u_useLut ? v_lut.a : 1.0);
    vec3 colour;
    if (u_flatColour) {
        colour = u_flatRgb;
    } else {
        vec3 N = u_flat ? normalize(cross(dFdx(v_viewPos), dFdy(v_viewPos))) : normalize(v_normal);
        vec3 V = normalize(-v_viewPos);
        if (dot(N, V) < 0.0) N = -N;
        vec3 L = normalize(u_light);
        float key = max(dot(N, L), 0.0);
        float head = max(dot(N, V), 0.0);
        float diffuse = u_material.y * (0.72 * key + 0.38 * head);
        vec3 ambient = mix(vec3(0.62, 0.6, 0.66), vec3(1.0), 0.5 + 0.5 * N.y) * u_material.x;
        float specular = pow(max(dot(N, normalize(L + V)), 0.0), u_material.w) * u_material.z;
        float rim = pow(1.0 - head, 3.0) * u_rim;
        colour = base * (ambient + diffuse) + vec3(specular) + base * rim;
        if (u_selected > 0u && v_object == u_selected) {
            colour = mix(colour, vec3(1.0, 0.86, 0.36), 0.38) + vec3(0.25, 0.2, 0.05) * pow(1.0 - head, 2.0);
        }
    }
#ifdef PASS_WBOIT
    float z = abs(v_viewPos.z);
    float w = alpha * clamp(10.0 / (1e-5 + pow(z / 5.0, 2.0) + pow(z / 200.0, 6.0)), 1e-2, 3e3);
    outAccum = vec4(colour * alpha * w, alpha);
    outWeight = vec4(alpha * w, 0.0, 0.0, 0.0);
#else
#ifdef PASS_SORTED
    outColor = vec4(colour * alpha, alpha);
#else
    outColor = vec4(colour, 1.0);
#endif
#endif
#endif
#endif
}`;
    }

    const QUAD_VERTEX = `#version 300 es
in vec2 a_position;
void main() { gl_Position = vec4(a_position, 0.0, 1.0); }`;

    const COMPOSITE_FRAGMENT = `#version 300 es
precision highp float;
uniform highp sampler2D u_opaque;
uniform highp sampler2D u_front;
uniform highp sampler2D u_back;
uniform highp sampler2D u_accum;
uniform highp sampler2D u_weight;
uniform int u_trans;           // 0 none, 1 weighted blended, 2 sorted (premultiplied)
uniform bool u_volume;
uniform vec3 u_ground;
out vec4 outColor;
void main() {
    ivec2 at = ivec2(gl_FragCoord.xy);
    vec4 o = texelFetch(u_opaque, at, 0);
    vec3 c = mix(u_ground, o.rgb, o.a);
    if (u_volume) {
        vec4 b = texelFetch(u_back, at, 0);
        c = b.rgb + (1.0 - b.a) * c;
    }
    if (u_trans == 1) {
        vec4 acc = texelFetch(u_accum, at, 0);
        float weight = texelFetch(u_weight, at, 0).r;
        float reveal = acc.a;
        if (reveal < 1.0) c = (acc.rgb / max(weight, 1e-5)) * (1.0 - reveal) + c * reveal;
    } else if (u_trans == 2) {
        vec4 t = texelFetch(u_accum, at, 0);
        c = t.rgb + (1.0 - t.a) * c;
    }
    if (u_volume) {
        vec4 f = texelFetch(u_front, at, 0);
        c = f.rgb + (1.0 - f.a) * c;
    }
    outColor = vec4(min(c, vec3(1.0)), 1.0);
}`;

    /**
     * The volume ray caster's surface variant, made from its own source (the
     * renderer's `fragmentSource`) so it follows every change made there: the
     * ray stops at the opaque surfaces' depth, and what lies in front of the
     * nearest translucent surface is accumulated apart from what lies behind
     * it -- `outFront` and `outBack`, premultiplied. Each anchor is checked:
     * a renderer whose shader no longer has one fails loudly here, in the
     * probe, rather than drawing something subtly wrong.
     */
    function volumeFragment(source) {
        const swaps = [
            ["out vec4 outColor;",
             `layout(location = 0) out vec4 outFront;
layout(location = 1) out vec4 outBack;
uniform highp sampler2D u_depth_opaque;
uniform highp sampler2D u_depth_front;`],
            ["void main() {",
             `float surfaceT(float d, vec3 origin, vec3 dir) {
    vec4 point = u_inverse * vec4(v_ndc, d * 2.0 - 1.0, 1.0);
    return dot(point.xyz / point.w - origin, dir);
}

void main() {`],
            ["    vec2 hit = hitBox(origin, dir, lo, hi);",
             `    vec2 hit = hitBox(origin, dir, lo, hi);
    float tFront = 1e20;
    {
        ivec2 at = ivec2(gl_FragCoord.xy);
        float dOpaque = texelFetch(u_depth_opaque, at, 0).r;
        float dFront = texelFetch(u_depth_front, at, 0).r;
        if (dOpaque < 1.0) hit.y = min(hit.y, surfaceT(dOpaque, origin, dir));
        if (dFront < 1.0) tFront = surfaceT(dFront, origin, dir);
    }`],
            ["        outColor = vec4(u_ground, 1.0);\n        return;",
             "        outFront = vec4(0.0);\n        outBack = vec4(0.0);\n        return;"],
            ["    float peak[NCH];\n    for (int c = 0; c < NCH; c++) peak[c] = 0.0;",
             `    float peak[NCH];
    float peakF[NCH];
    for (int c = 0; c < NCH; c++) { peak[c] = 0.0; peakF[c] = 0.0; }
    vec3 colourF = vec3(0.0);
    float alphaF = 0.0;`],
            [`        if (u_mode == 0) {
            for (int c = 0; c < NCH; c++) peak[c] = max(peak[c], v[c]);
        } else {`,
             `        bool inFront = t < tFront;
        if (u_mode == 0) {
            if (inFront) {
                for (int c = 0; c < NCH; c++) peakF[c] = max(peakF[c], v[c]);
            } else {
                for (int c = 0; c < NCH; c++) peak[c] = max(peak[c], v[c]);
            }
        } else {`],
            [`            colour += (1.0 - alpha) * emit * a;
            alpha += (1.0 - alpha) * a;
            if (alpha > 0.97) break;`,
             `            if (inFront) {
                colourF += (1.0 - alphaF) * emit * a;
                alphaF += (1.0 - alphaF) * a;
                if (alphaF > 0.97) break;
            } else {
                colour += (1.0 - alpha) * emit * a;
                alpha += (1.0 - alpha) * a;
                if (alpha > 0.97) break;
            }`],
            [`    if (u_mode == 0) {
        for (int c = 0; c < NCH; c++) colour += u_color[c] * level(peak[c], u_window[c]);
        outColor = vec4(max(min(colour, vec3(1.0)), u_ground), 1.0);
    } else {
        outColor = vec4(min(colour + (1.0 - alpha) * u_ground, vec3(1.0)), 1.0);
    }`,
             `    if (u_mode == 0) {
        vec3 back = vec3(0.0);
        vec3 front = vec3(0.0);
        float keepBack = 1.0;
        float keepFront = 1.0;
        for (int c = 0; c < NCH; c++) {
            float lb = level(peak[c], u_window[c]);
            float lf = level(peakF[c], u_window[c]);
            back += u_color[c] * lb;
            front += u_color[c] * lf;
            keepBack *= 1.0 - lb * u_opacity[c];
            keepFront *= 1.0 - lf * u_opacity[c];
        }
        outBack = vec4(min(back, vec3(1.0)), 1.0 - keepBack);
        outFront = vec4(min(front, vec3(1.0)), 1.0 - keepFront);
    } else {
        outBack = vec4(min(colour, vec3(1.0)), alpha);
        outFront = vec4(min(colourF, vec3(1.0)), alphaF);
    }`],
        ];
        let text = source;
        for (const [anchor, replacement] of swaps) {
            const at = text.indexOf(anchor);
            if (at < 0) {
                throw new Error(`surfaceRenderer: the volume shader has no "${anchor.split("\n")[0].trim()}"`);
            }
            text = text.slice(0, at) + replacement + text.slice(at + anchor.length);
        }
        return text;
    }

    function compile(gl, type, text) {
        const shader = gl.createShader(type);
        gl.shaderSource(shader, text);
        gl.compileShader(shader);
        if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
            const log = gl.getShaderInfoLog(shader);
            gl.deleteShader(shader);
            throw new Error(`surface shader: ${log}`);
        }
        return shader;
    }

    function link(gl, vertex, fragment, attributes = null) {
        const built = gl.createProgram();
        gl.attachShader(built, compile(gl, gl.VERTEX_SHADER, vertex));
        gl.attachShader(built, compile(gl, gl.FRAGMENT_SHADER, fragment));
        if (attributes) attributes.forEach((name, index) => gl.bindAttribLocation(built, index, name));
        gl.linkProgram(built);
        if (!gl.getProgramParameter(built, gl.LINK_STATUS)) {
            throw new Error(`surface program: ${gl.getProgramInfoLog(built)}`);
        }
        return built;
    }

    // -- the renderer ----------------------------------------------------------

    class SurfaceRenderer {
        /** `volume` is the VolumeRenderer whose context, camera and box these share. */
        constructor(volume) {
            this.volume = volume;
            this.gl = volume.gl;
            //: id -> mesh record (see load)
            this.meshes = new Map();
            this.programs = new Map();
            this._uniforms = new WeakMap();
            this.targets = null;
            this.show = true;
            this.hideVolume = false;
            this.selected = null;            // {surface, object}
            this.clipPlane = null;            // [a, b, c, d] in the box's world space
            this.light = { azimuth: -35, elevation: 40 };
            this.budget = SURFACE_BUDGET;
            this.uploads = 0;                 // bufferData calls, for the probes
            const gl = this.gl;
            this.oit = Boolean(gl?.getExtension?.("EXT_color_buffer_float"));
            this.maxUnits = gl?.getParameter?.(gl.MAX_TEXTURE_IMAGE_UNITS) || 16;
            this._whiteLut = null;
            this._lastDraw = 0;
            global.PlexoraViewPerf?.provide?.("surfaceBytes", () => this.bytes());
            global.PlexoraViewPerf?.provide?.("surfaceUploads", () => this.uploads);
        }

        capabilities() {
            return { oit: this.oit ? "weighted-blended" : "sorted", lutWidth: LUT_WIDTH,
                     budget: this.budget };
        }

        bytes() {
            let total = 0;
            for (const mesh of this.meshes.values()) total += mesh.bytes;
            return total;
        }

        /** Whether anything will be drawn: a surface loaded, shown and visible. */
        active() {
            if (!this.show || !this.gl) return false;
            for (const mesh of this.meshes.values()) {
                if (mesh.style.visible !== false && mesh.count) return true;
            }
            return false;
        }

        has(id) { return this.meshes.has(id); }

        // -- meshes ----------------------------------------------------------

        _buffer(target, data) {
            const gl = this.gl;
            const buffer = gl.createBuffer();
            gl.bindBuffer(target, buffer);
            gl.bufferData(target, data, gl.STATIC_DRAW);
            this.uploads += 1;
            return buffer;
        }

        /**
         * Upload a mesh payload (an ArrayBuffer of `/surfaces/<id>/mesh`, or a
         * parsed one) under `id`. Replaces what `id` held. Returns its summary.
         */
        load(id, payload, style = {}) {
            const gl = this.gl;
            if (!gl) return null;
            const data = payload && payload.positions ? payload : parse(payload);
            this.unload(id, { keepStyle: true });
            const previous = this._styles?.get(id);
            const vao = gl.createVertexArray();
            gl.bindVertexArray(vao);
            const positions = this._buffer(gl.ARRAY_BUFFER, data.positions);
            gl.enableVertexAttribArray(0);
            gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 0, 0);
            const normals = this._buffer(gl.ARRAY_BUFFER, data.normals);
            gl.enableVertexAttribArray(1);
            gl.vertexAttribPointer(1, 4, gl.BYTE, true, 0, 0);
            const objects = this._buffer(gl.ARRAY_BUFFER, data.objectId);
            gl.enableVertexAttribArray(2);
            gl.vertexAttribIPointer(2, 1, gl.UNSIGNED_INT, 0, 0);
            const indices = this._buffer(gl.ELEMENT_ARRAY_BUFFER, data.triangles);
            gl.bindVertexArray(null);
            gl.bindBuffer(gl.ARRAY_BUFFER, null);
            let maxId = 0;
            for (let i = 0; i < data.objects.length; i += 3) maxId = Math.max(maxId, data.objects[i]);
            const mesh = {
                id, vao, positions, normals, objects, indices,
                count: data.triangles.length,
                vertices: data.positions.length / 3,
                ranges: data.objects,
                maxId,
                header: data.header,
                data,
                bytes: data.positions.byteLength + data.normals.byteLength
                    + data.objectId.byteLength + data.triangles.byteLength,
                edges: null,
                raw: null,
                lut: null,
                lutRows: 0,
                centroids: null,
                used: Date.now(),
                style: { color: "#d8b45a", opacity: 1, style: "solid", material: "soft",
                         visible: true, colorBy: "uniform", hidden: null, isolate: null,
                         compareRaw: false, ...(previous || {}), ...style },
            };
            this.meshes.set(id, mesh);
            this._refreshLut(mesh);
            this._evict(id);
            this.volume?.invalidate?.();
            return { triangles: mesh.count / 3, vertices: mesh.vertices, objects: data.objects.length / 3,
                     bytes: mesh.bytes };
        }

        unload(id, { keepStyle = false } = {}) {
            const mesh = this.meshes.get(id);
            if (!mesh) return false;
            if (keepStyle) {
                this._styles = this._styles || new Map();
                this._styles.set(id, { ...mesh.style });
            }
            const gl = this.gl;
            if (gl) {
                gl.deleteVertexArray(mesh.vao);
                if (mesh.raw) gl.deleteVertexArray(mesh.raw.vao);
                for (const buffer of [mesh.positions, mesh.normals, mesh.objects, mesh.indices,
                                      mesh.edges?.buffer, mesh.raw?.buffer]) {
                    if (buffer) gl.deleteBuffer(buffer);
                }
                if (mesh.lut) gl.deleteTexture(mesh.lut);
            }
            this.meshes.delete(id);
            this.volume?.invalidate?.();
            return true;
        }

        _evict(keep) {
            if (this.bytes() <= this.budget) return;
            const order = [...this.meshes.values()]
                .filter((mesh) => mesh.id !== keep && mesh.style.visible === false)
                .sort((a, b) => a.used - b.used);
            for (const mesh of order) {
                if (this.bytes() <= this.budget) break;
                this.unload(mesh.id);
            }
        }

        /** The preview the threshold slider draws (never a layer). */
        setPreview(payload, style = {}) {
            if (!payload) {
                this.unload(PREVIEW_ID);
                return null;
            }
            return this.load(PREVIEW_ID, payload, { style: "solid", material: "soft",
                                                    opacity: 0.85, ...style });
        }

        // -- style -----------------------------------------------------------

        /** Change how surface `id` is drawn: `{color, opacity, style,
         *  material, visible, colorBy, hidden, isolate, compareRaw, lut}`. A
         *  uniform or the colour table; never a buffer (bar the two lazies). */
        setStyle(id, patch = {}) {
            const mesh = this.meshes.get(id);
            if (!mesh) {
                this._styles = this._styles || new Map();
                this._styles.set(id, { ...(this._styles.get(id) || {}), ...patch });
                return false;
            }
            const before = mesh.style;
            mesh.style = { ...before, ...patch };
            const lutKeys = ["colorBy", "hidden", "isolate", "lut", "color"];
            if (lutKeys.some((key) => key in patch)) this._refreshLut(mesh);
            this.volume?.invalidate?.();
            return true;
        }

        styleOf(id) {
            return this.meshes.get(id)?.style || this._styles?.get(id) || null;
        }

        setSelected(selection) {
            this.selected = selection && selection.surface ? { ...selection } : null;
            this.volume?.invalidate?.();
        }

        setClipPlane(plane) {
            this.clipPlane = Array.isArray(plane) && plane.length === 4 ? plane.map(Number) : null;
            this.volume?.invalidate?.();
        }

        setLight(light = {}) {
            this.light = { ...this.light, ...light };
            this.volume?.invalidate?.();
        }

        setShow({ surfaces, volume } = {}) {
            if (surfaces !== undefined) this.show = surfaces !== false;
            if (volume !== undefined) this.hideVolume = volume === false;
            this.volume?.invalidate?.();
        }

        /**
         * The per-object colour table: RGBA by object id. Needed when objects
         * are hidden or isolated, or coloured one by one; otherwise none and
         * the surface's one colour is a uniform.
         */
        _refreshLut(mesh) {
            const style = mesh.style;
            const hidden = style.hidden ? new Set(style.hidden) : null;
            const isolate = style.isolate ? new Set(style.isolate) : null;
            const byObject = style.colorBy && style.colorBy !== "uniform";
            if (!byObject && !hidden?.size && !isolate) {
                mesh.useLut = false;
                return;
            }
            const gl = this.gl;
            const rows = Math.max(1, Math.ceil((mesh.maxId + 1) / LUT_WIDTH));
            const table = new Uint8Array(LUT_WIDTH * rows * 4);
            const uniform = hexToRgb(style.color);
            const given = style.lut?.colors || null;
            for (let i = 0; i < mesh.ranges.length; i += 3) {
                const id = mesh.ranges[i];
                let rgb = uniform;
                let alpha = 255;
                if (byObject) {
                    if (given && given.length >= (id + 1) * 4) {
                        rgb = [given[id * 4] / 255, given[id * 4 + 1] / 255, given[id * 4 + 2] / 255];
                        alpha = given[id * 4 + 3];
                    } else if (style.colorBy === "id") {
                        rgb = idColour(id);
                    }
                }
                if (hidden?.has(id) || (isolate && !isolate.has(id))) alpha = 0;
                table[id * 4] = Math.round(rgb[0] * 255);
                table[id * 4 + 1] = Math.round(rgb[1] * 255);
                table[id * 4 + 2] = Math.round(rgb[2] * 255);
                table[id * 4 + 3] = alpha;
            }
            if (!mesh.lut || mesh.lutRows !== rows) {
                if (mesh.lut) gl.deleteTexture(mesh.lut);
                mesh.lut = gl.createTexture();
                mesh.lutRows = rows;
                gl.bindTexture(gl.TEXTURE_2D, mesh.lut);
                gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
                gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
                gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
                gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
                gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, LUT_WIDTH, rows, 0, gl.RGBA,
                              gl.UNSIGNED_BYTE, table);
            } else {
                gl.bindTexture(gl.TEXTURE_2D, mesh.lut);
                gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, LUT_WIDTH, rows, gl.RGBA,
                                 gl.UNSIGNED_BYTE, table);
            }
            mesh.useLut = true;
            mesh.lutColour = Boolean(byObject);
        }

        _edges(mesh) {
            if (mesh.edges) return mesh.edges;
            const gl = this.gl;
            const tris = mesh.data.triangles;
            const lines = new Uint32Array(tris.length * 2);
            for (let i = 0, j = 0; i < tris.length; i += 3, j += 6) {
                lines[j] = tris[i]; lines[j + 1] = tris[i + 1];
                lines[j + 2] = tris[i + 1]; lines[j + 3] = tris[i + 2];
                lines[j + 4] = tris[i + 2]; lines[j + 5] = tris[i];
            }
            // Made with no vertex array bound: binding it inside the mesh's
            // would replace the mesh's own triangles there.
            gl.bindVertexArray(null);
            const buffer = this._buffer(gl.ELEMENT_ARRAY_BUFFER, lines);
            mesh.edges = { buffer, count: lines.length };
            mesh.bytes += lines.byteLength;
            return mesh.edges;
        }

        /** The unsmoothed positions, in a VAO of their own, for the compare view. */
        _rawVao(mesh) {
            if (mesh.raw) return mesh.raw.vao;
            if (!mesh.data.positionsRaw) return null;
            const gl = this.gl;
            const vao = gl.createVertexArray();
            gl.bindVertexArray(vao);
            const buffer = this._buffer(gl.ARRAY_BUFFER, mesh.data.positionsRaw);
            gl.enableVertexAttribArray(0);
            gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 0, 0);
            gl.bindBuffer(gl.ARRAY_BUFFER, mesh.normals);
            gl.enableVertexAttribArray(1);
            gl.vertexAttribPointer(1, 4, gl.BYTE, true, 0, 0);
            gl.bindBuffer(gl.ARRAY_BUFFER, mesh.objects);
            gl.enableVertexAttribArray(2);
            gl.vertexAttribIPointer(2, 1, gl.UNSIGNED_INT, 0, 0);
            gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, mesh.indices);
            gl.bindVertexArray(null);
            gl.bindBuffer(gl.ARRAY_BUFFER, null);
            mesh.raw = { vao, buffer };
            mesh.bytes += mesh.data.positionsRaw.byteLength;
            return vao;
        }

        // -- programs and targets ---------------------------------------------

        _program(pass) {
            let built = this.programs.get(pass);
            if (!built) {
                const gl = this.gl;
                built = pass === "composite"
                    ? link(gl, QUAD_VERTEX, COMPOSITE_FRAGMENT, ["a_position"])
                    : link(gl, MESH_VERTEX, meshFragment(pass));
                this.programs.set(pass, built);
            }
            return built;
        }

        _uniform(program, name) {
            let locations = this._uniforms.get(program);
            if (!locations) {
                locations = new Map();
                this._uniforms.set(program, locations);
            }
            if (!locations.has(name)) locations.set(name, this.gl.getUniformLocation(program, name));
            return locations.get(name);
        }

        _texture(width, height, internal, format, type, filter = "NEAREST") {
            const gl = this.gl;
            const texture = gl.createTexture();
            gl.bindTexture(gl.TEXTURE_2D, texture);
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl[filter]);
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl[filter]);
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
            gl.texImage2D(gl.TEXTURE_2D, 0, internal, width, height, 0, format, type, null);
            return texture;
        }

        _framebuffer(attachments) {
            const gl = this.gl;
            const fbo = gl.createFramebuffer();
            gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
            const buffers = [];
            for (const [point, texture] of attachments) {
                gl.framebufferTexture2D(gl.FRAMEBUFFER, point, gl.TEXTURE_2D, texture, 0);
                if (point !== gl.DEPTH_ATTACHMENT) buffers.push(point);
            }
            gl.drawBuffers(buffers.length ? buffers : [gl.NONE]);
            return fbo;
        }

        /** The frame's buffers at `width` x `height`, made again on a resize. */
        _targets(width, height) {
            const held = this.targets;
            if (held && held.width === width && held.height === height) return held;
            this._dropTargets();
            const gl = this.gl;
            const t = { width, height };
            t.opaqueColour = this._texture(width, height, gl.RGBA8, gl.RGBA, gl.UNSIGNED_BYTE);
            t.opaqueDepth = this._texture(width, height, gl.DEPTH_COMPONENT24, gl.DEPTH_COMPONENT,
                                          gl.UNSIGNED_INT);
            t.frontDepth = this._texture(width, height, gl.DEPTH_COMPONENT24, gl.DEPTH_COMPONENT,
                                         gl.UNSIGNED_INT);
            t.volumeFront = this._texture(width, height, gl.RGBA8, gl.RGBA, gl.UNSIGNED_BYTE);
            t.volumeBack = this._texture(width, height, gl.RGBA8, gl.RGBA, gl.UNSIGNED_BYTE);
            if (this.oit) {
                t.accum = this._texture(width, height, gl.RGBA16F, gl.RGBA, gl.HALF_FLOAT);
                t.weight = this._texture(width, height, gl.R16F, gl.RED, gl.HALF_FLOAT);
            } else {
                t.accum = this._texture(width, height, gl.RGBA8, gl.RGBA, gl.UNSIGNED_BYTE);
                t.weight = null;
            }
            t.A = this._framebuffer([[gl.COLOR_ATTACHMENT0, t.opaqueColour],
                                     [gl.DEPTH_ATTACHMENT, t.opaqueDepth]]);
            t.F = this._framebuffer([[gl.DEPTH_ATTACHMENT, t.frontDepth]]);
            t.W = this._framebuffer(t.weight
                ? [[gl.COLOR_ATTACHMENT0, t.accum], [gl.COLOR_ATTACHMENT1, t.weight],
                   [gl.DEPTH_ATTACHMENT, t.opaqueDepth]]
                : [[gl.COLOR_ATTACHMENT0, t.accum], [gl.DEPTH_ATTACHMENT, t.opaqueDepth]]);
            t.V = this._framebuffer([[gl.COLOR_ATTACHMENT0, t.volumeFront],
                                     [gl.COLOR_ATTACHMENT1, t.volumeBack]]);
            gl.bindFramebuffer(gl.FRAMEBUFFER, null);
            this.targets = t;
            return t;
        }

        _dropTargets() {
            const t = this.targets;
            const gl = this.gl;
            if (!t || !gl) return;
            for (const key of ["A", "F", "W", "V", "pick"]) if (t[key]) gl.deleteFramebuffer(t[key]);
            for (const key of ["opaqueColour", "opaqueDepth", "frontDepth", "volumeFront", "volumeBack",
                               "accum", "weight", "pickIds"]) {
                if (t[key]) gl.deleteTexture(t[key]);
            }
            if (t.pickDepth) gl.deleteRenderbuffer(t.pickDepth);
            this.targets = null;
        }

        // -- the frame ---------------------------------------------------------

        /** Where the box is: `{origin, scale, extent, clipZ}` for the volume's
         *  current area, frame and slab band. */
        placement() {
            const volume = this.volume;
            const vs = volume.viewState;
            const info = vs?.info || {};
            const depth = Math.max(1, vs?.depth || 1);
            const covered = volume.covered || [0, 0, Number(info.width) || 1, Number(info.height) || 1];
            const extent = volume.extent();
            const span = [covered[2] - covered[0], covered[3] - covered[1], depth];
            const state = volume.view || vs?.get?.();
            const clipZ = typeof volume.clipRangeZ === "function"
                ? volume.clipRangeZ(state, depth)
                : (state ? [state.slab.z0 / depth, (state.slab.z1 + 1) / depth] : [0, 1]);
            // The box cut open from the side (the volume's setClip), as the voxels are.
            const xy = volume.clipXY || { x: [0, 1], y: [0, 1] };
            const clipXY = [xy.x[0], xy.x[1], xy.y[0], xy.y[1]];
            return { origin: [covered[0], covered[1], 0], extent, clipZ, clipXY,
                     scale: extent.map((side, i) => side / Math.max(1e-9, span[i])) };
        }

        /** The camera this frame: view, projection (forward = projection x
         *  view, exactly the ray caster's), and where the box is. */
        _frame(width, height) {
            const camera = this.volume.cameraMatrices();
            const mat4 = global.PlexoraVolumeRenderer?.mat4;
            const back = mat4?.invert(camera.view);
            const projection = back ? mat4.multiply(camera.forward, back) : camera.forward;
            return { view: camera.view, forward: camera.forward, projection,
                     placement: this.placement(), width, height };
        }

        _lightDir() {
            const az = (this.light.azimuth * Math.PI) / 180;
            const el = (this.light.elevation * Math.PI) / 180;
            return [Math.sin(az) * Math.cos(el), Math.sin(el), Math.cos(az) * Math.cos(el)];
        }

        _setCommon(program, frame, mesh, opacityOverride = null) {
            const gl = this.gl;
            const u = (name) => this._uniform(program, name);
            gl.uniformMatrix4fv(u("u_projection"), false, frame.projection);
            gl.uniformMatrix4fv(u("u_view"), false, frame.view);
            gl.uniform3fv(u("u_origin"), frame.placement.origin);
            gl.uniform3fv(u("u_scale"), frame.placement.scale);
            gl.uniform3fv(u("u_extent"), frame.placement.extent);
            gl.uniform2fv(u("u_clip_z"), frame.placement.clipZ);
            gl.uniform4fv(u("u_clip_xy"), frame.placement.clipXY || [0, 1, 0, 1]);
            gl.uniform1i(u("u_use_clip_plane"), this.clipPlane ? 1 : 0);
            gl.uniform4fv(u("u_clip_plane"), this.clipPlane || [0, 0, 1, 0]);
            const style = mesh.style;
            const look = style.style || "solid";
            const material = MATERIALS[look === "illustration" ? "matte"
                : (look === "glass" ? "glass" : style.material)] || MATERIALS.soft;
            gl.uniform3fv(u("u_color"), hexToRgb(style.color));
            gl.uniform1f(u("u_opacity"), opacityOverride ?? this.opacityOf(mesh));
            gl.uniform4fv(u("u_material"), [material.ambient, material.diffuse, material.specular,
                                             material.shininess]);
            gl.uniform1f(u("u_rim"), material.rim);
            gl.uniform3fv(u("u_light"), this._lightDir());
            gl.uniform1i(u("u_flat"), look === "illustration" ? 1 : 0);
            gl.uniform1i(u("u_flatColour"), 0);
            gl.uniform1f(u("u_inflate"), 0);
            const selected = this.selected && this.selected.surface === mesh.id
                ? Number(this.selected.object) || 0 : 0;
            gl.uniform1ui(u("u_selected"), selected);
            gl.uniform1i(u("u_useLut"), mesh.useLut ? 1 : 0);
            gl.uniform1i(u("u_lutColour"), mesh.lutColour ? 1 : 0);
            gl.activeTexture(gl.TEXTURE0);
            gl.bindTexture(gl.TEXTURE_2D, mesh.useLut ? mesh.lut : this._white());
            gl.uniform1i(u("u_lut"), 0);
        }

        _white() {
            if (this._whiteLut) return this._whiteLut;
            const gl = this.gl;
            this._whiteLut = gl.createTexture();
            gl.bindTexture(gl.TEXTURE_2D, this._whiteLut);
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
            gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE,
                          new Uint8Array([255, 255, 255, 255]));
            return this._whiteLut;
        }

        opacityOf(mesh) {
            const style = mesh.style;
            return Number.isFinite(Number(style.opacity)) ? Number(style.opacity) : 1;
        }

        translucent(mesh) {
            return this.opacityOf(mesh) < 0.995;
        }

        _drawTriangles(mesh) {
            const gl = this.gl;
            const vao = mesh.style.compareRaw ? (this._rawVao(mesh) || mesh.vao) : mesh.vao;
            gl.bindVertexArray(vao);
            gl.drawElements(gl.TRIANGLES, mesh.count, gl.UNSIGNED_INT, 0);
        }

        _drawEdges(mesh, program, rgb) {
            const gl = this.gl;
            const edges = this._edges(mesh);
            const u = (name) => this._uniform(program, name);
            gl.uniform1i(u("u_flatColour"), 1);
            gl.uniform3fv(u("u_flatRgb"), rgb);
            gl.bindVertexArray(mesh.vao);
            gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, edges.buffer);
            gl.drawElements(gl.LINES, edges.count, gl.UNSIGNED_INT, 0);
            gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, mesh.indices);
            gl.uniform1i(u("u_flatColour"), 0);
        }

        _visible() {
            return [...this.meshes.values()].filter((mesh) => mesh.style.visible !== false && mesh.count);
        }

        /**
         * Draw the surfaces' passes and leave buffer V bound for the volume's
         * ray caster. `channels` are what the volume draws this frame (their
         * area sets the box). Null -- and nothing done -- when no surface shows.
         */
        begin(channels = [], { fast = false } = {}) {
            if (!this.active()) return null;
            const gl = this.gl;
            const volume = this.volume;
            const canvas = volume.canvas;
            if (!gl || !canvas) return null;
            // The box the channels drawn this frame set, before it is used
            // (the volume sets the same just after; see its draw()).
            const lead = channels[0]?.entry;
            if (lead) {
                volume.covered = lead.covered;
                volume.shape = channels.reduce((most, channel) => {
                    const shape = channel.entry.shape;
                    return shape[0] * shape[1] * shape[2] > most[0] * most[1] * most[2] ? shape : most;
                }, lead.shape);
            }
            const width = canvas.width;
            const height = canvas.height;
            const targets = this._targets(width, height);
            const frame = this._frame(width, height);
            frame.fast = fast;
            this.frame = frame;
            const visible = this._visible();
            const now = Date.now();
            for (const mesh of visible) mesh.used = now;
            const solid = visible.filter((mesh) => !this.translucent(mesh));
            const glassy = visible.filter((mesh) => this.translucent(mesh));

            gl.disable(gl.BLEND);
            gl.disable(gl.CULL_FACE);
            gl.enable(gl.DEPTH_TEST);
            gl.depthFunc(gl.LESS);
            gl.depthMask(true);
            gl.viewport(0, 0, width, height);

            // 1. Opaque.
            gl.bindFramebuffer(gl.FRAMEBUFFER, targets.A);
            gl.clearColor(0, 0, 0, 0);
            gl.clearDepth(1);
            gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
            const opaque = this._program("opaque");
            gl.useProgram(opaque);
            for (const mesh of solid) this._drawOpaque(mesh, opaque, frame);

            // 2. Translucent: nearest depth, then the blend.
            gl.bindFramebuffer(gl.FRAMEBUFFER, targets.F);
            gl.clear(gl.DEPTH_BUFFER_BIT);
            if (glassy.length) {
                const depth = this._program("depth");
                gl.useProgram(depth);
                gl.activeTexture(gl.TEXTURE0 + this.maxUnits - 1);
                gl.bindTexture(gl.TEXTURE_2D, targets.opaqueDepth);
                gl.uniform1i(this._uniform(depth, "u_opaqueDepth"), this.maxUnits - 1);
                for (const mesh of glassy) {
                    this._setCommon(depth, frame, mesh);
                    this._drawTriangles(mesh);
                }
                gl.activeTexture(gl.TEXTURE0 + this.maxUnits - 1);
                gl.bindTexture(gl.TEXTURE_2D, null);
            }
            gl.bindFramebuffer(gl.FRAMEBUFFER, targets.W);
            if (this.oit) {
                gl.clearBufferfv(gl.COLOR, 0, [0, 0, 0, 1]);
                gl.clearBufferfv(gl.COLOR, 1, [0, 0, 0, 0]);
            } else {
                gl.clearBufferfv(gl.COLOR, 0, [0, 0, 0, 0]);
            }
            frame.trans = 0;
            if (glassy.length) {
                gl.depthMask(false);
                gl.enable(gl.BLEND);
                if (this.oit) {
                    gl.blendFuncSeparate(gl.ONE, gl.ONE, gl.ZERO, gl.ONE_MINUS_SRC_ALPHA);
                    const wboit = this._program("wboit");
                    gl.useProgram(wboit);
                    for (const mesh of glassy) {
                        this._setCommon(wboit, frame, mesh);
                        this._drawTriangles(mesh);
                    }
                    frame.trans = 1;
                } else {
                    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
                    const sorted = this._program("sorted");
                    gl.useProgram(sorted);
                    this._drawSorted(glassy, sorted, frame);
                    frame.trans = 2;
                }
                gl.disable(gl.BLEND);
                gl.depthMask(true);
            }
            gl.bindVertexArray(null);
            gl.disable(gl.DEPTH_TEST);

            // 3. The volume renders next, into V's two outputs.
            gl.bindFramebuffer(gl.FRAMEBUFFER, targets.V);
            gl.drawBuffers([gl.COLOR_ATTACHMENT0, gl.COLOR_ATTACHMENT1]);
            gl.clearBufferfv(gl.COLOR, 0, [0, 0, 0, 0]);
            gl.clearBufferfv(gl.COLOR, 1, [0, 0, 0, 0]);
            gl.viewport(0, 0, width, height);
            const self = this;
            return {
                frame,
                /** Bind the depth the ray caster stops and splits at. */
                bindDepth(uniform) {
                    const units = [self.maxUnits - 1, self.maxUnits - 2];
                    gl.activeTexture(gl.TEXTURE0 + units[0]);
                    gl.bindTexture(gl.TEXTURE_2D, targets.opaqueDepth);
                    gl.uniform1i(uniform("u_depth_opaque"), units[0]);
                    gl.activeTexture(gl.TEXTURE0 + units[1]);
                    gl.bindTexture(gl.TEXTURE_2D, targets.frontDepth);
                    gl.uniform1i(uniform("u_depth_front"), units[1]);
                },
                /** Composite everything onto the canvas. */
                finish(volumeDrawn = true) {
                    self._composite(targets, frame, Boolean(volumeDrawn));
                },
            };
        }

        _drawOpaque(mesh, program, frame) {
            const gl = this.gl;
            const look = mesh.style.style || "solid";
            this._setCommon(program, frame, mesh, 1);
            if (look === "outline" || look === "illustration") {
                // The inverted hull: back faces pushed out along the normal,
                // drawn dark, then the surface over them -- a silhouette line
                // about two pixels wide whatever the distance.
                const pixel = (2 * Math.tan(0.35)) / Math.max(1, frame.height);
                gl.enable(gl.CULL_FACE);
                gl.cullFace(gl.FRONT);
                gl.uniform1f(this._uniform(program, "u_inflate"), pixel * 2.2);
                gl.uniform1i(this._uniform(program, "u_flatColour"), 1);
                gl.uniform3fv(this._uniform(program, "u_flatRgb"), [0.06, 0.07, 0.09]);
                this._drawTriangles(mesh);
                gl.disable(gl.CULL_FACE);
                gl.uniform1f(this._uniform(program, "u_inflate"), 0);
                gl.uniform1i(this._uniform(program, "u_flatColour"), 0);
            }
            if (look === "wireframe") {
                const rgb = hexToRgb(mesh.style.color);
                this._drawEdges(mesh, program, rgb);
                return;
            }
            if (look === "solid+wire") {
                gl.enable(gl.POLYGON_OFFSET_FILL);
                gl.polygonOffset(1, 1);
                this._drawTriangles(mesh);
                gl.disable(gl.POLYGON_OFFSET_FILL);
                const rgb = hexToRgb(mesh.style.color).map((v) => v * 0.45);
                this._drawEdges(mesh, program, rgb);
                return;
            }
            this._drawTriangles(mesh);
        }

        /** Without float targets: translucent objects back to front, per object. */
        _drawSorted(meshes, program, frame) {
            const gl = this.gl;
            const view = frame.view;
            const items = [];
            for (const mesh of meshes) {
                const centroids = this._centroids(mesh, frame);
                for (let k = 0; k < mesh.ranges.length / 3; k += 1) {
                    const [x, y, z] = centroids.subarray(k * 3, k * 3 + 3);
                    const depth = view[2] * x + view[6] * y + view[10] * z + view[14];
                    items.push({ mesh, first: mesh.ranges[k * 3 + 1], count: mesh.ranges[k * 3 + 2], depth });
                }
            }
            items.sort((a, b) => a.depth - b.depth);   // most negative (farthest) first
            let current = null;
            for (const item of items) {
                if (item.mesh !== current) {
                    current = item.mesh;
                    this._setCommon(program, frame, current);
                    gl.bindVertexArray(current.vao);
                }
                gl.drawElements(gl.TRIANGLES, item.count * 3, gl.UNSIGNED_INT, item.first * 12);
            }
        }

        /** Each object's centroid in the box's world space (for sorting). */
        _centroids(mesh, frame) {
            const key = `${frame.placement.origin}|${frame.placement.scale}`;
            if (mesh.centroids && mesh.centroidsKey === key) return mesh.centroids;
            const { positions, triangles } = mesh.data;
            const ranges = mesh.ranges;
            const out = new Float32Array(ranges.length);
            const { origin, scale, extent } = frame.placement;
            for (let k = 0; k < ranges.length / 3; k += 1) {
                const first = ranges[k * 3 + 1];
                const count = ranges[k * 3 + 2];
                let sx = 0, sy = 0, sz = 0;
                const step = Math.max(1, Math.floor(count / 64));
                let n = 0;
                for (let t = first; t < first + count; t += step) {
                    const v = triangles[t * 3] * 3;
                    sx += positions[v]; sy += positions[v + 1]; sz += positions[v + 2];
                    n += 1;
                }
                const c = [sx / n, sy / n, sz / n];
                for (let a = 0; a < 3; a += 1) {
                    out[k * 3 + a] = (c[a] + 0.5 - origin[a]) * scale[a] - 0.5 * extent[a];
                }
            }
            mesh.centroids = out;
            mesh.centroidsKey = key;
            return out;
        }

        _quadVao() {
            if (this._quad) return this._quad;
            const gl = this.gl;
            this._quad = gl.createVertexArray();
            gl.bindVertexArray(this._quad);
            gl.bindBuffer(gl.ARRAY_BUFFER, this.volume.quad);
            gl.enableVertexAttribArray(0);
            gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
            gl.bindVertexArray(null);
            return this._quad;
        }

        _composite(targets, frame, volumeDrawn) {
            const gl = this.gl;
            gl.bindFramebuffer(gl.FRAMEBUFFER, null);
            gl.viewport(0, 0, frame.width, frame.height);
            gl.disable(gl.DEPTH_TEST);
            gl.disable(gl.BLEND);
            const program = this._program("composite");
            gl.useProgram(program);
            const bind = (unit, texture, name) => {
                gl.activeTexture(gl.TEXTURE0 + unit);
                gl.bindTexture(gl.TEXTURE_2D, texture);
                gl.uniform1i(this._uniform(program, name), unit);
            };
            bind(0, targets.opaqueColour, "u_opaque");
            bind(1, targets.volumeFront, "u_front");
            bind(2, targets.volumeBack, "u_back");
            bind(3, targets.accum, "u_accum");
            bind(4, targets.weight || targets.accum, "u_weight");
            gl.uniform1i(this._uniform(program, "u_trans"), frame.trans || 0);
            gl.uniform1i(this._uniform(program, "u_volume"), volumeDrawn ? 1 : 0);
            gl.uniform3fv(this._uniform(program, "u_ground"), [0, 0, 0]);
            gl.bindVertexArray(this._quadVao());
            gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
            gl.bindVertexArray(null);
            // Nothing of the frame's left bound where the next one draws into it.
            for (let unit = 0; unit < 5; unit += 1) {
                gl.activeTexture(gl.TEXTURE0 + unit);
                gl.bindTexture(gl.TEXTURE_2D, null);
            }
            for (const unit of [this.maxUnits - 1, this.maxUnits - 2]) {
                gl.activeTexture(gl.TEXTURE0 + unit);
                gl.bindTexture(gl.TEXTURE_2D, null);
            }
            gl.activeTexture(gl.TEXTURE0);
            this._lastDraw = Date.now();
        }

        // -- picking -----------------------------------------------------------

        /**
         * What is under canvas point (`x`, `y`) in CSS pixels: `{surface,
         * object, point: [x, y, z] level-0 voxels, depth}`, or null. Drawn
         * into one scissored pixel of an integer buffer, so it costs one
         * pass over the visible meshes and a one-pixel read.
         */
        pick(x, y) {
            const gl = this.gl;
            const volume = this.volume;
            const canvas = volume?.canvas;
            if (!gl || !canvas || !this.active()) return null;
            const visible = this._visible();
            if (!visible.length) return null;
            const width = canvas.width;
            const height = canvas.height;
            const px = Math.floor((x / Math.max(1, canvas.clientWidth)) * width);
            const py = height - 1 - Math.floor((y / Math.max(1, canvas.clientHeight)) * height);
            if (px < 0 || py < 0 || px >= width || py >= height) return null;
            const targets = this._targets(width, height);
            if (!targets.pick) {
                targets.pickIds = this._texture(width, height, gl.RGBA32UI, gl.RGBA_INTEGER,
                                                gl.UNSIGNED_INT);
                targets.pickDepth = gl.createRenderbuffer();
                gl.bindRenderbuffer(gl.RENDERBUFFER, targets.pickDepth);
                gl.renderbufferStorage(gl.RENDERBUFFER, gl.DEPTH_COMPONENT24, width, height);
                targets.pick = gl.createFramebuffer();
                gl.bindFramebuffer(gl.FRAMEBUFFER, targets.pick);
                gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D,
                                        targets.pickIds, 0);
                gl.framebufferRenderbuffer(gl.FRAMEBUFFER, gl.DEPTH_ATTACHMENT, gl.RENDERBUFFER,
                                           targets.pickDepth);
            }
            const frame = this._frame(width, height);
            gl.bindFramebuffer(gl.FRAMEBUFFER, targets.pick);
            gl.viewport(0, 0, width, height);
            gl.enable(gl.SCISSOR_TEST);
            gl.scissor(px, py, 1, 1);
            gl.clearBufferuiv(gl.COLOR, 0, [0, 0, 0, 0]);
            gl.clear(gl.DEPTH_BUFFER_BIT);
            gl.enable(gl.DEPTH_TEST);
            gl.depthFunc(gl.LESS);
            gl.depthMask(true);
            gl.disable(gl.BLEND);
            const program = this._program("pick");
            gl.useProgram(program);
            visible.forEach((mesh, index) => {
                if (mesh.id === PREVIEW_ID) return;
                this._setCommon(program, frame, mesh);
                gl.uniform1ui(this._uniform(program, "u_surfaceIndex"), index);
                this._drawTriangles(mesh);
            });
            const out = new Uint32Array(4);
            gl.readPixels(px, py, 1, 1, gl.RGBA_INTEGER, gl.UNSIGNED_INT, out);
            gl.disable(gl.SCISSOR_TEST);
            gl.disable(gl.DEPTH_TEST);
            gl.bindVertexArray(null);
            gl.bindFramebuffer(gl.FRAMEBUFFER, null);
            if (!out[1]) return null;
            const mesh = visible[out[1] - 1];
            const depth = new Float32Array(new Uint32Array([out[2]]).buffer)[0];
            const point = this.unproject(px + 0.5, py + 0.5, depth, frame);
            return { surface: mesh.id, object: out[0], depth, point };
        }

        /** A buffer pixel and its depth back to level-0 voxels. */
        unproject(px, py, depth, frame = this.frame) {
            const mat4 = global.PlexoraVolumeRenderer?.mat4;
            if (!mat4 || !frame) return null;
            const inverse = mat4.invert(frame.forward);
            if (!inverse) return null;
            const ndc = [(2 * px) / frame.width - 1, (2 * py) / frame.height - 1, 2 * depth - 1, 1];
            const out = [0, 0, 0, 0];
            for (let row = 0; row < 4; row += 1) {
                for (let k = 0; k < 4; k += 1) out[row] += inverse[k * 4 + row] * ndc[k];
            }
            const world = [out[0] / out[3], out[1] / out[3], out[2] / out[3]];
            const { origin, scale, extent } = frame.placement;
            return world.map((w, a) => (w + 0.5 * extent[a]) / scale[a] + origin[a] - 0.5);
        }

        lost() {
            this.meshes.clear();
            this.programs.clear();
            this.targets = null;
            this._quad = null;
            this._whiteLut = null;
        }

        destroy() {
            for (const id of [...this.meshes.keys()]) this.unload(id);
            this._dropTargets();
            const gl = this.gl;
            if (gl) for (const program of this.programs.values()) gl.deleteProgram(program);
            this.programs.clear();
        }
    }

    SurfaceRenderer.parse = parse;
    SurfaceRenderer.volumeFragment = volumeFragment;
    SurfaceRenderer.meshFragment = meshFragment;
    SurfaceRenderer.idColour = idColour;
    SurfaceRenderer.MATERIALS = MATERIALS;
    SurfaceRenderer.STYLES = STYLES;
    SurfaceRenderer.PREVIEW_ID = PREVIEW_ID;
    SurfaceRenderer.LUT_WIDTH = LUT_WIDTH;
    SurfaceRenderer.SHADERS = { MESH_VERTEX, COMPOSITE_FRAGMENT, QUAD_VERTEX };
    global.PlexoraSurfaceRenderer = SurfaceRenderer;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = SurfaceRenderer;
    }
})(typeof window !== "undefined" ? window : globalThis);
