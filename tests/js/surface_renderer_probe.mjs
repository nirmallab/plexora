/**
 * Surfaces in the 3-D view (views/surfaceRenderer.js), against a WebGL2
 * context that records what it is asked.
 *
 * Pinned:
 * - the mesh payload (server/surface/mesh.py) is read with no copy, and a
 *   mesh is uploaded once: positions, normals, object ids, triangles;
 * - colour, opacity, style, material, lighting, hidden and isolated objects
 *   and the camera never touch a buffer (the wireframe's edges are made
 *   once, the first time it is asked for);
 * - translucent surfaces blend order-independently with ONE blend state for
 *   both targets, ONE, ONE, ZERO, ONE_MINUS_SRC_ALPHA; without float targets
 *   they are sorted and blended over, and `capabilities()` says which;
 * - with nothing shown the volume draws exactly as before (no pass, the
 *   opaque clear); "surfaces only" draws with no channel; the ray caster's
 *   surface variant stops and splits at the surfaces in every variant
 *   (plain, detail, atlas), and the plain one is untouched;
 * - a buffer pixel and its depth unproject back to the voxel they came from.
 *
 * Run directly:  node tests/js/surface_renderer_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const JS = join(REPO, "plexora/client/src/js");

const failures = [];
let checks = 0;
function check(label, ok, why) {
    checks += 1;
    console.log(`${ok ? "ok" : "FAIL"} - ${label}`);
    if (!ok) failures.push(`${label}${why ? ` -- ${why}` : ""}`);
}

const calls = [];
let floatTargets = true;
let pickAnswer = [0, 0, 0, 0];
function fakeGl() {
    let id = 0;
    return new Proxy({}, {
        get(target, name) {
            if (typeof name !== "string") return undefined;
            if (/^[A-Z_0-9]+$/.test(name)) return name;
            return (...args) => {
                calls.push([name, args]);
                if (name.startsWith("create")) return { id: (id += 1), kind: name };
                if (name === "getExtension") return args[0] === "EXT_color_buffer_float" && floatTargets ? {} : null;
                if (name === "getParameter") return args[0] === "MAX_TEXTURE_IMAGE_UNITS" ? 16 : 2048;
                if (name === "getShaderParameter" || name === "getProgramParameter") return true;
                if (name === "getUniformLocation") return { uniform: args[1] };
                if (name === "readPixels") args[6].set(pickAnswer);
                return undefined;
            };
        },
    });
}
const count = (name) => calls.filter(([n]) => n === name).length;
const since = (mark, name) => calls.slice(mark).filter(([n]) => n === name).length;

const box = {
    console, setTimeout, clearTimeout, performance, URLSearchParams, TextDecoder,
    requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    dispatchEvent: () => true,
    document: { createElement: () => ({ getContext: () => ({ drawImage() {} }) }), getElementById: () => null },
    __plexora: { viewerSidebar: { channelSlots: [] } },
};
box.window = box;
box.globalThis = box;
const context = createContext(box);
for (const file of ["views/volumeCamera.js", "views/volumeRenderer.js", "views/surfaceRenderer.js"]) {
    runInContext(readFileSync(join(JS, file), "utf8"), context);
}
const VolumeRenderer = box.PlexoraVolumeRenderer;
const SurfaceRenderer = box.PlexoraSurfaceRenderer;

/** A PXMS payload: two tetrahedra, objects 3 and 9. */
function payload() {
    const positions = new Float32Array([0, 0, 0, 4, 0, 0, 0, 4, 0, 0, 0, 4,
                                        10, 10, 2, 14, 10, 2, 10, 14, 2, 10, 10, 6]);
    const normals = new Int8Array(8 * 4).fill(73);
    const objectId = new Uint32Array([3, 3, 3, 3, 9, 9, 9, 9]);
    const triangles = new Uint32Array([0, 2, 1, 0, 1, 3, 0, 3, 2, 1, 2, 3,
                                       4, 6, 5, 4, 5, 7, 4, 7, 6, 5, 6, 7]);
    const objects = new Uint32Array([3, 0, 4, 9, 4, 4]);
    const arrays = [["positions", "f4", [8, 3], positions], ["normals", "i1", [8, 4], normals],
                    ["object_id", "u4", [8], objectId], ["triangles", "u4", [8, 3], triangles],
                    ["objects", "u4", [2, 3], objects]];
    const headerFor = (start) => {
        let offset = start;
        const table = arrays.map(([name, dtype, shape, data]) => {
            const entry = { name, dtype, shape, offset };
            offset += data.byteLength;
            return entry;
        });
        return { format: "plexora-mesh", version: 1, vertices: 8, triangles: 8, objects: 2, arrays: table };
    };
    let text = JSON.stringify(headerFor(0));
    let start = 8 + new TextEncoder().encode(text).length;
    start += (8 - (start % 8)) % 8 + 64;
    text = JSON.stringify(headerFor(start)).padEnd(start - 8, " ");
    const head = new TextEncoder().encode(text);
    const total = start + arrays.reduce((sum, a) => sum + a[3].byteLength, 0);
    const out = new Uint8Array(total);
    out.set([80, 88, 77, 83], 0);
    new DataView(out.buffer).setUint32(4, head.length, true);
    out.set(head, 8);
    let at = start;
    for (const [, , , data] of arrays) {
        out.set(new Uint8Array(data.buffer), at);
        at += data.byteLength;
    }
    return out.buffer;
}

const parsed = SurfaceRenderer.parse(payload());
check("a payload is read into views over its own buffer",
      parsed.positions.length === 24 && parsed.triangles.length === 24
      && parsed.objectId[4] === 9 && parsed.objects.length === 6 && parsed.header.objects === 2);

function volumeFor() {
    const viewState = { depth: 16, info: { width: 64, height: 64, spacing: {} },
                        get: () => ({ t: 0, slab: { z0: 0, z1: 15 }, volume: {} }) };
    const volume = new VolumeRenderer({ viewState, datasource: "vol", fetchImpl: () => null });
    volume.gl = fakeGl();
    volume.canvas = { width: 200, height: 100, clientWidth: 200, clientHeight: 100, hidden: false };
    volume.quad = { quad: true };
    volume.surfaces = new SurfaceRenderer(volume);
    return volume;
}

let volume = volumeFor();
let surfaces = volume.surfaces;
check("nothing loaded, nothing shown: begin answers null", surfaces.begin([]) === null && !surfaces.active());
let mark = calls.length;
volume.draw();
check("with no surface the volume clears opaque as it always did",
      calls.slice(mark).some(([n, a]) => n === "clearColor" && a[3] === 1)
      && since(mark, "bindFramebuffer") === 0);

mark = calls.length;
const loaded = surfaces.load("surf-a", payload(), { color: "#ff8800" });
check("a mesh is uploaded once: four buffers", since(mark, "bufferData") === 4
      && loaded.objects === 2 && loaded.triangles === 8, JSON.stringify(loaded));
check("a loaded, visible surface is active", surfaces.active());

mark = calls.length;
surfaces.setStyle("surf-a", { color: "#00ff00" });
surfaces.setStyle("surf-a", { opacity: 0.5, material: "glossy" });
surfaces.setStyle("surf-a", { style: "outline" });
surfaces.setStyle("surf-a", { hidden: [9] });
surfaces.setStyle("surf-a", { isolate: [3], colorBy: "id" });
surfaces.setLight({ azimuth: 10 });
volume.camera.q = [0, 0, 0.6, 0.8];
volume.draw();
check("colour, opacity, style, material, hiding, light and camera touch no buffer",
      since(mark, "bufferData") === 0);
check("hiding objects is a colour table (a texture), not a buffer", since(mark, "texImage2D") >= 1);

surfaces.setStyle("surf-a", { hidden: null, isolate: null, colorBy: "uniform", style: "solid", opacity: 1 });
mark = calls.length;
volume.draw();
const blended = calls.slice(mark).filter(([n]) => n === "blendFuncSeparate" || n === "blendFunc");
check("opaque surfaces need no blending", blended.length === 0);
check("surfaces only (no channel on): composited onto the canvas",
      calls.slice(mark).some(([n, a]) => n === "bindFramebuffer" && a[1] === null)
      && calls.slice(mark).some(([n]) => n === "drawArrays"));
check("the volume's buffer is cleared transparent when surfaces draw",
      calls.slice(mark).some(([n, a]) => n === "clearColor" && a[3] === 0));

surfaces.setStyle("surf-a", { style: "glass", opacity: 0.35 });
mark = calls.length;
volume.draw();
const oit = calls.slice(mark).filter(([n]) => n === "blendFuncSeparate");
check("glass blends order-independently with one blend state for both targets",
      oit.length === 1 && oit[0][1].join() === "ONE,ONE,ZERO,ONE_MINUS_SRC_ALPHA", JSON.stringify(oit));
check("weighted blending clears revealage to one and the weights to zero",
      calls.slice(mark).some(([n, a]) => n === "clearBufferfv" && a[1] === 0 && a[2].join() === "0,0,0,1")
      && calls.slice(mark).some(([n, a]) => n === "clearBufferfv" && a[1] === 1 && a[2].join() === "0,0,0,0"));
check("weighted blending is reported", surfaces.capabilities().oit === "weighted-blended");

mark = calls.length;
surfaces.setStyle("surf-a", { style: "wireframe", opacity: 1 });
volume.draw();
volume.draw();
check("the wireframe's edges are made once", since(mark, "bufferData") === 1
      && calls.slice(mark).some(([n, a]) => n === "drawElements" && a[0] === "LINES"));

surfaces.setShow({ surfaces: false });
check("surfaces switched off: nothing active", !surfaces.active() && surfaces.begin([]) === null);
surfaces.setShow({ surfaces: true, volume: false });
check("volume switched off: surfaces still drawn", surfaces.active() && surfaces.hideVolume);

// Without float render targets: sorted and blended over.
floatTargets = false;
volume = volumeFor();
surfaces = volume.surfaces;
surfaces.load("surf-b", payload(), { style: "glass", opacity: 0.35 });
mark = calls.length;
volume.draw();
const over = calls.slice(mark).filter(([n]) => n === "blendFunc");
check("without float targets glass is sorted and blended over",
      surfaces.capabilities().oit === "sorted" && over.length === 1
      && over[0][1].join() === "ONE,ONE_MINUS_SRC_ALPHA"
      && calls.slice(mark).filter(([n, a]) => n === "drawElements" && a[0] === "TRIANGLES").length >= 2);
floatTargets = true;

// The ray caster's surface variant, made from the volume's own source.
for (const [detail, atlas] of [[false, false], [true, false], [false, true]]) {
    if (atlas && !box.PlexoraVolumeAtlas) {
        runInContext(readFileSync(join(JS, "views/volumeAtlas.js"), "utf8"), context);
    }
    const plain = VolumeRenderer.fragmentSource(2, detail, atlas);
    let variant = "";
    let error = null;
    try {
        variant = SurfaceRenderer.volumeFragment(plain);
    } catch (thrown) {
        error = thrown;
    }
    check(`the surface variant (detail ${detail}, atlas ${atlas}) stops and splits at surfaces`,
          !error && variant.includes("outFront") && variant.includes("outBack")
          && variant.includes("u_depth_opaque") && variant.includes("u_depth_front")
          && !variant.includes("outColor") && variant.includes("bool inFront = t < tFront;"),
          error?.message);
    check(`the plain shader (detail ${detail}, atlas ${atlas}) is untouched`,
          !plain.includes("u_depth_opaque") && plain.includes("out vec4 outColor;"));
}

// Picking: one scissored pixel, read back as ids and depth.
volume = volumeFor();
surfaces = volume.surfaces;
surfaces.load("surf-c", payload());
pickAnswer = [0, 0, 0, 0];
check("a click on nothing picks nothing", surfaces.pick(50, 50) === null);
const depthBits = new Uint32Array(new Float32Array([0.6]).buffer)[0];
pickAnswer = [9, 1, depthBits, 1];
const hit = surfaces.pick(50, 50);
check("a click on a surface names its object and where", hit && hit.surface === "surf-c"
      && hit.object === 9 && hit.point?.length === 3 && Math.abs(hit.depth - 0.6) < 1e-6,
      JSON.stringify(hit));
check("picking scissors to one pixel", calls.some(([n, a]) => n === "scissor" && a[2] === 1 && a[3] === 1));

// Unproject: a voxel projected into the buffer comes back.
const frame = surfaces._frame(200, 100);
const voxel = [20, 30, 7];
const { origin, scale, extent } = frame.placement;
const world = voxel.map((v, a) => (v + 0.5 - origin[a]) * scale[a] - 0.5 * extent[a]);
const m = frame.forward;
const clip = [0, 1, 2, 3].map((row) => m[row] * world[0] + m[4 + row] * world[1]
                                     + m[8 + row] * world[2] + m[12 + row]);
const ndc = clip.slice(0, 3).map((v) => v / clip[3]);
const back = surfaces.unproject((ndc[0] + 1) * 100, (ndc[1] + 1) * 50, (ndc[2] + 1) / 2, frame);
check("a buffer pixel and its depth unproject to the voxel they came from",
      back && back.every((v, a) => Math.abs(v - voxel[a]) < 1e-3), JSON.stringify(back));

// Unloading frees what was uploaded.
mark = calls.length;
surfaces.unload("surf-c");
check("unloading deletes its buffers", since(mark, "deleteBuffer") >= 4 && !surfaces.has("surf-c"));

if (failures.length) {
    console.log(`\n${failures.length} of ${checks} checks FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log(`\nall ${checks} checks passed`);
