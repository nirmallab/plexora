/**
 * The browser node's CSV reader (client src/js/node/csv.js) over the files
 * tests/test_browser_columns.py writes: each `<name>.csv` with its
 * `<name>.kinds.json` (what polars' schema makes of each column) is parsed
 * whole, and again in pieces of a few bytes (records cut anywhere, a quoted
 * field across pieces) and as parts merged (runs of whole lines, as the page's
 * workers parse a large file), and the result is written to
 * `<name>.parsed.json` for the test to build a frame from and compare with
 * polars. A difference between the three ways is a failure here.
 *
 * Run:  node tests/js/csv_columns_probe.mjs DIR
 */

import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const { Columns, merge, headerEnd } = await import(
    pathToFileURL(path.join(REPO, "plexora", "client", "src", "js", "node", "csv.js")).href);

const dir = process.argv[2];
const failures = [];

const parseWhole = (body, kinds) => {
    const columns = new Columns(kinds);
    columns.add(body, true);
    return columns.result();
};

const parsePieces = (body, kinds, size) => {
    const columns = new Columns(kinds);
    let carry = new Uint8Array(0);
    for (let at = 0; at < body.length; at += size) {
        const piece = body.subarray(at, Math.min(body.length, at + size));
        const bytes = new Uint8Array(carry.length + piece.length);
        bytes.set(carry);
        bytes.set(piece, carry.length);
        const final = at + size >= body.length;
        const took = columns.add(bytes, final);
        carry = bytes.slice(took);
    }
    if (carry.length) columns.add(carry, true);
    return columns.result();
};

//: Parts cut at line starts (only where the file has no quote, as the page does).
const parseParts = (body, kinds, parts) => {
    const cuts = [0];
    for (let i = 1; i < parts; i += 1) {
        const at = Math.floor((body.length * i) / parts);
        const nl = body.indexOf(10, at);
        if (nl >= 0 && nl + 1 > cuts[cuts.length - 1] && nl + 1 < body.length) cuts.push(nl + 1);
    }
    cuts.push(body.length);
    const results = [];
    for (let i = 0; i + 1 < cuts.length; i += 1) results.push(parseWhole(body.subarray(cuts[i], cuts[i + 1]), kinds));
    return merge(results);
};

const plain = (result) => ({
    rows: result.rows,
    columns: result.columns.map((c) => (c.kind === "num"
        ? { kind: "num", values: Array.from(c.values, (v) => (Number.isNaN(v) ? "NaN" : v === Infinity ? "Inf" : v === -Infinity ? "-Inf" : v)),
            missing: c.missing ? Array.from(c.missing) : null, bad: c.bad, fractions: c.fractions }
        : { kind: "str", codes: Array.from(c.codes), dictionary: c.dictionary })),
});

// A text column's codes depend on the order values were first seen, which
// merging keeps; compare the values the codes stand for.
const values = (result) => result.columns.map((c) => (c.kind === "num" ? plain({ rows: 0, columns: [c] }).columns[0]
    : { kind: "str", values: Array.from(c.codes, (k) => (k < 0 ? null : c.dictionary[k])) }));

let checks = 0;
for (const name of readdirSync(dir).filter((n) => n.endsWith(".csv")).sort()) {
    const stem = name.slice(0, -4);
    const file = new Uint8Array(readFileSync(path.join(dir, name)));
    const kinds = JSON.parse(readFileSync(path.join(dir, `${stem}.kinds.json`), "utf8"));
    const start = headerEnd(file);
    const body = start < 0 ? new Uint8Array(0) : file.subarray(start);
    const whole = parseWhole(body, kinds);
    const expect = JSON.stringify(values(whole));
    for (const size of [1, 7, 64]) {
        checks += 1;
        if (JSON.stringify(values(parsePieces(body, kinds, size))) !== expect) {
            failures.push(`${stem}: read in ${size}-byte pieces differs from the whole`);
        }
    }
    if (!whole.sawQuote) {
        for (const parts of [2, 5]) {
            checks += 1;
            if (JSON.stringify(values(parseParts(body, kinds, parts))) !== expect) {
                failures.push(`${stem}: read as ${parts} merged parts differs from the whole`);
            }
        }
    }
    writeFileSync(path.join(dir, `${stem}.parsed.json`), JSON.stringify(plain(whole)));
    console.log(`  ${stem}: ${whole.rows} rows`);
}

if (failures.length) {
    console.log(`\n${failures.length} CSV checks FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log(`\ncsv columns probe OK: ${checks} checks`);
