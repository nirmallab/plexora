// node/blosc.js, run in Node on chunks numcodecs (Python) encoded: for each
// `<name>.bin` in a folder's cases.json, whether it decoded to `<name>.raw`
// ("ok"), was declined for numcodecs.js ("declined"), or came out otherwise
// ("wrong", which must never happen). Zstandard by zstddec, as the reader;
// zlib by Node's own.
//
//   node tests/js/blosc_probe.mjs DIR   (from the repository root)
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import zlib from "node:zlib";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const CLIENT = path.join(REPO, "plexora", "client");
const { bloscDecode, lz4Codec } = await import(pathToFileURL(path.join(CLIENT, "src", "js", "node", "blosc.js")).href);
const { ZSTDDecoder } = await import(pathToFileURL(path.join(CLIENT, "node_modules", "zstddec", "dist", "zstddec.modern.js")).href);

const dir = process.argv[2];
const zstd = new ZSTDDecoder();
await zstd.init();
const inner = { zstd: async (bytes, size) => zstd.decode(bytes, size), zlib: async (bytes) => new Uint8Array(zlib.inflateSync(bytes)) };
const out = {};
for (const c of JSON.parse(readFileSync(path.join(dir, "cases.json"), "utf8"))) {
    const encoded = new Uint8Array(readFileSync(path.join(dir, `${c.name}.bin`)));
    const raw = new Uint8Array(readFileSync(path.join(dir, `${c.name}.raw`)));
    const got = c.kind === "lz4" ? lz4Codec(encoded) : await bloscDecode(encoded, inner);
    out[c.name] = got === null ? "declined"
        : got.length === raw.length && got.every((v, i) => v === raw[i]) ? "ok" : "wrong";
}
console.log(JSON.stringify(out));
