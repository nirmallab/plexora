/**
 * A CSV's columns as Plexora's Python reads them (`pl.read_csv`), parsed by the
 * page: numbers into float64, text into a dictionary and codes. The browser
 * node's table reader (services/browserTable.js) runs this in workers
 * (workers/csvWorker.js) over runs of whole lines of the picked file, and
 * plexora/browser/columns.py turns the parts into the frame polars would have
 * read -- polars in WebAssembly has no threads and cannot parse a large table
 * at all.
 *
 * What a field becomes follows polars' reader:
 * - the column kinds come from polars itself (its schema of the first rows),
 *   so this never infers anything: "num" columns (polars' integers and
 *   floats) and "str" columns (its strings, booleans and anything else);
 * - an empty field is missing (null); "nan", "inf", "infinity" in any case
 *   and sign are NaN and +/-Infinity in a number column; a number column's
 *   field that is not a number is missing and counted in `bad` (polars would
 *   refuse the whole file); an integer column's value with a fraction is
 *   counted in `fractions` (the column is then read as floats);
 * - a quoted field ("a, b", with "" for a quote) is one field, line breaks
 *   and all; its quotes are not part of its text; a quoted empty field is
 *   the empty text, not missing;
 * - text is UTF-8; a line may end in CRLF; a blank line is a row of missing
 *   values (polars 1.44 reads it so); a short row's missing fields are
 *   missing, and a long row's extra fields ignored (polars refuses the file).
 *
 *   new Columns(kinds)               kinds: "num" | "str" | null (not kept), per column
 *   columns.add(bytes, final)        -> how many bytes it took (whole records only,
 *                                       the rest is the next call's, unless `final`)
 *   columns.result()                 -> {rows, columns: [{kind, values, missing,
 *                                       bad, fractions} | {kind, codes, dictionary}]}
 *   merge(results)                   -> the parts as one result, in order
 *
 * A module of the browser node's worker bundle; tests/js/csv_columns_probe.mjs
 * runs it in node.
 */

const COMMA = 44;
const QUOTE = 34;
const LF = 10;
const CR = 13;

const latin1 = new TextDecoder("latin1");
const utf8 = new TextDecoder("utf-8");

/** A number column's field, as polars reads it: [value, missing, bad]. */
function number(text) {
    let t = text;
    if (t.length > 1 && t.charCodeAt(0) === QUOTE && t.charCodeAt(t.length - 1) === QUOTE) t = t.slice(1, -1);
    if (!t.length) return [NaN, true, false];
    const n = Number(t);
    if (n === n && t.trim().length === t.length) return [n, false, false];
    const l = t.toLowerCase();
    if (l === "nan" || l === "+nan" || l === "-nan") return [NaN, false, false];
    if (l === "inf" || l === "+inf" || l === "infinity" || l === "+infinity") return [Infinity, false, false];
    if (l === "-inf" || l === "-infinity") return [-Infinity, false, false];
    return [NaN, true, true];
}

class NumberColumn {
    constructor(integer) {
        this.integer = integer;
        this.values = new Float64Array(1024);
        this.missing = new Uint8Array(1024);
        this.anyMissing = false;
        this.bad = 0;
        this.fractions = 0;
    }

    grow(rows) {
        if (rows < this.values.length) return;
        const size = Math.max(rows + 1, this.values.length * 2);
        const values = new Float64Array(size);
        values.set(this.values);
        const missing = new Uint8Array(size);
        missing.set(this.missing);
        this.values = values;
        this.missing = missing;
    }

    put(row, text) {
        const [value, missing, bad] = number(text);
        this.values[row] = value;
        if (missing) {
            this.missing[row] = 1;
            this.anyMissing = true;
        }
        if (bad) this.bad += 1;
        else if (this.integer && !missing && Number.isFinite(value) && !Number.isInteger(value)) this.fractions += 1;
    }

    absent(row) {
        this.values[row] = NaN;
        this.missing[row] = 1;
        this.anyMissing = true;
    }

    result(rows) {
        return { kind: "num", values: this.values.slice(0, rows),
                 missing: this.anyMissing ? this.missing.slice(0, rows) : null,
                 bad: this.bad, fractions: this.fractions };
    }
}

class TextColumn {
    constructor() {
        this.codes = new Int32Array(1024);
        this.index = new Map();
        this.dictionary = [];
    }

    grow(rows) {
        if (rows < this.codes.length) return;
        const codes = new Int32Array(Math.max(rows + 1, this.codes.length * 2));
        codes.set(this.codes);
        this.codes = codes;
    }

    /** `raw` is the field's bytes as Latin-1 text (one char per byte);
     *  `quoted` whether it was written in quotes (already unquoted). */
    put(row, raw, quoted) {
        if (!raw.length && !quoted) {
            this.codes[row] = -1;
            return;
        }
        let code = this.index.get(raw);
        if (code === undefined) {
            code = this.dictionary.length;
            this.index.set(raw, code);
            const bytes = new Uint8Array(raw.length);
            for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i);
            this.dictionary.push(utf8.decode(bytes));
        }
        this.codes[row] = code;
    }

    absent(row) {
        this.codes[row] = -1;
    }

    result(rows) {
        return { kind: "str", codes: this.codes.slice(0, rows), dictionary: this.dictionary };
    }
}

export class Columns {
    /** `kinds[c]`: "num", "int" (a number column polars read as integers),
     *  "str", or null for a column not kept. */
    constructor(kinds) {
        this.kinds = kinds;
        this.columns = kinds.map((kind) => {
            if (kind === "num" || kind === "int") return new NumberColumn(kind === "int");
            if (kind === "str") return new TextColumn();
            return null;
        });
        this.rows = 0;
        this.sawQuote = false;
    }

    grow() {
        for (const column of this.columns) if (column) column.grow(this.rows);
    }

    /** The row of `fields` ([text, quoted] per field). */
    row(fields) {
        this.grow();
        const row = this.rows;
        for (let c = 0; c < this.columns.length; c += 1) {
            const column = this.columns[c];
            if (!column) continue;
            if (c >= fields.length) {
                column.absent(row);
                continue;
            }
            const [text, quoted] = fields[c];
            if (column instanceof TextColumn) column.put(row, text, quoted);
            else column.put(row, quoted && !text.length ? "" : text);
        }
        this.rows += 1;
    }

    /**
     * Whole records of `bytes`; returns how many bytes were taken. A record
     * cut by the end of `bytes` is left for the next call, unless `final`.
     */
    add(bytes, final) {
        const text = latin1.decode(bytes);
        if (text.indexOf('"') < 0) return this.addPlain(text, final);
        this.sawQuote = true;
        return this.addQuoted(text, final);
    }

    /** No quote anywhere in `text`: a line is a record, a comma a field. */
    addPlain(text, final) {
        let at = 0;
        for (;;) {
            let end = text.indexOf("\n", at);
            if (end < 0) {
                if (!final || at >= text.length) return final ? text.length : at;
                end = text.length;
            }
            let stop = end;
            if (stop > at && text.charCodeAt(stop - 1) === CR) stop -= 1;
            const fields = [];
            let from = at;
            for (;;) {
                const comma = text.indexOf(",", from);
                if (comma < 0 || comma >= stop) {
                    fields.push([text.slice(from, stop), false]);
                    break;
                }
                fields.push([text.slice(from, comma), false]);
                from = comma + 1;
            }
            this.row(fields);
            at = end + 1;
            if (at >= text.length) return text.length;
        }
    }

    /** RFC 4180: a field in quotes runs to the quote that ends it. Unquoted
     *  fields are sliced, not built a character at a time. */
    addQuoted(text, final) {
        const n = text.length;
        // Where an unquoted run ends: a comma, a line feed, or a CR that ends
        // the line (before a line feed, or the text's last character). -1
        // when the text ends first and more may follow.
        const runEnd = (i) => {
            for (let k = i; k < n; k += 1) {
                const c = text.charCodeAt(k);
                if (c === COMMA || c === LF) return k;
                if (c === CR) {
                    if (k + 1 < n) {
                        if (text.charCodeAt(k + 1) === LF) return k;
                    } else {
                        return final ? k : -1;
                    }
                }
            }
            return final ? n : -1;
        };
        let at = 0;
        while (at < n) {
            const start = at;
            const fields = [];
            let i = at;
            for (;;) {
                let field = "";
                let quoted = false;
                if (i < n && text.charCodeAt(i) === QUOTE) {
                    quoted = true;
                    let j = i + 1;
                    let closed = false;
                    for (;;) {
                        const q = text.indexOf('"', j);
                        if (q < 0) {
                            field += text.slice(j);
                            j = n;
                            break;
                        }
                        field += text.slice(j, q);
                        if (q + 1 >= n && !final) break; // a "" may be cut here
                        if (text.charCodeAt(q + 1) === QUOTE) {
                            field += '"';
                            j = q + 2;
                            continue;
                        }
                        j = q + 1;
                        closed = true;
                        break;
                    }
                    if (!closed) {
                        if (!final) return start;
                        fields.push([field, true]);
                        this.row(fields);
                        return n;
                    }
                    i = j;
                }
                // The field's (rest of its) unquoted text, to its delimiter.
                const end = runEnd(i);
                if (end < 0) return start;
                field += text.slice(i, end);
                i = end;
                fields.push([field, quoted]);
                if (i >= n) {
                    at = n;
                    break;
                }
                const c = text.charCodeAt(i);
                if (c === COMMA) {
                    i += 1;
                    continue;
                }
                at = i + (c === CR ? (i + 1 < n ? 2 : 1) : 1);
                break;
            }
            this.row(fields);
        }
        return n;
    }

    result() {
        return {
            rows: this.rows,
            sawQuote: this.sawQuote,
            columns: this.columns.map((column) => (column ? column.result(this.rows) : null)),
        };
    }
}

/** Parts parsed apart (runs of whole lines, in file order) as one result:
 * values joined, each part's text codes moved onto one dictionary. */
export function merge(parts) {
    const rows = parts.reduce((sum, part) => sum + part.rows, 0);
    const width = parts.length ? parts[0].columns.length : 0;
    const columns = [];
    for (let c = 0; c < width; c += 1) {
        const first = parts[0].columns[c];
        if (!first) {
            columns.push(null);
            continue;
        }
        if (first.kind === "num") {
            const values = new Float64Array(rows);
            const anyMissing = parts.some((part) => part.columns[c].missing);
            const missing = anyMissing ? new Uint8Array(rows) : null;
            let at = 0;
            let bad = 0;
            let fractions = 0;
            for (const part of parts) {
                const column = part.columns[c];
                values.set(column.values, at);
                if (missing && column.missing) missing.set(column.missing, at);
                bad += column.bad;
                fractions += column.fractions;
                at += part.rows;
            }
            columns.push({ kind: "num", values, missing, bad, fractions });
            continue;
        }
        const codes = new Int32Array(rows);
        const index = new Map();
        const dictionary = [];
        let at = 0;
        for (const part of parts) {
            const column = part.columns[c];
            const remap = column.dictionary.map((value) => {
                let code = index.get(value);
                if (code === undefined) {
                    code = dictionary.length;
                    index.set(value, code);
                    dictionary.push(value);
                }
                return code;
            });
            for (let i = 0; i < part.rows; i += 1) {
                const code = column.codes[i];
                codes[at + i] = code < 0 ? -1 : remap[code];
            }
            at += part.rows;
        }
        columns.push({ kind: "str", codes, dictionary });
    }
    return { rows, columns };
}

/** Where a record starts at or after byte `at` of `bytes` (no quotes): just
 * past the next line break; -1 when there is none. */
export function nextLineStart(bytes, at = 0) {
    const nl = bytes.indexOf(LF, at);
    return nl < 0 ? -1 : nl + 1;
}

/** The end of the header record in `bytes` (the first line break outside
 * quotes), or -1. */
export function headerEnd(bytes) {
    let quoted = false;
    for (let i = 0; i < bytes.length; i += 1) {
        const b = bytes[i];
        if (b === QUOTE) quoted = !quoted;
        else if (b === LF && !quoted) return i + 1;
    }
    return -1;
}
