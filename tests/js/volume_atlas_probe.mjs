/**
 * Full resolution where the 3-D view needs it (views/volumeAtlas.js).
 *
 * The atlas runs here against a WebGL2 context that records uploads, and a
 * brick route that answers from a pattern. Pinned:
 *
 * - the levels it uses are those finer than the base, finest first, each
 *   with its brick's extent, voxel size and grid in x, y, z, stacked in the
 *   page table; the atlas's slots fit its budget and the texture size;
 * - the cut: near the camera the finest level, far off coarser ones, the
 *   base alone where its voxels are already under a pixel; a brick off
 *   screen is not visited; past the atlas's room the cut is made coarser;
 * - a refined brick stays refined in the page table even when it is empty
 *   (a finer level can hold what averaging hid); an empty leaf is neither
 *   fetched nor stored, and its entry says EMPTY;
 * - a stored brick goes to a slot in every channel's atlas at the slot's
 *   origin, and its entry names the slot; slots are reused least recently
 *   wanted first, never one the cut draws;
 * - a request carries one level's bricks for every channel, with windows;
 * - each level's bricks are drawn at the base's brightness: a gain per level
 *   and channel from the bricks against the base under them, a level not
 *   measured yet stepping on from the coarser ones;
 * - the shader walks REFINED before EMPTY and says the level it stopped at,
 *   and the renderer's atlas program marches by its own t.
 *
 * Run directly:  node tests/js/volume_atlas_probe.mjs
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

const uploads = [];
function fakeGl() {
    return new Proxy({}, {
        get(target, name) {
            if (typeof name !== "string") return undefined;
            if (/^[A-Z_0-9]+$/.test(name)) return name;
            return (...args) => {
                if (name === "createTexture") return { id: Math.random() };
                if (name === "getError") return 0;
                if (name === "bindTexture") target.bound = args[1];
                if (name === "texSubImage3D") uploads.push({ texture: target.bound, args });
                return undefined;
            };
        },
    });
}

const box = { console, setTimeout, clearTimeout, performance, URLSearchParams, TextDecoder,
              requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
              document: { createElement: () => ({ getContext: () => null }) } };
box.window = box;
box.globalThis = box;
const context = createContext(box);
runInContext(readFileSync(join(JS, "views/volumeAtlas.js"), "utf8"), context);
runInContext(readFileSync(join(JS, "views/volumeCamera.js"), "utf8"), context);
runInContext(readFileSync(join(JS, "views/volumeRenderer.js"), "utf8"), context);
const Atlas = box.PlexoraVolumeAtlas;
const { RESIDENT, REFINED, EMPTY } = Atlas.STATE;

// A 64-plane stack of 1024 x 1024, 0.5 µm pixels, 2 µm planes: levels 0-3
// keep every plane; level 3's pixel is two planes deep.
const manifest = {
    brick: [32, 64, 64], apron: 1, max_per_request: 48, depth: 64, width: 1024, height: 1024,
    levels: [0, 1, 2, 3].map((level) => {
        const pool = level === 3 ? 2 : 1;
        const side = 1024 >> level;
        const depth = 64 / pool;
        return { level, pool, shape: [depth, side, side],
                 grid: [Math.ceil(depth / 32), Math.ceil(side / 64), Math.ceil(side / 64)],
                 brick_full: [32 * pool, 64 << level, 64 << level] };
    }),
};

// -- levels and layout --------------------------------------------------------

const levels = Atlas.atlasLevels(manifest, 3, 2048);
check("the atlas uses the levels finer than the base, finest first",
      levels.map((l) => l.level).join() === "0,1,2", JSON.stringify(levels.map((l) => l.level)));
check("each with x, y, z brick extent, voxel and grid",
      JSON.stringify(levels[1].brickFull) === "[128,128,32]"
      && JSON.stringify(levels[1].vox) === "[2,2,1]" && JSON.stringify(levels[1].grid) === "[8,8,2]",
      JSON.stringify(levels[1]));
check("stacked in the page table along z",
      levels.map((l) => l.pageZ).join() === "0,2,4");
check("a page table too large for a texture drops the finest level",
      Atlas.atlasLevels(manifest, 3, 12).map((l) => l.level).join() === "1,2");
const layout = Atlas.atlasLayout([66, 66, 34], 1, 2, 2048, 64 * 2 ** 20);
check("the atlas's slots fit its budget",
      layout.capacity * layout.slotBytes * 2 <= 64 * 2 ** 20 && layout.capacity > 100,
      JSON.stringify(layout));
check("and the texture size", layout.size.every((side) => side <= 2048));

// -- the payload --------------------------------------------------------------

function payload(header, bodies) {
    const text = new TextEncoder().encode(JSON.stringify(header));
    const total = 4 + text.length + bodies.reduce((sum, b) => sum + b.length, 0);
    const out = new Uint8Array(total);
    new DataView(out.buffer).setUint32(0, text.length, true);
    out.set(text, 4);
    let at = 4 + text.length;
    for (const body of bodies) {
        out.set(body, at);
        at += body.length;
    }
    return out.buffer;
}
const parsed = Atlas.parsePayload(payload({ bricks: [{ at: [0, 0, 0] }] }, [new Uint8Array([7, 8])]));
check("a payload's header and voxels come apart",
      parsed.header.bricks.length === 1 && new Uint8Array(parsed.voxels)[1] === 8);

// -- the cut --------------------------------------------------------------------

const requests = [];
const brickBytes = 34 * 66 * 66;
function fetchImpl(url) {
    requests.push(url);
    if (url.includes("/bricks/manifest")) {
        return Promise.resolve({ ok: true, json: async () => manifest });
    }
    const query = new URLSearchParams(url.split("?")[1]);
    const at = query.get("at").split(",");
    const channels = query.getAll("channel").length;
    // Bricks with x index 0 are empty; the rest hold voxels.
    const bricks = [];
    const bodies = [];
    let offset = 0;
    for (const item of at) {
        const [z, y, x] = item.split(".").map(Number);
        if (x === 0) {
            bricks.push({ at: [z, y, x], max: Array(channels).fill(0), offset: null });
            continue;
        }
        bricks.push({ at: [z, y, x], max: Array(channels).fill(9), offset });
        for (let c = 0; c < channels; c += 1) {
            bodies.push(new Uint8Array(brickBytes).fill(c + 1));
            offset += brickBytes;
        }
    }
    const header = { brick: [34, 66, 66], apron: 1, format: "u8", level: Number(query.get("level")),
                     bytes_per_brick: brickBytes, bricks };
    return Promise.resolve({ ok: true, arrayBuffer: async () => payload(header, bodies) });
}

const atlas = new Atlas({ gl: fakeGl(), datasource: "vol", maxTexture: 2048, fetchImpl });
await atlas.load();
check("the manifest is asked once and the atlas is ready", atlas.status === "ready");
const channels = [{ name: "DNA", stamp: "0,4095" }, { name: "CD8", stamp: "0,1000" }];
check("configured for two channels over base level 3",
      atlas.configure(channels, 0, "u8", 3) && atlas.textures.length === 2 && atlas.active());
{
    // What the 3-D view's crossfade keys on (volumeRenderer.draw).
    const version = atlas.pagesVersion;
    atlas.uploadPages();
    check("each page-table upload is a new version of what is drawn", atlas.pagesVersion === version + 1);
    const generation = atlas.generation;
    check("the same channels, windows, frame, format and level keep the atlas as it is",
          atlas.configure(channels, 0, "u8", 3) && atlas.generation === generation);
}

// The box: 1024 x 1024 px x 64 planes of 0.5/0.5/2 µm = 512 x 512 x 128 µm,
// longest side 1 -> extent [1, 1, 0.25]. Looking straight down z from above
// the stack's near corner (x, y small), close in.
const extent = [1, 1, 0.25];
function lookAt(eye, target) {
    const M = box.PlexoraVolumeRenderer.mat4;
    const view = M.lookAt(eye, target, [0, 1, 0]);
    return M.multiply(M.perspective(0.7, 1, 0.01, 20), view);
}
const view = {
    fullLo: [0, 0, 0], fullSpan: [1024, 1024, 64], extent, clip: [0, 63],
    eye: [-0.45, -0.45, 0.2], forward: lookAt([-0.45, -0.45, 0.2], [0.2, 0.2, 0]),
    pixel: (2 * Math.tan(0.35)) / 200, baseVox: [8, 8, 2],      // a 200-px-tall canvas
};
const cut = atlas.plan(view);
const byLevel = (k) => cut.leaves.filter((leaf) => leaf.k === k);
const tenth = Math.max(1, Math.floor(cut.leaves.length / 10));
const nearest = cut.leaves.slice(0, tenth);
const farthest = cut.leaves.slice(-tenth);
check("near the camera the finest level", nearest.every((leaf) => leaf.k === 0),
      JSON.stringify(nearest.slice(0, 4)));
check("farther off coarser ones", byLevel(1).length > 0
      && Math.max(...nearest.map((l) => l.k)) < Math.max(...farthest.map((l) => l.k)),
      `levels ${[0, 1, 2].map((k) => byLevel(k).length)}`);
check("the planes' depth alone does not refine: 2 µm planes over 0.5 µm pixels",
      byLevel(0).length < cut.leaves.length);
check("leaves nearest first", cut.leaves.every((leaf, i) => !i || cut.leaves[i - 1].d <= leaf.d));
check("a split brick is in the refined set", cut.refined.has("2/0/0/0"));
const behind = { ...view, eye: [0.45, 0.45, 0.2], forward: lookAt([0.45, 0.45, 0.2], [0.9, 0.9, 0.2]) };
const back = atlas.plan(behind);
check("a brick off screen is not visited",
      !back.leaves.some((leaf) => leaf.k === 2 && leaf.x === 0 && leaf.y === 0)
      && back.leaves.length < cut.leaves.length + byLevel(0).length,
      `${back.leaves.length} vs ${cut.leaves.length}`);
const far = { ...view, eye: [0, 0, 12], forward: lookAt([0, 0, 12], [0, 0, 0]) };
check("far off, the base alone", atlas.plan(far).leaves.length === 0);
const roomy = atlas.layout.capacity;
atlas.layout.capacity = 6;
const tight = atlas.plan(view);
check("past the atlas's room, the cut is coarser",
      tight.leaves.length <= 6 || !tight.leaves.some((leaf) => leaf.k === 0),
      `${tight.leaves.length} leaves`);
atlas.layout.capacity = 40;
const short = atlas.plan(view);
const mean = (list) => list.reduce((sum, leaf) => sum + leaf.d, 0) / Math.max(1, list.length);
check("short of room, it fills what room there is, nearest parts first",
      short.leaves.length > 20 && short.leaves.length <= 34 && mean(short.leaves) < mean(cut.leaves),
      `${short.leaves.length} leaves, mean distance ${mean(short.leaves).toFixed(3)} vs ${mean(cut.leaves).toFixed(3)}`);
atlas.layout.capacity = roomy;

// -- loading and the page table ------------------------------------------------

uploads.length = 0;
requests.length = 0;
await atlas.update(view);
const brickRequests = requests.filter((url) => url.includes("/bricks?"));
check("bricks are asked for, one level and every channel a request",
      brickRequests.length > 0 && brickRequests.every((url) => {
          const query = new URLSearchParams(url.split("?")[1]);
          return query.getAll("channel").join() === "DNA,CD8" && query.getAll("w").join() === "0,4095,0,1000";
      }), brickRequests[0]);
const index = (k, x, y, z) => atlas.entryIndex(k, x, y, z);
const filled = cut.leaves.find((leaf) => leaf.x > 0);
const empty = cut.leaves.find((leaf) => leaf.x === 0);
check("a stored brick's entry is resident and names its slot",
      (atlas.mirror[index(filled.k, filled.x, filled.y, filled.z) + 3] & RESIDENT) !== 0
      && atlas.slotOf.has(filled.key));
check("an empty leaf is marked EMPTY and holds no slot",
      atlas.mirror[index(empty.k, empty.x, empty.y, empty.z) + 3] === EMPTY
      && !atlas.slotOf.has(empty.key) && atlas.empties.has(empty.key));
check("a refined brick is REFINED in the page table",
      (atlas.mirror[index(2, 0, 0, 0) + 3] & REFINED) !== 0);
const slot = atlas.slotOf.get(filled.key);
const [sx, sy] = atlas.layout.slots;
const origin = [(slot % sx) * 66, (Math.floor(slot / sx) % sy) * 66, Math.floor(slot / (sx * sy)) * 34];
const into = uploads.filter((u) => u.args[2] === origin[0] && u.args[3] === origin[1]
                              && u.args[4] === origin[2] && u.args[5] === 66);
check("into every channel's atlas at the slot's origin",
      into.length === 2 && new Set(into.map((u) => u.texture)).size === 2
      && into[0].args[10][0] === 1 && into[1].args[10][0] === 2,
      JSON.stringify(into.map((u) => u.args.slice(2, 8))));
requests.length = 0;
await atlas.update(view);
check("asked again, nothing is fetched", requests.length === 0, requests.join(" "));

// Slot reuse: fill the atlas, then a brick not in the cut goes first.
const keep = new Set([filled.key]);
const stale = cut.leaves.find((leaf) => leaf.x > 0 && leaf.key !== filled.key);
atlas.slotUsed[atlas.slotOf.get(stale.key)] = -1;
atlas.free = [];
// Every slot in use: the never-used ones too.
for (let i = 0; i < atlas.slotKey.length; i += 1) {
    if (atlas.slotKey[i] === null) atlas.slotKey[i] = `9/${i}/0/0`;
}
const reused = atlas.allocate("0/9/9/1", keep);
check("a full atlas reuses the least recently wanted slot, never a kept one",
      reused === slot ? false : !atlas.slotOf.has(stale.key) && atlas.slotOf.get("0/9/9/1") === reused,
      `reused ${reused}`);
check("and its old brick is no longer resident",
      (atlas.mirror[index(stale.k, stale.x, stale.y, stale.z) + 3] & RESIDENT) === 0);
check("a new channel set starts the atlas again",
      atlas.configure([{ name: "DNA", stamp: "0,4095" }], 0, "u8", 3) && atlas.slotOf.size === 0
      && atlas.empties.size === 0);

// -- the link: gzip only when it is slower than compressing ---------------------------

{
    const LINK = Atlas.LINK;
    const realPerformance = box.performance;
    const entries = new Map();
    box.performance = { now: () => realPerformance.now(), getEntriesByName: (url) => entries.get(url) || [] };
    // 10 MB in 50 ms past the server's 10 ms: 250 MB/s, localhost.
    entries.set("http://h/a", [{ requestStart: 0, responseEnd: 60, encodedBodySize: 10e6,
                                 serverTiming: [{ name: "total", duration: 10 }] }]);
    entries.set("http://h/b", [{ requestStart: 0, responseEnd: 55, encodedBodySize: 10e6, serverTiming: [] }]);
    LINK.note("http://h/a");
    LINK.note("http://h/b");
    check("a fast link keeps voxels uncompressed", LINK.compress === false
          && LINK.ask(new URLSearchParams()).get("z") === null);
    // 10 MB in 1.6 s: 6 MB/s, a 50 Mbit/s link.
    LINK.rates = [];
    entries.set("http://h/c", [{ requestStart: 0, responseEnd: 1600, encodedBodySize: 10e6, serverTiming: [] }]);
    entries.set("http://h/d", [{ requestStart: 0, responseEnd: 1700, encodedBodySize: 10e6, serverTiming: [] }]);
    LINK.note("http://h/c");
    check("one slow response is not yet a slow link", LINK.compress === false);
    LINK.note("http://h/d");
    check("two are: every later request asks for gzip",
          LINK.compress === true && LINK.ask(new URLSearchParams()).get("z") === "gzip");
    LINK.compress = false;
    LINK.rates = [];
    box.performance = realPerformance;
}

// -- bricks at the base's brightness -----------------------------------------------
{
    // The base under any brick: 64 samples of 0.5. Bricks of level 2 (the
    // coarsest the atlas holds) all 1.0: twice as bright.
    const asked = [];
    const atlas = new Atlas({ gl: fakeGl(), datasource: "vol", maxTexture: 2048, fetchImpl,
                              reference: (c, lo, hi) => (asked.push({ c, lo, hi }), { sum: 32, n: 64 }) });
    atlas.manifest = manifest;
    atlas.status = "ready";
    atlas.configure([{ name: "DNA", stamp: "" }], 0, "u8", 3);
    const voxels = new Uint8Array(brickBytes).fill(255).buffer;
    const header = { brick: [34, 66, 66], apron: 1, format: "u8", bytes_per_brick: brickBytes,
                     bricks: [{ at: [1, 3, 2], offset: 0 }] };
    atlas.store(2, header, voxels, new Set());
    check("a brick is measured against the base over its own place, in full-resolution pixels and planes",
          asked.length === 1 && JSON.stringify(asked[0]) === JSON.stringify({ c: 0, lo: [512, 768, 32], hi: [768, 1024, 64] }),
          JSON.stringify(asked));
    const gain = (k) => atlas.gains[k * 8];
    check("its level is brought to the base's brightness", Math.abs(gain(2) - 0.5) < 1e-6, String(gain(2)));
    check("a finer level not measured yet steps on by the same ratio, within the range",
          Math.abs(gain(1) - 0.25) < 1e-6 && gain(0) === 0.25, `${gain(1)} ${gain(0)}`);
    const sent = {};
    atlas.bind(new Proxy({}, { get: (t, name) => (typeof name !== "string" ? undefined
        : (/^[A-Z_0-9]+$/.test(name) ? name : (...args) => { if (name.startsWith("uniform")) sent[args[0]] = args[1]; })) }),
               (name) => name, 1, { fullLo: [0, 0, 0], fullSpan: [1024, 1024, 64] });
    check("the gains go to the shader, two vec4s a level", sent.u_atlas_gain?.length === Atlas.MAXL * 8
          && Math.abs(sent.u_atlas_gain[16] - 0.5) < 1e-6);
    atlas.reset();
    check("and start again at 1 with the atlas", atlas.gains.every((g) => g === 1));
}

// -- the shader ------------------------------------------------------------------

const chunk = Atlas.shaderChunk(2);
check("one atlas sampler per channel, one page table",
      /u_atlas0;/.test(chunk.declarations) && /u_atlas1;/.test(chunk.declarations)
      && /usampler3D u_pages/.test(chunk.declarations));
const walk = chunk.declarations;
check("the walk tests REFINED before it skips an EMPTY brick",
      walk.indexOf("bool refined") > 0 && walk.indexOf("bool refined") < walk.indexOf("state = 2"));
const source = box.PlexoraVolumeRenderer.fragmentSource(2, false, true);
check("the atlas program marches by its own t and leaves empty bricks",
      /float t = t0 \+ jitter \* stepLength;/.test(source) && /t \+= stepLength;/.test(source)
      && /hitBox\(A, B, cLo, cHi\)/.test(source) && /locate\(full,/.test(source));
check("and samples the atlas where the walk found a brick, at its level's gain, the base (through the frame) elsewhere",
      source.includes("cState == 1 ? texture(u_atlas1, a).r * cGain0[1] : baseRead(u_volume1, p)"));
check("the walk says the level it stopped at, and its gains are read once per brick, in the march and at a cut face",
      /out int level\)/.test(walk) && /level = k;/.test(walk)
      && /locate\(full, cLo, cHi, cState, cBase, cVox, cAnchor, cLevel\);\s*cGain0 = u_atlas_gain\[cLevel \* 2\];\s*cGain1 = u_atlas_gain\[cLevel \* 2 \+ 1\];/.test(source)
      && source.includes("locate(full, b.lo, b.hi, b.state, b.base, b.vox, b.anchor, b.level);")
      && source.includes("vec4 cGain0 = u_atlas_gain[b.level * 2];"));
const plain = box.PlexoraVolumeRenderer.fragmentSource(2, false, false);
check("the plain program is unchanged by it", !/u_pages/.test(plain) && /for \(int i = 0; i < 4096; i\+\+\)/.test(plain));

console.log(failures.length ? `\n${failures.length} of ${checks} checks FAILED:\n- ${failures.join("\n- ")}`
                            : `\nall ${checks} checks passed`);
process.exit(failures.length ? 1 : 0);
