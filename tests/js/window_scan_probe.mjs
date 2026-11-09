/**
 * The page's window scans (services/browserNode.js `windowOf`): one line of
 * them, a round of windows at a time, an urgent channel ahead of the
 * warm-up's between two rounds without losing what was read, each answer
 * kept, and a look (`wait`) that answers within its time whether or not the
 * scan is done. Run in Node over the real module, its workers stood in for:
 *
 *   node tests/js/window_scan_probe.mjs
 *
 * Prints JSON; tests/test_page_window_warm.py holds it to the expectations.
 */

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = readFileSync(path.join(REPO, "plexora", "client", "src", "js", "services", "browserNode.js"), "utf8");

const WIDTH = 4096 * 4;
const HEIGHT = 4096 * 2;
const ROUND_MS = 20;
const log = [];

// A channel's largest sample: its index times 1000, plus the window's corner.
const sample = (channel, [, , x1, y1]) => channel * 1000 + (x1 + y1) / 4096;

class Worker {
    constructor() {
        this.onmessage = null;
        Worker.alive += 1;
    }

    postMessage(message) {
        setTimeout(() => {
            let result;
            if (message.op === "open") {
                result = { geometry: { compression: 1, numChannels: 3, bitsPerSample: 16,
                                       levels: [{ width: WIDTH, height: HEIGHT }],
                                       fileTile: { tiled: true, width: 512, height: 512 } },
                           seed: null, timing: {} };
            } else if (message.op === "max") {
                log.push({ channel: message.channel, windows: message.windows.length });
                result = { max: Math.max(...message.windows.map((w) => sample(message.channel, w))) };
            }
            this.onmessage && this.onmessage({ data: { id: message.id, ok: true, result } });
        }, ROUND_MS);
    }

    terminate() {
        Worker.alive -= 1;
    }
}
Worker.alive = 0;

const file = { size: 123, lastModified: 456 };
const window = {
    navigator: { hardwareConcurrency: 4 },
    PlexoraPicks: { fileFor: (p) => (p === "/picked/a/i.tif" ? file : null) },
};
const context = vm.createContext({ window, Worker, setTimeout, clearTimeout, console, performance,
                                   URL, Uint8Array, Uint32Array, DataView, TextEncoder, Map, Set,
                                   Promise, Math, Number, String, Object, Array, Error, JSON });
vm.runInContext(SOURCE, context);
const node = window.PlexoraNode;

const ask = (channel, extra = {}) => node.windowOf({ path: "/picked/a/i.tif", channel,
                                                      shape: [3, HEIGHT, WIDTH], dtype: "uint16", ...extra });
const out = {};

// A look at a scan just begun answers within its wait, not done; the channel
// it names `ahead` is queued behind it.
const t0 = Date.now();
out.firstLook = (await ask(0, { wait: 0, urgent: false, ahead: [1] })).value;
out.firstLookMs = Date.now() - t0;
// An urgent wait for channel 2 arrives while channel 0 is being read: it goes
// next, after the round in progress, ahead of channel 1.
await new Promise((resolve) => setTimeout(resolve, ROUND_MS + 5));
out.urgent = (await ask(2)).value;
out.orderAfterUrgent = [...new Set(log.map((entry) => entry.channel))];
// The interrupted scan finishes with everything it had read.
let look;
do {
    look = (await ask(0, { wait: 50, urgent: false })).value;
} while (!look.done);
out.resumed = look;
out.windowsRead = log.filter((entry) => entry.channel === 0).reduce((n, entry) => n + entry.windows, 0);
out.windowsInPlane = (WIDTH / 4096) * (HEIGHT / 4096);
// Kept: asking again reads nothing.
const before = log.length;
out.again = (await ask(2)).value;
out.readAgain = log.length - before;
// A file the page does not hold.
out.notHeld = await node.windowOf({ path: "/picked/b/x.tif", channel: 0, shape: [3, HEIGHT, WIDTH], dtype: "uint16" })
    .then(() => "answered", (error) => String(error.message));
// Wait for channel 1 (queued `ahead`) to finish, then the pool is let go.
do {
    look = (await ask(1, { wait: 50, urgent: false })).value;
} while (!look.done);
await new Promise((resolve) => setTimeout(resolve, 10));
out.workersLeft = Worker.alive;
// Which channel each worker's share of each round was for, in order.
out.rounds = log.map((entry) => entry.channel);
console.log(JSON.stringify(out));
