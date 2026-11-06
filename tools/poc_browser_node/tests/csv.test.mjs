// workers/csvWorker.js on the cases a cut CSV must get right: CRLF, blank
// lines, a file without a final line break, empty fields, short lines,
// "nan"/"inf" spellings, padded and quoted numbers, text in a numeric column,
// and a line that straddles the worker's 8 MB read step. Values are checked
// against Number() on the same text, which is what polars' reading comes to
// for these files (P3b compares the parser with DuckDB on a 1.26 M-row file).
import { test } from "node:test";
import assert from "node:assert/strict";

const posted = [];
globalThis.self = { postMessage: (m) => posted.push(m) };
await import("../page/src/workers/csvWorker.js");

async function parse(text, numeric, { delimiter = ",", start = null, end = null } = {}) {
    const file = new Blob([text]);
    const headerEnd = text.indexOf("\n") + 1;
    posted.length = 0;
    await self.onmessage({ data: { id: 0, file, start: start ?? headerEnd, end: end ?? file.size, numeric, delimiter } });
    const m = posted[0];
    if (!m.ok) throw new Error(m.error);
    return m;
}

const col = (m, j) => Array.from(m.values[j]);

test("CRLF, blank lines and no final line break", async () => {
    const m = await parse("a,b\r\n1,2\r\n\r\n3,4.5\r\n5,6", [true, true]);
    assert.equal(m.rows, 3);
    assert.deepEqual(col(m, 0), [1, 3, 5]);
    assert.deepEqual(col(m, 1), [2, 4.5, 6]);
});

test("empty fields, short lines and spellings of nan and inf", async () => {
    const m = await parse("a,b,c\n1,,3\n4\nnan,-Inf,INFINITY\n NA ,7 ,\" 8 \"\n", [true, true, true]);
    assert.equal(m.rows, 4);
    assert.deepEqual(col(m, 0), [1, 4, NaN, NaN]);
    assert.deepEqual(col(m, 1), [NaN, NaN, -Infinity, 7]);
    assert.deepEqual(col(m, 2), [3, NaN, Infinity, 8]);
    // "NA" is not a number polars knows: counted as text.
    assert.deepEqual(m.bad, [1, 0, 0]);
});

test("a blank field is missing, not zero; a text column is not stored", async () => {
    const m = await parse("id,name,x\n1,alpha,  \n2,beta,0\n", [true, false, true]);
    assert.equal(m.values.length, 2);
    assert.deepEqual(col(m, 0), [1, 2]);
    assert.deepEqual(col(m, 1), [NaN, 0]);
});

test("17-digit values are Number()'s, tabs as the delimiter", async () => {
    const values = ["2326.304761904762", "480.87619047619046", "0.1", "1e-300", "-0.0", "123456789012345678"];
    const text = "a\tb\n" + values.map((v, i) => `${v}\t${i}`).join("\n") + "\n";
    const m = await parse(text, [true, true], { delimiter: "\t" });
    assert.deepEqual(col(m, 0), values.map(Number));
    assert.ok(Object.is(m.values[0][4], -0));
});

test("a run cut out of the middle, across the 8 MB read step", async () => {
    // ~12 MB of lines: the run [cut0, cut1) crosses one 8 MB step boundary.
    const lines = [];
    for (let i = 0; i < 400000; i += 1) lines.push(`${i},${(i * 0.1234567890123).toPrecision(17)},${i % 7}`);
    const text = "id,v,k\n" + lines.join("\n") + "\n";
    const enc = new TextEncoder().encode(text);
    const at = (n) => { let p = Math.floor(n); while (enc[p - 1] !== 10) p += 1; return p; };
    const cut0 = at(enc.length * 0.1), cut1 = at(enc.length * 0.95);
    const m = await parse(text, [true, true, true], { start: cut0, end: cut1 });
    const first = Number(new TextDecoder().decode(enc.subarray(cut0, cut0 + 12)).split(",")[0]);
    assert.equal(m.values[0][0], first);
    assert.equal(m.rows, (new TextDecoder().decode(enc.subarray(cut0, cut1)).match(/\n/g) || []).length);
    for (let r = 0; r < m.rows; r += 997) {
        const i = first + r;
        assert.equal(m.values[1][r], Number((i * 0.1234567890123).toPrecision(17)));
        assert.equal(m.values[2][r], i % 7);
    }
});
