/**
 * One slice of a numeric CSV, parsed into float64 columns.
 *
 * {id, file, start, end, columns, skipHeader} -> {id, ok, rows, values: [Float64Array...]}
 *
 * The slice is [start, end) in bytes. A slice owns every line that STARTS
 * inside it: it skips to the first newline after `start` (unless start is 0,
 * where it skips the header line instead) and reads past `end` to finish the
 * line it is in. So N workers over N contiguous slices read every line once.
 *
 * Numbers are parsed exactly: a field with at most 15 significant digits and
 * a small exponent goes through Clinger's fast path (exact in float64);
 * anything else through Number(), which is correctly rounded -- the same
 * double polars or DuckDB would produce. An empty or non-numeric field is NaN.
 */
const POW10 = [1, 1e1, 1e2, 1e3, 1e4, 1e5, 1e6, 1e7, 1e8, 1e9, 1e10, 1e11, 1e12, 1e13, 1e14, 1e15,
               1e16, 1e17, 1e18, 1e19, 1e20, 1e21, 1e22];

function parseField(bytes, a, b, slow) {
    if (a >= b) return NaN;
    let i = a;
    let negative = false;
    if (bytes[i] === 45 /* - */) { negative = true; i += 1; } else if (bytes[i] === 43 /* + */) i += 1;
    let mantissa = 0;
    let digits = 0;
    let exponent = 0;
    let seen = false;
    for (; i < b; i += 1) {
        const c = bytes[i] - 48;
        if (c < 0 || c > 9) break;
        seen = true;
        if (digits || c) { mantissa = mantissa * 10 + c; digits += 1; }
    }
    if (i < b && bytes[i] === 46 /* . */) {
        i += 1;
        for (; i < b; i += 1) {
            const c = bytes[i] - 48;
            if (c < 0 || c > 9) break;
            seen = true;
            if (digits || c) { mantissa = mantissa * 10 + c; digits += 1; }
            exponent -= 1;
        }
    }
    if (i < b && (bytes[i] === 101 || bytes[i] === 69) /* e E */) {
        let j = i + 1;
        let sign = 1;
        if (bytes[j] === 45) { sign = -1; j += 1; } else if (bytes[j] === 43) j += 1;
        let e = 0;
        let any = false;
        for (; j < b; j += 1) {
            const c = bytes[j] - 48;
            if (c < 0 || c > 9) break;
            e = e * 10 + c;
            any = true;
        }
        if (!any) return slow(a, b);
        exponent += sign * e;
        i = j;
    }
    if (!seen || i !== b) return slow(a, b);   // "nan", "inf", text, quotes: let Number() decide
    if (digits > 15 || exponent < -22 || exponent > 22) return slow(a, b);
    const value = exponent < 0 ? mantissa / POW10[-exponent] : mantissa * POW10[exponent];
    return negative ? -value : value;
}

self.onmessage = async (event) => {
    const { id, file, start, end, columns, skipHeader } = event.data;
    try {
        // From one byte BEFORE the slice (to see whether a line starts exactly
        // at `start`), and a little past the end to finish the last line.
        const tail = 1 << 16;
        const from = start > 0 ? start - 1 : 0;
        const buffer = new Uint8Array(await file.slice(from, Math.min(file.size, end + tail)).arrayBuffer());
        const limit = Math.min(buffer.length, end - from);
        let pos = 0;
        if (start > 0 || skipHeader) {
            // To the first line that starts at or after `start` (for slice 0,
            // past the header).
            while (pos < buffer.length && buffer[pos] !== 10) pos += 1;
            pos += 1;
        }
        const decoder = new TextDecoder();
        const slow = (a, b) => {
            let s = decoder.decode(buffer.subarray(a, b)).trim();
            if (s.startsWith('"') && s.endsWith('"')) s = s.slice(1, -1);
            if (!s) return NaN;
            const v = Number(s);
            return Number.isNaN(v) ? NaN : v;
        };
        let capacity = Math.max(1024, Math.ceil(limit / 64));
        let values = Array.from({ length: columns }, () => new Float64Array(capacity));
        let rows = 0;
        while (pos < limit) {
            if (rows === capacity) {
                capacity *= 2;
                values = values.map((v) => { const n = new Float64Array(capacity); n.set(v); return n; });
            }
            let col = 0;
            let a = pos;
            let i = pos;
            for (; i < buffer.length; i += 1) {
                const c = buffer[i];
                if (c === 44 /* , */ || c === 10 || c === 13) {
                    if (col < columns) values[col][rows] = parseField(buffer, a, i, slow);
                    col += 1;
                    if (c !== 44) break;
                    a = i + 1;
                }
            }
            if (i >= buffer.length && a < i && col < columns) values[col++][rows] = parseField(buffer, a, i, slow);
            while (col < columns) values[col++][rows] = NaN;
            // \r\n
            if (buffer[i] === 13 && buffer[i + 1] === 10) i += 1;
            pos = i + 1;
            if (i > a || col > 0) rows += 1;
        }
        const out = values.map((v) => v.slice(0, rows));
        self.postMessage({ id, ok: true, rows, values: out }, out.map((v) => v.buffer));
    } catch (error) {
        self.postMessage({ id, ok: false, error: String(error?.stack || error) });
    }
};
