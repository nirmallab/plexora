// A reload of the site in Chrome or Edge keeps the tab's Python (a SharedWorker,
// client workers/pythonWorker.js), and with it every mount the page before
// made. The new page mounts its picked folders, slides, DICOM and NDPIS sets
// again (services/pickedFiles.js `restore` -> worker `mountTree`): mounting
// over a mount point that is still there is Emscripten's EBUSY, which left
// every such pick behind Reconnect. Runs the worker's `mountTree` against a
// stand-in FS that refuses as Emscripten's does. Prints JSON;
// tests/test_tab_python_remount_tree.py asserts on it.
//
//   node tests/js/tab_python_remount_tree_probe.mjs   (from the repository root)
import assert from "node:assert/strict";
import fs from "node:fs";

const source = fs.readFileSync("plexora/client/src/js/workers/pythonWorker.js", "utf8");
const found = source.match(/\nfunction mountTree\(\{ dir, blobs \}\) \{[\s\S]*?\n\}\n/);
assert.ok(found, "pythonWorker.js has no mountTree({ dir, blobs })");

//: Emscripten's FS as far as mounting goes: a mount over a mount point throws.
function stubFS() {
    const mounts = new Map();
    const dirs = new Set(["/"]);
    return {
        mounts,
        filesystems: { WORKERFS: {} },
        analyzePath: (path) => ({ exists: dirs.has(path) }),
        lookupPath: (path) => ({ node: { path, mounted: mounts.has(path) } }),
        isMountpoint: (node) => node.mounted,
        mkdirTree: (path) => { dirs.add(path); },
        mount: (type, opts, path) => {
            if (mounts.has(path)) {
                const error = new Error("FS error"); error.errno = 10; error.code = "EBUSY"; throw error;
            }
            mounts.set(path, opts);
        },
        unmount: (path) => {
            if (!mounts.has(path)) throw new Error("EINVAL");
            mounts.delete(path);
        },
    };
}

const py = { FS: stubFS() };
const datedFiles = (workerfs) => workerfs;
const mountTree = new Function("py", "datedFiles", `${found[0]}; return mountTree;`)(py, datedFiles);

const before = [{ name: "set/a.tif", data: new Blob([new Uint8Array(4)]) }];
const after = [{ name: "set/a.tif", data: new Blob([new Uint8Array(4)]) },
               { name: "set/b.tif", data: new Blob([new Uint8Array(8)]) }];
const result = { first: null, again: null, error: null, blobs: null };
result.first = mountTree({ dir: "/picked/p1", blobs: before });
try {
    result.again = mountTree({ dir: "/picked/p1", blobs: after });
} catch (error) {
    result.error = error.code || String(error.message || error);
}
result.blobs = (py.FS.mounts.get("/picked/p1")?.blobs || []).map((blob) => blob.name);
console.log(JSON.stringify(result));
