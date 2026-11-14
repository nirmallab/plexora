/**
 * The page's end of a relay room (services/relayRoom.js): a node's answer
 * that spans several frames comes out whole and in the node's order, however
 * the browser's decryptions finish.
 *
 * Each incoming frame is AES-GCM decrypted with `crypto.subtle.decrypt`,
 * which runs "in parallel" and promises nothing about which of two calls
 * finishes first (Firefox's thread pool and Node's both finish a 10-byte
 * frame before the 1 MiB frame ahead of it). The node sends an answer as a
 * RESPONSE_HEAD and then RESPONSE_BODY frames of up to 1 MiB, the last one
 * marked `end`. Handled in the order their decryptions finished, the small
 * end frame closed the answer before the 1 MiB chunk ahead of it was in: the
 * caller got a short body with status 200, and two full chunks could swap.
 *
 * Here the real relayRoom.js runs against a fake WebSocket and the real
 * WebCrypto, except that each decrypt's result is held until the probe lets
 * it go, in an order the probe picks -- so the interleaving is the same on
 * every run.
 *
 * Run directly:  node tests/js/relay_room_order_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { webcrypto } from "node:crypto";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/services/relayRoom.js");
const ROOM = "RoomRoomRoomRoomRoom1";
const MIB = 1024 * 1024;

const failures = [];
function check(name, ok, detail = "") {
    console.log(`${ok ? "ok  " : "FAIL"} ${name}${ok || !detail ? "" : `  (${detail})`}`);
    if (!ok) failures.push(name);
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

// -- WebCrypto whose decrypt results the probe releases ------------------------

let holding = false;
let held = [];
const subtle = {
    importKey: (...args) => webcrypto.subtle.importKey(...args),
    encrypt: (...args) => webcrypto.subtle.encrypt(...args),
    decrypt(...args) {
        const real = webcrypto.subtle.decrypt(...args);
        if (!holding) return real;
        let release;
        const gate = new Promise((resolve) => { release = resolve; });
        held.push({ release: () => release(real) });
        return gate;
    },
};
const crypto = { subtle, getRandomValues: (array) => webcrypto.getRandomValues(array) };

/** Let `count` decrypts finish, preferring the order `order` (frame indices
 * in arrival order): of the decrypts in flight, the one earliest in `order`
 * finishes next, and is handled completely before the next one finishes. */
async function finishIn(order, count) {
    const done = new Set();
    for (let idle = 0; done.size < count && idle < 50;) {
        const next = order.find((index) => index < held.length && !done.has(index));
        if (next === undefined) {
            idle += 1;
            await tick();
            continue;
        }
        done.add(next);
        held[next].release();
        await tick();
    }
    held = [];
    if (done.size !== count) throw new Error(`only ${done.size} of ${count} frames reached a decrypt`);
}

// -- a relay socket the probe plays the node on -----------------------------------

let socket = null;
class FakeWebSocket {
    constructor(url, protocols) {
        this.url = url;
        this.protocols = protocols;
        this.readyState = 1;
        this.sent = [];
        socket = this;
        setTimeout(() => this.onopen && this.onopen(), 0);
    }
    send(frame) { this.sent.push(frame); }
    close() { this.readyState = 3; }
}

const window = {
    crypto, WebSocket: FakeWebSocket, TextEncoder, TextDecoder, atob, btoa,
    setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {},
};
const context = createContext({ window, ...window, console, Uint8Array, DataView, Promise, Date, Math });
runInContext(readFileSync(SOURCE, "utf8"), context, { filename: SOURCE });
const Relay = window.PlexoraRelayRoom;

async function sealed(key, kind, stream, header, body) {
    const frame = await Relay.seal(key, ROOM, "n2p", Relay.encode(kind, stream, header, body));
    return frame.buffer.slice(frame.byteOffset, frame.byteOffset + frame.byteLength);
}

function filled(length, value) {
    return new Uint8Array(length).fill(value);
}

function joined(chunks) {
    const out = new Uint8Array(chunks.reduce((sum, chunk) => sum + chunk.length, 0));
    let at = 0;
    for (const chunk of chunks) { out.set(chunk, at); at += chunk.length; }
    return out;
}

function sameBytes(a, b) {
    return a.length === b.length && Buffer.from(a).equals(Buffer.from(b));
}

function describe(body, expected) {
    if (body.length !== expected.length) return `${body.length} bytes, expected ${expected.length}`;
    const firsts = [];
    for (let at = 0; at < body.length; at += MIB) firsts.push(body[at]);
    return `chunks start ${firsts.join(",")}`;
}

async function main() {
    const keyText = Relay.newSecret(32);
    const key = await webcrypto.subtle.importKey("raw", Relay.b64urlToBytes(keyText), "AES-GCM",
                                                 false, ["encrypt", "decrypt"]);
    const room = Relay.join({ relay: "wss://relay.example", room: ROOM, auth: "a", key: keyText });
    await tick();
    socket.onmessage({ data: '{"t":"presence","node":true}' });
    socket.onmessage({ data: await sealed(key, Relay.KIND.HELLO, 0, { node_id: "n" }) });
    await tick();
    await tick();
    check("the page is in the room with its node", room.state().node === true);

    /** One request; the node answers with `frames` (HEAD first) in one burst,
     * and their decryptions finish in `order`. */
    async function answer(frames, order) {
        const pending = room.fetch({ method: "GET", path: "/node/v1/table/t1/all_cells" });
        await tick();
        await tick();
        const request = Relay.decode(await Relay.open(key, ROOM, "p2n", socket.sent.at(-1)));
        const stream = request.stream;
        const data = [];
        for (const [kind, header, body] of frames) data.push(await sealed(key, kind, stream, header, body));
        holding = true;
        for (const frame of data) socket.onmessage({ data: frame });   // one burst, as the socket delivers
        await tick();   // the handlers reach their decrypts
        await finishIn(order, frames.length);
        holding = false;
        let settled = null;
        pending.then((value) => { settled = value; }, (error) => { settled = { error }; });
        for (let i = 0; i < 5 && settled === null; i += 1) await tick();
        return settled;
    }

    const HEAD = [Relay.KIND.RESPONSE_HEAD, { s: 200, h: [["Content-Type", "application/octet-stream"]] }, null];
    const body = (header, bytes) => [Relay.KIND.RESPONSE_BODY, header, bytes];

    // Control: decryptions finishing in arrival order.
    let big = filled(MIB, 7);
    let tail = filled(10, 9);
    let expected = joined([big, tail]);
    let result = await answer([HEAD, body({ end: false }, big), body({ end: true }, tail)], [0, 1, 2]);
    check("an answer whose frames decrypt in order comes back whole",
          result && result.status === 200 && sameBytes(result.body, expected),
          result && result.body ? describe(result.body, expected) : String(result && result.error));

    // A 1 MiB chunk then a small end frame: the small one finishes first.
    big = filled(MIB, 3);
    tail = filled(10, 4);
    expected = joined([big, tail]);
    result = await answer([HEAD, body({ end: false }, big), body({ end: true }, tail)], [0, 2, 1]);
    check("a 1 MiB chunk is not lost when the small end frame behind it decrypts first",
          result && result.status === 200 && sameBytes(result.body, expected),
          result && result.body ? describe(result.body, expected) : String(result && result.error));

    // Three full chunks; the second finishes before the first.
    const chunks = [filled(MIB, 1), filled(MIB, 2), filled(MIB, 3)];
    expected = joined(chunks);
    result = await answer([HEAD, body({ end: false }, chunks[0]), body({ end: false }, chunks[1]),
                           body({ end: true }, chunks[2])], [0, 2, 1, 3]);
    check("full chunks come back in the node's order when their decryptions finish out of order",
          result && result.status === 200 && sameBytes(result.body, expected),
          result && result.body ? describe(result.body, expected) : String(result && result.error));

    // The head finishes after the first body chunk.
    big = filled(MIB, 5);
    tail = filled(10, 6);
    expected = joined([big, tail]);
    result = await answer([HEAD, body({ end: false }, big), body({ end: true }, tail)], [1, 0, 2]);
    check("a chunk is not wiped by its answer's head decrypting after it",
          result && result.status === 200 && sameBytes(result.body, expected),
          result && result.body ? describe(result.body, expected) : String(result && result.error));

    room.close();
    console.log(failures.length ? `\n${failures.length} failed` : "\nall relay order checks passed");
    if (failures.length) process.exitCode = 1;
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
