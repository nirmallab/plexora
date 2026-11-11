// JPEG XR as the website's Python worker decodes it: client/src/js/workers/
// jpegxr.js (jxrlib in WebAssembly, client/external/jpegxr-0.3.0), through
// the path the worker takes -- the first decode fetches the .wasm with a
// synchronous XMLHttpRequest, stood in for here by one that reads file: URLs.
// Run with --disallow-code-generation-from-strings: the site's CSP has no
// 'unsafe-eval', so nothing here may evaluate code.
//
// For each file: <file>.raw (the pixels as jxrlib wrote them) beside it, and
// one JSON line on stdout {name, width, height, channels, colorFormat,
// bitDepth, bitsPerPixel, alpha, bgr} or {name, error}; then a last line
// {evalBlocked, fetched}. tests/test_imagecodecs_shim.py holds each, through
// imagecodecs_shim.jpegxr_decode, to imagecodecs.
//
//   node --disallow-code-generation-from-strings tests/js/jpegxr_probe.mjs a.jxr b.jxr
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const fetched = [];
globalThis.XMLHttpRequest = class {
    open(method, url, async) { this.url = url; this.async = async; }
    send() {
        if (this.async !== false) throw new Error("the worker's request must be synchronous");
        fetched.push(path.relative(REPO, fileURLToPath(this.url)).split(path.sep).join("/"));
        this.response = fs.readFileSync(fileURLToPath(this.url)).buffer;
        this.status = 200;
    }
};
let evalBlocked = false;
try {
    new Function("return 1")();
} catch (error) {
    evalBlocked = error instanceof EvalError;
}

const { jpegxrDecode } = await import(pathToFileURL(path.join(REPO, "plexora/client/src/js/workers/jpegxr.js")).href);
for (const file of process.argv.slice(2)) {
    const name = path.basename(file);
    try {
        const { data, ...facts } = jpegxrDecode(new Uint8Array(fs.readFileSync(file)));
        fs.writeFileSync(`${file}.raw`, data);
        console.log(JSON.stringify({ name, ...facts }));
    } catch (error) {
        console.log(JSON.stringify({ name, error: String(error && error.message || error) }));
    }
}
console.log(JSON.stringify({ evalBlocked, fetched }));
