// A file mounted in the website's worker dated by its own time, at every visit
// (client workers/pythonWorker.js `datedFiles`), run against Emscripten's
// WORKERFS as Pyodide 314.0.7 ships it (pyodide.asm.mjs, copied below with
// only FS.createNode stubbed). Node's File has no `lastModifiedDate`, as
// Firefox's has none: the case that dated every mount by the time of the mount.
//
//   node tests/js/workerfs_times_probe.mjs   (from the repository root)
import assert from "node:assert/strict";
import fs from "node:fs";

const source = fs.readFileSync("plexora/client/src/js/workers/pythonWorker.js", "utf8");
const found = source.match(/\nfunction datedFiles\(workerfs\) \{[\s\S]*?\n\}\n/);
assert.ok(found, "pythonWorker.js has no datedFiles(workerfs)");
const datedFiles = new Function(`${found[0]}; return datedFiles;`)();

let nextId = 1;
//: Emscripten's WORKERFS, as Pyodide 314.0.7's runtime has it.
function emscriptenWorkerfs() {
    const FS = { createNode: (parent, name, mode) => ({ id: nextId++, parent, name, mode }) };
    const WORKERFS = {
        DIR_MODE: 16895,
        FILE_MODE: 33279,
        mount(mount) {
            const root = WORKERFS.createNode(null, "/", WORKERFS.DIR_MODE, 0);
            const createdParents = {};
            function ensureParent(path) {
                const parts = path.split("/");
                let parent = root;
                for (let i = 0; i < parts.length - 1; i++) {
                    const curr = parts.slice(0, i + 1).join("/");
                    createdParents[curr] ||= WORKERFS.createNode(parent, parts[i], WORKERFS.DIR_MODE, 0);
                    parent = createdParents[curr];
                }
                return parent;
            }
            function base(path) {
                const parts = path.split("/");
                return parts[parts.length - 1];
            }
            for (const file of mount.opts.files || []) {
                WORKERFS.createNode(ensureParent(file.name), base(file.name), WORKERFS.FILE_MODE, 0, file,
                                    file.lastModifiedDate);
            }
            for (const obj of mount.opts.blobs || []) {
                WORKERFS.createNode(ensureParent(obj.name), base(obj.name), WORKERFS.FILE_MODE, 0, obj.data);
            }
            return root;
        },
        createNode(parent, name, mode, dev, contents, mtime) {
            const node = FS.createNode(parent, name, mode);
            node.mode = mode;
            node.atime = node.mtime = node.ctime = (mtime || new Date()).getTime();
            if (mode === WORKERFS.FILE_MODE) {
                node.size = contents.size;
                node.contents = contents;
            } else {
                node.size = 4096;
                node.contents = {};
            }
            if (parent) parent.contents[name] = node;
            return node;
        },
    };
    return WORKERFS;
}

const at = (root, path) => path.split("/").reduce((node, part) => node.contents[part], root);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const MASK_TIME = Date.UTC(2025, 4, 6, 7, 8, 9, 123);
const mask = new File([new Uint8Array(16)], "mask.tif", { lastModified: MASK_TIME });
const table = new File([new Uint8Array(8)], "cells.csv", { lastModified: MASK_TIME - 5000 });
assert.equal(mask.lastModifiedDate, undefined, "this node's File has lastModifiedDate: not Firefox's case");

// As shipped, each mount is dated by when it happened: a visit later is a
// changed file.
{
    const workerfs = emscriptenWorkerfs();
    const first = workerfs.mount({ opts: { files: [mask] } });
    await sleep(5);
    const again = workerfs.mount({ opts: { files: [mask] } });
    assert.notEqual(at(first, "mask.tif").mtime, MASK_TIME);
    assert.notEqual(at(first, "mask.tif").mtime, at(again, "mask.tif").mtime);
}

const workerfs = datedFiles(emscriptenWorkerfs());
assert.equal(datedFiles(workerfs), workerfs);
const wrapped = workerfs.createNode;
datedFiles(workerfs);
assert.equal(workerfs.createNode, wrapped, "dated twice: wrapped twice");

// Picked files (`files`): their own time, at every mount.
const picked = workerfs.mount({ opts: { files: [mask] } });
await sleep(5);
const reopened = workerfs.mount({ opts: { files: [mask] } });
assert.equal(at(picked, "mask.tif").mtime, MASK_TIME);
assert.equal(at(reopened, "mask.tif").mtime, MASK_TIME);
assert.equal(at(picked, "mask.tif").atime, MASK_TIME);
assert.equal(at(picked, "mask.tif").size, 16);

// A picked folder (`blobs`), which WORKERFS dates by nothing in any browser.
const folder = workerfs.mount({ opts: { blobs: [{ name: "run/segmentation/mask.tif", data: mask },
                                                 { name: "run/cells.csv", data: table }] } });
assert.equal(at(folder, "run/segmentation/mask.tif").mtime, MASK_TIME);
assert.equal(at(folder, "run/cells.csv").mtime, MASK_TIME - 5000);

// What has no time of its own keeps the mount's: a folder, a plain Blob.
const before = Date.now();
const plain = workerfs.mount({ opts: { blobs: [{ name: "a/slice.bin", data: new Blob([new Uint8Array(4)]) }] } });
assert.ok(at(plain, "a").mtime >= before);
assert.ok(at(plain, "a/slice.bin").mtime >= before);

// A File given a time of 0 keeps it (the epoch, not "no time").
const epoch = workerfs.mount({ opts: { files: [new File([new Uint8Array(1)], "old.tif", { lastModified: 0 })] } });
assert.equal(at(epoch, "old.tif").mtime, 0);

console.log("workerfs times probe OK");
