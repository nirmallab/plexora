// The page's Python (P9): Pyodide in a module worker, so the viewer's main
// thread never waits on it. Everything it loads comes from the page's own
// origin: /pyodide/ (the release, bench/p9_dist.py) and /wheels/ (Plexora
// and the pure-Python wheels Pyodide does not ship, unpacked straight into
// site-packages: no micropip, no resolver, nothing from another origin).
//
// Messages (each answered with {id, ok, result | error}):
//   boot {packages, wheels, sources}     -> {phases, shims, ready}
//   mount {dir, files: File[]}           WORKERFS: the user's picked files, read on demand
//   write {path, data}                   a file into MEMFS (config.json; a table polars maps)
//   link {target, path}                  a symlink (a picked file where the config names it)
//   columns {names, buffers}             Float64Array buffers for Python (js.p9Columns)
//   run {code}                           runPythonAsync; the last expression, as a string
//   heap                                 the wasm heap's size
//   reads                                picked-file reads: calls, file slices, bytes
//   resources                            this worker's fetches (bytes, cache hits)
// Phase marks are posted as they pass ({mark, at}, `at` on the epoch clock).
import { loadPyodide } from "/pyodide/pyodide.mjs";

let py = null;
const at = () => performance.timeOrigin + performance.now();
const mark = (name) => self.postMessage({ mark: name, at: at() });

// WORKERFS reads a picked file with one FileReaderSync slice per read() call,
// about 0.4 ms each however small; tifffile reads a header in thousands of
// small reads. Picked files are read here in 1 MiB blocks instead, the last
// 64 kept (an LRU), and reads over 4 MiB go straight through.
const BLOCK = 1 << 20;
const KEEP = 64;
const reads = { calls: 0, slices: 0, bytes: 0 };

function blockReads(workerfs) {
    const reader = new FileReaderSync();
    const blocks = new Map();
    const slice = (node, from, to) => {
        reads.slices += 1;
        return new Uint8Array(reader.readAsArrayBuffer(node.contents.slice(from, to)));
    };
    const block = (node, index) => {
        const key = `${node.id}:${index}`;
        let data = blocks.get(key);
        if (data) blocks.delete(key);
        else data = slice(node, index * BLOCK, (index + 1) * BLOCK);
        blocks.set(key, data);
        if (blocks.size > KEEP) blocks.delete(blocks.keys().next().value);
        return data;
    };
    workerfs.stream_ops.read = (stream, buffer, offset, length, position) => {
        const node = stream.node;
        const end = Math.min(node.size, position + length);
        if (position >= end) return 0;
        reads.calls += 1;
        reads.bytes += end - position;
        if (end - position > 4 * BLOCK) {
            buffer.set(slice(node, position, end), offset);
            return end - position;
        }
        for (let p = position; p < end;) {
            const index = Math.floor(p / BLOCK);
            const data = block(node, index);
            const from = p - index * BLOCK;
            const n = Math.min(data.length - from, end - p);
            buffer.set(data.subarray(from, from + n), offset + (p - position));
            p += n;
        }
        return end - position;
    };
}

async function boot({ packages, wheels, sources, bare = false }) {
    const phases = { start: at() };
    // The wheels download while the core boots and the packages load.
    const fetched = wheels.map((url) => fetch(url).then((r) => {
        if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
        return r.arrayBuffer();
    }));
    const texts = Object.fromEntries(await Promise.all(Object.entries(sources).map(
        async ([name, url]) => [name, await (await fetch(url)).text()])));
    py = await loadPyodide({ indexURL: "/pyodide/" });
    blockReads(py.FS.filesystems.WORKERFS);
    phases.core = at(); mark("core");
    await py.loadPackage(packages, { messageCallback: () => {}, errorCallback: (m) => self.postMessage({ log: m }) });
    phases.packages = at(); mark("packages");
    const site = py.runPython("import site; site.getsitepackages()[0]");
    for (const buffer of await Promise.all(fetched)) py.unpackArchive(buffer, "wheel", { extractDir: site });
    phases.wheels = at(); mark("wheels");
    py.FS.mkdirTree("/p9");
    for (const [name, text] of Object.entries(texts)) py.FS.writeFile(`/p9/${name}`, text);
    py.runPython("import sys; sys.path.insert(0, '/p9')");
    if (bare) return { phases, shims: null, ready: null };
    py.globals.set("SITE", site);
    const shims = JSON.parse(await py.runPythonAsync("import p9_boot, json; json.dumps(p9_boot.install(SITE, '/data'))"));
    phases.shims = at(); mark("shims");
    const ready = JSON.parse(await py.runPythonAsync("import p9_run, json; json.dumps(p9_run.warm())"));
    phases.ready = at(); mark("ready");
    return { phases, shims, ready };
}

const OPS = {
    boot,
    mount({ dir, files }) {
        py.FS.mkdirTree(dir);
        py.FS.mount(py.FS.filesystems.WORKERFS, { files }, dir);
        return files.map((f) => f.name);
    },
    write({ path, data }) {
        py.FS.mkdirTree(path.slice(0, path.lastIndexOf("/")) || "/");
        py.FS.writeFile(path, data);
        return true;
    },
    link({ target, path }) {
        py.FS.mkdirTree(path.slice(0, path.lastIndexOf("/")) || "/");
        py.FS.symlink(target, path);
        return true;
    },
    columns({ names, buffers }) {
        self.p9Columns = { names, buffers };
        return names.length;
    },
    async run({ code }) {
        const value = await py.runPythonAsync(code);
        return value === undefined ? null : String(value);
    },
    heap() {
        return py ? py._module.HEAP8.length : null;
    },
    reads() {
        return { ...reads };
    },
    resources() {
        return performance.getEntriesByType("resource").map((e) => ({
            name: new URL(e.name).pathname, transfer: e.transferSize, encoded: e.encodedBodySize,
            decoded: e.decodedBodySize, start: e.startTime, ms: e.duration,
        }));
    },
};

self.onmessage = async ({ data }) => {
    const { id, op, args } = data;
    try {
        self.postMessage({ id, ok: true, result: await OPS[op](args || {}) });
    } catch (error) {
        self.postMessage({ id, ok: false, error: String(error?.message || error).slice(-6000) });
    }
};
self.postMessage({ mark: "loaded", at: at() });
