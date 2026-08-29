/**
 * remoteState.js -- one owner of "what are the remote connections doing?".
 *
 * Four surfaces ask that question: the Settings page's server cards, the
 * machine picker a data field opens, the connection modal, and the globe in
 * the navbar. Before this, the first two each ran their own timer against
 * their own endpoint with their own idea of which states mean "still
 * happening" and their own guess at whether a prompt was a secret -- so
 * Settings masked a host-key fingerprint the picker showed in the clear, and
 * two open surfaces meant two independent polls of the same three processes.
 *
 * So the state lives here and the surfaces subscribe to it.
 *
 * **The poll is scoped, deliberately.** It runs at `POLL_MS` only while there
 * is somebody to tell AND there is something to tell them: a session on its
 * way up, or a subscriber that is `active` -- an open page or dialog whose
 * whole job is showing this. A connected-and-idle connection watched only by
 * the navbar globe polls at nothing at all, which is the state a viewer sits
 * in for hours and the reason the globe can exist on every page without
 * costing the viewer a request a second.
 *
 * **One fetch pair per tick, no matter how many subscribers.** Settings open
 * in one tab beside a modal is one round trip, not two, because the in-flight
 * promise is shared.
 *
 * **The snapshot is the merge of both halves.** `GET /settings/remotes` knows
 * about viewer connections and `GET /data_places` about data nodes; one saved
 * profile can have both at once and they mean different things. Consumers get
 * the raw lists they already read plus `entries`, which pairs the two halves
 * of each profile so a surface showing "this machine" does not have to do the
 * join itself.
 *
 * Nothing here holds a secret. An answer to a prompt is passed straight
 * through to `POST …/answer` and is never stored on the snapshot, which is the
 * thing every subscriber sees.
 */
window.PlexoraRemotes = (function () {
    "use strict";

    //: How often to re-ask while something is happening. An ssh login is a few
    //: seconds and a password prompt has a person on the other end of it, so
    //: this is a progress display rather than a race -- but it is also what
    //: draws the terminal log in the connection modal, where a line arriving a
    //: second late reads as a stall.
    const POLL_MS = 1000;

    //: Every state meaning "on its way up", mirroring
    //: remote_sessions.OPENING_STATES. Anything not here is settled, one way
    //: or the other, and asking again would be asking a question whose answer
    //: cannot change on its own.
    const OPENING = ["connecting", "authenticating", "waiting_for_job",
                     "tunneling", "waiting_for_app"];

    //: What each state is called on screen. One map, because the same word has
    //: to appear on the card, in the modal's step list and in the globe's
    //: panel -- three spellings of "waiting_for_job" is how a user ends up
    //: believing they are looking at three different things.
    const LABELS = {
        idle: "Not connected",
        connecting: "Connecting",
        authenticating: "Needs your password",
        waiting_for_job: "Queued",
        tunneling: "Tunnelling",
        waiting_for_app: "Starting",
        connected: "Connected",
        failed: "Failed",
        exited: "Disconnected",
    };

    //: How much of the log a focused watcher asks for. The server clamps this
    //: to what it keeps; the shallow list keeps its short tail.
    const DEEP_LOG = 200;

    const KIND_VIEWER = "viewer";
    const KIND_NODE = "node";

    function isOpening(state) {
        return OPENING.indexOf(state) >= 0;
    }

    function label(state) {
        return LABELS[state] || state || "";
    }

    /**
     * Whether what ssh is asking for is a secret.
     *
     * Three kinds of question come through the one channel and only one of
     * them is confidential. A host-key confirmation -- "Are you sure you want
     * to continue connecting (yes/no/[fingerprint])?" -- is the common one on
     * a first connection, and masking it means somebody types `yes` into a row
     * of dots, next to a fingerprint they are being asked to check. The
     * fingerprint is public by construction; hiding the answer to it protects
     * nothing and costs the user the one thing they need to see.
     *
     * One implementation for every surface. Two of them had diverged, so the
     * same prompt was masked on the Settings page and legible in the picker.
     */
    function isSecret(text) {
        const lowered = String(text || "").toLowerCase();
        return !(/\(yes\/no/.test(lowered)
                 || lowered.includes("fingerprint")
                 || /\byes\b.*\bno\b/.test(lowered));
    }

    function url(path) {
        return (typeof plexoraUrl === "function")
            ? plexoraUrl(path) : "/" + String(path).replace(/^\/+/, "");
    }

    async function ask(path, options) {
        const response = await fetch(url(path), options);
        let payload = {};
        try {
            payload = await response.json();
        } catch (e) {
            payload = {};
        }
        if (!response.ok) {
            throw new Error(payload.error || "That machine could not be reached.");
        }
        return payload;
    }

    function remotePath(name, suffix, kind) {
        const base = "settings/remotes/" + encodeURIComponent(name) + suffix;
        return kind === KIND_NODE ? base + "?kind=node" : base;
    }

    // -- the snapshot --------------------------------------------------------

    //: The last answer, handed to every subscriber and to `snapshot()`. Never
    //: replaced in place: consumers keep a reference across a render, and a
    //: mutated object would let a half-drawn card read next tick's values.
    let current = {
        loaded: false,
        error: null,
        remotes: [],
        places: [],
        entries: [],
        clientNode: "",
        serverIsRemote: false,
        server: null,
        focus: {},
    };

    function focusKey(spec) {
        return (spec.kind || KIND_VIEWER) + ":" + spec.name;
    }

    /**
     * One row per saved profile, with both of its halves.
     *
     * A profile is one login that can be running two unrelated things: a
     * viewer over there with the browser tunnelled to it, and a data node over
     * there serving bytes to the Plexora here. Every surface that shows a
     * machine has to show both, and none of them should have to do this join.
     */
    function merge(remotes, places) {
        const byName = {};
        places.forEach((place) => {
            if (place.kind === "remote") byName[place.id] = place;
        });
        return remotes.map((remote) => {
            const place = byName[remote.name] || {};
            const viewer = {
                state: remote.state || "idle",
                phase: remote.phase || "",
                error: remote.error || null,
                prompt: remote.prompt || null,
                url: remote.url || null,
                log: remote.log || [],
                dataNodes: remote.data_nodes || [],
                nodeErrors: remote.node_errors || [],
            };
            const node = {
                state: place.state || "idle",
                phase: place.phase || "",
                error: place.error || null,
                prompt: place.prompt || null,
                //: The short tail `/data_places` carries, so a card can draw a
                //: terminal before anybody has focused this connection. The
                //: deep 200-line pull replaces it via `focused()`.
                log: place.log || [],
                //: The name the data node is on the map under, which is what a
                //: field addresses -- not necessarily the profile's own name.
                node: place.node || null,
            };
            return {
                name: remote.name,
                target: remote.target || "",
                label: remote.name,
                detail: remote.target || "",
                //: Whether connecting waits for a scheduler. The profile's own
                //: setting, and worth knowing BEFORE pressing Connect: it
                //: turns seconds into a queue.
                queued: remote.srun !== null && remote.srun !== undefined,
                viewer: viewer,
                node: node,
                //: Either half up is "this machine is reachable" for the
                //: purposes of a globe; a data field cares only about `node`.
                connected: viewer.state === "connected" || Boolean(node.node),
                opening: isOpening(viewer.state) || isOpening(node.state),
                prompt: viewer.prompt || node.prompt || null,
            };
        });
    }

    function half(entry, kind) {
        return kind === KIND_NODE ? entry.node : entry.viewer;
    }

    // -- subscribers ---------------------------------------------------------

    const subscribers = new Set();
    let timer = null;
    let inFlight = null;

    function activeSubscribers() {
        let any = false;
        subscribers.forEach((sub) => { if (sub.active) any = true; });
        return any;
    }

    /**
     * Which connections somebody has open far enough to want the whole log.
     *
     * A subscriber's `focus` may be a FUNCTION, and may name SEVERAL. The
     * Settings page needs both: which cards have their log expanded changes as
     * the user opens and closes them, and re-subscribing on every toggle would
     * mean tearing down and rebuilding the one thing whose state is being
     * preserved. A single plain object is still accepted -- the modal watches
     * exactly one connection for its whole life.
     *
     * Keyed, so two surfaces watching the same connection is one fetch.
     */
    function focusSpecs() {
        const specs = new Map();
        subscribers.forEach((sub) => {
            const asked = typeof sub.focus === "function" ? sub.focus() : sub.focus;
            const list = Array.isArray(asked) ? asked : [asked];
            list.forEach((spec) => {
                if (spec && spec.name) specs.set(focusKey(spec), spec);
            });
        });
        return Array.from(specs.values());
    }

    function somethingHappening() {
        return current.entries.some((entry) => entry.opening || entry.prompt);
    }

    /**
     * Whether to look again, and the whole of the poll policy.
     *
     * Nobody watching: no. Nothing moving and nobody with a dialog open: also
     * no -- a settled connection changes only when somebody acts on it, and
     * whoever acts calls `refresh()` themselves.
     */
    function wanted() {
        if (!subscribers.size) return false;
        return somethingHappening() || activeSubscribers();
    }

    function reschedule() {
        window.clearTimeout(timer);
        timer = null;
        if (!wanted()) return;
        timer = window.setTimeout(() => { refresh(); }, POLL_MS);
    }

    function publish(next) {
        current = next;
        subscribers.forEach((sub) => {
            try {
                sub.cb(current);
            } catch (e) {
                // One surface throwing must not stop the others being told, and
                // must not stop the poll: the throw is a bug in that renderer,
                // not a reason for the modal beside it to freeze mid-connect.
                if (window.console) console.error("remote state subscriber", e);
            }
        });
    }

    /**
     * Read both halves (and any focused connection's deep log) and publish.
     *
     * The in-flight promise is shared, so a modal and the Settings page open at
     * once are one round trip. Callers that just POSTed something await this to
     * get a snapshot that includes what they did.
     */
    function refresh() {
        if (inFlight) return inFlight;
        window.clearTimeout(timer);
        timer = null;
        inFlight = read().then((next) => {
            inFlight = null;
            publish(next);
            reschedule();
            return next;
        }).catch((error) => {
            inFlight = null;
            // The previous data is still the best account of the world there
            // is; a failed poll is a note beside it, not a reason to blank
            // every card. Notably a 404 here is what forgetting a profile in
            // another tab looks like.
            publish(Object.assign({}, current,
                                  { error: error.message, loaded: true }));
            reschedule();
            return current;
        });
        return inFlight;
    }

    async function read() {
        const specs = focusSpecs();
        const [remotes, places] = await Promise.all([
            ask("settings/remotes"),
            ask("data_places"),
        ]);
        const focus = {};
        // Sequential rather than parallel and deliberately after the pair: a
        // focused fetch exists to deepen one entry the list already described,
        // and there is at most one open modal.
        for (const spec of specs) {
            try {
                focus[focusKey(spec)] = await ask(
                    remotePath(spec.name, "/status", spec.kind)
                    + (spec.kind === KIND_NODE ? "&" : "?") + "log=" + DEEP_LOG);
            } catch (e) {
                focus[focusKey(spec)] = { error: e.message };
            }
        }
        const list = places.places || [];
        return {
            loaded: true,
            error: null,
            remotes: remotes.remotes || [],
            places: list,
            entries: merge(remotes.remotes || [], list),
            clientNode: places.client_node || "",
            serverIsRemote: Boolean(places.server_is_remote),
            server: list.find((place) => place.kind === "server") || null,
            focus: focus,
        };
    }

    /**
     * @function subscribe - be told what the connections are doing.
     *
     * @param cb called with the snapshot on every change, and once as soon as
     *   one is available.
     * @param options `active` -- this subscriber is a surface somebody is
     *   looking at, so keep polling even when everything is settled;
     *   `focus: {name, kind}` (or a function returning one, or null) -- also
     *   fetch that connection's full log.
     * @returns a function that stops this subscription.
     */
    function subscribe(cb, options = {}) {
        const sub = {
            cb: cb,
            active: Boolean(options.active),
            focus: options.focus || null,
        };
        subscribers.add(sub);
        if (current.loaded) {
            try {
                cb(current);
            } catch (e) {
                if (window.console) console.error("remote state subscriber", e);
            }
        }
        // A new active subscriber, or the first one of any kind, has nothing to
        // draw yet -- everything else is already inside the cadence.
        if (sub.active || subscribers.size === 1 || !current.loaded) refresh();
        else reschedule();

        let stopped = false;
        return function unsubscribe() {
            if (stopped) return;
            stopped = true;
            subscribers.delete(sub);
            if (!subscribers.size) {
                window.clearTimeout(timer);
                timer = null;
            } else {
                reschedule();
            }
        };
    }

    function snapshot() {
        return current;
    }

    /** The deep status of a focused connection, or null. */
    function focused(name, kind) {
        return current.focus[focusKey({ name: name, kind: kind })] || null;
    }

    function entry(name) {
        return current.entries.find((item) => item.name === name) || null;
    }

    // -- acting on a connection ----------------------------------------------
    //
    // Centralised for the same reason the reading is: the URL a surface POSTs
    // to encodes which of the two things it means, and a caller that forgot
    // `?kind=node` disconnected somebody's viewer instead of their data node.

    function connect(name, kind) {
        return ask(remotePath(name, "/connect", kind), { method: "POST" })
            .finally(() => refresh());
    }

    function disconnect(name, kind) {
        return ask(remotePath(name, "/disconnect", kind), { method: "POST" })
            .finally(() => refresh());
    }

    function answer(name, kind, id, value) {
        return ask(remotePath(name, "/answer", kind), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ id: id, answer: value }),
        }).finally(() => refresh());
    }

    function forget(name) {
        return ask("settings/remotes/" + encodeURIComponent(name),
                   { method: "DELETE" }).finally(() => refresh());
    }

    function save(body) {
        return ask("settings/remotes", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body || {}),
        }).finally(() => refresh());
    }

    return {
        POLL_MS, OPENING, LABELS, KIND_VIEWER, KIND_NODE,
        isOpening, isSecret, label,
        subscribe, snapshot, refresh, focused, entry, half,
        connect, disconnect, answer, forget, save,
    };
})();
