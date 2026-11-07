// JPEG 2000 in the website's Python: each TIFF is mounted in the worker and
// read with tifffile, whose JPEG 2000 tiles go through imagecodecs_shim to the
// page's OpenJPEG (client/external/openjpeg-1.3.6). Prints, per file, what was
// read (shape, dtype, sha256 of the pixels) for tests/test_browser_j2k.py to
// hold to CPython's imagecodecs.
//
//   node tests/browser/j2k.mjs --dir DIR --files a.tif,b.tif
import { appFrame, launch, startServer, REPO } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name) => args[args.indexOf(name) + 1];
const dir = opt("--dir");
const files = opt("--files").split(",");

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
        await window.PlexoraPython.mount("/picked/j2k", picked);
    }, files);
    for (const name of files) {
        const started = Date.now();
        const answer = await page.evaluate((path) => window.PlexoraPython.run([
            "import hashlib, json, tifffile",
            `a = tifffile.imread(${JSON.stringify(path)})`,
            "json.dumps({'shape': list(a.shape), 'dtype': str(a.dtype), 'sha256': hashlib.sha256(a.tobytes()).hexdigest()})",
        ].join("\n")), `/picked/j2k/${name}`).catch((error) => JSON.stringify({ error: String(error).slice(0, 500) }));
        out.files[name] = { ...JSON.parse(answer.replace(/^'|'$/g, "")), ms: Date.now() - started };
    }
} finally {
    await browser.close();
    server.stop();
}
console.log(JSON.stringify(out));
