// The website's `.plexora` codec (client workers/bundleWorker.js), in Node:
// its CRC-32 and SHA-256 against known values, and the ZIP64 file it writes
// read back field by field. tests/test_bundle_codec.py runs this and pins its
// lines; it also runs the two modes below against Python's own bundles.
//
//   node tests/js/bundle_codec_probe.mjs                  the checks
//   node tests/js/bundle_codec_probe.mjs --write PLAN OUT  a bundle written from
//        bundles.plan_for_page's JSON, as the page does: prints {sums, left_out},
//        reads the finished manifest on stdin, prints {size}
//   node tests/js/bundle_codec_probe.mjs --read FILE       every member read back:
//        {name: {size, crc, sha256, method}}
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { closeSync, openAsBlob, openSync, writeSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import zlib from "node:zlib";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..", "..");
const codec = await import(pathToFileURL(path.join(REPO, "plexora", "client", "src", "js", "workers", "bundleWorker.js")).href);
const { crc32, Sha256, ZipWriter, readCentral, pumpMember, readMember, chooseMethod, MANIFEST, STORED, DEFLATED } = codec;

const text = (value) => new TextEncoder().encode(value);
const hex = (bytes) => Buffer.from(bytes).toString("hex");

//: Positional writes into memory, as the Save dialog's file takes them.
class MemorySink {
    constructor() {
        this.buffer = new Uint8Array(1 << 16);
        this.size = 0;
    }

    write(bytes, at) {
        const end = at + bytes.length;
        if (end > this.buffer.length) {
            const grown = new Uint8Array(Math.max(end, this.buffer.length * 2));
            grown.set(this.buffer.subarray(0, this.size));
            this.buffer = grown;
        }
        this.buffer.set(bytes, at);
        this.size = Math.max(this.size, end);
    }

    blob() {
        return new Blob([this.buffer.slice(0, this.size)]);
    }
}

function streamOf(bytes) {
    return new Blob([bytes]).stream();
}

async function writeOne(members, { reserve = 256, zip64From, zip64End } = {}) {
    const sink = new MemorySink();
    const writer = new ZipWriter(sink, { zip64From, zip64End });
    await writer.reserve(MANIFEST, reserve);
    const written = {};
    for (const [name, bytes, method] of members) {
        written[name] = await writer.add(name, streamOf(bytes), { size: bytes.length, method });
    }
    return { sink, writer, written };
}

// -- the modes Python drives ---------------------------------------------------

async function readStdin() {
    const parts = [];
    for await (const chunk of process.stdin) parts.push(chunk);
    return Buffer.concat(parts);
}

if (process.argv[2] === "--write") {
    const plan = JSON.parse(Buffer.from(await openAsBlob(process.argv[3]).then((b) => b.arrayBuffer())).toString("utf8"));
    const fd = openSync(process.argv[4], "w");
    const sink = { write: (bytes, at) => { writeSync(fd, bytes, 0, bytes.length, at); } };
    const writer = new ZipWriter(sink);
    await writer.reserve(MANIFEST, plan.reserve);
    const sums = {};
    const leftOut = [];
    for (const member of plan.members) {
        let blob;
        try {
            blob = await openAsBlob(member.source);
        } catch (error) {
            leftOut.push({ path: member.bundle_path, why: "gone before it was packed" });
            continue;
        }
        const method = await chooseMethod(member.bundle_path, blob);
        const done = await writer.add(member.bundle_path, blob.stream(), { size: blob.size, method });
        sums[member.bundle_path] = { sha256: done.sha256, size: done.size };
    }
    process.stdout.write(`${JSON.stringify({ sums, left_out: leftOut })}\n`);
    await writer.fill(MANIFEST, await readStdin());
    const size = await writer.finish();
    closeSync(fd);
    process.stdout.write(`${JSON.stringify({ size })}\n`);
    process.exit(0);
}

if (process.argv[2] === "--read") {
    const blob = await openAsBlob(process.argv[3]);
    const out = {};
    for (const entry of await readCentral(blob)) {
        const digest = createHash("sha256");
        const done = await pumpMember(blob, entry, (chunk) => { digest.update(chunk); });
        out[entry.name] = { size: done.size, crc: done.crc, sha256: digest.digest("hex"), method: entry.method };
    }
    process.stdout.write(`${JSON.stringify(out)}\n`);
    process.exit(0);
}

// -- the checks ----------------------------------------------------------------

const checks = [];
async function check(name, run) {
    await run();
    checks.push(name);
    console.log(`ok - ${name}`);
}

await check("crc32 matches zlib's check values", () => {
    assert.equal(crc32(new Uint8Array(0)), 0);
    assert.equal(crc32(text("123456789")), 0xcbf43926);
    assert.equal(crc32(text("The quick brown fox jumps over the lazy dog")), 0x414fa339);
    assert.equal(crc32(new Uint8Array(32)), 0x190a55ad);
});

await check("crc32 runs on across chunks and matches node's zlib on a megabyte", () => {
    const data = new Uint8Array(1 << 20);
    for (let i = 0; i < data.length; i += 1) data[i] = (i * 2654435761) >>> 24;
    let running = 0;
    for (let at = 0; at < data.length; at += 7919) running = crc32(data.subarray(at, Math.min(data.length, at + 7919)), running);
    assert.equal(running, crc32(data));
    assert.equal(crc32(data), zlib.crc32(data));
});

await check("sha256 matches the FIPS 180 vectors", () => {
    const of = (value) => new Sha256().update(value).hex();
    assert.equal(of(new Uint8Array(0)), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
    assert.equal(of(text("abc")), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    assert.equal(of(text("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq")),
                 "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1");
    assert.equal(of(new Uint8Array(1000000).fill(0x61)),
                 "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0");
});

await check("sha256 fed in odd chunks matches one digest of the whole", () => {
    for (const length of [0, 1, 55, 56, 63, 64, 65, 119, 120, 128, 1000, 100003]) {
        const data = new Uint8Array(length);
        for (let i = 0; i < length; i += 1) data[i] = (i * 31 + 7) & 0xff;
        const digest = new Sha256();
        for (let at = 0, step = 1; at < length; at += step, step = (step * 3 + 1) % 97 + 1) {
            digest.update(data.subarray(at, Math.min(length, at + step)));
        }
        assert.equal(digest.hex(), createHash("sha256").update(data).digest("hex"), `length ${length}`);
    }
});

await check("a member is stored or deflated as Python chooses", async () => {
    const noise = new Uint8Array(1 << 20);
    let seed = 1;
    for (let i = 0; i < noise.length; i += 1) {
        seed = (Math.imul(seed, 1103515245) + 12345) >>> 0;
        noise[i] = seed >>> 24;
    }
    const csv = text("CellID,X,Y,CD3\n".repeat(20000));
    assert.equal(await chooseMethod("a/picture.png", new Blob([csv])), STORED);
    assert.equal(await chooseMethod("a/table.PARQUET", new Blob([csv])), STORED);
    assert.equal(await chooseMethod("a/slide.ome.tif", new Blob([noise])), STORED);
    assert.equal(await chooseMethod("a/cells.csv", new Blob([csv])), DEFLATED);
    assert.equal(await chooseMethod("a/tiny.json", new Blob([text("{}")])), DEFLATED);
});

await check("the manifest is the first member, stored, padded with spaces, its CRC patched", async () => {
    const { sink, writer } = await writeOne([["projects/p/state/a.csv", text("x,y\n1,2\n".repeat(200)), DEFLATED]],
                                            { reserve: 64 });
    const manifest = text('{"format": 2}');
    await writer.fill(MANIFEST, manifest);
    await writer.finish();
    const bytes = sink.buffer.subarray(0, sink.size);
    const view = new DataView(bytes.buffer, bytes.byteOffset);
    assert.equal(view.getUint32(0, true), 0x04034b50);
    assert.equal(view.getUint16(8, true), STORED);
    const nameLength = view.getUint16(26, true);
    assert.equal(new TextDecoder().decode(bytes.subarray(30, 30 + nameLength)), MANIFEST);
    const data = bytes.subarray(30 + nameLength, 30 + nameLength + 64);
    assert.equal(JSON.parse(new TextDecoder().decode(data)).format, 2);
    assert.ok(data.subarray(manifest.length).every((byte) => byte === 0x20));
    assert.equal(view.getUint32(14, true), crc32(data));
    assert.equal(view.getUint32(18, true), 64);
    const entries = await readCentral(sink.blob());
    assert.equal(entries[0].name, MANIFEST);
    assert.equal(entries[0].crc, crc32(data));
    await assert.rejects(() => writer.fill(MANIFEST, new Uint8Array(65)), /outgrew/);
});

await check("a large member has ZIP64 sizes in its local header", async () => {
    const body = text("ZIP64 from the start\n".repeat(50));
    const { sink, writer, written } = await writeOne([["media/k/big.csv", body, DEFLATED], ["small.txt", text("hi"), STORED]],
                                                     { zip64From: 100 });
    await writer.finish();
    const bytes = sink.buffer.subarray(0, sink.size);
    const view = new DataView(bytes.buffer, bytes.byteOffset);
    const big = writer.entries.find((entry) => entry.name === "media/k/big.csv");
    const at = big.offset;
    assert.equal(view.getUint16(at + 4, true), 45);
    assert.equal(view.getUint32(at + 14, true), crc32(body));
    assert.equal(view.getUint32(at + 18, true), 0xffffffff);
    assert.equal(view.getUint32(at + 22, true), 0xffffffff);
    const nameLength = view.getUint16(at + 26, true);
    assert.equal(view.getUint16(at + 28, true), 20);
    const extra = at + 30 + nameLength;
    assert.equal(view.getUint16(extra, true), 1);
    assert.equal(view.getUint16(extra + 2, true), 16);
    assert.equal(Number(view.getBigUint64(extra + 4, true)), body.length);
    assert.equal(Number(view.getBigUint64(extra + 12, true)), written["media/k/big.csv"].csize);
    // A small one has plain sizes, patched in.
    const small = writer.entries.find((entry) => entry.name === "small.txt");
    assert.equal(view.getUint16(small.offset + 28, true), 0);
    assert.equal(view.getUint32(small.offset + 22, true), 2);
});

await check("ZIP64 sizes and offsets in the central directory read back", async () => {
    const { sink, writer } = await writeOne([["a.bin", text("abc"), STORED], ["b.bin", text("defg"), STORED]]);
    // As a 5 GB member 6 GB into the file would be written (the manifest's
    // room is entry 0).
    Object.assign(writer.entries[2], { usize: 5e9, csize: 4.5e9, offset: 6e9 });
    await writer.finish();
    const entries = await readCentral(sink.blob());
    assert.equal(entries[2].name, "b.bin");
    assert.equal(entries[2].usize, 5e9);
    assert.equal(entries[2].csize, 4.5e9);
    assert.equal(entries[2].offset, 6e9);
    assert.equal(entries[1].usize, 3);
    // In the record itself: the 32-bit fields full, the extra holding them.
    const bytes = sink.buffer.subarray(0, sink.size);
    const view = new DataView(bytes.buffer, bytes.byteOffset);
    let at = view.getUint32(writer.at - 22 + 16, true);
    for (let skip = 0; skip < 2; skip += 1) {
        at += 46 + view.getUint16(at + 28, true) + view.getUint16(at + 30, true) + view.getUint16(at + 32, true);
    }
    assert.equal(view.getUint32(at, true), 0x02014b50);
    assert.equal(view.getUint16(at + 6, true), 45);
    assert.equal(view.getUint32(at + 20, true), 0xffffffff);
    assert.equal(view.getUint32(at + 24, true), 0xffffffff);
    assert.equal(view.getUint32(at + 42, true), 0xffffffff);
    assert.equal(view.getUint16(at + 30, true), 4 + 24);
});

await check("ZIP64 end records are found from the end record", async () => {
    const { sink, writer } = await writeOne([["a.txt", text("one"), DEFLATED], ["b.txt", text("two"), STORED]],
                                            { zip64End: true });
    await writer.fill(MANIFEST, text("{}"));
    await writer.finish();
    const bytes = sink.buffer.subarray(0, sink.size);
    const view = new DataView(bytes.buffer, bytes.byteOffset);
    const end = sink.size - 22;
    assert.equal(view.getUint32(end - 20, true), 0x07064b50);
    const at64 = Number(view.getBigUint64(end - 12, true));
    assert.equal(view.getUint32(at64, true), 0x06064b50);
    assert.equal(Number(view.getBigUint64(at64 + 32, true)), 3);
    // The end record's own fields full, as Python writes them when it must:
    // the reader goes by the ZIP64 record.
    view.setUint16(end + 10, 0xffff, true);
    view.setUint32(end + 12, 0xffffffff, true);
    view.setUint32(end + 16, 0xffffffff, true);
    const entries = await readCentral(sink.blob());
    assert.deepEqual(entries.map((entry) => entry.name), [MANIFEST, "a.txt", "b.txt"]);
});

await check("every member reads back with its CRC and sha256", async () => {
    const members = [["one.csv", text("a,b\n".repeat(5000)), DEFLATED], ["two.png", new Uint8Array(3000).fill(7), STORED],
                     ["empty.txt", new Uint8Array(0), DEFLATED], ["naïve/ü.json", text('{"ok": true}'), DEFLATED]];
    const { sink, writer, written } = await writeOne(members);
    await writer.fill(MANIFEST, text('{"format": 2}'));
    await writer.finish();
    const blob = sink.blob();
    const entries = new Map((await readCentral(blob)).map((entry) => [entry.name, entry]));
    for (const [name, bytes, method] of members) {
        const entry = entries.get(name);
        assert.equal(entry.method, method, name);
        const back = await readMember(blob, entry);
        assert.deepEqual(back, bytes, name);
        assert.equal(written[name].sha256, createHash("sha256").update(bytes).digest("hex"), name);
        await pumpMember(blob, entry, () => {}, { sha256: written[name].sha256 });
    }
    assert.equal(entries.get("naïve/ü.json").flags & 0x800, 0x800);
    assert.equal(JSON.parse(new TextDecoder().decode(await readMember(blob, entries.get(MANIFEST)))).format, 2);
});

await check("a damaged member is refused", async () => {
    const { sink, writer } = await writeOne([["x.txt", text("hello hello hello"), STORED]]);
    await writer.finish();
    const entry = writer.entries.find((item) => item.name === "x.txt");
    sink.buffer[entry.offset + 30 + entry.nameBytes.length + 2] ^= 0xff;
    const [, read] = await readCentral(sink.blob());
    await assert.rejects(() => pumpMember(sink.blob(), read, () => {}), /does not match/);
    await assert.rejects(() => pumpMember(new Blob([sink.buffer.slice(0, sink.size)]), { ...read }, () => {},
                                          { sha256: "0".repeat(64) }), /does not match/);
    await assert.rejects(() => readCentral(new Blob([text("not a zip at all")])), /not a Plexora project file/);
});

await check("a member that inflates past its size is refused", async () => {
    const zeros = new Uint8Array(4 << 20);
    const { sink, writer } = await writeOne([["bomb.bin", zeros, DEFLATED]]);
    await writer.finish();
    const [, entry] = await readCentral(sink.blob());
    // What it says it is, and what a bomb would say instead.
    await assert.rejects(() => pumpMember(sink.blob(), { ...entry, usize: 1000 }, () => {}), /longer than it says/);
    await assert.rejects(() => pumpMember(sink.blob(), { ...entry, usize: entry.csize * 2000 }, () => {}), /inflates far beyond/);
});

assert.equal(hex(new Sha256().update(text("abc")).digest()).length, 64);
console.log(`${checks.length} checks passed`);
