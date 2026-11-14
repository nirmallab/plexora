/**
 * A relayed request its caller gave up on (services/webRemote.js `relay`,
 * services/relayRoom.js `Room.fetch`) is cancelled at the node and forgotten
 * by the room.
 *
 * The website's Python times out a node call by aborting its fetch
 * (plexora/browser/fetch.py `_abortable`); the service worker passes the abort
 * on (workers/backendRelay.js) and services/backend.js hands PlexoraWebRemote
 * a Request whose signal aborts. `relay` never read that signal and
 * `Room.fetch` took none, so the request stayed in the room's pending map:
 * the node kept working on it (the wire's CANCEL was never sent), and every
 * node HELLO or reconnect sent it again.
 *
 * Here the real webRemote.js and relayRoom.js run together against a fake
 * WebSocket (the probe plays the node) and the real WebCrypto.
 *
 * Run directly:  node tests/js/web_remote_relay_abort_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { webcrypto } from "node:crypto";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const ROOM_SOURCE = join(REPO, "plexora/client/src/js/services/relayRoom.js");
const REMOTE_SOURCE = join(REPO, "plexora/client/src/js/services/webRemote.js");
const ROOM = "RoomRoomRoomRoomRoom1";

const failures = [];
function check(name, ok, detail = "") {
    console.log(`${ok ? "ok  " : "FAIL"} ${name}${ok || !detail ? "" : `  (${detail})`}`);
    if (!ok) failures.push(name);
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
async function ticks(count) { for (let i = 0; i < count; i += 1) await tick(); }

// -- a relay socket the probe plays the node on ---------------------------------

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

// Only the short timers run: the room's reconnect waits and the page's
// "no longer running" checks (seconds to minutes) never fire in this probe.
const shortTimeout = (fn, ms) => (ms > 5000 ? 0 : setTimeout(fn, ms));

const keyText = Buffer.from(webcrypto.getRandomValues(new Uint8Array(32))).toString("base64url");
const stored = new Map([["plexora.webRemote.rooms",
    JSON.stringify({ [ROOM]: { name: "gpu", auth: "a", key: keyText, relay: "wss://relay.example" } })]]);
const window = {
    location: { href: "https://viewer.example/" },
    localStorage: { getItem: (k) => stored.get(k) ?? null, setItem: (k, v) => stored.set(k, v),
                    removeItem: (k) => stored.delete(k) },
    crypto: { subtle: webcrypto.subtle, getRandomValues: (array) => webcrypto.getRandomValues(array) },
    WebSocket: FakeWebSocket, TextEncoder, TextDecoder, atob, btoa,
    setTimeout: shortTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {},
    dispatchEvent: () => true, CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init && init.detail; } },
};
const context = createContext({
    window, ...window, console, URL, Headers, Response, Request, AbortController, DecompressionStream,
    Uint8Array, DataView, Promise, Date, Math,
});
runInContext(readFileSync(ROOM_SOURCE, "utf8"), context, { filename: ROOM_SOURCE });
runInContext(readFileSync(REMOTE_SOURCE, "utf8"), context, { filename: REMOTE_SOURCE });
const Relay = window.PlexoraRelayRoom;
const Remote = window.PlexoraWebRemote;

async function sealedFromNode(key, kind, stream, header, body) {
    const frame = await Relay.seal(key, ROOM, "n2p", Relay.encode(kind, stream, header, body));
    return frame.buffer.slice(frame.byteOffset, frame.byteOffset + frame.byteLength);
}

/** Every frame the page has sent, decrypted. */
async function sentFrames(key) {
    const out = [];
    for (const frame of socket.sent) {
        if (typeof frame === "string") continue;
        out.push(Relay.decode(await Relay.open(key, ROOM, "p2n", frame)));
    }
    return out;
}

async function main() {
    const key = await webcrypto.subtle.importKey("raw", Relay.b64urlToBytes(keyText), "AES-GCM",
                                                 false, ["encrypt", "decrypt"]);

    // The website's Python asks the node for something, with a timeout.
    const caller = new AbortController();
    const request = new Request(`https://viewer.example/__plexora/relay/${ROOM}/node/v1/resources/status`,
                                { signal: caller.signal });
    let settled = null;
    Remote.answer(request).then((value) => { settled = { value }; }, (error) => { settled = { error }; });
    await ticks(3);
    check("the page dialled its room", socket !== null);

    // The node is in the room and says hello: the request goes out.
    socket.onmessage({ data: await sealedFromNode(key, Relay.KIND.HELLO, 0, { node_id: "n", hostname: "gpu" }) });
    await ticks(10);
    let frames = await sentFrames(key);
    const requests = frames.filter((frame) => frame.kind === Relay.KIND.REQUEST);
    check("the request reached the node", requests.length === 1 && requests[0].header.p === "/node/v1/resources/status",
          `${requests.length} REQUEST frames`);
    const stream = requests.length ? requests[0].stream : -1;

    // Python's timeout runs out: its fetch is aborted.
    caller.abort();
    await ticks(10);
    frames = await sentFrames(key);
    const cancels = frames.filter((frame) => frame.kind === Relay.KIND.CANCEL && frame.stream === stream);
    check("the node is told to stop working on the abandoned request (a CANCEL for its stream)",
          cancels.length === 1, `${cancels.length} CANCEL frames for stream ${stream}`);
    check("the caller's wait ends when it aborts", settled !== null, "the answer is still pending");

    // The node rejoins (a restart, a replaced socket): what is still pending
    // is sent again. The abandoned request must not be among it.
    socket.onmessage({ data: await sealedFromNode(key, Relay.KIND.HELLO, 0, { node_id: "n", hostname: "gpu" }) });
    await ticks(10);
    frames = await sentFrames(key);
    const again = frames.filter((frame) => frame.kind === Relay.KIND.REQUEST && frame.stream === stream);
    check("the abandoned request is not sent again after the node's next hello",
          again.length === 1, `sent ${again.length} times`);

    console.log(failures.length ? `\n${failures.length} failed` : "\nall relay abort checks passed");
    process.exit(failures.length ? 1 : 0);
}

main().catch((error) => { console.error(error); process.exit(1); });
