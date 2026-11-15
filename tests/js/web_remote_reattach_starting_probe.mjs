/**
 * A tab reloaded while its data node is still starting (a queued job, an
 * install: services/webRemote.js `run`). The node's start command goes on
 * without the page and joins its room when it is up, which can be many
 * minutes later. The reopened tab (`reattach`) waited 60 s for its hello and
 * then forgot the room's keys: the node joined later with nobody listening
 * and held its allocation until its idle limit. A room whose start is still
 * within its plan's timeout is waited for until then; one already connected
 * keeps the short wait.
 *
 * Runs the real webRemote.js with the relay room stood in for and every
 * timer recorded, not run. Prints JSON; tests/test_web_remote_reattach_starting.py
 * asserts on it.
 *
 *   node tests/js/web_remote_reattach_starting_probe.mjs
 */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/services/webRemote.js");
const NOW = 1_800_000_000_000;

function load(rooms) {
    const stored = new Map([["plexora.webRemote.rooms", JSON.stringify(rooms)]]);
    const timers = [];
    const listeners = [];
    const room = { state: () => ({ socket: "open", node: false }), on: (fn) => listeners.push(fn), close() {} };
    const window = {
        location: { href: "https://viewer.example/" },
        localStorage: { getItem: (k) => stored.get(k) ?? null, setItem: (k, v) => stored.set(k, v),
                        removeItem: (k) => stored.delete(k) },
        PlexoraRelayRoom: { join: () => room, newSecret: () => "s".repeat(12) },
        setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, clearTimeout() {},
        setInterval: () => 0, clearInterval() {},
        dispatchEvent: () => true, CustomEvent: class {},
    };
    const context = createContext({
        window, ...window, console, URL, Headers, Response, Request, AbortController,
        Uint8Array, Promise, Math, Date: { now: () => NOW },
    });
    runInContext(readFileSync(SOURCE, "utf8"), context, { filename: SOURCE });
    window.PlexoraWebRemote.configure({});
    return { stored, timers, listeners };
}

const out = {};
// Started 2 minutes ago with a 30-minute start timeout: still starting.
{
    const { stored, timers } = load({ R1: { name: "o2", auth: "a", key: "k", relay: "wss://r", at: NOW - 120000,
                                            until: NOW + 28 * 60000 } });
    const waits = timers.map((t) => t.ms);
    out.startingWait = Math.max(...waits, 0);
    for (const t of timers.filter((one) => one.ms <= 60000)) t.fn();
    out.startingKeptAfter60s = Object.keys(JSON.parse(stored.get("plexora.webRemote.rooms"))).includes("R1");
}
// A room whose node had connected (no start deadline): the short wait.
{
    const { timers } = load({ R2: { name: "o2", auth: "a", key: "k", relay: "wss://r", at: NOW - 3600000 } });
    out.connectedWait = Math.max(...timers.map((t) => t.ms), 0);
}
// The node says hello: the record loses its start deadline.
{
    const { stored, listeners } = load({ R3: { name: "o2", auth: "a", key: "k", relay: "wss://r", at: NOW,
                                               until: NOW + 600000 } });
    for (const fn of listeners) fn({ socket: "open", node: true, hello: { hostname: "compute-a-1" } });
    out.untilAfterHello = JSON.parse(stored.get("plexora.webRemote.rooms")).R3?.until ?? null;
}
console.log(JSON.stringify(out));
