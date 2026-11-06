/**
 * One run of whole lines of a CSV, its numeric columns parsed into float64.
 *
 * {id, file, start, end, numeric: [bool per column], delimiter, rowsHint}
 *   -> {id, ok, rows, values: [Float64Array per numeric column], bad: [count per numeric column]}
 *
 * [start, end) holds whole lines only (the page cuts at line starts, past the
 * header). It is read 8 MB at a time, each piece decoded once as Latin-1 (a
 * number is ASCII), and every field handed to Number() as a substring of that
 * one string: V8's Number() is correctly rounded, so a value is the double
 * polars or DuckDB reads. (Decoding each field on its own, the first version
 * of this worker, cost twice as much again.)
 *
 * An empty field is NaN (a missing value); "nan", "inf" and "infinity" in any
 * case and sign are NaN and +/-Infinity, as polars reads them; anything else
 * that is not a number is NaN and counted in `bad`, so the page can tell a
 * text column the head did not show.
 */
const STEP = 8 * 1024 * 1024;
const latin1 = new TextDecoder("latin1");
const DIGIT = /[0-9]/;

function special(s) {
    let t = s.trim();
    if (t.length > 1 && t[0] === '"' && t[t.length - 1] === '"') t = t.slice(1, -1).trim();
    if (!t) return { value: NaN, bad: false };
    const n = Number(t);
    if (n === n && DIGIT.test(t)) return { value: n, bad: false };
    const l = t.toLowerCase();
    if (l === "nan" || l === "+nan" || l === "-nan") return { value: NaN, bad: false };
    if (l === "inf" || l === "+inf" || l === "infinity" || l === "+infinity") return { value: Infinity, bad: false };
    if (l === "-inf" || l === "-infinity") return { value: -Infinity, bad: false };
    return { value: NaN, bad: true };
}

self.onmessage = async (event) => {
    const { id, file, start, end, numeric, delimiter, rowsHint = 1024 } = event.data;
    try {
        const D = delimiter.charCodeAt(0);
        const ncol = numeric.length;
        // Column -> output slot (or -1 for a column that is not stored).
        const slot = [];
        let nNumeric = 0;
        for (let c = 0; c < ncol; c += 1) slot.push(numeric[c] ? nNumeric++ : -1);
        let capacity = Math.max(1024, Math.ceil(rowsHint * 1.05));
        let values = Array.from({ length: nNumeric }, () => new Float64Array(capacity));
        const bad = new Array(nNumeric).fill(0);
        let rows = 0;
        let carry = new Uint8Array(0);
        let at = start;
        for (;;) {
            const take = Math.min(STEP, end - at);
            const fresh = take > 0 ? new Uint8Array(await file.slice(at, at + take).arrayBuffer()) : new Uint8Array(0);
            at += take;
            let bytes = fresh;
            if (carry.length) { bytes = new Uint8Array(carry.length + fresh.length); bytes.set(carry); bytes.set(fresh, carry.length); }
            const last = at >= end;
            // Whole lines only; the rest waits for the next piece (or, at the
            // end, is the file's last line without a line break).
            const stop = last ? bytes.length : bytes.lastIndexOf(10) + 1;
            if (stop <= 0) { carry = bytes; continue; }
            carry = stop < bytes.length ? bytes.slice(stop) : new Uint8Array(0);
            const text = latin1.decode(bytes.subarray(0, stop));
            const n = text.length;
            let col = 0;
            let a = 0;
            let lineStart = 0;
            for (let i = 0; i <= n; i += 1) {
                const c = i < n ? text.charCodeAt(i) : 10;
                if (c !== D && c !== 10 && c !== 13) continue;
                const endOfLine = c !== D;
                if (endOfLine && i === lineStart) {
                    // A blank line (or nothing after the last line break).
                    if (c === 13 && i + 1 < n && text.charCodeAt(i + 1) === 10) i += 1;
                    lineStart = a = i + 1;
                    continue;
                }
                if (col === 0 && rows === capacity) {
                    // A new line and no room left: grow every column.
                    capacity = Math.ceil(capacity * 1.5);
                    values = values.map((v) => { const g = new Float64Array(capacity); g.set(v); return g; });
                }
                if (col < ncol && slot[col] >= 0) {
                    let v = NaN;
                    if (a < i) {
                        const s = text.substring(a, i);
                        v = Number(s);
                        // Number() reads " " as 0 and words as NaN: look again.
                        if (v !== v || (v === 0 && !DIGIT.test(s))) {
                            const r = special(s);
                            v = r.value;
                            if (r.bad) bad[slot[col]] += 1;
                        }
                    }
                    values[slot[col]][rows] = v;
                }
                col += 1;
                a = i + 1;
                if (!endOfLine) continue;
                if (c === 13 && i + 1 < n && text.charCodeAt(i + 1) === 10) { i += 1; a = i + 1; }
                // End of a line; a short one is NaN-padded.
                for (; col < ncol; col += 1) if (slot[col] >= 0) values[slot[col]][rows] = NaN;
                rows += 1;
                col = 0;
                lineStart = a;
            }
            if (last) break;
        }
        const out = values.map((v) => v.slice(0, rows));
        self.postMessage({ id, ok: true, rows, values: out, bad }, out.map((v) => v.buffer));
    } catch (error) {
        self.postMessage({ id, ok: false, error: `${error}${error?.stack ? ` | ${error.stack}` : ""}` });
    }
};
