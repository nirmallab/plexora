/**
 * P3: a cell table in the browser node. DuckDB-Wasm reads the user's CSV (or
 * Parquet) from the picked File, writes it once as Parquet into the origin's
 * private file system (OPFS), and from then on every column comes from that
 * Parquet copy as an Arrow vector -> typed array. The five things the viewer
 * asks a table for are computed over those arrays with the server's rules:
 *
 *  - all_cells: data_model._all_cells_from_frame -- columns interleaved row
 *    by row, cast to uint32 (numpy astype: truncation) or float32.
 *  - describe: data_model._describe_column -- finite values only; numpy's
 *    linear percentiles; a 50-bin density histogram; mean in float64; std
 *    with ddof=1.
 *  - nearest: SpatialIndex.nearest -- the row nearest (x, y) in float64,
 *    here through kdbush instead of scipy's cKDTree.
 *  - ROI -> cells: roi/server/mapping.assign -- centroids strictly within the
 *    polygon (even-odd ray casting; a centroid exactly on an edge is a coin
 *    flip under either rule).
 *  - a gate: low < v <= high compared in float32 (labelGpu.evaluateGateMask).
 */
import * as duckdb from "@duckdb/duckdb-wasm";
import KDBush from "kdbush";
import { histogram, percentiles } from "./stats.js";

const now = () => performance.now();

/** A DuckDB-Wasm instance in a worker of its own (the "eh" bundle: wasm exceptions). */
function startDb() {
    return (async () => {
        const t0 = now();
        const base = new URL("../nm/@duckdb/duckdb-wasm/dist/", location.href).href;
        const worker = new Worker(`${base}duckdb-browser-eh.worker.js`);
        const db = new duckdb.AsyncDuckDB(new duckdb.VoidLogger(), worker);
        await db.instantiate(`${base}duckdb-eh.wasm`);
        return { db, worker, initMs: now() - t0 };
    })();
}

/** Instances by index; the first is the page's own (getDb). */
const pool = [];

/** One DuckDB-Wasm instance per page for everything but a parallel ingest. */
export function getDb() {
    pool[0] ||= startDb();
    return pool[0];
}

/** `n` instances, started together; the parallel ingest's hands. */
export function getDbPool(n) {
    for (let i = 0; i < n; i += 1) pool[i] ||= startDb();
    return Promise.all(pool.slice(0, n));
}

/** Stop every instance past the first (an ingest's hands hold wasm heaps). */
export async function releaseDbPool() {
    const extra = pool.splice(1);
    for (const p of extra) {
        const { db, worker } = await p;
        await db.terminate().catch(() => {});
        worker.terminate();
    }
}

async function opfsDir(name) {
    const root = await navigator.storage.getDirectory();
    return root.getDirectoryHandle(name, { create: true });
}

/** A content key for a picked file: name, size and mtime (no hashing a GB). */
export function fingerprintOf(file) {
    return `${file.name}-${file.size}-${file.lastModified}`.replace(/[^A-Za-z0-9._-]/g, "_");
}

/**
 * The Parquet copy of `file` in OPFS, made on first sight. Returns the File
 * of the copy plus how long each step took and how big it is.
 */
export async function ensureParquet(file, { force = false } = {}) {
    const { db, initMs } = await getDb();
    const dir = await opfsDir("tables");
    const key = fingerprintOf(file) + ".parquet";
    const timings = { initMs };
    if (!force) {
        try {
            const handle = await dir.getFileHandle(key);
            const copy = await handle.getFile();
            if (copy.size > 0) return { copy, key, reused: true, timings };
        } catch (e) { /* not there yet */ }
    }
    const t0 = now();
    const source = `src-${key}`;
    await db.registerFileHandle(source, file, duckdb.DuckDBDataProtocol.BROWSER_FILEREADER, true);
    const conn = await db.connect();
    const isParquet = /\.parquet$/i.test(file.name);
    const out = `out-${key}`;
    try {
        // `header`, and every column numeric where it can be: the CSVs Plexora
        // reads are numbers, and DuckDB's sniffer would otherwise read a column
        // of ids as BIGINT (fine) or a sparse one as VARCHAR (not).
        const read = isParquet ? `read_parquet('${source}')` : `read_csv('${source}', header = true, sample_size = 20480)`;
        await conn.query(`COPY (SELECT * FROM ${read}) TO '${out}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 131072)`);
        timings.convertMs = now() - t0;
        const t1 = now();
        const bytes = await db.copyFileToBuffer(out);
        timings.copyOutMs = now() - t1;
        const t2 = now();
        const handle = await dir.getFileHandle(key, { create: true });
        const writable = await handle.createWritable();
        await writable.write(bytes);
        await writable.close();
        timings.opfsWriteMs = now() - t2;
        timings.parquetBytes = bytes.byteLength;
        await db.dropFile(out);
        const copy = await handle.getFile();
        return { copy, key, reused: false, timings };
    } finally {
        await conn.close();
        await db.dropFile(source).catch(() => {});
    }
}

/**
 * The column store: the table parsed ONCE by DuckDB, then kept in OPFS as one
 * raw float64 file per numeric column plus a manifest -- the browser's twin of
 * the server's derived frame. P3b measured why not Parquet: single-threaded
 * wasm spends ~8 s encoding 1.26 M x 29 doubles as Parquet whatever the codec,
 * against ~2.6 s to parse the CSV. A raw column costs only its OPFS write, and
 * a reopen reads the three columns a first draw needs without starting DuckDB.
 *
 * `first` names the columns written first (ids and coordinates): the load is
 * "drawable" once they are on disk, and the rest follow.
 *
 * A CSV or TSV is cut at line starts into `parallel` parts ("auto": one per
 * 16 MB, up to the cores less two, at most 16), each parsed by `parser`:
 * "js" (workers/csvWorker.js, V8's own Number(); ingestJs; the default) or
 * "duckdb" (a DuckDB instance each; ingestParallel, which needs at least two
 * parts). A Parquet file, a CSV a cut cannot be trusted on (a quoted field),
 * or "duckdb" with one part takes the one-instance DuckDB path.
 */
export async function ensureColumnStore(file, { force = false, first = [], parallel = "auto", parser = "js" } = {}) {
    const root = await opfsDir("columns");
    const key = fingerprintOf(file);
    const timings = {};
    if (!force) {
        try {
            const dir = await root.getDirectoryHandle(key);
            const manifest = JSON.parse(await (await (await dir.getFileHandle("manifest.json")).getFile()).text());
            return { table: openColumnStore(dir, manifest), reused: true, timings };
        } catch (e) { /* not there yet */ }
    }
    try { await root.removeEntry(key, { recursive: true }); } catch (e) { /* none */ }
    const dir = await root.getDirectoryHandle(key, { create: true });
    const t0 = now();
    const parts = parallel === "auto"
        ? Math.max(1, Math.min((navigator.hardwareConcurrency || 4) - 2, 16, Math.ceil(file.size / (16 << 20))))
        : Number(parallel);
    if (!/\.parquet$/i.test(file.name) && (parser === "js" || parts > 1)) {
        try {
            timings.parser = parser;
            return parser === "js"
                ? await ingestJs(file, dir, first, parts, t0, timings)
                : await ingestParallel(file, dir, first, parts, t0, timings);
        } catch (error) {
            // Anything the cut cannot be trusted on: the one-instance path.
            timings.parallelRefused = String(error?.message || error).slice(0, 300);
        }
    }
    const { db, initMs } = await getDb();
    timings.initMs = initMs;
    const source = `src-${key}`;
    await db.registerFileHandle(source, file, duckdb.DuckDBDataProtocol.BROWSER_FILEREADER, true);
    const conn = await db.connect();
    const isParquet = /\.parquet$/i.test(file.name);
    const read = isParquet ? `read_parquet('${source}')` : `read_csv('${source}', header = true, sample_size = 20480)`;
    const table = `ingest_${Math.random().toString(36).slice(2, 8)}`;
    try {
        let s = now();
        await conn.query(`CREATE TABLE ${table} AS SELECT * FROM ${read}`);
        timings.parseMs = now() - s;
        const schema = (await conn.query(`DESCRIBE ${table}`)).toArray()
            .map((r) => ({ name: r.column_name, type: r.column_type }));
        const rows = Number((await conn.query(`SELECT count(*) AS n FROM ${table}`)).toArray()[0].n);
        const numeric = schema.filter((c) => /INT|DOUBLE|FLOAT|DECIMAL|REAL/i.test(c.type)).map((c) => c.name);
        const order = [...first.filter((c) => numeric.includes(c)), ...numeric.filter((c) => !first.includes(c))];
        const files = {};
        let extractMs = 0, writeMs = 0;
        for (let i = 0; i < order.length; i += 1) {
            const name = order[i];
            s = now();
            const r = await conn.query(`SELECT CAST("${name.replaceAll('"', '""')}" AS DOUBLE) AS v FROM ${table}`);
            const vec = r.getChildAt(0);
            const values = Float64Array.from(vec.toArray());
            if (vec.nullCount > 0) for (let k = 0; k < values.length; k += 1) if (!vec.isValid(k)) values[k] = NaN;
            extractMs += now() - s;
            s = now();
            const fname = `c${i}.f64`;
            const writable = await (await dir.getFileHandle(fname, { create: true })).createWritable();
            await writable.write(values);
            await writable.close();
            writeMs += now() - s;
            files[name] = fname;
            if (i === first.length - 1) timings.drawableMs = now() - t0;
        }
        timings.extractMs = extractMs;
        timings.opfsWriteMs = writeMs;
        const manifest = { source: file.name, rows, columns: schema, numeric, files, version: 1 };
        const writable = await (await dir.getFileHandle("manifest.json", { create: true })).createWritable();
        await writable.write(JSON.stringify(manifest));
        await writable.close();
        timings.totalMs = now() - t0;
        timings.bytes = rows * 8 * order.length;
        return { table: openColumnStore(dir, manifest), reused: false, timings };
    } finally {
        await conn.query(`DROP TABLE IF EXISTS ${table}`).catch(() => {});
        await conn.close();
        await db.dropFile(source).catch(() => {});
    }
}

/** `parts` - 1 cut points at line starts, so every part holds whole lines. */
async function lineCuts(file, parts) {
    const cuts = [0];
    for (let i = 1; i < parts; i += 1) {
        const at = Math.floor((file.size * i) / parts);
        const bytes = new Uint8Array(await file.slice(at, Math.min(file.size, at + (1 << 20))).arrayBuffer());
        const nl = bytes.indexOf(10);
        if (nl < 0) continue; // a line longer than 1 MB: one part fewer
        const cut = at + nl + 1;
        if (cut > cuts[cuts.length - 1] && cut < file.size) cuts.push(cut);
    }
    cuts.push(file.size);
    return cuts;
}

/** A field as csvWorker.js reads it: a number, NaN for nothing, null for text. */
function headValue(s) {
    let t = s.trim();
    if (t.length > 1 && t[0] === '"' && t[t.length - 1] === '"') t = t.slice(1, -1).trim();
    if (!t) return NaN;
    const n = Number(t);
    if (n === n && /[0-9]/.test(t)) return n;
    return /^[+-]?(nan|inf|infinity)$/i.test(t) ? NaN : null;
}

/**
 * Write a store's columns (float64 files), ids and coordinates first: the
 * load is drawable once those are on disk. `column(name)` makes one.
 */
async function writeStore(dir, file, { schema, numeric, rows, first, column, t0, timings, extra = {} }) {
    const order = [...first.filter((c) => numeric.includes(c)), ...numeric.filter((c) => !first.includes(c))];
    const files = {};
    const writeColumn = async (name, i) => {
        const values = column(name);
        const fname = `c${i}.f64`;
        const writable = await (await dir.getFileHandle(fname, { create: true })).createWritable();
        await writable.write(values);
        await writable.close();
        files[name] = fname;
    };
    const s = now();
    const nFirst = first.filter((c) => numeric.includes(c)).length;
    await Promise.all(order.slice(0, nFirst).map((name, i) => writeColumn(name, i)));
    timings.drawableMs = now() - t0;
    await Promise.all(order.slice(nFirst).map((name, k) => writeColumn(name, nFirst + k)));
    timings.opfsWriteMs = now() - s;
    const manifest = { source: file.name, rows, columns: schema, numeric, files, version: 1, ...extra };
    const writable = await (await dir.getFileHandle("manifest.json", { create: true })).createWritable();
    await writable.write(JSON.stringify(manifest));
    await writable.close();
    timings.totalMs = now() - t0;
    timings.bytes = rows * 8 * order.length;
    return { table: openColumnStore(dir, manifest), reused: false, timings };
}

/**
 * A CSV parsed by `parts` plain workers at once (workers/csvWorker.js): names,
 * delimiter and which columns are numbers from the first MB; the rest cut at
 * line starts. Refused for a quoted field in the data (a cut could land inside
 * one). Returns the numeric columns' per-part arrays.
 */
async function parseCsvJs(file, parts) {
    const timings = {};
    let s = now();
    const headBytes = new Uint8Array(await file.slice(0, Math.min(file.size, 1 << 20)).arrayBuffer());
    const headerEnd = headBytes.indexOf(10) + 1;
    if (headerEnd <= 0) throw new Error("no line break in the first MB");
    const lastNl = headBytes.lastIndexOf(10);
    const head = new TextDecoder("latin1").decode(headBytes.subarray(0, lastNl + 1));
    const lines = head.split("\n").map((l) => l.replace(/\r$/, "")).filter((l, i) => i === 0 || l.length);
    const headerLine = lines[0];
    const delimiter = headerLine.split("\t").length > headerLine.split(",").length ? "\t" : ",";
    if (lines.slice(1).some((l) => l.includes('"'))) throw new Error("a quoted field in the data");
    const names = headerLine.split(delimiter).map((n) => n.trim().replace(/^"(.*)"$/, "$1"));
    const sample = lines.slice(1, -1);
    const numericFlags = names.map((_, c) => {
        let any = false;
        for (const line of sample) {
            const v = headValue(line.split(delimiter)[c] ?? "");
            if (v === null) return false;
            if (!Number.isNaN(v)) any = true;
        }
        return any;
    });
    const avgLine = Math.max(1, (lastNl + 1 - headerEnd) / Math.max(1, sample.length + 1));
    const cuts = [headerEnd];
    for (let i = 1; i < parts; i += 1) {
        const at = headerEnd + Math.floor(((file.size - headerEnd) * i) / parts);
        const bytes = new Uint8Array(await file.slice(at, Math.min(file.size, at + (1 << 20))).arrayBuffer());
        const nl = bytes.indexOf(10);
        if (nl < 0) continue;
        const cut = at + nl + 1;
        if (cut > cuts[cuts.length - 1] && cut < file.size) cuts.push(cut);
    }
    cuts.push(file.size);
    timings.sniffMs = now() - s;
    timings.parts = cuts.length - 1;

    s = now();
    const url = new URL("dist/csvWorker.js", location.href).href;
    const results = await Promise.all(cuts.slice(0, -1).map((start, i) => new Promise((resolve, reject) => {
        const worker = new Worker(url);
        worker.onmessage = (e) => {
            worker.terminate();
            if (e.data.ok) resolve(e.data);
            else reject(new Error(e.data.error));
        };
        worker.postMessage({ id: i, file, start, end: cuts[i + 1], numeric: numericFlags, delimiter,
                             rowsHint: Math.ceil((cuts[i + 1] - start) / avgLine) });
    })));
    timings.parseMs = now() - s;
    timings.partRows = results.map((r) => r.rows);
    const rows = results.reduce((a, r) => a + r.rows, 0);
    const stored = names.filter((_, c) => numericFlags[c]);
    const bad = stored.map((_, j) => results.reduce((a, r) => a + r.bad[j], 0));
    /** One stored column, its parts joined. */
    const join = (name) => {
        const j = stored.indexOf(name);
        const out = new Float64Array(rows);
        let at = 0;
        for (const r of results) { out.set(r.values[j], at); at += r.values[j].length; }
        return out;
    };
    return { names, stored, bad, rows, join, timings };
}

/**
 * The column store from parseCsvJs. A column the head called numeric that
 * holds text further down is dropped, as a reader inferring from the whole
 * file would not call it numeric either.
 */
async function ingestJs(file, dir, first, parts, t0, timings) {
    const parsed = await parseCsvJs(file, parts);
    Object.assign(timings, parsed.timings);
    const { names, stored, bad, rows } = parsed;
    const numeric = stored.filter((_, j) => bad[j] === 0);
    if (bad.some((b) => b)) timings.textInNumericColumns = Object.fromEntries(stored.map((n, j) => [n, bad[j]]).filter(([, b]) => b));
    const schema = names.map((name) => ({ name, type: numeric.includes(name) ? "DOUBLE" : "VARCHAR" }));
    let mergeMs = 0;
    const column = (name) => {
        const m = now();
        const out = parsed.join(name);
        mergeMs += now() - m;
        return out;
    };
    const result = await writeStore(dir, file, { schema, numeric, rows, first, column, t0, timings,
                                                 extra: { parser: "js", parts: timings.parts } });
    timings.mergeMs = mergeMs;
    return result;
}

/** A SQL string literal. */
const sqlString = (s) => `'${String(s).replaceAll("'", "''")}'`;
const sqlName = (s) => `"${String(s).replaceAll('"', '""')}"`;

/**
 * The column store from `parts` DuckDB instances at once, each parsing its own
 * run of whole lines of the CSV. Names and kinds come from the first MB
 * (DuckDB's own sniffing); every part is then read with those columns fixed,
 * numeric ones as DOUBLE (what the one-instance path casts them to). Refused
 * -- and the caller falls back -- when the head shows a quoted field across a
 * line break (a cut could land inside it) or any part fails to parse.
 */
async function ingestParallel(file, dir, first, parts, t0, timings) {
    // The head: delimiter, quoting, names, kinds.
    let s = now();
    const headBytes = new Uint8Array(await file.slice(0, Math.min(file.size, 1 << 20)).arrayBuffer());
    const lastNl = headBytes.lastIndexOf(10);
    if (lastNl < 0) throw new Error("no line break in the first MB");
    const head = new TextDecoder().decode(headBytes.subarray(0, lastNl + 1));
    const lines = head.split("\n");
    const headerLine = lines[0].replace(/\r$/, "");
    const delim = headerLine.split("\t").length > headerLine.split(",").length ? "\t" : ",";
    if (head.includes('"') && lines.some((l) => ((l.match(/"/g) || []).length % 2) === 1)) {
        throw new Error("a quoted field spans a line break");
    }
    const dbs = await getDbPool(parts);
    timings.initMs = now() - t0;
    timings.parts = parts;
    const { db: db0 } = dbs[0];
    await db0.registerFileBuffer("head.csv", headBytes.slice(0, lastNl + 1));
    const c0 = await db0.connect();
    const schema = (await c0.query(
        `DESCRIBE SELECT * FROM read_csv('head.csv', header = true, delim = ${sqlString(delim)}, sample_size = -1)`))
        .toArray().map((r) => ({ name: r.column_name, type: r.column_type }));
    await c0.close();
    await db0.dropFile("head.csv");
    const isNumeric = (t) => /INT|DOUBLE|FLOAT|DECIMAL|REAL/i.test(t);
    const numeric = schema.filter((c) => isNumeric(c.type)).map((c) => c.name);
    const spec = schema.map((c) => `${sqlString(c.name)}: ${sqlString(isNumeric(c.type) ? "DOUBLE" : "VARCHAR")}`).join(", ");
    const cuts = await lineCuts(file, parts);
    timings.sniffMs = now() - s;

    // Every part at once, each on its own instance.
    s = now();
    const results = await Promise.all(cuts.slice(0, -1).map(async (start, i) => {
        const { db } = dbs[i % dbs.length];
        const name = `part-${i}.csv`;
        await db.registerFileHandle(name, file.slice(start, cuts[i + 1]), duckdb.DuckDBDataProtocol.BROWSER_FILEREADER, true);
        const conn = await db.connect();
        try {
            const r = await conn.query(`SELECT ${numeric.map(sqlName).join(", ")} FROM read_csv(${sqlString(name)},
                header = ${i === 0}, auto_detect = false, delim = ${sqlString(delim)}, quote = '"', escape = '"',
                columns = {${spec}})`);
            return numeric.map((_, j) => {
                const vec = r.getChildAt(j);
                const values = vec.toArray();
                const out = values instanceof Float64Array ? values : Float64Array.from(values);
                if (vec.nullCount > 0) for (let k = 0; k < out.length; k += 1) if (!vec.isValid(k)) out[k] = NaN;
                return out;
            });
        } finally {
            await conn.close();
            await db.dropFile(name).catch(() => {});
        }
    }));
    timings.parseMs = now() - s;
    const rows = results.reduce((a, cols) => a + (cols[0]?.length || 0), 0);
    timings.partRows = results.map((cols) => cols[0]?.length || 0);

    // One file per numeric column: ids and coordinates first ("drawable"),
    // the rest written together.
    const order = [...first.filter((c) => numeric.includes(c)), ...numeric.filter((c) => !first.includes(c))];
    const files = {};
    let mergeMs = 0;
    const column = (name) => {
        const m = now();
        const j = numeric.indexOf(name);
        const out = new Float64Array(rows);
        let at = 0;
        for (const cols of results) { out.set(cols[j], at); at += cols[j].length; }
        mergeMs += now() - m;
        return out;
    };
    const writeColumn = async (name, i) => {
        const values = column(name);
        const fname = `c${i}.f64`;
        const writable = await (await dir.getFileHandle(fname, { create: true })).createWritable();
        await writable.write(values);
        await writable.close();
        files[name] = fname;
    };
    s = now();
    const nFirst = first.filter((c) => numeric.includes(c)).length;
    for (let i = 0; i < nFirst; i += 1) await writeColumn(order[i], i);
    timings.drawableMs = now() - t0;
    await Promise.all(order.slice(nFirst).map((name, k) => writeColumn(name, nFirst + k)));
    timings.opfsWriteMs = now() - s - mergeMs;
    timings.mergeMs = mergeMs;
    const manifest = { source: file.name, rows, columns: schema, numeric, files, version: 1, parts };
    const writable = await (await dir.getFileHandle("manifest.json", { create: true })).createWritable();
    await writable.write(JSON.stringify(manifest));
    await writable.close();
    timings.totalMs = now() - t0;
    timings.bytes = rows * 8 * order.length;
    return { table: openColumnStore(dir, manifest), reused: false, timings };
}

function openColumnStore(dir, manifest) {
    const cache = new Map();
    return {
        name: manifest.source,
        columns: manifest.columns,
        numeric: manifest.numeric,
        count: manifest.rows,
        async column(col) {
            if (cache.has(col)) return cache.get(col);
            const fname = manifest.files[col];
            if (!fname) throw new Error(`no numeric column ${col}`);
            const buffer = await (await (await dir.getFileHandle(fname)).getFile()).arrayBuffer();
            const values = new Float64Array(buffer);
            cache.set(col, values);
            return values;
        },
        forget() { cache.clear(); },
        async close() { cache.clear(); },
    };
}

/** A table opened from its Parquet copy: schema now, columns on demand. */
export async function openTable(copy, key) {
    const { db } = await getDb();
    const name = `t-${key}`;
    await db.registerFileHandle(name, copy, duckdb.DuckDBDataProtocol.BROWSER_FILEREADER, true);
    const conn = await db.connect();
    const t0 = now();
    const schema = await conn.query(`DESCRIBE SELECT * FROM read_parquet('${name}')`);
    const columns = schema.toArray().map((r) => ({ name: r.column_name, type: r.column_type }));
    const count = Number((await conn.query(`SELECT count(*) AS n FROM read_parquet('${name}')`)).toArray()[0].n);
    const cache = new Map();
    return {
        name, columns, count, schemaMs: now() - t0,
        numeric: columns.filter((c) => /INT|DOUBLE|FLOAT|DECIMAL|REAL/i.test(c.type)).map((c) => c.name),
        /** One column as float64, cached (the whole-column reads the viewer makes). */
        async column(col) {
            if (cache.has(col)) return cache.get(col);
            const r = await conn.query(`SELECT CAST("${col.replaceAll('"', '""')}" AS DOUBLE) AS v FROM read_parquet('${name}')`);
            const vector = r.getChildAt(0);
            const out = Float64Array.from(vector.toArray());
            if (vector.nullCount > 0) {
                // NULL -> NaN, as polars hands numpy a NaN for a missing float.
                for (let i = 0; i < out.length; i += 1) if (!vector.isValid(i)) out[i] = NaN;
            }
            cache.set(col, out);
            return out;
        },
        forget() { cache.clear(); },
        async close() { await conn.close(); },
    };
}

// ---------------------------------------------------------------------------
// A numeric CSV parsed by N workers at once (workers/csvWorker.js)

/**
 * Every numeric column of a CSV as float64, parsed by `workers` plain workers
 * (parseCsvJs), for the P3b comparison against DuckDB's own parse.
 */
export async function parseCsvParallel(file, workers = navigator.hardwareConcurrency || 4) {
    const t0 = now();
    const parsed = await parseCsvJs(file, workers);
    const tParsed = now();
    const columns = parsed.stored.map((name) => parsed.join(name));
    return { names: parsed.stored, columns, rows: parsed.rows, ms: now() - t0, parseMs: tParsed - t0, workers,
             bad: parsed.bad };
}

// ---------------------------------------------------------------------------
// The viewer's five questions

export async function allCells(table, columns, integer) {
    const arrays = await Promise.all(columns.map((c) => table.column(c)));
    const n = table.count;
    const k = columns.length;
    const out = integer ? new Uint32Array(n * k) : new Float32Array(n * k);
    for (let j = 0; j < k; j += 1) {
        const a = arrays[j];
        // Uint32Array assignment is ToUint32: truncation toward zero, as
        // numpy's float -> uint32 astype does for the values tables hold.
        for (let i = 0; i < n; i += 1) out[i * k + j] = a[i];
    }
    return out;
}

/** data_model._describe_column over one column (values as float64 or float32). */
export function describeColumn(values) {
    let count = 0;
    for (let i = 0; i < values.length; i += 1) if (Number.isFinite(values[i])) count += 1;
    if (!count) return { count: 0, mean: 0, std: 0, min: 0, "25%": 0, "50%": 0, "75%": 0, max: 0, histogram: [] };
    const finite = new Float64Array(count);
    let k = 0;
    let sum = 0;
    let min = Infinity, max = -Infinity;
    for (let i = 0; i < values.length; i += 1) {
        const v = values[i];
        if (Number.isFinite(v)) {
            finite[k++] = v;
            sum += v;
            if (v < min) min = v;
            if (v > max) max = v;
        }
    }
    const mean = sum / count;
    let ss = 0;
    for (let i = 0; i < count; i += 1) ss += (finite[i] - mean) ** 2;
    const hist = histogram(finite, 50, min, max);
    const [q25, q50, q75] = percentiles(finite, [25, 50, 75]);
    return {
        count,
        mean,
        std: count > 1 ? Math.sqrt(ss / (count - 1)) : 0,
        min,
        "25%": q25,
        "50%": q50,
        "75%": q75,
        max,
        histogram: hist,
    };
}

export async function describe(table, { float32 = false } = {}) {
    const out = {};
    for (const name of table.numeric) {
        const values = await table.column(name);
        out[name] = describeColumn(float32 ? Float32Array.from(values) : values);
    }
    return out;
}

/** A kdbush index over the centroids, and nearest-neighbour on it. */
export function spatialIndex(xs, ys) {
    const index = new KDBush(xs.length, 64, Float64Array);
    for (let i = 0; i < xs.length; i += 1) index.add(xs[i], ys[i]);
    index.finish();
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (let i = 0; i < xs.length; i += 1) {
        minX = Math.min(minX, xs[i]); maxX = Math.max(maxX, xs[i]);
        minY = Math.min(minY, ys[i]); maxY = Math.max(maxY, ys[i]);
    }
    const typical = Math.sqrt(((maxX - minX) * (maxY - minY)) / Math.max(1, xs.length));
    return {
        index,
        nearest(x, y) {
            for (let r = typical * 2; ; r *= 2) {
                const hits = index.within(x, y, r);
                if (hits.length) {
                    let best = -1, bestD = Infinity;
                    for (const h of hits) {
                        const d = Math.hypot(xs[h] - x, ys[h] - y);
                        if (d < bestD || (d === bestD && h < best)) { bestD = d; best = h; }
                    }
                    if (bestD <= r) return { row: best, distance: bestD };
                }
                if (r > 1e9) return null;
            }
        },
        /** Rows whose centroid lies strictly inside the polygon [[x, y], ...]. */
        within(polygon) {
            let a = Infinity, b = Infinity, c = -Infinity, d = -Infinity;
            for (const [px, py] of polygon) { a = Math.min(a, px); b = Math.min(b, py); c = Math.max(c, px); d = Math.max(d, py); }
            const candidates = index.range(a, b, c, d);
            const out = [];
            for (const i of candidates) if (pointInPolygon(xs[i], ys[i], polygon)) out.push(i);
            return out.sort((p, q) => p - q);
        },
    };
}

export function pointInPolygon(x, y, polygon) {
    let inside = false;
    for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
        const [xi, yi] = polygon[i];
        const [xj, yj] = polygon[j];
        if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
}

/** How many rows pass low < v <= high, compared in float32. */
export function gateCount(values, low, high) {
    const lo = Math.fround(low), hi = Math.fround(high);
    let n = 0;
    for (let i = 0; i < values.length; i += 1) {
        const v = Math.fround(values[i]);
        if (v > lo && v <= hi) n += 1;
    }
    return n;
}
