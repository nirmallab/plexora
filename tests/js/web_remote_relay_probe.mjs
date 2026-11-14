/**
 * The page's relay to a data node (services/webRemote.js `relay`): what its
 * Response hands the website's Python.
 *
 * The node compresses a table's `all_cells` whatever it is asked, and its
 * relay link carries the bytes as served. A Response built in the page is
 * never decoded by the browser, so an answer still gzipped reached
 * `NodeTable.all_cells` as gzip and was read as float32 cell values. Pinned
 * here: an encoded body comes out inflated, without its Content-Encoding.
 *
 * Run directly:  node tests/js/web_remote_relay_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { gzipSync, deflateSync } from "node:zlib";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/services/webRemote.js");
const ROOM = "RoomRoomRoomRoomRoom1";

const failures = [];
function check(name, ok) {
    console.log(`${ok ? "ok  " : "FAIL"} ${name}`);
    if (!ok) failures.push(name);
}

// -- a data node behind the room: answers whatever this test says ------------

let reply = null;
const asked = [];
const fakeRoom = {
    fetch: async (request) => { asked.push(request); return reply; },
    on: () => {},
    state: () => ({ socket: "open", node: { hostname: "gpu" } }),
    close: () => {},
    stop: async () => {},
};

const stored = new Map([["plexora.webRemote.rooms",
    JSON.stringify({ [ROOM]: { name: "gpu", auth: "a", key: "k", relay: "wss://relay.example" } })]]);
const window = {
    location: { href: "https://viewer.example/" },
    localStorage: { getItem: (k) => stored.get(k) ?? null, setItem: (k, v) => stored.set(k, v) },
    PlexoraRelayRoom: { newSecret: () => "s", join: () => fakeRoom },
    setTimeout: () => 0, clearTimeout: () => {},
};
const context = createContext({
    window, console, URL, Headers, Response, Request, DecompressionStream, TextEncoder, TextDecoder,
    setTimeout: () => 0, clearTimeout: () => {},
});
runInContext(readFileSync(SOURCE, "utf8"), context, { filename: SOURCE });
const Remote = window.PlexoraWebRemote;

async function get(path) {
    return Remote.answer(new Request(`https://viewer.example/__plexora/relay/${ROOM}${path}`));
}

async function main() {
    const cells = new Float32Array([1.5, 2.25, 3, 4096]);
    const plain = new Uint8Array(cells.buffer);

    reply = { status: 200, body: new Uint8Array(gzipSync(plain)),
              headers: [["Content-Type", "application/octet-stream"], ["Content-Encoding", "gzip"],
                        ["Content-Length", "99"]] };
    let response = await get("/node/v1/table/t1/all_cells?columns=CD3&dtype=float");
    let bytes = new Uint8Array(await response.arrayBuffer());
    check("the request reached the node at its own path",
          asked.at(-1)?.path === "/node/v1/table/t1/all_cells?columns=CD3&dtype=float");
    check("a gzipped answer comes out as the node's plain bytes",
          response.status === 200 && Buffer.from(bytes).equals(Buffer.from(plain)));
    check("...without the encoding it no longer has",
          response.headers.get("Content-Encoding") === null
          && response.headers.get("Content-Type") === "application/octet-stream");

    reply = { status: 200, body: new Uint8Array(deflateSync(plain)), headers: [["content-encoding", "deflate"]] };
    bytes = new Uint8Array(await (await get("/x")).arrayBuffer());
    check("deflate likewise", Buffer.from(bytes).equals(Buffer.from(plain)));

    reply = { status: 200, body: plain, headers: [["Content-Type", "application/octet-stream"]] };
    bytes = new Uint8Array(await (await get("/x")).arrayBuffer());
    check("an answer that was never encoded is passed as it is", Buffer.from(bytes).equals(Buffer.from(plain)));

    reply = { status: 304, body: new Uint8Array(0), headers: [["Content-Encoding", "gzip"], ["ETag", "\"t\""]] };
    response = await get("/x");
    check("a 304 still has no body", response.status === 304 && response.body === null);

    console.log(failures.length ? `\n${failures.length} failed` : "\nall relay checks passed");
    if (failures.length) process.exitCode = 1;
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
