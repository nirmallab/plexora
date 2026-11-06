/**
 * P3: the five table questions answered in the page, timed, and checked
 * against the Python node (all_cells, columns, describe: byte- or 9-digit
 * identity) and against the Python truth file (nearest, ROI, gates).
 */
import { NodeClient } from "./nodeClient.js";
import { firstDifference, sha256 } from "./png.js";

const now = () => performance.now();

function relDiff(a, b) {
    if (a === b) return 0;
    const scale = Math.max(Math.abs(a), Math.abs(b), 1e-300);
    return Math.abs(a - b) / scale;
}

async function clearTables() {
    const root = await navigator.storage.getDirectory();
    for (const name of ["tables", "columns"]) {
        try { await root.removeEntry(name, { recursive: true }); } catch (e) { /* none */ }
    }
}

async function duckMemory(t) {
    try {
        const { db } = await t.getDb();
        const conn = await db.connect();
        const r = await conn.query("SELECT sum(memory_usage_bytes) AS b FROM duckdb_memory()");
        await conn.close();
        return Number(r.toArray()[0].b);
    } catch (e) {
        return null;
    }
}

/**
 * @param file     the picked CSV/Parquet's name
 * @param roles    {cell_id, x, y}
 * @param node     {endpoint, token, tableId} -- the same file served by a node
 * @param truth    bench/truth_table.py's output
 * @param cold     clear OPFS first (the first-ever open)
 */
export async function p3({ file, roles, node, truth, cold = true, markers = 5, store = "columns", parallel = "auto", parser = "js" }) {
    const t = await import("./table.js");
    const picked = [...document.getElementById("picker").files].find((f) => f.name === file);
    if (!picked) throw new Error(`no picked file ${file}`);
    const out = { timings: {}, checks: {} };
    if (cold) await clearTables();

    // -- load: what the viewer waits on before it can draw a cell -------------
    const t0 = now();
    const opened = await openStore(t, picked, roles, store, parallel, parser);
    const table = opened.table;
    const ids = await t.allCells(table, [roles.cell_id, roles.x, roles.y], true);
    out.timings.loadMs = now() - t0;
    // The parallel ingest's extra DuckDB instances are done: their heaps go.
    await t.releaseDbPool();
    out.timings.ingest = opened.timings;
    out.timings.reused = opened.reused;
    out.store = store;
    out.table = { rows: table.count, columns: table.columns.length, numeric: table.numeric.length,
                  storeBytes: opened.bytes, csvBytes: picked.size };

    // -- columns: one uncached Parquet column read each ------------------------
    const markerNames = (truth?.markers || table.numeric).slice(0, markers);
    const columnMs = [];
    for (const name of markerNames) {
        const s = now();
        await table.column(name);
        columnMs.push(now() - s);
    }
    out.timings.columnMs = columnMs;

    // -- describe ------------------------------------------------------------
    let s = now();
    const described = await t.describe(table);
    out.timings.describeMs = now() - s;
    s = now();
    const described32 = await t.describe(table, { float32: true });
    out.timings.describe32Ms = now() - s;

    // -- nearest -------------------------------------------------------------
    const xs = await table.column(roles.x);
    const ys = await table.column(roles.y);
    s = now();
    const index = t.spatialIndex(xs, ys);
    out.timings.indexBuildMs = now() - s;
    if (truth) {
        const per = [];
        let agree = 0, tieOnly = 0;
        const misses = [];
        for (const q of truth.nearest) {
            const s1 = now();
            const hit = index.nearest(q.x, q.y);
            per.push(now() - s1);
            if (hit.row === q.row) agree += 1;
            else if (Math.abs(hit.distance - q.distance) < 1e-9) tieOnly += 1;
            else misses.push({ q, got: hit });
        }
        out.timings.nearestMs = per;
        out.checks.nearest = { count: truth.nearest.length, agree, tiesResolvedDifferently: tieOnly, misses: misses.slice(0, 5) };

        // -- ROI -> cells ----------------------------------------------------
        const roi = [];
        for (const r of truth.rois) {
            const s2 = now();
            const rows = index.within(r.polygon);
            const ms = now() - s2;
            const sha = await sha256(new Uint8Array(Uint32Array.from(rows).buffer));
            let symmetricDiff = null;
            if (sha !== r.rowsSha256 && r.rows) {
                const a = new Set(rows), b = new Set(r.rows);
                symmetricDiff = [...a].filter((v) => !b.has(v)).length + [...b].filter((v) => !a.has(v)).length;
            }
            roi.push({ vertices: r.vertices, count: rows.length, truthCount: r.count, identical: sha === r.rowsSha256,
                       symmetricDiff, ms });
        }
        out.checks.roi = roi;

        // -- gates -------------------------------------------------------------
        const gates = [];
        for (const g of truth.gates) {
            const values = await table.column(g.column);
            const s3 = now();
            const count = t.gateCount(values, g.low, g.high);
            gates.push({ column: g.column, count, truthCount: g.count, ms: now() - s3 });
        }
        out.checks.gates = { count: gates.length, agree: gates.filter((g) => g.count === g.truthCount).length,
                             disagree: gates.filter((g) => g.count !== g.truthCount).slice(0, 5),
                             ms: gates.map((g) => g.ms) };
    }

    // -- identity against the node ------------------------------------------
    if (node) {
        const client = new NodeClient(node.endpoint, node.token);
        const inspected = await client.inspectTable(node.tableId);
        const spec = {
            ...(inspected.proposed || {}), type: inspected.data_type || "csv", src: "node",
            roles: { ...(inspected.proposed?.roles || {}), ...roles },
        };
        s = now();
        await client.loadTable(node.tableId, spec);
        out.timings.nodeLoadMs = now() - s;
        s = now();
        const nodeIds = await client.allCells(node.tableId, [roles.cell_id, roles.x, roles.y], true);
        out.timings.nodeAllCellsMs = now() - s;
        const a = new Uint8Array(ids.buffer), b = new Uint8Array(nodeIds.buffer, nodeIds.byteOffset, nodeIds.byteLength);
        out.checks.allCellsIdentical = a.length === b.length && firstDifference(a, b) === -1;
        out.checks.allCellsFirstDiff = firstDifference(a, b);

        s = now();
        const nodeCols = await client.columns(node.tableId, markerNames);
        out.timings.nodeColumnsMs = now() - s;
        const colChecks = {};
        for (const name of markerNames) {
            const mine = Float32Array.from(await table.column(name));
            const theirs = nodeCols[name];
            colChecks[name] = firstDifference(new Uint8Array(mine.buffer), new Uint8Array(theirs.buffer, theirs.byteOffset, theirs.byteLength)) === -1;
        }
        out.checks.columnsIdentical = colChecks;

        s = now();
        const nodeDescribe = await client.describe(node.tableId);
        out.timings.nodeDescribeMs = now() - s;
        const compare = (mine) => {
            let worst = 0, worstAt = null, columns = 0, missing = [];
            for (const [name, d] of Object.entries(nodeDescribe)) {
                const m = mine[name];
                if (!m) { missing.push(name); continue; }
                columns += 1;
                for (const k of ["count", "mean", "std", "min", "25%", "50%", "75%", "max"]) {
                    const r = relDiff(Number(m[k]), Number(d[k]));
                    if (r > worst) { worst = r; worstAt = `${name}.${k}`; }
                }
                for (let i = 0; i < Math.min(d.histogram.length, m.histogram.length); i += 1) {
                    const r = Math.max(relDiff(m.histogram[i].x, d.histogram[i].x), relDiff(m.histogram[i].y, d.histogram[i].y));
                    if (r > worst) { worst = r; worstAt = `${name}.histogram[${i}]`; }
                }
            }
            return { columns, missing, worstRelativeDiff: worst, worstAt, nineDigits: worst < 5e-10 };
        };
        out.checks.describeFloat64 = compare(described);
        out.checks.describeFloat32 = compare(described32);
    }

    out.memory = { duckdbBytes: await duckMemory(t), jsHeap: performance.memory?.usedJSHeapSize ?? null };
    out.storage = await navigator.storage.estimate().then((e) => ({ usage: e.usage, quota: e.quota })).catch(() => null);
    await table.close();
    return out;
}

/**
 * P3b: where the cold-ingest time goes, and whether it has to. DuckDB-Wasm
 * runs single-threaded without cross-origin isolation, so the same CSV is
 * read four ways and the times compared:
 *   a) DuckDB COPY ... TO parquet   (what P3's load does; Parquet for reopen)
 *   b) DuckDB CREATE TABLE AS read_csv  (parse only, no Parquet)
 *   c) DuckDB SELECT id, x, y FROM read_csv  (projection: what a first draw needs)
 *   d) a numeric CSV parsed by N workers at once (workers/csvWorker.js)
 * and then e) the Parquet copy made from d)'s columns instead of from the CSV
 * (the deferred conversion), with d)'s values checked against DuckDB's, bit
 * for bit, column by column.
 */
export async function p3Ingest({ file, roles, workers = [4, 8, 16, 24] }) {
    const t = await import("./table.js");
    const duckdb = await import("@duckdb/duckdb-wasm");
    const arrow = await import("apache-arrow");
    const picked = [...document.getElementById("picker").files].find((f) => f.name === file);
    const { db, initMs } = await t.getDb();
    const out = { initMs, duckdb: {}, parallel: [], checks: {} };
    const src = "ingest-src.csv";
    await db.registerFileHandle(src, picked, duckdb.DuckDBDataProtocol.BROWSER_FILEREADER, true);
    const conn = await db.connect();
    const q = (name) => `"${name.replaceAll('"', '""')}"`;
    let s = now();
    await conn.query(`COPY (SELECT * FROM read_csv('${src}', header = true)) TO 'ingest-a.parquet' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 131072)`);
    out.duckdb.copyToParquetMs = now() - s;
    await db.dropFile("ingest-a.parquet");
    s = now();
    await conn.query(`CREATE TABLE ingest_b AS SELECT * FROM read_csv('${src}', header = true)`);
    out.duckdb.createTableMs = now() - s;
    s = now();
    const proj = await conn.query(`SELECT ${q(roles.cell_id)}, ${q(roles.x)}, ${q(roles.y)} FROM read_csv('${src}', header = true)`);
    out.duckdb.projectionMs = now() - s;
    out.duckdb.projectionRows = proj.numRows;

    let parsed = null;
    for (const n of workers) {
        const r = await t.parseCsvParallel(picked, n);
        out.parallel.push({ workers: n, ms: r.ms, parseMs: r.parseMs, rows: r.rows });
        parsed = r;
    }
    // Bit-for-bit against DuckDB's own parse of every column.
    const checks = {};
    for (let c = 0; c < parsed.names.length; c += 1) {
        const name = parsed.names[c];
        const ref = await conn.query(`SELECT CAST(${q(name)} AS DOUBLE) AS v FROM ingest_b`);
        const vec = ref.getChildAt(0);
        const want = Float64Array.from(vec.toArray());
        if (vec.nullCount > 0) for (let i = 0; i < want.length; i += 1) if (!vec.isValid(i)) want[i] = NaN;
        const got = parsed.columns[c];
        let differ = 0;
        for (let i = 0; i < want.length; i += 1) {
            if (!(Object.is(got[i], want[i]) || (Number.isNaN(got[i]) && Number.isNaN(want[i])))) differ += 1;
        }
        checks[name] = { rows: want.length === got.length, differ };
    }
    out.checks.parallelVsDuckdb = checks;
    out.checks.allIdentical = Object.values(checks).every((c) => c.rows && c.differ === 0);

    // The Parquet write's codec: zstd (a, e) against snappy and none, from
    // the parsed table b) -- which part of a)'s time is compression.
    for (const codec of ["snappy", "uncompressed"]) {
        s = now();
        await conn.query(`COPY ingest_b TO 'ingest-${codec}.parquet' (FORMAT parquet, COMPRESSION ${codec}, ROW_GROUP_SIZE 131072)`);
        out.duckdb[`parquetFromTable_${codec}Ms`] = now() - s;
        const bytes = await db.copyFileToBuffer(`ingest-${codec}.parquet`);
        out.duckdb[`parquetFromTable_${codec}Bytes`] = bytes.byteLength;
        await db.dropFile(`ingest-${codec}.parquet`);
    }
    s = now();
    await conn.query(`COPY ingest_b TO 'ingest-zstd.parquet' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 131072)`);
    out.duckdb.parquetFromTable_zstdMs = now() - s;
    await db.dropFile("ingest-zstd.parquet");

    // e) the Parquet copy from the parsed columns (no CSV parse).
    s = now();
    const table = arrow.tableFromArrays(Object.fromEntries(parsed.names.map((n, i) => [n, parsed.columns[i]])));
    const ipc = arrow.tableToIPC(table, "stream");
    out.arrowIpcMs = now() - s;
    s = now();
    await conn.insertArrowFromIPCStream(ipc, { name: "ingest_e" });
    await conn.query(`COPY ingest_e TO 'ingest-e.parquet' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 131072)`);
    out.parquetFromColumnsMs = now() - s;
    await db.dropFile("ingest-e.parquet");
    await conn.query("DROP TABLE ingest_b; DROP TABLE ingest_e;");
    await conn.close();
    await db.dropFile(src);
    out.jsHeap = performance.memory?.usedJSHeapSize ?? null;
    return out;
}

/** The table in OPFS as raw columns (default) or as a Parquet copy. */
async function openStore(t, picked, roles, store, parallel = "auto", parser = "js") {
    if (store === "parquet") {
        const parquet = await t.ensureParquet(picked);
        const table = await t.openTable(parquet.copy, parquet.key);
        return { table, timings: parquet.timings, reused: parquet.reused, bytes: parquet.copy.size };
    }
    const r = await t.ensureColumnStore(picked, { first: [roles.cell_id, roles.x, roles.y], parallel, parser });
    return { table: r.table, timings: r.timings, reused: r.reused, bytes: r.timings.bytes ?? null };
}

/** Reopen only: the store is in OPFS from an earlier page. */
export async function p3Reopen({ file, roles, store = "columns" }) {
    const t = await import("./table.js");
    const picked = [...document.getElementById("picker").files].find((f) => f.name === file);
    const t0 = now();
    const opened = await openStore(t, picked, roles, store);
    await t.allCells(opened.table, [roles.cell_id, roles.x, roles.y], true);
    const loadMs = now() - t0;
    await opened.table.close();
    return { loadMs, reused: opened.reused, ingest: opened.timings };
}
