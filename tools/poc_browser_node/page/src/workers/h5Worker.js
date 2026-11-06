/**
 * P4: an .h5ad read the way Plexora's AnnData adapter reads it, with h5wasm
 * (libhdf5 in wasm) over the picked File -- mounted through Emscripten's
 * WORKERFS, so libhdf5 reads only the bytes it asks for, never the whole file.
 *
 * {op: "init", h5wasmUrl, pluginBase, plugins} -> {ok, plugins}
 * {op: "read", file, name, repeat, rowBlockBytes, column} -> {ok, ms (median), runs, sha256, info}
 *
 * One read per message, so the page can tell which one fails: a filter plugin
 * that aborts takes the whole wasm instance with it, and the page starts a
 * fresh worker for the next read.
 *
 * Answers are hashed in the layout bench/truth_p4.py hashes Python's:
 * float64/float32/int8 C-order little-endian bytes, strings joined with "\n"
 * as UTF-8. A string array is read whether it is a dataset (`string-array`)
 * or, as anndata writes since 0.11, a `nullable-string-array` group.
 */
let mounted = 0;
let open = null; // {file, f, info}

async function sha(bytes) {
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
const textBytes = (list) => new TextEncoder().encode(list.map(String).join("\n"));
const asBytes = (typed) => new Uint8Array(typed.buffer, typed.byteOffset, typed.byteLength);
const num = (v) => (typeof v === "bigint" ? Number(v) : v);
const isGroup = (node) => typeof node.keys === "function";
const attr = (node, name) => node.attrs?.[name]?.value;
const float64 = (v) => (v instanceof Float64Array ? v : Float64Array.from(v, num));
const float32 = (v) => (v instanceof Float32Array ? v : Float32Array.from(v, num));

function stringsOf(node) {
    const ds = isGroup(node) ? node.get("values") : node;
    return Array.from(ds.value, (s) => (s == null ? "" : String(s)));
}

async function openFile(file, rowBlockBytes) {
    if (open && open.file === file) return open;
    const { FS } = await h5wasm.ready;
    const dir = `/work${mounted++}`;
    FS.mkdir(dir);
    FS.mount(FS.filesystems.WORKERFS, { files: [file] }, dir);
    const t0 = performance.now();
    const f = new h5wasm.File(`${dir}/${file.name}`, "r");
    const openMs = performance.now() - t0;
    const x = f.get("X");
    const csr = isGroup(x);
    const xShape = csr ? Array.from(attr(x, "shape"), num) : x.shape.map(num);
    const [nObs, nVars] = xShape;
    const rows = Math.min(nObs, Math.max(1, Math.floor(rowBlockBytes / (nVars * 4))));
    const codec = (csr ? x.get("data") : x).filters?.map((p) => p.name || p.id) ?? null;
    open = { file, f, x, csr, nObs, nVars, rows, info: { layout: csr ? "csr" : "dense", nObs, nVars, rowBlock: rows, openMs, codec } };
    return open;
}

const READS = {
    async obs_names({ f }) {
        const obs = f.get("obs");
        return sha(textBytes(stringsOf(obs.get(attr(obs, "_index")))));
    },
    async obs_numeric({ f }) {
        return sha(asBytes(float64(f.get("obs/X_centroid").value)));
    },
    async obs_categorical({ f }) {
        const g = f.get("obs/phenotype");
        const codes = g.get("codes").value;
        return { codes: await sha(asBytes(codes instanceof Int8Array ? codes : Int8Array.from(codes, num))),
                 categories: await sha(textBytes(stringsOf(g.get("categories")))) };
    },
    async obsm_spatial({ f }) {
        return sha(asBytes(float64(f.get("obsm/spatial").value)));
    },
    async var_names({ f }) {
        const v = f.get("var");
        return sha(textBytes(stringsOf(v.get(attr(v, "_index")))));
    },
    async x_rows({ x, csr, rows, nVars }) {
        if (!csr) return sha(asBytes(float32(x.slice([[0, rows], []]))));
        const ptr = Array.from(x.get("indptr").slice([[0, rows + 1]]), num);
        const data = x.get("data").slice([[ptr[0], ptr[rows]]]);
        const idx = x.get("indices").slice([[ptr[0], ptr[rows]]]);
        const dense = new Float32Array(rows * nVars);
        for (let r = 0; r < rows; r += 1) {
            for (let k = ptr[r]; k < ptr[r + 1]; k += 1) dense[r * nVars + num(idx[k - ptr[0]])] = data[k - ptr[0]];
        }
        return sha(asBytes(dense));
    },
    async x_all({ x, csr, rows, nObs, nVars }) {
        // All of X in row blocks, as columns (the one pass that would fill a
        // column store): float32, feature-major.
        const out = new Float32Array(nVars * nObs);
        const ptr = csr ? Array.from(x.get("indptr").value, num) : null;
        for (let r0 = 0; r0 < nObs; r0 += rows) {
            const r1 = Math.min(nObs, r0 + rows);
            if (!csr) {
                const block = float32(x.slice([[r0, r1], []]));
                for (let r = 0; r < r1 - r0; r += 1) {
                    for (let j = 0; j < nVars; j += 1) out[j * nObs + r0 + r] = block[r * nVars + j];
                }
                continue;
            }
            const data = x.get("data").slice([[ptr[r0], ptr[r1]]]);
            const idx = x.get("indices").slice([[ptr[r0], ptr[r1]]]);
            for (let r = r0; r < r1; r += 1) {
                for (let k = ptr[r]; k < ptr[r + 1]; k += 1) out[num(idx[k - ptr[r0]]) * nObs + r] = data[k - ptr[r0]];
            }
        }
        return sha(asBytes(out));
    },
    async x_column({ x, csr, nObs }, column) {
        if (!csr) return sha(asBytes(float32(x.slice([[], [column, column + 1]]))));
        const ptr = x.get("indptr").value;
        const idx = x.get("indices").value;
        const data = x.get("data").value;
        const col = new Float32Array(nObs);
        for (let r = 0; r < nObs; r += 1) {
            for (let k = num(ptr[r]); k < num(ptr[r + 1]); k += 1) if (num(idx[k]) === column) col[r] = data[k];
        }
        return sha(asBytes(col));
    },
};

self.onmessage = async (event) => {
    const msg = event.data;
    try {
        if (msg.op === "init") {
            self.importScripts(msg.h5wasmUrl);
            const Module = await h5wasm.ready;
            const path = Module.get_plugin_search_paths()[0];
            Module.FS.mkdirTree(path);
            for (const name of msg.plugins) {
                const r = await fetch(`${msg.pluginBase}/libH5Z${name}.so`);
                Module.FS.writeFile(`${path}/libH5Z${name}.so`, new Uint8Array(await r.arrayBuffer()));
            }
            self.postMessage({ ok: true, plugins: Module.FS.readdir(path).filter((n) => n.endsWith(".so")) });
        } else if (msg.op === "read") {
            const state = await openFile(msg.file, msg.rowBlockBytes || 64 * 1024 * 1024);
            const runs = [];
            let value = null;
            for (let i = 0; i < (msg.repeat || 1); i += 1) {
                const s = performance.now();
                value = await READS[msg.name](state, msg.column ?? 5);
                runs.push(performance.now() - s);
            }
            const sorted = [...runs].sort((a, b) => a - b);
            self.postMessage({ ok: true, ms: sorted[Math.floor(sorted.length / 2)], runs, sha256: value, info: state.info });
        }
    } catch (error) {
        self.postMessage({ ok: false, error: `${error}${error?.stack ? ` | ${error.stack}` : ""}` });
    }
};
