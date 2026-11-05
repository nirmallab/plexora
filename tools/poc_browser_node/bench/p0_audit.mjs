// node bench/p0_audit.mjs [--out FILE] -- which client scripts assume the
// same-origin Python server, and how.
//
// A text scan, not a parser: it counts call sites of the server-shaped
// globals in every classic script and plugin static file. Comments are
// stripped first so a sentence that names plexoraUrl is not a call site. The
// counts are the size of the "same-origin server" seam (investigation §6.2,
// §6.3), file by file; the rendering set is listed separately because P0 asks
// whether it loads from a static page unmodified.
import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { REPO } from "./procs.mjs";

const RENDERING = ["views/glInit.js", "views/tileColorize.js", "views/tileDecode.js", "views/tileFetch.js",
                   "views/labelTile.js", "views/labelGpu.js", "services/displayCurve.js",
                   "workers/tileDecoder.js", "services/glRenderer.js"];
const PATTERNS = {
    plexoraUrl: /\bplexoraUrl\s*\(/g,
    datasourceGlobal: /(?<![.\w$])datasource\b(?!\s*:)/g,
    flaskVariables: /\bflaskVariables\b/g,
    fetchCall: /\bfetch\s*\(/g,
    xhr: /\bXMLHttpRequest\b/g,
    imageViewerGlobals: /\b(toFloatColor|PlexoraLayerStack|__plexora)\b/g,
};

function strip(source) {
    return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:\\])\/\/.*$/gm, "$1");
}

function walk(dir, out = []) {
    for (const name of readdirSync(dir)) {
        const full = path.join(dir, name);
        if (name === "node_modules" || name === "dist" || name === "external") continue;
        if (statSync(full).isDirectory()) walk(full, out);
        else if (name.endsWith(".js")) out.push(full);
    }
    return out;
}

const client = path.join(REPO, "plexora", "client", "src", "js");
const plugins = path.join(REPO, "plexora", "plugins");
const files = [...walk(client), ...walk(plugins).filter((f) => f.includes(`${path.sep}static${path.sep}`))];
const rows = files.map((file) => {
    const text = strip(readFileSync(file, "utf8"));
    const counts = {};
    for (const [key, re] of Object.entries(PATTERNS)) counts[key] = (text.match(re) || []).length;
    return { file: path.relative(REPO, file).replaceAll("\\", "/"), ...counts };
});
const total = (key, set = rows) => set.reduce((a, r) => a + r[key], 0);
const rendering = rows.filter((r) => RENDERING.some((m) => r.file.endsWith(m)));
const out = {
    scanned: rows.length,
    totals: Object.fromEntries(Object.keys(PATTERNS).map((k) => [k, total(k)])),
    filesUsing: Object.fromEntries(Object.keys(PATTERNS).map((k) => [k, rows.filter((r) => r[k]).length])),
    rendering,
    heaviest: rows.slice().sort((a, b) => (b.plexoraUrl + b.fetchCall) - (a.plexoraUrl + a.fetchCall)).slice(0, 15),
};
const target = process.argv.includes("--out") ? process.argv[process.argv.indexOf("--out") + 1] : null;
if (target) writeFileSync(target, JSON.stringify(out, null, 1) + "\n");
console.log(JSON.stringify({ scanned: out.scanned, totals: out.totals, filesUsing: out.filesUsing }, null, 1));
console.table(out.rendering);
