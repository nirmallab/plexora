// node build.mjs [--watch] -- bundle the POC page and its workers into page/dist/.
//
// The page entry is an ES module (split, so DuckDB-Wasm loads only on the
// pages that ask for it); workers are classic IIFE scripts so `new Worker(url)`
// works the same in Chromium, Firefox and Safari.
import { build } from "esbuild";

const common = { bundle: true, sourcemap: true, target: "es2022", logLevel: "info", minify: false };

await build({
    ...common,
    entryPoints: { poc: "page/src/poc.js" },
    outdir: "page/dist",
    format: "esm",
    splitting: true,
});
await build({
    ...common,
    entryPoints: {
        tiffWorker: "page/src/workers/tiffWorker.js",
        csvWorker: "page/src/workers/csvWorker.js",
        pyramidWorker: "page/src/workers/pyramidWorker.js",
        pyramidBandWorker: "page/src/workers/pyramidBandWorker.js",
        h5Worker: "page/src/workers/h5Worker.js",
    },
    outdir: "page/dist",
    format: "iife",
});
