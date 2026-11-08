/**
 * PlexoraRelayRoom -- the page's end of a data node's room on the relay
 * (docs/internal/WEB_REMOTE_RELAY.md; plexora/relay_wire.py is the reference
 * for every byte here, tests/fixtures/relay_wire_vectors.json the check).
 *
 * A node started from the website dials out to its room; the page dials the
 * same room, and every request the website makes of the node -- a tile, a
 * table, a directory listing -- travels as an encrypted frame between the
 * two. The relay forwards bytes it cannot read: each frame is AES-256-GCM
 * under a key this page made and handed to the node over SSH.
 *
 * **Nothing here ends a session.** The relay may drop a socket (a Cloudflare
 * restart, a network blip, a forced update): the page reconnects on its own,
 * with the same room password, and every request that had not finished is
 * sent again under its own stream id -- the node answers a repeated GET by
 * running it again and a repeated write from the copy it kept, so nothing is
 * done twice. A caller sees a slower answer, never a lost one.
 *
 *   join({relay, room, auth, key, licence, log}) -> Room
 *   Room.fetch({method, path, headers, body}) -> Promise<{status, headers, body}>
 *   Room.stop()           ask the node to shut down (its job ends)
 *   Room.close()          leave the room; the node keeps running
 *   Room.state()          {socket, node, hello}
 *   Room.on(fn)           fn(state) on every change
 *
 * Classic script, `window.PlexoraRelayRoom`; loaded in the site's host page
 * (services/shell.js), so a room outlives the app frame's page changes.
 */
(function (global) {
    "use strict";

    const PROTOCOL = "plexora-relay.v1";
    const KIND = Object.freeze({ HELLO: 1, REQUEST: 2, REQUEST_BODY: 3, RESPONSE_HEAD: 4,
                                 RESPONSE_BODY: 5, CANCEL: 6, STOP: 7 });
    const PID_BYTES = 8;
    const NONCE_BYTES = 12;
    const MAX_CHUNK = 1024 * 1024;
    const PING_MS = 30000;
    const DEAD_MS = 75000;
    //: A request that has heard nothing for this long fails. Generous: the
    //: wire has no keepalive, and a node may think a long while before its
    //: answer's first byte (a mask conversion).
    const QUIET_MS = 600000;
    //: Close codes after which reconnecting cannot help.
    const FINAL = { 4401: "The room refused this page's password.",
                    4403: "The relay refused this connection.",
                    4404: "The relay has no such room.",
                    4429: "Too many pages are connected to this server." };

    const encoder = new TextEncoder();
    const decoder = new TextDecoder();

    function b64urlToBytes(text) {
        const padded = String(text).replace(/-/g, "+").replace(/_/g, "/");
        const binary = atob(padded + "===".slice((padded.length + 3) % 4));
        const out = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i += 1) out[i] = binary.charCodeAt(i);
        return out;
    }

    function bytesToB64url(bytes) {
        let binary = "";
        for (const byte of bytes) binary += String.fromCharCode(byte);
        return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    }

    function random(count) {
        return global.crypto.getRandomValues(new Uint8Array(count));
    }

    /** `kind ‖ stream(u32 BE) ‖ hlen(u32 BE) ‖ header JSON ‖ body`. */
    function encode(kind, stream, header, body) {
        const head = encoder.encode(JSON.stringify(header || {}));
        const tail = body ? new Uint8Array(body) : new Uint8Array(0);
        const out = new Uint8Array(9 + head.length + tail.length);
        const view = new DataView(out.buffer);
        view.setUint8(0, kind);
        view.setUint32(1, stream >>> 0);
        view.setUint32(5, head.length);
        out.set(head, 9);
        out.set(tail, 9 + head.length);
        return out;
    }

    function decode(bytes) {
        const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
        if (bytes.byteLength < 9) throw new Error("short frame");
        const kind = view.getUint8(0);
        const stream = view.getUint32(1);
        const length = view.getUint32(5);
        if (9 + length > bytes.byteLength) throw new Error("bad header length");
        const header = JSON.parse(decoder.decode(bytes.subarray(9, 9 + length)) || "{}");
        return { kind, stream, header, body: bytes.subarray(9 + length) };
    }

    function aad(room, direction) {
        return encoder.encode(`${PROTOCOL}|${room}|${direction}`);
    }

    async function seal(key, room, direction, plaintext, nonce = random(NONCE_BYTES)) {
        const sealed = new Uint8Array(await global.crypto.subtle.encrypt(
            { name: "AES-GCM", iv: nonce, additionalData: aad(room, direction) }, key, plaintext));
        const out = new Uint8Array(NONCE_BYTES + sealed.length);
        out.set(nonce, 0);
        out.set(sealed, NONCE_BYTES);
        return out;
    }

    async function open(key, room, direction, sealed) {
        const nonce = sealed.subarray(0, NONCE_BYTES);
        const plain = await global.crypto.subtle.decrypt(
            { name: "AES-GCM", iv: nonce, additionalData: aad(room, direction) }, key,
            sealed.subarray(NONCE_BYTES));
        return new Uint8Array(plain);
    }

    function importKey(text) {
        return global.crypto.subtle.importKey("raw", b64urlToBytes(text), "AES-GCM", false,
                                              ["encrypt", "decrypt"]);
    }

    function join({ relay, room, auth, key, licence = null, log = () => {} }) {
        const keyPromise = importKey(key);
        //: This page's name for itself to the node: which of its requests a
        //: repeated stream id means, across this page's reconnects.
        const client = bytesToB64url(random(12));
        let nextStream = (new DataView(random(4).buffer).getUint32(0) % 0x7fffffff) + 1;
        const pending = new Map();
        const listeners = new Set();
        let socket = null;
        let state = { socket: "connecting", node: false, hello: null, error: null };
        let closed = false;
        let attempt = 0;
        let lastPong = Date.now();
        let pinger = null;
        let retryTimer = null;
        // Frames go out one at a time, in order: encryption is asynchronous.
        let sending = Promise.resolve();

        function set(change) {
            state = { ...state, ...change };
            for (const listener of listeners) {
                try { listener(state); } catch (error) { /* a listener's own trouble */ }
            }
        }

        function send(kind, stream, header, body) {
            const work = sending.then(async () => {
                if (!socket || socket.readyState !== 1) throw new Error("not connected");
                const frame = await seal(await keyPromise, room, "p2n", encode(kind, stream, header, body));
                socket.send(frame);
            });
            sending = work.catch(() => null);
            return work;
        }

        function sendRequest(entry) {
            if (!state.node || !socket || socket.readyState !== 1) return;
            entry.status = null;
            entry.headers = null;
            entry.chunks = [];
            entry.sent = true;
            entry.heard = Date.now();
            const body = entry.body || new Uint8Array(0);
            const first = body.subarray(0, MAX_CHUNK);
            send(KIND.REQUEST, entry.stream, { m: entry.method, p: entry.path, h: entry.headerList,
                                               client, end: body.length <= MAX_CHUNK }, first)
                .catch(() => { entry.sent = false; });
            for (let offset = MAX_CHUNK; offset < body.length; offset += MAX_CHUNK) {
                send(KIND.REQUEST_BODY, entry.stream, { end: offset + MAX_CHUNK >= body.length },
                     body.subarray(offset, offset + MAX_CHUNK)).catch(() => { entry.sent = false; });
            }
        }

        //: Every unfinished request, cleared of what it had heard, waits to be
        //: sent again: the socket or the node went away mid-answer.
        function rewind() {
            for (const entry of pending.values()) {
                entry.sent = false;
                entry.chunks = [];
                entry.status = null;
            }
        }

        function resend() {
            for (const entry of pending.values()) if (!entry.sent) sendRequest(entry);
        }

        async function onBinary(data) {
            let frame;
            try {
                frame = decode(await open(await keyPromise, room, "n2p", new Uint8Array(data)));
            } catch (error) {
                log(`relay: a frame did not decrypt (${error.message || error})`);
                return;
            }
            if (frame.kind === KIND.HELLO) {
                // A node's hello is the node (re)joining or answering this
                // page's own: whatever it had in hand from before may be gone
                // with its old socket, which the relay replaces without the
                // page ever hearing `node: false`. Everything unfinished is
                // sent again; the node runs a repeated GET again and answers a
                // repeated write from the copy it kept.
                set({ node: true, hello: frame.header });
                rewind();
                resend();
                return;
            }
            const entry = pending.get(frame.stream);
            if (!entry) return;
            entry.heard = Date.now();
            if (frame.kind === KIND.RESPONSE_HEAD) {
                entry.status = frame.header.s;
                entry.headers = frame.header.h || [];
                entry.chunks = [];
            } else if (frame.kind === KIND.RESPONSE_BODY) {
                if (frame.body.length) entry.chunks.push(new Uint8Array(frame.body));
                if (frame.header.end) {
                    pending.delete(frame.stream);
                    if (frame.header.error && entry.status === null) {
                        entry.reject(new Error(frame.header.error));
                        return;
                    }
                    const total = entry.chunks.reduce((sum, chunk) => sum + chunk.length, 0);
                    const body = new Uint8Array(total);
                    let at = 0;
                    for (const chunk of entry.chunks) {
                        body.set(chunk, at);
                        at += chunk.length;
                    }
                    entry.resolve({ status: entry.status ?? 502, headers: entry.headers || [], body });
                }
            }
        }

        function onText(text) {
            if (text === "pong") {
                lastPong = Date.now();
                return;
            }
            let message = null;
            try { message = JSON.parse(text); } catch (error) { return; }
            if (message && message.t === "presence") {
                const node = Boolean(message.node);
                if (!node && state.node) rewind();
                set({ node: node && Boolean(state.hello) ? true : node });
                if (node) resend();
            }
        }

        function connect() {
            if (closed) return;
            set({ socket: "connecting" });
            const protocols = [PROTOCOL, `a.${auth}`];
            if (licence) protocols.push(`l.${licence}`);
            let ws;
            try {
                ws = new WebSocket(`${relay.replace(/\/+$/, "")}/v1/rooms/${room}?role=page`, protocols);
            } catch (error) {
                retry();
                return;
            }
            ws.binaryType = "arraybuffer";
            socket = ws;
            ws.onopen = () => {
                attempt = 0;
                lastPong = Date.now();
                set({ socket: "open", error: null });
                send(KIND.HELLO, 0, { client, v: 1 }).catch(() => null);
                clearInterval(pinger);
                pinger = setInterval(() => {
                    if (Date.now() - lastPong > DEAD_MS) {
                        log("relay: no answer to pings; reconnecting");
                        try { ws.close(4000, "dead"); } catch (error) { /* already */ }
                        return;
                    }
                    try { ws.send("ping"); } catch (error) { /* closing */ }
                    // A request nobody has answered for QUIET_MS fails.
                    const now = Date.now();
                    for (const [stream, entry] of pending) {
                        if (entry.sent && now - entry.heard > QUIET_MS) {
                            pending.delete(stream);
                            send(KIND.CANCEL, stream, {}).catch(() => null);
                            entry.reject(new Error("The server did not answer in time."));
                        }
                    }
                }, PING_MS);
            };
            ws.onmessage = (event) => {
                if (typeof event.data === "string") onText(event.data);
                else onBinary(event.data);
            };
            ws.onclose = (event) => {
                if (socket === ws) socket = null;
                clearInterval(pinger);
                rewind();
                set({ socket: "closed", node: false });
                if (FINAL[event.code]) {
                    const error = new Error(event.reason || FINAL[event.code]);
                    set({ error: error.message });
                    closed = true;
                    for (const entry of pending.values()) entry.reject(error);
                    pending.clear();
                    return;
                }
                retry();
            };
        }

        function retry() {
            if (closed) return;
            const wait = Math.min(30000, 1000 * 2 ** Math.min(attempt, 5)) * (0.75 + Math.random() * 0.5);
            attempt += 1;
            clearTimeout(retryTimer);
            retryTimer = setTimeout(connect, wait);
        }

        connect();

        return Object.freeze({
            room,
            state: () => state,
            on(listener) {
                listeners.add(listener);
                return () => listeners.delete(listener);
            },
            fetch({ method = "GET", path, headers = [], body = null }) {
                if (closed) return Promise.reject(new Error(state.error || "This connection has ended."));
                return new Promise((resolve, reject) => {
                    const stream = nextStream;
                    nextStream = nextStream >= 0xfffffffe ? 1 : nextStream + 1;
                    const entry = { stream, method: String(method).toUpperCase(), path,
                                    headerList: headers, body: body ? new Uint8Array(body) : null,
                                    resolve, reject, sent: false, chunks: [], status: null,
                                    headers: null, heard: Date.now() };
                    pending.set(stream, entry);
                    sendRequest(entry);
                });
            },
            async stop() {
                await send(KIND.STOP, 0, {});
            },
            close() {
                closed = true;
                clearTimeout(retryTimer);
                clearInterval(pinger);
                if (socket) {
                    try { socket.close(1000, "left"); } catch (error) { /* already */ }
                }
                for (const entry of pending.values()) entry.reject(new Error("This connection was closed."));
                pending.clear();
            },
        });
    }

    function newSecret(bytes = 32) {
        return bytesToB64url(random(bytes));
    }

    global.PlexoraRelayRoom = Object.freeze({ join, newSecret, encode, decode, seal, open, importKey,
                                              b64urlToBytes, bytesToB64url, KIND, PROTOCOL });
})(typeof window !== "undefined" ? window : globalThis);
