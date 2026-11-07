/**
 * The browser node's OME-Zarr reader (client src/js/node/zarrReader.js) run in
 * Node over the module the workers load: tests/test_browser_zarr_node.py hands
 * it a store and what Python reads of it (node_spec's `zarr`), and compares
 * every tile it reads with Python's answer to the same tile URL.
 *
 *   node tests/js/zarr_probe.mjs SPEC.json
 *   SPEC: {root, zarr, tile: [w, h], channels, levels: [[h, w], ...]}
 *
 * The store is the website's kind (`store.page`): each file asked for by path,
 * answered here from the disk (a missing file null), as services/browserNode.js
 * answers from the picked folder. A second reader opens from the first's
 * recorded metadata (`seed`) and must ask for no metadata of its own.
 * Prints {geometry, tiles: {"c/level/tx_ty": sha256 of the HD bytes}, asked,
 * seededAsks, refused?} as JSON.
 */

import { createHash } from "node:crypto";
import { openAsBlob, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const CLIENT = path.join(REPO, "plexora", "client");
globalThis.PLEXORA_NUMCODECS_URL = pathToFileURL(path.join(CLIENT, "external", "numcodecs-0.3.2") + path.sep).href;
const { createZarrReader } = await import(pathToFileURL(path.join(CLIENT, "src", "js", "node", "zarrReader.js")).href);

const spec = JSON.parse(readFileSync(process.argv[2], "utf-8"));
const asked = [];
const ask = async (wanted) => {
    asked.push(wanted);
    if (!wanted.startsWith(`${spec.root}/`) || wanted.split("/").includes("..")) return null;
    try {
        return await openAsBlob(wanted);
    } catch (error) {
        return null;
    }
};

const reader = createZarrReader({ ask });
let opened;
try {
    opened = await reader.open({ store: { page: spec.root }, zarr: spec.zarr, record: true });
} catch (error) {
    console.log(JSON.stringify({ refused: String(error && error.message || error) }));
    process.exit(0);
}
const metadataAsks = asked.length;
const seeded = createZarrReader({ ask });
await seeded.open({ store: { page: spec.root }, zarr: spec.zarr, seed: structuredClone(opened.seed) });
const seededAsks = asked.length - metadataAsks;

const [tw, th] = spec.tile;
const tiles = {};
const bytes = {};
for (let channel = 0; channel < spec.channels; channel += 1) {
    for (const [level, { width, height }] of opened.geometry.levels.entries()) {
        for (let ty = 0; ty * th < height; ty += 1) {
            for (let tx = 0; tx * tw < width; tx += 1) {
                const tile = await reader.tile({ channel, level, tx, ty, tw, th });
                const again = await seeded.tile({ channel, level, tx, ty, tw, th });
                // An 8-bit tile as the HD PNG carries it (services/browserNode.js `widen`).
                const hd = (t) => {
                    if (t.format !== "u8") return t.array;
                    const out = new Uint8Array(t.array.length * 2);
                    for (let i = 0; i < t.array.length; i += 1) out[i * 2 + 1] = t.array[i];
                    return out;
                };
                const hash = createHash("sha256").update(hd(tile)).digest("hex");
                if (createHash("sha256").update(hd(again)).digest("hex") !== hash) {
                    throw new Error(`the seeded reader read ${channel}/${level}/${tx}_${ty} differently`);
                }
                tiles[`${channel}/${level}/${tx}_${ty}`] = { hash, width: tile.width, height: tile.height,
                                                            format: tile.format };
                bytes[tile.format] = (bytes[tile.format] || 0) + tile.array.length;
            }
        }
    }
}
console.log(JSON.stringify({ geometry: opened.geometry, tiles, metadataAsks, seededAsks, bytes }));
