/**
 * Stop connecting while the server's sign-in asks for a password or a Duo
 * answer (services/webRemote.js `stop`, `fail` -> `session.answer(null)`).
 * The question was answered with an empty string, sent to the server as the
 * password; the server asked again, and the new question put the stopped
 * connection back to "authenticating" -- whose name then refused a new
 * connection with 409. A question given up must give up the sign-in.
 *
 * Runs the real webRemote.js with the SSH library stood in for: a server that
 * asks for a password until it gets one, three times at most. Prints JSON;
 * tests/test_web_remote_stop_during_prompt.py asserts on it.
 *
 *   node tests/js/web_remote_stop_during_prompt_probe.mjs
 */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/services/webRemote.js");
const tick = () => new Promise((resolve) => setImmediate(resolve));
const settle = async (n = 30) => { for (let i = 0; i < n; i += 1) await tick(); };

const sent = [];
const PlexoraSshLib = {
    async open({ ask }) {
        for (let round = 0; round < 3; round += 1) {
            const answers = await ask({ name: "", instruction: "", prompts: [{ prompt: "Password:", echo: false }] });
            if (answers === null) throw new Error("The sign-in was cancelled.");
            sent.push(answers);
        }
        throw new Error("The server did not accept what was typed.");
    },
};
let n = 0;
const stored = new Map();
const window = {
    location: { href: "https://viewer.example/" },
    localStorage: { getItem: (k) => stored.get(k) ?? null, setItem: (k, v) => stored.set(k, v), removeItem() {} },
    sessionStorage: { getItem: (k) => stored.get(k) ?? null, setItem: (k, v) => stored.set(k, v), removeItem() {} },
    PlexoraRelayRoom: { newSecret: () => `id${(n += 1)}` },
    PlexoraSshLib,
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
    dispatchEvent: () => true, CustomEvent: class {},
};
const context = createContext({ window, ...window, console, URL, Headers, Response, Request, Promise, Math, Date });
runInContext(readFileSync(SOURCE, "utf8"), context, { filename: SOURCE });
const Remote = window.PlexoraWebRemote;
const API = "https://viewer.example/__plexora/remote/v1/sessions";
const call = async (method, url, body) => {
    const response = await Remote.answer(new Request(url, { method, ...(body ? { body: JSON.stringify(body) } : {}) }));
    return { status: response.status, body: await response.json() };
};

const plan = { kind: "files", host: "transfer.example", user: "me", timeout: 300 };
const started = await call("POST", API, { name: "o2-files", plan });
await settle();
const asking = await call("GET", `${API}/${started.body.id}`);
const stopped = await call("DELETE", `${API}/${started.body.id}`);
await settle();
const after = await call("GET", `${API}/${started.body.id}`);
const again = await call("POST", API, { name: "o2-files", plan });
console.log(JSON.stringify({
    askedBeforeStop: asking.body.state, stateAfterStop: after.body.state, promptAfterStop: after.body.prompt,
    sentToServer: sent, reconnectStatus: again.status, stoppedStatus: stopped.status,
}));
