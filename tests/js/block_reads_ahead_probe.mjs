// The website worker's reads of a picked file (client workers/pythonWorker.js
// `blockReads`) and its read ahead: the pieces asked for are read at once --
// neighbours as one slice -- and held; a read that starts in them is answered
// from them alone, short where it runs past them, with no block read; a read
// anywhere else reads its block as before. FileReaderSync is the browser's,
// stood in for here with a count.
//
//   node tests/js/block_reads_ahead_probe.mjs   (from the repository root)
import assert from "node:assert/strict";
import fs from "node:fs";

const source = fs.readFileSync("plexora/client/src/js/workers/pythonWorker.js", "utf8");
const pick = (pattern, name) => {
    const found = source.match(pattern);
    assert.ok(found, `pythonWorker.js has no ${name}`);
    return found[0];
};
const code = ["BLOCK", "KEEP", "PIECE", "PIECES"].map((name) => pick(new RegExp(`\\nconst ${name} = [^\\n]*\\n`), name))
    .concat([pick(/\nfunction withGil\(module, read\) \{[\s\S]*?\n\}\n/, "withGil(module, read)"),
             pick(/\nfunction blockReads\(workerfs, module\) \{[\s\S]*?\n\}\n/, "blockReads(workerfs, module)")])
    .join("");
const { blockReads, BLOCK, PIECE, PIECES } = new Function(`${code}; return { blockReads, BLOCK, PIECE, PIECES };`)();

//: Reads of the file: FileReaderSync's (a block, one at a time) and the read
//: ahead's slices ([from, to] each).
let synchronous = 0;
const slices = [];
globalThis.FileReaderSync = class {
    readAsArrayBuffer(part) {
        synchronous += 1;
        return part.bytes.slice().buffer;
    }
};

//: A picked file of `size` bytes, each byte its offset mod 251, as WORKERFS holds it.
function picked(id, size) {
    const bytes = new Uint8Array(size).map((_, i) => i % 251);
    const contents = {
        size,
        slice: (from, to) => ({
            bytes: bytes.subarray(from, Math.min(to, size)),
            arrayBuffer: async () => { slices.push([from, Math.min(to, size)]); return bytes.slice(from, Math.min(to, size)).buffer; },
        }),
    };
    return { id, size, contents };
}

const workerfs = { stream_ops: {} };
const readAhead = blockReads(workerfs, { _PyGILState_Check: () => 1 });
//: One read() as Emscripten makes it: -> the bytes it answered, checked.
const read = (node, position, length) => {
    const buffer = new Uint8Array(length);
    const n = workerfs.stream_ops.read({ node }, buffer, 0, length, position);
    for (let i = 0; i < n; i += 1) assert.equal(buffer[i], (position + i) % 251);
    return n;
};

const size = 2 * BLOCK + 100;
const file = picked(1, size);
const last = Math.floor((size - 1) / PIECE);
// Pieces 0, 1, 2 and the last: two slices, the three neighbours as one; again,
// past the end, negative or not whole: not read.
assert.equal(await readAhead(file, [2, 0, 1, 1, last, last + 1, -1, 1.5]), 4);
assert.deepEqual(slices, [[0, 3 * PIECE], [last * PIECE, size]]);
assert.equal(read(file, 10, 100), 100);
assert.equal(read(file, PIECE - 4, 8), 8, "a read across held neighbours is whole");
// A buffered reader's 128 KiB from inside the third piece: what is held, short.
assert.equal(read(file, 3 * PIECE - 100, 128 << 10), 100);
assert.equal(read(file, last * PIECE + 50, 1000), size - last * PIECE - 50, "the file's end, from its piece");
assert.equal(synchronous, 0, "a read that starts in what was read ahead reads no block");
assert.equal(read(file, 3 * PIECE, 16), 16);
assert.equal(synchronous, 1, "a read anywhere else reads its block, as before");
assert.equal(read(file, 3 * PIECE + 20, 16), 16);
assert.equal(synchronous, 1, "and keeps it");
assert.equal(await readAhead(file, [0, 1, last]), 0, "held pieces are not read again");

const wide = picked(2, 40 * BLOCK);
assert.equal(await readAhead(wide, Array.from({ length: PIECES + 40 }, (_, i) => 2 * i)), PIECES,
             "one read ahead reads at most PIECES");
console.log(JSON.stringify({ ok: true, slices: slices.length, synchronous }));
