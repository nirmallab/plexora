/**
 * P4: AnnData and SpatialData in the browser node -- the capability matrix.
 *
 * .h5ad: workers/h5Worker.js (h5wasm over WORKERFS) answers the reads
 * Plexora's adapter makes -- obs names, a numeric and a categorical obs
 * column, obsm["spatial"], var names, a 64 MB row block of X and one X column
 * -- for dense and CSR X under none / gzip / lzf.
 *
 * SpatialData (zarr v3, as spatialdata 0.8 writes it): zarrita over the picked
 * folder reads the table (obs, obsm, X), the image's multiscales and a chunk,
 * a label region; DuckDB-Wasm reads the shapes and points Parquet.
 *
 * Every answer is a SHA-256 of its bytes, compared by the driver with
 * bench/truth_p4.py's (h5py, zarr-python, pyarrow) on the same files.
 */
import * as zarr from "zarrita";

const now = () => performance.now();
const workerUrl = (name) => new URL(`dist/${name}`, location.href).href;

async function sha(bytes) {
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
const textBytes = (list) => new TextEncoder().encode(Array.from(list, (s) => String(s ?? "")).join("\n"));
const asBytes = (t) => new Uint8Array(t.buffer, t.byteOffset, t.byteLength);
const toFloat64 = (a) => (a instanceof Float64Array ? a : Float64Array.from(a, (v) => Number(v)));

function picked(name) {
    const file = [...document.getElementById("picker").files].find((f) => f.name === name);
    if (!file) throw new Error(`no picked file ${name}`);
    return file;
}

const H5AD_READS = ["obs_names", "obs_numeric", "obs_categorical", "obsm_spatial", "var_names", "x_rows", "x_column", "x_all"];

/**
 * h5ad files through workers/h5Worker.js, one read at a time, each run
 * `repeat` times (median, as Python's). A worker serves a file until a read
 * fails; a failed read can have aborted its wasm, so the next gets a new one.
 */
async function h5ad(names, column, repeat) {
    const nm = new URL("../nm/", location.href).href;
    let initMs = null;
    let plugins = null;
    const start = async () => {
        const worker = new Worker(workerUrl("h5Worker.js"));
        const call = (msg) => new Promise((resolve, reject) => {
            worker.onmessage = (e) => (e.data.ok ? resolve(e.data) : reject(new Error(e.data.error)));
            worker.onerror = (e) => reject(new Error(e.message || "worker error"));
            worker.postMessage(msg);
        });
        const t0 = now();
        const init = await call({ op: "init", h5wasmUrl: `${nm}h5wasm/dist/iife/h5wasm.js`,
                                  pluginBase: `${nm}h5wasm-plugins/plugins`, plugins: ["lzf", "zstd", "blosc", "lz4", "bshuf"] });
        initMs ??= now() - t0;
        plugins ??= init.plugins;
        return { worker, call };
    };
    const files = {};
    for (const name of names) {
        const file = picked(name);
        const ops = {};
        let info = null;
        let hand = await start();
        for (const read of H5AD_READS) {
            try {
                const r = await hand.call({ op: "read", file, name: read, repeat, column });
                ops[read] = { ms: r.ms, runs: r.runs, sha256: r.sha256 };
                info ||= r.info;
            } catch (error) {
                ops[read] = { error: String(error?.message || error).split("\n")[0].slice(0, 300) };
                hand.worker.terminate();
                hand = await start();
            }
        }
        hand.worker.terminate();
        files[name] = { ops, info };
    }
    return { initMs, plugins, files };
}

/** A zarrita store over a folder picked with <input webkitdirectory>. */
function folderStore(files) {
    const map = new Map();
    let rootName = null;
    for (const f of files) {
        const parts = f.webkitRelativePath.split("/");
        rootName ||= parts[0];
        map.set(`/${parts.slice(1).join("/")}`, f);
    }
    return {
        rootName,
        size: map.size,
        file: (key) => map.get(key),
        async get(key) {
            const f = map.get(key);
            return f ? new Uint8Array(await f.arrayBuffer()) : undefined;
        },
        async getRange(key, range) {
            const f = map.get(key);
            if (!f) return undefined;
            const blob = "suffixLength" in range ? f.slice(f.size - range.suffixLength)
                : f.slice(range.offset, range.offset + range.length);
            return new Uint8Array(await blob.arrayBuffer());
        },
    };
}

async function openArray(root, path) {
    return zarr.open(root.resolve(path), { kind: "array" });
}

/** A string array as anndata writes it in zarr: an array, or a nullable-string-array group. */
async function zarrStrings(root, path) {
    const node = await zarr.open(root.resolve(path));
    const arr = node instanceof zarr.Array ? node : await openArray(root, `${path}/values`);
    const { data } = await zarr.get(arr);
    return Array.from(data);
}

async function spatialData(column) {
    const store = folderStore(document.getElementById("dirpicker").files);
    const root = zarr.root(store);
    const ops = {};
    const time = async (name, fn) => {
        const s = now();
        try {
            ops[name] = { value: await fn(), ms: now() - s };
        } catch (error) {
            ops[name] = { error: String(error?.message || error).slice(0, 300), ms: now() - s };
        }
    };
    const t = "tables/table";
    await time("table_obs", async () => {
        const obsGroup = await zarr.open(root.resolve(`${t}/obs`), { kind: "group" });
        const indexName = obsGroup.attrs._index || "_index";
        const index = await zarrStrings(root, `${t}/obs/${indexName}`);
        const inst = (await zarr.get(await openArray(root, `${t}/obs/instance_id`))).data;
        const codes = (await zarr.get(await openArray(root, `${t}/obs/phenotype/codes`))).data;
        const cats = await zarrStrings(root, `${t}/obs/phenotype/categories`);
        return { index: await sha(textBytes(index)), instance_id: await sha(asBytes(toFloat64(inst))),
                 codes: await sha(asBytes(codes instanceof Int8Array ? codes : Int8Array.from(codes, Number))),
                 categories: await sha(textBytes(cats)) };
    });
    await time("table_obsm", async () => sha(asBytes(toFloat64((await zarr.get(await openArray(root, `${t}/obsm/spatial`))).data))));
    await time("table_x", async () => {
        const { data } = await zarr.get(await openArray(root, `${t}/X`));
        return sha(asBytes(data instanceof Float32Array ? data : Float32Array.from(data)));
    });
    await time("image_chunk", async () => {
        const arr = await openArray(root, "images/he_like/s0");
        const { data } = await zarr.get(arr, [zarr.slice(0, Math.min(3, arr.shape[0])), zarr.slice(0, 256), zarr.slice(0, 256)]);
        return { shape: arr.shape, sha256: await sha(asBytes(data)) };
    });
    await time("labels_region", async () => {
        const arr = await openArray(root, "labels/cells_labels/s0");
        const { data } = await zarr.get(arr, [zarr.slice(0, 512), zarr.slice(0, 512)]);
        return { shape: arr.shape, sha256: await sha(asBytes(data)) };
    });
    await time("multiscales", async () => {
        const g = await zarr.open(root.resolve("images/he_like"), { kind: "group" });
        const ms = g.attrs.ome?.multiscales || g.attrs.multiscales;
        return ms[0].datasets.map((d) => d.path);
    });
    // Parquet elements: hyparquet (JavaScript, nothing fetched), then DuckDB-Wasm
    // for comparison (its Parquet reader is an extension it downloads).
    const SHAPES = "/shapes/cells_circles/shapes.parquet";
    const POINTS = "/points/transcripts/points.parquet/part.0.parquet";
    const { parquetMetadataAsync, parquetReadObjects } = await import("hyparquet");
    const buffer = (key) => {
        const f = store.file(key);
        if (!f) throw new Error(`no ${key} in the picked folder`);
        return { byteLength: f.size, slice: (s, e) => f.slice(s, e).arrayBuffer() };
    };
    await time("shapes", async () => {
        const file = buffer(SHAPES);
        // geoparquet: false keeps the geometry as its WKB bytes (to hash).
        const metadata = await parquetMetadataAsync(file, { geoparquet: false });
        const [first] = await parquetReadObjects({ file, metadata, columns: ["geometry"], rowStart: 0, rowEnd: 1, utf8: false });
        return { rows: Number(metadata.num_rows), firstGeometry: await sha(first.geometry) };
    });
    await time("shapes_geojson", async () => {
        // The same file with hyparquet's GeoParquet decoding on: every geometry as GeoJSON.
        const rows = await parquetReadObjects({ file: buffer(SHAPES), columns: ["geometry"] });
        return { rows: rows.length, type: rows[0].geometry?.type ?? null };
    });
    await time("points", async () => {
        const rows = await parquetReadObjects({ file: buffer(POINTS), columns: ["x"] });
        return { rows: rows.length, x: await sha(asBytes(Float64Array.from(rows, (r) => r.x))) };
    });
    const duck = await import("@duckdb/duckdb-wasm");
    const { getDb } = await import("./table.js");
    const s0 = now();
    const { db } = await getDb();
    const conn = await db.connect();
    const duckdbStartMs = now() - s0;
    const register = async (key, name) => {
        await db.registerFileHandle(name, store.file(key), duck.DuckDBDataProtocol.BROWSER_FILEREADER, true);
        return name;
    };
    await time("shapes_duckdb", async () => {
        const name = await register(SHAPES, "shapes.parquet");
        const rows = Number((await conn.query(`SELECT count(*) AS n FROM read_parquet('${name}')`)).toArray()[0].n);
        const first = (await conn.query(`SELECT geometry FROM read_parquet('${name}') LIMIT 1`)).getChildAt(0).get(0);
        return { rows, firstGeometry: await sha(first) };
    });
    await time("points_duckdb", async () => {
        const name = await register(POINTS, "points.parquet");
        const r = await conn.query(`SELECT x FROM read_parquet('${name}')`);
        const x = r.getChildAt(0).toArray();
        return { rows: r.numRows, x: await sha(asBytes(toFloat64(x))) };
    });
    await conn.close();
    void column;
    return { files: store.size, root: store.rootName, ops, duckdbStartMs };
}

export async function p4({ h5ad: names = [], sdata = false, column = 5, repeat = 3 }) {
    const out = {};
    if (names.length) out.h5ad = await h5ad(names, column, repeat);
    if (sdata) out.sdata = await spatialData(column);
    out.jsHeap = performance.memory?.usedJSHeapSize ?? null;
    return out;
}
