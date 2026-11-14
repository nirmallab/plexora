/**
 * The website's browser node across a walk of projects in one tab
 * (services/browserNode.js `project`, `check`, `revalidate`, TiffPool). The
 * host page keeps one PlexoraNode for the tab's life; each project the app
 * frame shows is a frame navigation, which calls `revalidate()` and then
 * `project(name)` (services/shell.js frameBoot). Run in Node over the real
 * module, its workers stood in for and counted:
 *
 *   node tests/js/browser_node_project_walk_probe.mjs
 *
 * Prints JSON; tests/test_browser_node_project_walk.py holds it to the
 * expectations.
 */

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = readFileSync(path.join(REPO, "plexora", "client", "src", "js", "services", "browserNode.js"), "utf8");

const W = 1024;
const H = 1024;
const NAMES = ["a", "b", "c", "d", "e", "f"];
const files = {};
for (const name of NAMES) {
    files[`/picked/${name}/image.ome.tif`] = { name: `image_${name}.ome.tif`, size: 10, lastModified: 1 };
    files[`/picked/${name}/cell.labels.pyramid.ome.tiff`] = { name: `cell_${name}.labels.pyramid.ome.tiff`, size: 20, lastModified: 1 };
}
const isMask = (file) => file && file.name.startsWith("cell");

// -- the workers: every one made and every one terminated is counted -------------
const workers = [];
class Worker {
    constructor() { this.onmessage = null; this.onerror = null; this.dead = false; this.file = null; workers.push(this); }

    postMessage(message) {
        if (this.dead) return;
        const reply = (result) => { if (!this.dead && this.onmessage) this.onmessage({ data: { id: message.id, ok: true, result } }); };
        if (message.op === "open") {
            this.file = message.file;
            const mask = isMask(message.file);
            setTimeout(() => reply({ geometry: mask
                ? { compression: 1, numChannels: 1, bitsPerSample: 32, sampleFormat: 1, levels: [{ width: W, height: H }] }
                : { compression: 1, numChannels: 3, bitsPerSample: 16, levels: [{ width: W, height: H }] }, seed: null, timing: {} }), 1);
        }
    }

    terminate() { this.dead = true; }
}

// -- Python's spec of each project: its image and its mask, both served ----------
const specOf = (name) => ({
    served: true, name, generation: 1, tile: [256, 256], path: `/picked/${name}/image.ome.tif`, format: "ome-tiff",
    levels: [[3, H, W]], dtype: "uint16", channels: ["x", "y", "z"],
    labels: { served: true, path: `/picked/${name}/cell.labels.pyramid.ome.tiff`, format: "ome-tiff",
              levels: [[0, H, W]], dtype: "uint32" },
});

const window = {
    navigator: { hardwareConcurrency: 4 },
    location: { href: "https://viewer.example/" },
    PlexoraPicks: { fileFor: (p) => files[p] || null },
    PlexoraPython: { nodeSpec: async (name) => specOf(name) },
};
const context = vm.createContext({ window, Worker, setTimeout, clearTimeout, console, performance, Date,
                                   URL, Uint8Array, Uint16Array, Uint32Array, DataView, TextEncoder, Map, Set,
                                   Promise, Math, Number, String, Object, Array, Error, JSON, Response: class {} });
vm.runInContext(SOURCE, context);
const node = window.PlexoraNode;

const alive = () => workers.filter((w) => !w.dead).length;
/** Live workers reading project `name`'s files. */
const aliveFor = (name) => workers.filter((w) => !w.dead && w.file && w.file.name.includes(`_${name}.`)).length;

const out = { perProject: null, aliveAfterEach: [], aliveForFirstAtEnd: null, aliveForLastAtEnd: null };

// Each project page, as services/shell.js frameBoot runs it: the frame
// navigates, the host's node is told to check again, and the page's project
// is checked ahead of its first tile.
for (const name of NAMES) {
    node.revalidate();
    const entry = await node.project(name);
    if (!entry.image.pool || !entry.labels.pool) {
        out.error = `project ${name} was refused: ${entry.image.refused || entry.labels.refused}`;
        break;
    }
    out.aliveAfterEach.push(alive());
}
out.perProject = out.aliveAfterEach[0];
out.aliveForFirstAtEnd = aliveFor(NAMES[0]);
out.aliveForLastAtEnd = aliveFor(NAMES[NAMES.length - 1]);
out.made = workers.length;

console.log(JSON.stringify(out));
process.exit(0);
