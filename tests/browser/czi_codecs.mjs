// JPEG XR and CZI's Zstandard in the website's Python: each file is mounted in
// the worker and decoded as Plexora's readers decode it -- a JPEG XR TIFF with
// tifffile (its tiles through imagecodecs_shim to the worker's jxrlib,
// client/src/js/workers/jpegxr.js), a CZI zstd subblock with
// imagecodecs.zstd1_decode and the arguments czifile passes. Prints, per file,
// what was read (shape, dtype, sha256 of the pixels) for
// tests/test_browser_czi_codecs.py to hold to CPython's imagecodecs.
//
//   node tests/browser/czi_codecs.mjs --dir DIR --spec SPEC.json
//   SPEC: {"<file>": {"kind": "tiff"} | {"kind": "zstd1", itemsize, samples, out, dtype}}
import { readFileSync } from "node:fs";
import { appFrame, launch, startServer, REPO } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name) => args[args.indexOf(name) + 1];
const dir = opt("--dir");
const spec = JSON.parse(readFileSync(opt("--spec"), "utf8"));
const files = Object.keys(spec);

function reading(path, item) {
    if (item.kind === "tiff") return `a = tifffile.imread(${JSON.stringify(path)})`;
    return `a = np.frombuffer(imagecodecs.zstd1_decode(open(${JSON.stringify(path)}, "rb").read(), `
        + `itemsize=${item.itemsize}, samples=${item.samples}, out=${item.out}), ${JSON.stringify(item.dtype)})`;
}

const server = await startServer(["--site", `${REPO}/plexora/client/dist/static`, "--tree", `fixture=${dir}`]);
const browser = await launch({});
const out = { files: {} };
try {
    const page = await browser.context.newPage();
    await page.goto(`http://127.0.0.1:${server.port}/?pydebug`);
    await appFrame(page);
    await page.evaluate(() => window.PlexoraPython.ready);
    await page.evaluate(async (names) => {
        const picked = await Promise.all(names.map(async (name) => new File([await (await fetch(`/fixture/${name}`)).blob()], name)));
        await window.PlexoraPython.mount("/picked/codecs", picked);
    }, files);
    for (const name of files) {
        const started = Date.now();
        const answer = await page.evaluate((code) => window.PlexoraPython.run(code), [
            "import hashlib, json, imagecodecs, tifffile",
            "import numpy as np",
            reading(`/picked/codecs/${name}`, spec[name]),
            "json.dumps({'shape': list(a.shape), 'dtype': str(a.dtype), 'sha256': hashlib.sha256(a.tobytes()).hexdigest()})",
        ].join("\n")).catch((error) => JSON.stringify({ error: String(error).slice(0, 500) }));
        out.files[name] = { ...JSON.parse(answer.replace(/^'|'$/g, "")), ms: Date.now() - started };
    }
} finally {
    await browser.close();
    server.stop();
}
console.log(JSON.stringify(out));
