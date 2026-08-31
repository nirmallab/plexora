/**
 * One owner of remote polling, and the properties that make that worth doing.
 *
 * Four surfaces watch the same three ssh processes. What this pins is the part
 * that fails quietly if it is wrong -- nothing throws, the page still draws,
 * and either the browser is making a request a second forever or a card sits
 * frozen on "Connecting…":
 *
 *   1. **No subscribers, no timer.** The globe is on every page including the
 *      viewer, so a module that polled on load would cost every user a request
 *      a second for the rest of the session.
 *   2. **Settled and only passively watched is also no timer.** A connection
 *      sits connected for hours; asking again changes nothing, because nothing
 *      can change it but an action, and whoever acts refreshes.
 *   3. **Something happening, or somebody looking, means poll.** Both halves
 *      matter: a queued job moves on its own, and an open dialog has to notice
 *      what another tab did.
 *   4. **One fetch pair per tick, however many subscribers.** Settings beside
 *      a modal is one round trip.
 *   5. **A throwing subscriber does not starve the others**, or one broken
 *      renderer freezes the connection modal next to it.
 *   6. **Both halves are merged per profile.** A viewer and a data node on one
 *      login are different things and every surface shows both.
 *   7. **The secret heuristic is one heuristic.** Masking a host-key
 *      fingerprint hides the thing the user is being asked to check.
 *
 * Run directly:  node tests/js/remote_state_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/services/remoteState.js");

// -- a server that answers whatever this test says --------------------------

const calls = [];
let remotes = [];
let places = [];
let statusReply = null;
let failNext = null;

function fetchStub(url, options = {}) {
    calls.push({ url, method: options.method || "GET", body: options.body });
    if (failNext) {
        const message = failNext;
        failNext = null;
        return Promise.resolve({
            ok: false, status: 500,
            json: () => Promise.resolve({ error: message }),
        });
    }
    let payload = {};
    if (url.indexOf("/status") >= 0) {
        payload = statusReply || { log: [] };
    } else if (url.indexOf("data_places") >= 0) {
        payload = { places, client_node: "laptop", server_is_remote: true };
    } else {
        payload = { remotes };
    }
    return Promise.resolve({
        ok: true, status: 200, json: () => Promise.resolve(payload),
    });
}

// -- a clock the test drives ------------------------------------------------

let now = 0;
const pending = new Map();
let nextTimer = 1;

function setTimeoutStub(fn, delay) {
    const id = nextTimer++;
    pending.set(id, { at: now + (delay || 0), fn });
    return id;
}

function clearTimeoutStub(id) {
    pending.delete(id);
}

/** Advance the clock and run whatever was due. */
function tickClock(ms) {
    now += ms;
    Array.from(pending.entries())
        .filter(([, t]) => t.at <= now)
        .sort((a, b) => a[1].at - b[1].at)
        .forEach(([id, t]) => {
            pending.delete(id);
            t.fn();
        });
}

function timersOutstanding() {
    return pending.size;
}

// -- load the shipped file --------------------------------------------------

//: Events the module dispatched at the page -- the announcement main.js
//: repairs tile routing on. Collected rather than ignored, because whether
//: one fires (and when one must not) is a property under test.
const dispatched = [];

const context = {
    console,
    setTimeout: setTimeoutStub,
    clearTimeout: clearTimeoutStub,
    fetch: fetchStub,
    plexoraUrl: (path) => `/${String(path).replace(/^\/+/, "")}`,
    encodeURIComponent,
    JSON,
    Promise,
    Map,
    Set,
    CustomEvent: class {
        constructor(type, options) {
            this.type = type;
            this.detail = (options || {}).detail;
        }
    },
    dispatchEvent: (event) => { dispatched.push(event); return true; },
};
// The clock a countdown counts against, and it has to be the same one
// `tickClock` drives: `remaining()` measures how long ago a snapshot arrived,
// so against the real wall clock it would be off by however long the test took
// to get there. Only `now()` is ever called.
context.Date = { now: () => now };
context.window = context;
createContext(context);
runInContext(readFileSync(SOURCE, "utf-8"), context);

const Remotes = context.window.PlexoraRemotes;

// -- checks -----------------------------------------------------------------

const failures = [];

function check(label, condition) {
    if (condition) {
        console.log(`ok  ${label}`);
    } else {
        failures.push(label);
        console.log(`FAIL ${label}`);
    }
}

/** Let every already-resolved promise run. */
const settle = () => new Promise((resolve) => {
    let left = 20;
    const step = () => (left-- > 0 ? Promise.resolve().then(step) : resolve());
    step();
});

function fetches(match) {
    return calls.filter((c) => c.url.indexOf(match) >= 0).length;
}

function profile(name, state, extra) {
    return Object.assign({ name, target: `me@${name}`, state, srun: null,
                           phase: "", error: null, prompt: null, url: null,
                           log: [], data_nodes: [], node_errors: [] }, extra);
}

function place(id, state, extra) {
    return Object.assign({ id, kind: "remote", label: id, detail: `me@${id}`,
                           state, node: null, phase: "", error: null,
                           prompt: null, queued: false }, extra);
}

async function main() {
    // -- 1. nothing subscribed, nothing polled --------------------------------
    remotes = [profile("hpc", "idle")];
    places = [place("hpc", "idle")];
    await settle();
    check("a module nobody subscribed to makes no request",
          calls.length === 0 && timersOutstanding() === 0);

    // -- 3/4. one round trip for two subscribers ------------------------------
    const seenA = [];
    const seenB = [];
    const stopA = Remotes.subscribe((s) => seenA.push(s), { active: true });
    const stopB = Remotes.subscribe((s) => seenB.push(s), { active: true });
    await settle();
    check("two subscribers cost one pair of fetches, not two",
          fetches("settings/remotes") === 1 && fetches("data_places") === 1);
    check("...and both are told", seenA.length >= 1 && seenB.length >= 1);

    // -- 6. the merge ---------------------------------------------------------
    remotes = [profile("hpc", "connected", { url: "http://127.0.0.1:9000/",
                                             srun: "-p interactive" }),
               profile("o2", "idle")];
    places = [place("hpc", "connected", { node: "hpc-data" }),
              place("o2", "idle", { registered_node: "o2-data" })];
    await Remotes.refresh();
    const entry = Remotes.entry("hpc");
    check("a profile's viewer and data node are two halves of one row",
          entry.viewer.state === "connected" && entry.node.node === "hpc-data");
    check("...and the node's own name is what the row carries",
          entry.node.node === "hpc-data" && entry.name === "hpc");
    check("...a profile that runs inside a job says so before you connect",
          entry.queued === true);
    // Two ways to know that name, and only one of them survives a restart: a
    // data node outlives the process that started it, so the session's copy is
    // empty for a machine that is up. A surface testing only for "is anything
    // there" can read either; one MATCHING a name has to have both, or it
    // compares an empty with an empty and calls that a match.
    check("...and a node only the registry knows about is still named",
          Remotes.entry("o2").node.node === null
          && Remotes.entry("o2").node.registered === "o2-data");

    // -- a node coming or going is announced to the page ----------------------
    //
    // The page holding the tile URLs is not a subscriber: main.js resolved its
    // routing once at boot. This event is what lets it repair itself when a
    // node reconnects on a new port instead of failing against the old one
    // until somebody thinks to reload.
    check("a node session coming up is dispatched as an event",
          dispatched.some((event) =>
              event.type === "plexora:remote-nodes-changed"
              && (event.detail.changed || []).some(
                  (c) => c.name === "hpc" && c.node === "hpc-data"
                      && c.up === true)));
    dispatched.length = 0;
    await Remotes.refresh();
    check("...and a poll that changed nothing announces nothing",
          dispatched.length === 0);

    // -- 3. an open surface keeps the poll alive ------------------------------
    const before = fetches("data_places");
    tickClock(Remotes.POLL_MS);
    await settle();
    check("an active subscriber is re-read even with everything settled",
          fetches("data_places") === before + 1);

    stopA();
    stopB();
    check("dropping every subscriber cancels the timer",
          timersOutstanding() === 0);

    // -- 2. passive + settled = silence ---------------------------------------
    const passive = [];
    const stopPassive = Remotes.subscribe((s) => passive.push(s));
    await settle();
    const settledAt = calls.length;
    tickClock(Remotes.POLL_MS * 5);
    await settle();
    check("a passive watcher of a settled connection polls at nothing",
          calls.length === settledAt);

    // -- 3. ...until something starts happening -------------------------------
    remotes = [profile("hpc", "waiting_for_job")];
    places = [place("hpc", "waiting_for_job")];
    await Remotes.refresh();
    const openingAt = calls.length;
    tickClock(Remotes.POLL_MS);
    await settle();
    check("a connection on its way up is polled even by a passive watcher",
          calls.length > openingAt);
    stopPassive();

    // -- 5. one broken renderer does not stop the rest ------------------------
    const survivor = [];
    const stopBad = Remotes.subscribe(() => { throw new Error("bad render"); },
                                      { active: true });
    const stopGood = Remotes.subscribe((s) => survivor.push(s),
                                       { active: true });
    await Remotes.refresh();
    check("a subscriber that throws does not starve the ones after it",
          survivor.length >= 1);
    stopBad();
    stopGood();

    // -- the focused deep log -------------------------------------------------
    calls.length = 0;
    statusReply = { name: "hpc", state: "authenticating",
                    log: ["line one", "line two"] };
    const stopFocus = Remotes.subscribe(() => {},
                                        { active: true,
                                          focus: { name: "hpc", kind: "node" } });
    await settle();
    const deep = calls.find((c) => c.url.indexOf("/status") >= 0);
    check("a focused watcher asks for that connection's whole log",
          Boolean(deep) && deep.url.indexOf("log=200") >= 0);
    check("...and asks about the right kind of connection",
          Boolean(deep) && deep.url.indexOf("kind=node") >= 0);
    check("...which is handed back keyed by kind and name",
          (Remotes.focused("hpc", "node") || {}).log.length === 2);
    stopFocus();

    // -- a failed poll keeps the last account of the world --------------------
    const kept = Remotes.snapshot().entries.length;
    failNext = "the server went away";
    await Remotes.refresh();
    check("a failed poll notes the error without blanking the list",
          Remotes.snapshot().error === "the server went away"
          && Remotes.snapshot().entries.length === kept);

    // -- 7. the one secret heuristic ------------------------------------------
    check("a password prompt is a secret",
          Remotes.isSecret("me@host's password:") === true);
    check("a Duo prompt is a secret",
          Remotes.isSecret("Enter a passcode or select one of the following:")
          === true);
    check("a host-key question is not, and neither is its fingerprint",
          Remotes.isSecret(
              "The authenticity of host 'login (10.0.0.1)' can't be "
              + "established.\nED25519 key fingerprint is SHA256:abc.\nAre you "
              + "sure you want to continue connecting (yes/no/[fingerprint])?")
          === false);

    // -- one spelling of every state ------------------------------------------
    check("every session state has a label",
          Remotes.OPENING.every((state) => Remotes.label(state) !== state)
          && Remotes.label("connected") === "Connected");
    check("the opening states are the server's opening states",
          Remotes.OPENING.join(",")
          === "preparing_compute,connecting,authenticating,mounting_data,"
             + "installing,waiting_for_job,tunneling,waiting_for_app");

    // -- acting on a connection names which kind ------------------------------
    calls.length = 0;
    await Remotes.connect("hpc", "node").catch(() => {});
    const posted = calls.find((c) => c.method === "POST");
    check("connecting a data node says so in the request",
          Boolean(posted) && posted.url.indexOf("/connect?kind=node") >= 0);
    calls.length = 0;
    await Remotes.disconnect("hpc").catch(() => {});
    const viewerPost = calls.find((c) => c.method === "POST");
    check("...and a viewer disconnect carries no kind, as it always did",
          Boolean(viewerPost) && viewerPost.url.endsWith("/disconnect"));

    // -- 8. the clock on a scheduled job --------------------------------------
    //
    // A connection running inside a job has a deadline nobody is watching:
    // Slurm does not warn, it kills the allocation. The server sends how long
    // is left AT THE MOMENT IT ANSWERED rather than a deadline, because the
    // two clocks are different machines' -- and this is the other half of it.
    remotes = [profile("gpu", "idle"), profile("plain", "idle")];
    places = [place("gpu", "connected", { node: "gpu-data", time_left: 900,
                                          time_limit: 14400 }),
              place("plain", "connected", { node: "plain-data" })];
    await Remotes.refresh();
    check("a connection inside a job carries how long is left of it",
          Remotes.entry("gpu").node.timeLeft === 900
          && Remotes.entry("gpu").node.timeLimit === 14400);
    check("...and one that is not on a clock has no countdown at all",
          Remotes.remaining(Remotes.entry("plain")) === null);

    // The poll stops when everything is settled -- that is the whole reason
    // the globe can sit on every page -- so a countdown that only moved when a
    // request came back would sit frozen for the entire four hours it is
    // counting down.
    check("the countdown starts from what the last answer said",
          Remotes.remaining(Remotes.entry("gpu")) === 900);
    tickClock(60000);
    check("...and keeps running with nothing polling behind it",
          Remotes.remaining(Remotes.entry("gpu")) === 840);
    tickClock(900000);
    check("...and stops at zero rather than going negative",
          Remotes.remaining(Remotes.entry("gpu")) === 0);

    // A clock is about an allocation, and a connection somebody disconnected
    // has none -- the job it was running in is usually cancelled in the same
    // breath. This is the guard that says so on the browser's side: the poll
    // stops when everything is settled, and `remaining()` interpolates, so
    // without it the last snapshot before the stop went on counting down for a
    // machine nobody was talking to any more.
    remotes = [profile("gpu", "idle")];
    places = [place("gpu", "exited", { node: null, time_left: 900,
                                       time_limit: 14400 })];
    await Remotes.refresh();
    check("a disconnected connection has no countdown, whatever it last said",
          Remotes.remaining(Remotes.entry("gpu")) === null);

    // The node that outlived the Plexora that started it: no session, so no
    // state to read, and the registry's copy of the deadline is the only thing
    // that knows there is one. `registered` is how that case is told apart
    // from a connection that has gone.
    places = [place("gpu", "idle", { node: null, registered_node: "gpu-data",
                                     time_left: 900, time_limit: 14400 })];
    await Remotes.refresh();
    check("...but a node still on the map keeps its clock with no session",
          Remotes.remaining(Remotes.entry("gpu")) === 900);

    // Every state on the way up counts. A data node's first start runs into
    // minutes, and all of it is time off an allocation that started before it.
    places = [place("gpu", "waiting_for_app", { node: null, time_left: 900,
                                                time_limit: 14400 })];
    await Remotes.refresh();
    check("...and one still coming up is already on its job's clock",
          Remotes.remaining(Remotes.entry("gpu")) === 900);

    check("what is left is shown as a clock, never a count of seconds",
          Remotes.duration(3661) === "1:01:01"
          && Remotes.duration(600) === "10:00"
          && Remotes.duration(59) === "0:59"
          && Remotes.duration(0) === "0:00");

    console.log(failures.length
        ? `\n${failures.length} failed`
        : "\nall remote-state checks passed");
    if (failures.length) process.exitCode = 1;
}

main();
