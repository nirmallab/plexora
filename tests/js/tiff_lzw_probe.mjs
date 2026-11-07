/**
 * The browser node's TIFF LZW decoder (client src/js/node/lzw.js) against
 * vectors tests/test_browser_node_lzw.py wrote with imagecodecs and libtiff:
 * each `<name>.lzw` must decode to `<name>.raw`, byte for byte.
 *
 * Run:  node tests/js/tiff_lzw_probe.mjs VECTOR_DIR
 */

import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const { lzwDecode } = await import(pathToFileURL(path.join(REPO, "plexora", "client", "src", "js", "node", "lzw.js")).href);

const dir = process.argv[2];
const failures = [];
let checks = 0;
for (const name of readdirSync(dir).filter((n) => n.endsWith(".lzw")).sort()) {
    checks += 1;
    const stem = name.slice(0, -4);
    const packed = new Uint8Array(readFileSync(path.join(dir, name)));
    const expected = new Uint8Array(readFileSync(path.join(dir, `${stem}.raw`)));
    for (const hint of [0, expected.length]) {
        let got;
        try {
            got = lzwDecode(packed, hint);
        } catch (error) {
            failures.push(`${stem} (size hint ${hint}): ${error.message}`);
            continue;
        }
        let first = -1;
        const n = Math.min(got.length, expected.length);
        for (let i = 0; i < n; i += 1) {
            if (got[i] !== expected[i]) { first = i; break; }
        }
        if (got.length !== expected.length || first >= 0) {
            failures.push(`${stem} (size hint ${hint}): ${got.length} bytes for ${expected.length}`
                          + (first >= 0 ? `, first difference at ${first}` : ""));
        }
    }
    console.log(`  ${stem}: ${packed.length} -> ${expected.length} bytes`);
}

if (!checks) failures.push(`no vectors in ${dir}`);
if (failures.length) {
    console.log(`\n${failures.length} LZW checks FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log(`\nlzw probe OK: all ${checks} vectors decoded`);
