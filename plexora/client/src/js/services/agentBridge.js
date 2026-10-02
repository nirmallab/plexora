/**
 * agentBridge.js -- the viewer tab's half of the live viewer control plane.
 *
 * An external agent (an MCP process, a notebook, anything that speaks the
 * `/agent/v1` routes) can drive a viewer somebody already has open: read what
 * is on screen, move it, change its channels, open a tool, capture the view,
 * and show a result back over the image. The server has never been able to
 * push anything to a browser, so this tab does the asking: it registers, then
 * polls "is there anything for me?", runs what it is given ONE COMMAND AT A
 * TIME, and acknowledges each with what happened. The server side is
 * server/routes/agent_routes.py over server/models/viewer_sessions.py; the
 * wire is described there and nothing here invents any of it.
 *
 * **Two speeds.** A tab nobody is driving polls with `wait=0` every
 * `idle_poll_s` (5 s) -- a heartbeat that costs one short request. Once the
 * server says an agent is attached, the poll is HELD open (`wait=15`) and
 * re-issued back to back, so a command arrives the moment it is sent. A held
 * request is a Waitress worker thread, which is why the server caps how many
 * may be held at once: past it a poll comes back at once with
 * `budget_exhausted`, and this retries a second later -- slower, never
 * starved. A network failure backs off to the idle interval, a 404 (the server
 * forgot this tab -- restarted, or the tab was asleep for longer than the
 * session TTL) re-registers, and a 403 (the agent API does not answer this
 * client at all) stops the loop for good: it is an answer, not a hiccup.
 *
 * **One session per tab, surviving its own navigation.** The id is kept in
 * sessionStorage, so `open_project` -- which has to navigate, because a
 * different project is a different document (see appRouter.js) -- lands on a
 * page that re-registers under the same id and picks up the commands the
 * server kept for it. The poll cursors ride along so the new page neither
 * replays the old page's events nor skips new ones; if the server no longer
 * knows the id the cursors are reset. A duplicated tab copies sessionStorage
 * too, so on boot the id is checked against every other live tab (a
 * BroadcastChannel) and dropped if one is already using it.
 *
 * **Revision.** A counter bumped by `touch()` whenever what is on screen
 * changes -- the view moved, a layer, a channel, the cell mode. It is reported
 * on every poll and ack, and a command sent with `expected_revision` that no
 * longer matches still runs (the agent asked for it) but is acknowledged with
 * a warning, so an agent reasoning about a view the user has since changed is
 * told so.
 *
 * **Core does not call plugins; plugins answer.** Three DOM events are the
 * whole of the plugin interface, and no plugin name appears below:
 *
 *   `plexora:agent-state-changed` -- an event the agent published ("gating
 *     changed on this project"), re-dispatched on window for every event that
 *     did not come from this tab. A plugin listens for its own `plugin` and
 *     re-reads its saved state. Core handles `plugin === "core"` itself.
 *   `plexora:agent-command` -- a command core cannot carry out on its own
 *     (`set_active_marker`, `focus_roi`). `detail.claim(valueOrPromise, by)`
 *     takes it; nobody claiming is an `unsupported` ack.
 *   `plexora:agent-state` -- `get_state` asking every plugin for its part;
 *     `detail.contribute(name, object)`.
 *
 * **The viewer is leased, and given back.** The first command of a run that
 * changes what is on screen (`MUTATING`) snapshots what the user had: the
 * channel slots, HD mode, the cell render mode, the viewport, every plugin's
 * `get_state` part, and -- as they are opened -- the tools the agent opened.
 * `restore_viewer` (sent by the server's teardown at finish, stop and
 * rollback; also run locally by the agent panel on Stop and on `finished`)
 * puts all of it back, clears highlights and the evidence dialog, asks the
 * plugins to put their own part back (`plexora:agent-restore`, with a
 * `wait(promise)` to be awaited), and drops the lease. The lease is what the
 * agent touched, restored wholesale: a change the user made BY HAND to the
 * same things during the run is undone with it. That is accepted -- the
 * alternative is an agent that leaves its channels behind. The lease lives in
 * this document only: `open_project` navigates, and the page it lands on has
 * none (a restore there is `had_lease: false`, a no-op).
 *
 * Loaded by index.html (the viewer page only) BEFORE main.js, deferred like
 * it, and started once `window.__plexoraReady` settles -- the viewer, the
 * sidebar and the layer stack exist from then on, and every handler below
 * reads them through `window.__plexora` at call time rather than capturing
 * anything at load.
 */
window.PlexoraAgentBridge = (function () {
    "use strict";

    const API = "agent/v1/viewer/sessions";
    const STORE_KEY = "plexora.agentBridge";
    //: Overwritten by what the server says at registration.
    let idleMs = 5000;
    let heldS = 15;
    //: How long to wait after the server said its held-request budget is spent.
    const BUSY_RETRY_MS = 1000;
    //: How long to back off after a poll that failed outright.
    const ERROR_MS = 5000;
    //: How long a command may wait for auto-contrast fits to land before the
    //: channel save is released. Bounded so a fit that never answers cannot
    //: leave the project's channel list unsaveable.
    const SETTLE_LIMIT_MS = 30000;

    let session = null;
    const cursor = { after: 0, afterEvent: 0 };
    let revision = 0;
    let attached = false;
    let running = false;
    //: Set on a 403: the server said no, and it will keep saying no.
    let stopped = false;
    //: This tab is leaving on purpose (open_project, a hard reload) and has
    //: already told the server where to; pagehide must not also say "closed".
    let navigating = false;
    let unloaded = false;
    //: Where this tab last said it was going. Kept after a navigation that
    //: did not happen, so the pagehide that eventually does come still says
    //: "moving" rather than "closed" and the next page keeps the session.
    let intendedTarget = null;
    let wake = null;
    //: Bumped by every start and stop; a loop runs only while it is current.
    let generation = 0;
    let subscribed = false;
    let channel = null;
    let pendingProbe = null;
    let evidenceDialog = null;
    //: What the user had before an agent's first on-screen change; see the
    //: header. Null when nothing has been leased (or it was given back).
    let lease = null;
    //: The commands that take the lease. Reads, captures and evidence do not:
    //: an agent that only looks leaves nothing to restore.
    const MUTATING = new Set([
        "set_channels", "set_channel_color", "set_contrast", "set_hd_mode",
        "set_cell_render_mode", "open_tool", "preview_gate", "highlight_cells", "show_shapes",
        "set_active_marker", "fit_region", "focus_cell", "pan_to", "zoom_to",
    ]);
    const CHANNEL_COMMANDS = new Set(["set_channels", "set_channel_color", "set_contrast"]);

    // -- small things -------------------------------------------------------

    function url(path) {
        return (typeof plexoraUrl === "function") ? plexoraUrl(path) : "/" + path;
    }

    function core() {
        return window.__plexora || {};
    }

    function datasource() {
        return (window.flaskVariables && window.flaskVariables.datasource) || "";
    }

    function datasetName() {
        try {
            const place = window.PlexoraDatasetNav && window.PlexoraDatasetNav.place
                ? window.PlexoraDatasetNav.place() : null;
            return (place && place.datasetName) || null;
        } catch (error) {
            return null;
        }
    }

    function clientKind() {
        if (window.PlexoraDesktop) return "desktop";
        let framed = false;
        try {
            framed = window.parent && window.parent !== window;
        } catch (error) {
            framed = true;
        }
        if ((window.flaskVariables && window.flaskVariables.notebook_mode) || framed) {
            return "notebook";
        }
        return "browser";
    }

    function visible() {
        return typeof document === "undefined" || document.visibilityState !== "hidden";
    }

    /**
     * Wait for a redraw only where one can happen. A hidden tab's frames are
     * paused by the browser, so a swap that resolves on OpenSeadragon's
     * "fully loaded" never settles there and the command would miss its
     * acknowledgement deadline. Hidden, the state is set and the tiles are
     * drawn when the tab is next shown; the ack says so.
     */
    async function untilDrawn(work, call) {
        if (visible()) return work;
        Promise.resolve(work).catch((error) => console.error("agentBridge: a deferred draw failed", error));
        if (call) call.warn("the tab is in the background: drawn when it is next shown");
        return undefined;
    }

    function pause(ms) {
        return new Promise((resolve) => window.setTimeout(resolve, ms));
    }

    /** A sleep the loop can be woken from (a tab coming back into view). */
    function sleep(ms) {
        return new Promise((resolve) => {
            const timer = window.setTimeout(() => { wake = null; resolve(); }, ms);
            wake = () => { window.clearTimeout(timer); wake = null; resolve(); };
        });
    }

    function round(value, places = 2) {
        const factor = 10 ** places;
        return Number.isFinite(value) ? Math.round(value * factor) / factor : value;
    }

    function unsupported(message) {
        const error = new Error(message);
        error.unsupported = true;
        return error;
    }

    function messageOf(error) {
        return (error && error.message) ? error.message : String(error || "failed");
    }

    function json(response) {
        return response.json().catch(() => ({}));
    }

    function post(path, body, extra = {}) {
        return fetch(url(path), Object.assign({
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body || {}),
        }, extra));
    }

    function readStore() {
        try {
            return JSON.parse(window.sessionStorage.getItem(STORE_KEY) || "null") || {};
        } catch (error) {
            return {};
        }
    }

    function writeStore() {
        try {
            window.sessionStorage.setItem(STORE_KEY, JSON.stringify({
                session_id: session, after: cursor.after,
                after_event: cursor.afterEvent, revision,
            }));
        } catch (error) {
            // A private window with storage off: the session simply does not
            // survive a navigation, and the next page registers a new one.
        }
    }

    /** Something on screen changed. Cheap: a counter, read by the next poll. */
    function touch() {
        revision += 1;
        return revision;
    }

    // -- the session --------------------------------------------------------

    /**
     * Whether another live tab is already using `id` -- a duplicated tab
     * inherits this one's sessionStorage, and two tabs polling one session
     * would each receive half of its commands.
     */
    function openChannel() {
        if (channel || typeof BroadcastChannel !== "function") return;
        try {
            channel = new BroadcastChannel("plexora-agent-bridge");
            channel.onmessage = (event) => {
                const message = event.data || {};
                if (message.type === "probe" && message.id && message.id === session) {
                    channel.postMessage({ type: "taken", id: message.id, nonce: message.nonce });
                } else if (message.type === "taken" && pendingProbe
                           && message.nonce === pendingProbe.nonce) {
                    pendingProbe.resolve(true);
                }
            };
        } catch (error) {
            channel = null;
        }
    }

    function takenElsewhere(id) {
        if (!channel) return Promise.resolve(false);
        return new Promise((resolve) => {
            const nonce = Math.random().toString(36).slice(2);
            pendingProbe = { nonce, resolve };
            channel.postMessage({ type: "probe", id, nonce });
            window.setTimeout(() => resolve(false), 200);
        }).finally(() => { pendingProbe = null; });
    }

    function availableTools() {
        const names = new Set();
        try {
            const rows = document.querySelectorAll ? document.querySelectorAll("a[data-tool]") : [];
            Array.from(rows || []).forEach((row) => {
                const name = row && row.dataset && row.dataset.tool;
                if (name) names.add(name);
            });
        } catch (error) {
            // No menu: only what is loaded.
        }
        const loader = window.PlexoraToolLoader;
        ((loader && loader.loadedTools && loader.loadedTools()) || [])
            .forEach((name) => names.add(name));
        return Array.from(names);
    }

    function openTools() {
        const loader = window.PlexoraToolLoader;
        if (!loader || !loader.loadedTools) return [];
        return loader.loadedTools().filter((name) => loader.isToolOpen && loader.isToolOpen(name));
    }

    /** Register, or re-register under the id this tab already had. */
    async function register() {
        const stored = readStore();
        let id = session || stored.session_id || null;
        if (id && !session && await takenElsewhere(id)) id = null;
        if (id && !session) {
            cursor.after = 0;
            cursor.afterEvent = 0;
            // Does the server still know it? A session it has forgotten comes
            // back as a NEW one under the same id, with its sequence numbers
            // starting again from zero -- and cursors carried from the old one
            // would silently skip everything it is sent.
            try {
                const response = await fetch(url(`${API}/${encodeURIComponent(id)}`),
                    { credentials: "same-origin" });
                if (response.status === 403) {
                    halt();
                    return false;
                }
                if (response.ok) {
                    cursor.after = Number(stored.after) || 0;
                    cursor.afterEvent = Number(stored.after_event) || 0;
                    revision = Math.max(revision, (Number(stored.revision) || 0) + 1);
                }
            } catch (error) {
                // Unreachable: register below will say so.
            }
        }
        const response = await post(API, {
            session_id: id,
            project: datasource(),
            dataset: datasetName(),
            client: clientKind(),
            capabilities: Object.keys(HANDLERS),
            tools: availableTools(),
            page_url: window.location ? window.location.href : "",
            title: (typeof document !== "undefined" && document.title) || "",
            revision,
        });
        if (response.status === 403) {
            halt();
            return false;
        }
        if (!response.ok) return false;
        const payload = await json(response);
        const described = payload.session || {};
        if (!described.view_id) return false;
        if (described.view_id !== id) {
            cursor.after = 0;
            cursor.afterEvent = 0;
        }
        session = described.view_id;
        if (Number(payload.idle_poll_s) > 0) idleMs = Number(payload.idle_poll_s) * 1000;
        if (Number(payload.held_poll_s) > 0) heldS = Number(payload.held_poll_s);
        writeStore();
        return true;
    }

    /** Tell the server this tab is going -- or only moving, when `to` is set. */
    function leave(to) {
        if (!session) return Promise.resolve();
        const path = `${API}/${encodeURIComponent(session)}/leave`;
        const body = JSON.stringify(to ? { navigating_to: to } : {});
        if (!to && typeof navigator !== "undefined" && navigator.sendBeacon) {
            try {
                // A beacon, because this runs from pagehide, where an ordinary
                // request is cancelled with the page.
                if (navigator.sendBeacon(url(path), new Blob([body], { type: "application/json" }))) {
                    return Promise.resolve();
                }
            } catch (error) {
                // Fall through to a keepalive fetch.
            }
        }
        return fetch(url(path), {
            method: "POST", credentials: "same-origin", keepalive: true,
            headers: { "Content-Type": "application/json" }, body,
        }).catch(() => {});
    }

    /** Leave for `href` on purpose, keeping this session for the page there. */
    async function navigateTo(href, go) {
        navigating = true;
        intendedTarget = href;
        running = false;
        if (wake) wake();
        await leave(href);
        // A page that does not go after all -- a beforeunload prompt the user
        // answered "stay" (ROI's, with regions still unsaved) -- must not be
        // left deaf to the agent for the rest of its life. As long as the
        // server keeps a navigating session's commands (NAVIGATING_GRACE_S),
        // because until then a slow server may simply not have answered the
        // new page yet, and the old one is still up while it waits.
        window.setTimeout(() => {
            if (unloaded) return;
            navigating = false;
            start();
        }, 30000);
        go();
    }

    function hardReload() {
        const here = window.location ? window.location.href : "";
        return navigateTo(here, () => window.location.reload());
    }

    function halt() {
        stopped = true;
        running = false;
        if (wake) wake();
        console.info("Plexora: the agent API does not answer this viewer; it will not ask again.");
    }

    // -- the loop -----------------------------------------------------------

    /**
     * One poll, and everything it brought.
     * @returns how long to wait before the next one (ms), or null to stop.
     */
    async function pollOnce() {
        if (stopped) return null;
        if (!session) {
            const ok = await register().catch(() => false);
            if (stopped) return null;
            return ok ? 0 : ERROR_MS;
        }
        const query = new URLSearchParams({
            after: String(cursor.after),
            after_event: String(cursor.afterEvent),
            wait: String(attached ? heldS : 0),
            revision: String(revision),
            visible: visible() ? "1" : "0",
        });
        let response;
        try {
            response = await fetch(url(`${API}/${encodeURIComponent(session)}/commands?${query}`),
                { credentials: "same-origin" });
        } catch (error) {
            attached = false;
            return ERROR_MS;
        }
        if (response.status === 403) {
            halt();
            return null;
        }
        if (response.status === 404) {
            // Forgotten -- the server restarted, or this tab slept past the
            // session TTL. Same id, fresh cursors (register decides).
            session = null;
            const ok = await register().catch(() => false);
            if (stopped) return null;
            return ok ? 0 : ERROR_MS;
        }
        if (!response.ok) return ERROR_MS;
        const payload = await json(response);
        attached = Boolean(payload.attached);
        const events = Array.isArray(payload.events) ? payload.events : [];
        const commands = Array.isArray(payload.commands) ? payload.commands : [];
        for (const event of events) await handleEvent(event);
        // Sequentially, and each acknowledged before the next starts: a
        // "set the channels, then capture" pair has to capture the channels.
        for (const command of commands) {
            if (!running) break;
            await runCommand(command);
            if (navigating) return null;
        }
        if (commands.length || events.length) return 0;
        if (payload.budget_exhausted) return BUSY_RETRY_MS;
        return attached ? 0 : idleMs;
    }

    /**
     * `mine` is the generation this loop was started under. A stop followed at
     * once by a start (a tab coming back from the back-forward cache) would
     * otherwise leave the woken old loop running beside the new one -- two
     * polls per interval, each taking half the commands.
     */
    async function loop(mine) {
        while (running && mine === generation) {
            let delay;
            try {
                delay = await pollOnce();
            } catch (error) {
                console.error("agentBridge: a poll failed", error);
                delay = ERROR_MS;
            }
            if (!running || mine !== generation || delay === null) break;
            if (delay > 0) await sleep(delay);
        }
    }

    async function start() {
        if (running || stopped || unloaded) return;
        running = true;
        openChannel();
        subscribe();
        generation += 1;
        loop(generation);
    }

    function stop() {
        running = false;
        generation += 1;
        if (wake) wake();
    }

    function subscribe() {
        if (subscribed) return;
        subscribed = true;
        const plexora = core();
        const viewer = plexora.seaDragonViewer;
        try {
            plexora.layers?.subscribe?.(() => touch());
            viewer?.viewTransform?.subscribe?.(() => touch());
            viewer?.viewer?.addHandler?.("animation-finish", () => touch());
        } catch (error) {
            console.error("agentBridge: could not watch the viewer", error);
        }
        window.addEventListener("plexora:cell-mode-changed", () => touch());
        window.addEventListener("plexora:channels-renamed", () => touch());
        if (typeof document !== "undefined" && document.addEventListener) {
            // The poll carries `visible`; a tab coming back into view says so now
            // rather than at the end of an idle sleep.
            document.addEventListener("visibilitychange", () => { if (wake) wake(); });
        }
    }

    // -- events -------------------------------------------------------------

    async function handleEvent(event) {
        if (!event) return;
        cursor.afterEvent = Math.max(cursor.afterEvent, Number(event.event_seq) || 0);
        writeStore();
        // This tab's own doing, echoed back: it has already happened here.
        if (event.origin && event.origin === session) return;
        window.dispatchEvent(new CustomEvent("plexora:agent-state-changed", { detail: event }));
        if (event.plugin === "core" && CORE_EVENTS[event.kind]) {
            try {
                await CORE_EVENTS[event.kind](event.payload || {}, event);
            } catch (error) {
                console.error(`agentBridge: core could not take on "${event.kind}"`, error);
            }
        }
        touch();
    }

    async function freshChannelNames() {
        const response = await fetch(`${url("config")}?t=${Date.now()}`, { credentials: "same-origin" });
        if (!response.ok) return null;
        const entry = (await json(response))[datasource()];
        if (!entry || !Array.isArray(entry.imageData)) return null;
        return entry.imageData.filter((channel) => channel.fullname !== "Area")
            .map((channel) => channel.name || channel.fullname);
    }

    /**
     * What core does with its own events. Each is the path the app already
     * takes for the same change made by a person, so an agent's change lands
     * exactly as the user's would have.
     */
    const CORE_EVENTS = {
        /** The project's record changed underneath this page. `refreshDataset`
         *  re-reads the read spec and the column statistics in place, and
         *  `adoptLayers` the layer list; a mask attached since the page was
         *  drawn is the one change neither can take on (main.js says why),
         *  and a `hard` reload is the sender asking for the page outright. */
        async reload(payload) {
            const plexora = core();
            const refreshed = plexora.refreshDataset ? await plexora.refreshDataset() : null;
            if (plexora.adoptLayers) await plexora.adoptLayers();
            if (payload.hard || (refreshed && refreshed.maskAttached)) await hardReload();
        },
        async layers() {
            await core().adoptLayers?.();
        },
        async config() {
            await core().refreshDataset?.();
        },
        /** New channel names. Given in the payload, or read off /config; a
         *  list that no longer fits the image is a reload, as adoptChannelNames
         *  asks of its callers. */
        async channels_renamed(payload) {
            const names = Array.isArray(payload.names) ? payload.names : await freshChannelNames();
            if (!names || !core().adoptChannelNames) return;
            const adopted = await core().adoptChannelNames(names);
            if (adopted === false) await hardReload();
        },
    };

    // -- commands -----------------------------------------------------------

    async function sendAck(command, reply) {
        const body = {
            command_id: command.command_id,
            status: reply.status,
            resulting_revision: revision,
        };
        if (reply.result !== undefined) body.result = reply.result;
        if (reply.error) body.error = reply.error;
        if (reply.warning) body.warning = reply.warning;
        try {
            const response = await post(`${API}/${encodeURIComponent(session)}/acks`, body);
            if (response.status === 403) halt();
        } catch (error) {
            console.error("agentBridge: could not acknowledge", command.type, error);
        }
    }

    async function runCommand(command) {
        if (!command || !command.command_id) return;
        cursor.after = Math.max(cursor.after, Number(command.seq) || 0);
        writeStore();
        const warnings = [];
        const expected = command.expected_revision;
        if (expected !== null && expected !== undefined && Number(expected) !== revision) {
            warnings.push(`the view changed since revision ${expected} (it is at ${revision} now)`);
        }
        const call = {
            command,
            acked: false,
            warn(text) { if (text) warnings.push(text); },
            /** Acknowledge before returning -- open_project's, which has to
             *  say "done" while there is still a page here to say it. */
            async ack(status, result) {
                call.acked = true;
                await sendAck(command, { status, result, warning: warnings.join("; ") || undefined });
            },
        };
        const handler = HANDLERS[command.type];
        if (handler && MUTATING.has(command.type)) {
            takeLease();
            if (CHANNEL_COMMANDS.has(command.type)) lease.channelsTouched = true;
        }
        if (!handler) {
            await sendAck(command, {
                status: "unsupported",
                error: `this viewer has no "${command.type}" command`,
                warning: warnings.join("; ") || undefined,
            });
            return;
        }
        let reply;
        try {
            const result = await handler(command.arguments || {}, call);
            reply = { status: "done", result: result === undefined ? null : result };
        } catch (error) {
            reply = { status: error && error.unsupported ? "unsupported" : "rejected",
                      error: messageOf(error) };
        }
        if (call.acked) {
            if (reply.status !== "done") console.error("agentBridge:", command.type, reply.error);
            return;
        }
        reply.warning = warnings.join("; ") || undefined;
        await sendAck(command, reply);
    }

    /**
     * Offer a command to the plugins. Resolves `{by, result}` when one of them
     * claimed it (after awaiting what it claimed with), or null when none did.
     */
    async function offer(type, args) {
        let claimed = null;
        const detail = {
            type,
            arguments: args,
            claimed: false,
            claim(value, by) {
                if (detail.claimed) return false;
                detail.claimed = true;
                claimed = { value, by: by || null };
                return true;
            },
        };
        window.dispatchEvent(new CustomEvent("plexora:agent-command", { detail }));
        if (!claimed) return null;
        return { by: claimed.by, result: await claimed.value };
    }

    // -- reading the view ---------------------------------------------------

    function scene() {
        if (!window.PlexoraViewerScene) throw unsupported("viewerScene.js is not loaded on this page");
        return window.PlexoraViewerScene;
    }

    function viewer() {
        const found = core().seaDragonViewer;
        if (!found || !found.viewer) throw new Error("the viewer has not finished opening");
        return found;
    }

    function sidebar() {
        const found = core().viewerSidebar;
        if (!found) {
            throw unsupported("this image has no channel controls (a flat RGB image, "
                + "or the viewer is still opening)");
        }
        return found;
    }

    /** A viewport as the wire has it: `{x, y, width, height}`, image pixels. */
    function box(viewport) {
        const out = {
            x: round(viewport.x), y: round(viewport.y),
            width: round(viewport.w), height: round(viewport.h),
        };
        if (viewport.orientation) out.orientation = viewport.orientation;
        return out;
    }

    function viewState() {
        const found = viewer();
        return {
            viewport: box(scene().currentViewport(found)),
            scale: round(scene().scaleOf(found), 5),
        };
    }

    function imageSize(found) {
        const item = scene().referenceItem(found);
        const dimensions = item && item.source && item.source.dimensions;
        if (!dimensions) return null;
        const scale = scene().levelScale(found);
        return { width: round(dimensions.x / scale), height: round(dimensions.y / scale) };
    }

    function describeChannels(panel) {
        return (panel.channelSlots || [])
            .filter((slot) => slot && slot.visible && slot.name)
            .map((slot) => {
                // Raw units, the form a saved channel list uses -- and null
                // rather than a byte pair mislabelled as raw when the channel's
                // quantization window has not arrived yet.
                const convertible = panel.isHdMode() || Boolean(panel.quantWindow(slot.name));
                const range = convertible ? panel.toRawRangeForSlot(slot) : null;
                return {
                    name: slot.name,
                    color: slot.colorHex,
                    window: range ? [round(Number(range[0]), 3), round(Number(range[1]), 3)] : null,
                    enabled: Boolean(slot.enabled),
                };
            });
    }

    function describeLayers() {
        const stack = core().layers;
        if (!stack || !stack.describe) return [];
        return stack.describe().map((layer) => ({
            id: layer.id,
            kind: layer.kind,
            label: (stack.get && stack.get(layer.id) && stack.get(layer.id).label) || null,
            visible: layer.visible,
            opacity: layer.opacity,
            order: layer.order,
            pinned: layer.pinned,
        }));
    }

    function pluginStates() {
        const states = {};
        const detail = {
            contribute(name, value) {
                if (name) states[String(name)] = value;
            },
        };
        window.dispatchEvent(new CustomEvent("plexora:agent-state", { detail }));
        return states;
    }

    function getState() {
        const plexora = core();
        const found = plexora.seaDragonViewer && plexora.seaDragonViewer.viewer
            ? plexora.seaDragonViewer : null;
        const panel = plexora.viewerSidebar || null;
        const loader = window.PlexoraToolLoader;
        const state = {
            project: datasource(),
            dataset: datasetName(),
            revision,
            url: window.location ? window.location.href : "",
            viewport: null,
            scale: null,
            image_size: null,
            view_transform: null,
        };
        if (found && window.PlexoraViewerScene) {
            state.viewport = box(scene().currentViewport(found));
            state.scale = round(scene().scaleOf(found), 5);
            state.image_size = imageSize(found);
            state.view_transform = found.viewTransform && found.viewTransform.get
                ? found.viewTransform.get() : null;
        }
        state.channels = panel ? describeChannels(panel) : [];
        state.hd_mode = panel ? Boolean(panel.isHdMode()) : false;
        state.layers = describeLayers();
        state.cell_mode = plexora.viewerControls ? (plexora.viewerControls.mode || null) : null;
        state.tools_open = openTools();
        state.tools_loaded = (loader && loader.loadedTools && loader.loadedTools()) || [];
        state.active_tool = (loader && loader.activeTool && loader.activeTool()) || null;
        state.tools_available = availableTools();
        state.plugins = pluginStates();
        return state;
    }

    // -- channels -----------------------------------------------------------

    function colorHex(value) {
        if (value === undefined || value === null || value === "") return null;
        if (typeof value === "string") {
            const text = value.trim().replace(/^#/, "");
            if (/^[0-9a-f]{6}$/i.test(text)) return "#" + text.toLowerCase();
            if (/^[0-9a-f]{3}$/i.test(text)) {
                return "#" + text.split("").map((c) => c + c).join("").toLowerCase();
            }
        } else if (typeof value === "object"
                   && ["r", "g", "b"].every((key) => Number.isFinite(Number(value[key])))) {
            const hex = (key) => Math.max(0, Math.min(255, Math.round(Number(value[key]))))
                .toString(16).padStart(2, "0");
            return `#${hex("r")}${hex("g")}${hex("b")}`;
        }
        throw new Error(`${JSON.stringify(value)} is not a colour -- use "#rrggbb" or {r, g, b}`);
    }

    function windowOf(value) {
        if (value === undefined || value === null) return null;
        if (value === "auto") return "auto";
        if (Array.isArray(value) && value.length === 2
            && value.every((v) => Number.isFinite(Number(v))) && Number(value[0]) < Number(value[1])) {
            return [Number(value[0]), Number(value[1])];
        }
        throw new Error(`${JSON.stringify(value)} is not a display window -- use [low, high] `
            + `in raw intensity units, or "auto"`);
    }

    /**
     * Let every auto-contrast fit the setters just scheduled land BEFORE
     * persistence is released.
     *
     * The setters level a newly shown channel on a `setTimeout(0)`, and the
     * fit's second pass (the GMM) calls `scheduleSaveChannels` when it lands --
     * a second or two later, after `resumePersistence` would already have run.
     * Without this wait, `persist: false` would write the arrangement to the
     * project anyway, once per channel, just late enough to look unrelated.
     */
    async function settleAutoLevels(slots) {
        await pause(0);
        const deadline = Date.now() + SETTLE_LIMIT_MS;
        while (slots.some((slot) => slot.autoLeveling) && Date.now() < deadline) {
            await pause(50);
        }
        // The launch path's "this auto-level is not an edit" flag, where no
        // fit consumed it. Left set, it would swallow the save of the next
        // auto-level the user asks for by hand.
        slots.forEach((slot) => { if (slot.autoSilent) slot.autoSilent = false; });
    }

    /** One channel into the slots as they stand (`mode: "merge"`). */
    async function mergeChannel(panel, entry) {
        let slot = panel.channelSlots.find((s) => s && s.visible && s.name === entry.name);
        if (!slot) {
            // Nothing on screen to turn off.
            if (entry.enabled === false) return null;
            slot = panel.channelSlots.find((s) => s && !s.visible)
                || panel.createAdditionalSlot(panel.channelSlots.filter((s) => s.name).map((s) => s.name));
            if (!slot) {
                throw new Error(`every channel slot (${panel.maxChannelSlots}) is in use -- `
                    + `turn one off, or send mode "replace"`);
            }
            // A window, if one was given, is set below; these two flags have to
            // be up before anything yields, or the auto-level setSlotMarker
            // schedules lands afterwards and overwrites it (the same race
            // applySavedChannels documents).
            panel.setSlotMarker(slot.index, entry.name,
                { keepColor: true, enable: true, reveal: true, force: true });
        }
        if (Array.isArray(entry.window)) {
            slot.userRangeChanged = true;
            slot.autoLeveled = true;
        }
        if (entry.color) panel.setSlotColor(slot.index, entry.color, true);
        if (entry.enabled !== undefined && entry.enabled !== Boolean(slot.enabled)) {
            panel.setSlotEnabled(slot.index, entry.enabled);
        }
        if (Array.isArray(entry.window)) {
            await panel.channelList.ensureChannelStats(entry.name).catch(() => {});
            let range = entry.window;
            if (!panel.isHdMode()) {
                const packet = panel.quantWindow(entry.name);
                if (!packet) throw new Error(`no intensity statistics for ${entry.name} yet`);
                range = panel.rawToByteRange(range, packet);
            }
            panel.setSlotRange(slot.index, range, true);
            panel.updateSlotReadout(slot);
        } else if (entry.window === "auto" && slot.enabled) {
            await panel.autoChannel(slot.index, { force: true });
        }
        return slot;
    }

    /**
     * `set_channels` and its two thin wrappers.
     *
     * Through the sidebar's own setters, never around them, so the picture,
     * the sliders and the saved list stay one answer. Persistence is
     * SUSPENDED for the whole change either way -- the setters would otherwise
     * schedule a save apiece -- and, with `persist`, released into exactly one
     * save at the end. Without it the project's channel list on disk does not
     * change: this is somebody looking, the way a figure panel's restore is.
     */
    async function applyChannels(entries, options, call) {
        const panel = sidebar();
        const mode = options.mode || "merge";
        if (mode !== "merge" && mode !== "replace") {
            throw new Error(`mode must be "merge" or "replace", not ${JSON.stringify(mode)}`);
        }
        if (!Array.isArray(entries) || !entries.length) {
            throw new Error("needs a non-empty `channels` list");
        }
        const known = new Set(panel.columns || []);
        const wanted = [];
        const missing = [];
        for (const raw of entries) {
            const name = raw && (raw.name !== undefined ? raw.name : raw.channel);
            if (!known.has(name)) {
                missing.push(String(name));
                continue;
            }
            wanted.push({
                name,
                color: colorHex(raw.color),
                window: windowOf(raw.window !== undefined ? raw.window : raw.range),
                enabled: raw.enabled === undefined ? undefined : Boolean(raw.enabled),
            });
        }
        if (!wanted.length) throw new Error(`this image has no channel named ${missing.join(", ")}`);
        if (missing.length) call.warn(`not in this image, skipped: ${missing.join(", ")}`);

        const persist = Boolean(options.persist);
        panel.suspendPersistence();
        try {
            let touched;
            if (mode === "replace") {
                // The launch path: rebuild the slot list with exactly these, a
                // window honoured when given and the channel auto-levelled when
                // not -- which is what "auto" means on a channel just placed.
                await panel.applyLaunchChannels(wanted.map((entry) => {
                    const row = { name: entry.name };
                    if (entry.color) row.color = entry.color;
                    if (Array.isArray(entry.window)) row.range = entry.window;
                    if (entry.enabled === false) row.enabled = false;
                    return row;
                }), { silent: true });
                touched = panel.channelSlots.filter((slot) => slot && slot.visible && slot.name);
            } else {
                touched = [];
                for (const entry of wanted) {
                    const slot = await mergeChannel(panel, entry);
                    if (slot) touched.push(slot);
                }
            }
            await settleAutoLevels(touched);
        } finally {
            // In a finally for the reason FigureScene.restore gives: a change
            // that throws halfway must not leave the channel list unsaveable.
            panel.resumePersistence();
        }
        if (persist) {
            // Asked for in so many words: the agent means this arrangement
            // to be the project's, so the lease's hold on saves ends here.
            if (lease && lease.suspended) {
                lease.suspended = false;
                panel.resumePersistence();
            }
            panel.scheduleSaveChannels();
        }
        touch();
        return {
            mode, persisted: persist,
            applied: wanted.map((entry) => entry.name), missing,
            channels: describeChannels(panel),
        };
    }

    function channelName(args) {
        const name = args.channel !== undefined ? args.channel : args.name;
        if (!name) throw new Error("needs a `channel`");
        return name;
    }

    // -- layers -------------------------------------------------------------

    function stack() {
        const found = core().layers;
        if (!found) throw unsupported("this viewer has no layer stack");
        return found;
    }

    function layerFor(args) {
        const layers = stack();
        const id = args.layer_id !== undefined ? args.layer_id : args.id;
        if (!layers.has(id)) {
            throw new Error(`no layer ${JSON.stringify(id)} (layers: ${layers.order().join(", ")})`);
        }
        return id;
    }

    function describeLayer(id) {
        return describeLayers().find((layer) => layer.id === id) || null;
    }

    // -- tools --------------------------------------------------------------

    function loader() {
        const found = window.PlexoraToolLoader;
        if (!found || !found.openTool) throw unsupported("this page has no tool loader");
        return found;
    }

    function toolName(args) {
        const name = args.tool !== undefined ? args.tool : args.name;
        if (!name) throw new Error("needs a `tool`");
        return String(name);
    }

    // -- capture ------------------------------------------------------------

    /** Wait (bounded) until every drawn item has its tiles for this view, so a
     *  capture after a move is of the view and not of the blur on the way. */
    async function settleTiles(found, limitMs) {
        const world = found.viewer && found.viewer.world;
        if (!world || !world.getItemCount) return;
        const deadline = Date.now() + (Number(limitMs) >= 0 ? Number(limitMs) : 4000);
        const loaded = () => {
            for (let i = 0; i < world.getItemCount(); i++) {
                const item = world.getItemAt(i);
                if (!item || typeof item.getFullyLoaded !== "function") continue;
                if (item.getOpacity && item.getOpacity() === 0) continue;
                if (!item.getFullyLoaded()) return false;
            }
            return true;
        };
        while (!loaded() && Date.now() < deadline) await pause(100);
    }

    function toPng(canvas) {
        return new Promise((resolve, reject) => {
            try {
                canvas.toBlob((blob) => (blob ? resolve(blob)
                    : reject(new Error("the view could not be encoded as a PNG"))), "image/png");
            } catch (error) {
                reject(error);
            }
        });
    }

    // -- evidence -----------------------------------------------------------

    function evidenceSource(args) {
        const id = args.artifact_id;
        if (id !== undefined && id !== null && id !== "") {
            if (!/^[\w.-]+$/.test(String(id))) throw new Error("that is not an artifact id");
            return url(`agent/v1/captures/${encodeURIComponent(String(id))}`);
        }
        const given = typeof args.url === "string" ? args.url.trim() : "";
        // Same origin only: a scheme or a protocol-relative URL could put any
        // server's image over this viewer under Plexora's name.
        if (!given || /^[a-z][a-z0-9+.-]*:/i.test(given) || given.startsWith("//")) {
            throw new Error("show_evidence needs an artifact_id, or a url on this server");
        }
        return url(given);
    }

    /** An already-resolved same-origin path (the panel's "enlarge"), or null. */
    function resolvedSource(args) {
        const given = typeof args.src === "string" ? args.src.trim() : "";
        if (!given) return null;
        if (!given.startsWith("/") || given.startsWith("//")) {
            throw new Error("show_evidence needs an artifact_id, or a url on this server");
        }
        return given;
    }

    function closeEvidence() {
        if (evidenceDialog) evidenceDialog.close();
    }

    /**
     * A result the agent wants the user to see, over the viewer.
     *
     * A native <dialog> in core's own `plx-dialog` look (main.css, which every
     * page has), built from text nodes so a caption can never be markup. One
     * at a time: a second replaces the first rather than stacking.
     *
     * NON-modal (`show()`, not `showModal()`): the agent keeps working while
     * the user looks, and a modal would lock them out of the viewer -- and out
     * of the agent panel's Pause and Stop -- for as long as it was up. Escape
     * closes it, which a non-modal dialog does not do by itself.
     */
    function showEvidence(args) {
        const src = resolvedSource(args || {}) || evidenceSource(args || {});
        closeEvidence();
        const dialog = document.createElement("dialog");
        dialog.className = "plx-dialog plx-agent-evidence-dialog";
        dialog.style.width = "min(980px, 94vw)";
        const title = document.createElement("h2");
        title.className = "plx-dialog-title";
        title.textContent = args.title ? String(args.title) : "From your agent";
        dialog.appendChild(title);
        if (args.caption) {
            const caption = document.createElement("p");
            caption.className = "plx-confirm-body";
            caption.textContent = String(args.caption);
            dialog.appendChild(caption);
        }
        const image = document.createElement("img");
        image.src = src;
        image.alt = args.caption ? String(args.caption) : "A capture the agent is showing you";
        Object.assign(image.style, {
            display: "block", maxWidth: "100%", maxHeight: "68vh", margin: "0 auto",
            borderRadius: "var(--radius-md)", background: "var(--surface-0)",
        });
        dialog.appendChild(image);
        const actions = document.createElement("div");
        actions.className = "plx-dialog-actions";
        const close = document.createElement("button");
        close.type = "button";
        close.className = "plx-button plx-button-primary";
        close.textContent = "Close";
        close.addEventListener("click", () => dialog.close());
        actions.appendChild(close);
        dialog.appendChild(actions);
        const onKey = (event) => {
            if (event && event.key === "Escape") dialog.close();
        };
        dialog.addEventListener("close", () => {
            document.removeEventListener?.("keydown", onKey);
            dialog.remove();
            if (evidenceDialog === dialog) evidenceDialog = null;
        });
        document.body.appendChild(dialog);
        evidenceDialog = dialog;
        document.addEventListener?.("keydown", onKey);
        if (typeof dialog.show === "function") dialog.show();
        else dialog.setAttribute("open", "");
        return { shown: true, src };
    }

    /** The agent panel, when one is up (views/agentPanel.js). */
    function agentPanel() {
        const panel = window.PlexoraAgentPanel;
        try {
            return panel && typeof panel.isAttached === "function" && panel.isAttached() ? panel : null;
        } catch (error) {
            return null;
        }
    }

    // -- pointing at cells ------------------------------------------------------
    //
    // `highlight_cells`: a ring and a short caption on each named cell, drawn
    // as DOM over the canvas and re-placed on every viewport change, so an
    // agent can say "these cells" and the user sees which. Session-only by
    // construction -- nothing is saved, and it clears itself after `ttl_ms`
    // (or on the next highlight, or on `clear`). Positions come with the
    // command (full-resolution image pixels): the tab never has to look a
    // cell up to point at it.

    let highlight = null;

    function clearHighlight() {
        if (!highlight) return;
        window.clearTimeout(highlight.timer);
        try {
            highlight.osd.removeHandler("update-viewport", highlight.place);
            highlight.osd.removeHandler("animation", highlight.place);
        } catch (error) { /* the viewer is already gone */ }
        highlight.root.remove();
        highlight = null;
    }

    function screenOf(found, x, y) {
        const osd = found.viewer;
        const item = typeof found.referenceItem === "function" ? found.referenceItem()
            : osd.world.getItemAt(0);
        if (!item) return null;
        const scale = 2 ** ((found.config && found.config.extraZoomLevels) || 0);
        const transform = window.PlexoraViewTransform;
        if (transform && typeof transform.imageToScreen === "function") {
            return transform.imageToScreen(osd, item, x * scale, y * scale);
        }
        return osd.viewport.pixelFromPoint(item.imageToViewportCoordinates(x * scale, y * scale,
                                                                           true), true);
    }

    function showHighlight(args) {
        const found = viewer();
        const osd = found.viewer;
        if (args.clear !== false) clearHighlight();
        const cells = (Array.isArray(args.cells) ? args.cells : [])
            .filter((cell) => Number.isFinite(Number(cell.x)) && Number.isFinite(Number(cell.y)))
            .slice(0, 200);
        if (!cells.length) return { shown: 0 };
        const root = document.createElement("div");
        root.className = "plx-agent-highlight";
        root.setAttribute("aria-hidden", "true");
        Object.assign(root.style, { position: "absolute", inset: "0", pointerEvents: "none",
                                    zIndex: "5", overflow: "hidden" });
        const marks = cells.map((cell) => {
            const mark = document.createElement("div");
            const colour = /^#[0-9a-f]{6}$/i.test(cell.color || "") ? cell.color : "#ff3df2";
            Object.assign(mark.style, { position: "absolute", width: "26px", height: "26px",
                                        marginLeft: "-13px", marginTop: "-13px",
                                        borderRadius: "50%", border: `2px solid ${colour}`,
                                        boxShadow: "0 0 0 1px rgba(0,0,0,0.6)" });
            root.appendChild(mark);
            let label = null;
            if (cell.caption) {
                label = document.createElement("div");
                label.textContent = String(cell.caption).slice(0, 40);
                Object.assign(label.style, { position: "absolute", font: "11px/1.3 system-ui, sans-serif",
                                             color: "#fff", background: "rgba(0,0,0,0.7)",
                                             padding: "1px 5px", borderRadius: "3px",
                                             whiteSpace: "nowrap", transform: "translate(16px, -8px)" });
                root.appendChild(label);
            }
            return { x: Number(cell.x), y: Number(cell.y), mark, label };
        });
        const place = () => {
            for (const entry of marks) {
                const point = screenOf(found, entry.x, entry.y);
                if (!point) continue;
                entry.mark.style.left = `${point.x}px`;
                entry.mark.style.top = `${point.y}px`;
                if (entry.label) {
                    entry.label.style.left = `${point.x}px`;
                    entry.label.style.top = `${point.y}px`;
                }
            }
        };
        (osd.element || osd.container).appendChild(root);
        osd.addHandler("update-viewport", place);
        osd.addHandler("animation", place);
        const ttl = Math.max(1000, Math.min(600000, Number(args.ttl_ms) || 60000));
        highlight = { root, osd, place, timer: window.setTimeout(clearHighlight, ttl) };
        place();
        return { shown: marks.length, ttl_ms: ttl };
    }

    // -- outlining regions -------------------------------------------------------
    //
    // `show_shapes`: polygons (GeoJSON) or boxes in full-resolution image
    // pixels, drawn as one SVG over the canvas and re-projected on every
    // viewport change -- a QC candidate's outline, a grid square. Session
    // only, like a highlight: it clears itself after `ttl_ms`, on the next
    // `show_shapes` (unless `clear: false`), and when the viewer is restored.

    let shapes = null;

    function clearShapes() {
        if (!shapes) return;
        window.clearTimeout(shapes.timer);
        try {
            shapes.osd.removeHandler("update-viewport", shapes.place);
            shapes.osd.removeHandler("animation", shapes.place);
        } catch (error) { /* the viewer is already gone */ }
        shapes.root.remove();
        shapes = null;
    }

    function ringsOf(shape) {
        if (shape.bounds) {
            const b = shape.bounds;
            return [[[b.x, b.y], [b.x + b.width, b.y], [b.x + b.width, b.y + b.height],
                     [b.x, b.y + b.height], [b.x, b.y]]];
        }
        const geometry = shape.geometry || {};
        const polygons = geometry.type === "MultiPolygon" ? geometry.coordinates || []
            : [geometry.coordinates || []];
        const rings = [];
        for (const polygon of polygons) {
            for (const ring of polygon || []) rings.push(ring);
        }
        return rings;
    }

    function showShapes(args) {
        const found = viewer();
        const osd = found.viewer;
        const list = (Array.isArray(args.shapes) ? args.shapes : []).slice(0, 32);
        if (args.clear !== false) clearShapes();
        if (!list.length) return { shown: 0 };
        const ns = "http://www.w3.org/2000/svg";
        const root = document.createElementNS(ns, "svg");
        root.setAttribute("class", "plx-agent-shapes");
        root.setAttribute("aria-hidden", "true");
        Object.assign(root.style, { position: "absolute", left: "0", top: "0", width: "100%",
                                    height: "100%", pointerEvents: "none", zIndex: "5",
                                    overflow: "hidden" });
        const drawn = list.map((shape) => {
            const colour = /^#[0-9a-f]{6}$/i.test(shape.color || "") ? shape.color : "#ff3df2";
            const path = document.createElementNS(ns, "path");
            path.setAttribute("fill", colour);
            path.setAttribute("fill-opacity", String(Math.max(0, Math.min(0.6, Number(shape.fill_alpha) || 0))));
            path.setAttribute("fill-rule", "evenodd");
            path.setAttribute("stroke", colour);
            path.setAttribute("stroke-width", "2");
            if (shape.dash) path.setAttribute("stroke-dasharray", "8 5");
            root.appendChild(path);
            let label = null;
            if (shape.label) {
                label = document.createElementNS(ns, "text");
                label.textContent = String(shape.label).slice(0, 24);
                label.setAttribute("fill", "#ffffff");
                label.setAttribute("stroke", "#000000");
                label.setAttribute("stroke-width", "3");
                label.setAttribute("paint-order", "stroke");
                label.setAttribute("font-size", "12");
                label.setAttribute("font-family", "system-ui, sans-serif");
                root.appendChild(label);
            }
            return { rings: ringsOf(shape), path, label };
        });
        const place = () => {
            for (const entry of drawn) {
                let d = "";
                let top = null;
                for (const ring of entry.rings) {
                    const points = [];
                    for (const vertex of ring) {
                        const point = screenOf(found, Number(vertex[0]), Number(vertex[1]));
                        if (!point) continue;
                        points.push(`${point.x.toFixed(1)},${point.y.toFixed(1)}`);
                        if (!top || point.y < top.y) top = point;
                    }
                    if (points.length > 1) d += `M${points.join("L")}Z`;
                }
                entry.path.setAttribute("d", d);
                if (entry.label && top) {
                    entry.label.setAttribute("x", String(top.x));
                    entry.label.setAttribute("y", String(Math.max(12, top.y - 6)));
                }
            }
        };
        (osd.element || osd.container).appendChild(root);
        osd.addHandler("update-viewport", place);
        osd.addHandler("animation", place);
        const ttl = Math.max(1000, Math.min(600000, Number(args.ttl_ms) || 120000));
        shapes = { root, osd, place, timer: window.setTimeout(clearShapes, ttl) };
        place();
        return { shown: drawn.length, ttl_ms: ttl };
    }

    // -- the lease ------------------------------------------------------------

    /** Snapshot what the user had, once, before the agent's first change. */
    function takeLease() {
        if (lease) return lease;
        const plexora = core();
        const attempt = (fn, fallback) => {
            try {
                return fn();
            } catch (error) {
                return fallback;
            }
        };
        const manager = plexora.seaDragonViewer && plexora.seaDragonViewer.viewerManagerVMain;
        lease = {
            taken_at: new Date().toISOString(),
            // A flat RGB image has no sidebar, and so no channels to give back.
            slots: attempt(() => sidebar().snapshotSlots(), null),
            // The per-channel memory the sidebar keeps beside the slots (each
            // marker's remembered window): an agent's inspection windows land
            // there too, and without this a marker picked after the session
            // reopened at the agent's window -- or, read in the other tile
            // domain, collapsed at the right end of its slider.
            memory: attempt(() => sidebar().snapshotChannelMemory(), null),
            // Nothing the agent shows is saved to the project while it holds
            // the view; `restore` releases this.
            suspended: attempt(() => { sidebar().suspendPersistence(); return true; }, false),
            hd: attempt(() => (manager && typeof manager.isHdMode === "function"
                ? Boolean(manager.isHdMode()) : null), null),
            cellMode: plexora.viewerControls ? (plexora.viewerControls.mode || null) : null,
            view: attempt(() => box(scene().currentViewport(viewer())), null),
            plugins: attempt(() => pluginStates(), {}),
            openedTools: [],
            channelsTouched: false,
        };
        return lease;
    }

    /**
     * Give the viewer back as the lease found it (see the header). Every step
     * is tried on its own, so one that fails (a tool that will not close) does
     * not keep the rest from happening; what failed is in `errors`. The lease
     * is dropped FIRST, so a second restore racing this one (the panel's local
     * one and the server's `restore_viewer`) is a no-op, not a second rebuild.
     */
    async function restore(options = {}) {
        const reason = options && options.reason ? String(options.reason) : null;
        const held = lease;
        lease = null;
        const restored = [];
        const errors = [];
        const step = async (name, fn) => {
            try {
                if ((await fn()) !== false) restored.push(name);
            } catch (error) {
                errors.push(`${name}: ${messageOf(error)}`);
            }
        };
        await step("highlights", () => {
            clearHighlight();
            clearShapes();
            closeEvidence();
        });
        if (!held) return { had_lease: false, reason, restored, errors };
        const plexora = core();
        // "Continue in background": the user takes the viewer as it is now.
        // The lease is released -- saving resumes, plugins drop their
        // previews -- but nothing on screen is put back.
        const keep = reason === "detached";
        const manager = plexora.seaDragonViewer && plexora.seaDragonViewer.viewerManagerVMain;
        // HD first: the leased windows are raw units, converted into whichever
        // domain is showing when they are applied -- so that has to be the
        // final one.
        await step("hd_mode", async () => {
            if (keep || held.hd === null || !manager || typeof manager.setHdMode !== "function") return false;
            if (Boolean(manager.isHdMode && manager.isHdMode()) === held.hd) return false;
            const checkbox = document.getElementById?.("viewer_controls_hd");
            if (checkbox) checkbox.checked = held.hd;
            await untilDrawn(manager.setHdMode(held.hd));
            return true;
        });
        await step("channels", async () => {
            // Rebuilt only when a channel command ran: a rebuild re-draws
            // every channel, and an agent that only moved the view has
            // nothing here to give back.
            if (keep || !held.channelsTouched || !Array.isArray(held.slots) || !held.slots.length) return false;
            const panel = sidebar();
            // applyChannels' replace branch, without the save: the user's own
            // arrangement was never unsaved, so nothing is written.
            panel.suspendPersistence();
            try {
                await panel.applyLaunchChannels(held.slots.map((slot) => {
                    const row = { name: slot.name, color: slot.colorHex, enabled: Boolean(slot.enabled) };
                    if (Array.isArray(slot.range)) row.range = [slot.range[0], slot.range[1]];
                    return row;
                }), { silent: true });
                await settleAutoLevels(panel.channelSlots.filter((slot) => slot && slot.visible && slot.name));
            } finally {
                panel.resumePersistence();
            }
            return true;
        });
        await step("channel_memory", () => {
            if (keep || !held.memory) return false;
            sidebar().restoreChannelMemory(held.memory);
            return true;
        });
        await step("persistence", () => {
            if (!held.suspended) return false;
            sidebar().resumePersistence();
            return true;
        });
        await step("cell_mode", async () => {
            const controls = plexora.viewerControls;
            if (keep || !held.cellMode || !controls || !controls.selectMode) return false;
            if (controls.mode === held.cellMode) return false;
            await controls.selectMode(held.cellMode);
            return true;
        });
        await step("tools", () => {
            const tools = window.PlexoraToolLoader;
            if (keep || !held.openedTools.length || !tools || !tools.closeTool) return false;
            const loaded = tools.loadedTools ? tools.loadedTools() : held.openedTools;
            held.openedTools.filter((name) => loaded.includes(name))
                .forEach((name) => tools.closeTool(name));
            return true;
        });
        await step("plugins", async () => {
            const waits = [];
            window.dispatchEvent(new CustomEvent("plexora:agent-restore", { detail: {
                plugins: held.plugins || {},
                reason,
                wait(promise) { waits.push(Promise.resolve(promise)); },
            } }));
            const settled = await Promise.allSettled(waits);
            settled.filter((entry) => entry.status === "rejected")
                .forEach((entry) => errors.push(`plugins: ${messageOf(entry.reason)}`));
        });
        await step("viewport", () => {
            if (keep || !held.view) return false;
            scene().fitRegion(viewer(), held.view);
            return true;
        });
        touch();
        return { had_lease: true, reason, restored, errors };
    }

    // -- the command table ----------------------------------------------------
    //
    // type -> async (arguments, call) => result. A thrown Error is a
    // `rejected` ack carrying its message; one made by `unsupported()` is an
    // `unsupported` ack. What each returns is the ack's `result`.

    const HANDLERS = {
        async get_state() {
            return getState();
        },

        async open_project(args, call) {
            const project = String(args.project || "").trim();
            if (!project) throw new Error("needs a `project`");
            const tool = args.tool ? String(args.tool) : "";
            if (project === datasource()) {
                // Already here: nothing to navigate, at most a tool to open.
                const opened = tool ? await HANDLERS.open_tool({ tool }, call) : null;
                return Object.assign({ navigating: false, already_open: true }, opened || {});
            }
            let href = url(encodeURIComponent(project));
            if (tool) href += "?tool=" + encodeURIComponent(tool);
            // `carry`: the arrangement goes with the user, exactly as walking
            // a dataset with the arrows does (services/carryOver.js).
            if (args.carry && window.PlexoraCarryOver && window.PlexoraCarryOver.stash) {
                window.PlexoraCarryOver.stash(project);
            }
            // Acknowledged FIRST: the page that could say "done" is about to go.
            await call.ack("done", { navigating: true, project, url: href });
            await navigateTo(href, () => {
                // Through the router, which hands a different project to the
                // browser as a full navigation -- see datasetNav.go.
                if (window.PlexoraRouter && window.PlexoraRouter.go) window.PlexoraRouter.go(href);
                else window.location.href = href;
            });
            return undefined;
        },

        async set_channels(args, call) {
            return applyChannels(args.channels,
                { mode: args.mode || "merge", persist: args.persist }, call);
        },

        async set_channel_color(args, call) {
            return applyChannels([{ name: channelName(args), color: args.color }],
                { mode: "merge", persist: args.persist }, call);
        },

        async set_contrast(args, call) {
            const display = args.window !== undefined ? args.window : args.range;
            if (display === undefined || display === null) {
                throw new Error('needs a `window` ([low, high] in raw units, or "auto")');
            }
            return applyChannels([{ name: channelName(args), window: display }],
                { mode: "merge", persist: args.persist }, call);
        },

        async set_layer_visibility(args) {
            const id = layerFor(args);
            stack().setVisible(id, Boolean(args.visible));
            touch();
            return { layer: describeLayer(id) };
        },

        async set_layer_opacity(args) {
            const id = layerFor(args);
            const opacity = Number(args.opacity);
            if (!Number.isFinite(opacity) || opacity < 0 || opacity > 1) {
                throw new Error("opacity must be a number from 0 to 1");
            }
            stack().setOpacity(id, opacity);
            touch();
            return { layer: describeLayer(id) };
        },

        async reorder_layers(args, call) {
            const layers = stack();
            if (!Array.isArray(args.ids) || !args.ids.length) throw new Error("needs `ids`, bottom first");
            const unknown = args.ids.filter((id) => !layers.has(id));
            if (unknown.length) call.warn(`no such layers, ignored: ${unknown.join(", ")}`);
            layers.setOrder(args.ids.filter((id) => layers.has(id)));
            touch();
            return { order: layers.order(), layers: describeLayers() };
        },

        async pan_to(args) {
            scene().panTo(viewer(), args.x, args.y);
            touch();
            return viewState();
        },

        async zoom_to(args) {
            scene().zoomTo(viewer(), args.scale);
            touch();
            return viewState();
        },

        async fit_region(args) {
            scene().fitRegion(viewer(), args);
            touch();
            return viewState();
        },

        async focus_cell(args) {
            const x = Number(args.x);
            const y = Number(args.y);
            if (!Number.isFinite(x) || !Number.isFinite(y)) {
                throw new Error("needs the cell's `x` and `y` in image pixels");
            }
            const width = Number(args.width) > 0 ? Number(args.width) : 150;
            scene().fitRegion(viewer(), { x: x - width / 2, y: y - width / 2, width, height: width });
            touch();
            return Object.assign({ cell_id: args.cell_id === undefined ? null : args.cell_id },
                viewState());
        },

        async focus_roi(args) {
            // The ROI tool can SELECT the region as well as frame it, so it is
            // asked first; a box is the fallback when no plugin answers.
            const answer = await offer("focus_roi", args);
            if (answer) {
                touch();
                return Object.assign({ handled_by: answer.by }, answer.result || {}, viewState());
            }
            if (["x", "y", "width", "height"].every((key) => Number.isFinite(Number(args[key])))) {
                scene().fitRegion(viewer(), args);
                touch();
                return Object.assign({ handled_by: null, selected: false }, viewState());
            }
            throw unsupported("no plugin handled it, and no box was given to fall back on");
        },

        async set_hd_mode(args, call) {
            const manager = core().seaDragonViewer && core().seaDragonViewer.viewerManagerVMain;
            if (!manager || typeof manager.setHdMode !== "function") {
                throw unsupported("this viewer has no HD mode");
            }
            const enabled = Boolean(args.enabled);
            const was = Boolean(manager.isHdMode && manager.isHdMode());
            if (was !== enabled) {
                // The checkbox shows the new mode; the swap itself is awaited
                // here rather than left to the checkbox's change handler (which
                // only calls setHdMode and returns before the tiles are rebuilt),
                // so the acknowledgement means the swap has finished.
                const box = document.getElementById?.("viewer_controls_hd");
                if (box) box.checked = enabled;
                await untilDrawn(manager.setHdMode(enabled), call);
            }
            touch();
            return { hd_mode: Boolean(manager.isHdMode && manager.isHdMode()), changed: was !== enabled };
        },

        async show_shapes(args) {
            if (args.clear && !(args.shapes || []).length) {
                clearShapes();
                return { shown: 0, cleared: true };
            }
            return showShapes(args);
        },

        async highlight_cells(args) {
            if (args.clear && !(args.cells || []).length) {
                clearHighlight();
                return { shown: 0, cleared: true };
            }
            return showHighlight(args);
        },

        async preview_gate(args) {
            const answer = await offer("preview_gate", args);
            if (!answer) throw unsupported("no plugin handled it (is Thresholding open?)");
            touch();
            return Object.assign({ handled_by: answer.by }, answer.result || {});
        },

        async set_active_marker(args) {
            const answer = await offer("set_active_marker", args);
            if (!answer) throw unsupported("no plugin handled it (is Thresholding open?)");
            touch();
            return Object.assign({ handled_by: answer.by }, answer.result || {});
        },

        async open_tool(args) {
            const tools = loader();
            const name = toolName(args);
            if (!availableTools().includes(name)) {
                throw new Error(`no tool "${name}" on this page (tools: ${availableTools().join(", ")})`);
            }
            const wasOpen = Boolean(tools.isToolOpen && tools.isToolOpen(name));
            const outcome = await tools.openTool(name, null, { quiet: true });
            if (outcome && outcome.needs && outcome.needs.length && args.ask) {
                // The tool waits on a question only the user can answer. Put
                // the tab's own prompt to them and acknowledge at once: the
                // prompt waits on a person, a command must not. The tool opens
                // when they answer.
                tools.openTool(name, null, { quiet: false }).then((later) => {
                    if (later && later.loaded && lease && !wasOpen
                        && !lease.openedTools.includes(name)) lease.openedTools.push(name);
                }).catch(() => {});
                touch();
                return { tool: name, open: false, asked: outcome.needs, tools_open: openTools() };
            }
            if (!outcome || !outcome.loaded) {
                const hint = outcome && outcome.needs && outcome.needs.length
                    ? " (the user must answer it first: send open_tool with ask to show them the prompt)"
                    : "";
                throw new Error(`${name} did not open: ${(outcome && outcome.skipped) || "no reason given"}${hint}`);
            }
            // Only a tool the agent opened is closed again on restore; one the
            // user already had open stays.
            if (lease && !wasOpen && !lease.openedTools.includes(name)) lease.openedTools.push(name);
            touch();
            return { tool: name, open: Boolean(tools.isToolOpen && tools.isToolOpen(name)),
                     tools_open: openTools() };
        },

        async close_tool(args) {
            const tools = loader();
            const name = toolName(args);
            if (!(tools.loadedTools ? tools.loadedTools() : []).includes(name)) {
                return { tool: name, open: false, was_open: false };
            }
            tools.closeTool(name);
            touch();
            return { tool: name, open: Boolean(tools.isToolOpen && tools.isToolOpen(name)),
                     was_open: true, tools_open: openTools() };
        },

        async set_cell_render_mode(args) {
            const controls = core().viewerControls;
            if (!controls || !controls.selectMode) throw unsupported("this image has no cell overlay controls");
            const mode = String(args.mode || "");
            await controls.selectMode(mode);
            if (controls.mode !== mode) {
                const offered = controls.offeredModes ? controls.offeredModes() : {};
                const names = Object.keys(offered).filter((key) => offered[key]);
                throw new Error(`"${mode}" is not available here right now `
                    + `(offered: ${names.join(", ") || "none"})`);
            }
            touch();
            return { cell_mode: controls.mode };
        },

        async capture_view(args, call) {
            const found = viewer();
            if (typeof found.renderCurrentViewCanvas !== "function") {
                throw unsupported("this viewer cannot render its view to an image");
            }
            await settleTiles(found, args.settle_ms);
            const canvas = found.renderCurrentViewCanvas();
            if (!canvas) throw new Error("nothing has been drawn yet");
            const png = await toPng(canvas);
            const state = getState();
            const query = new URLSearchParams({ project: datasource(), command_id: call.command.command_id });
            const response = await fetch(url(`${API}/${encodeURIComponent(session)}/captures?${query}`), {
                method: "POST",
                credentials: "same-origin",
                headers: { "Content-Type": "image/png" },
                body: png,
            });
            const payload = await json(response);
            if (!response.ok || !payload.success) {
                throw new Error(payload.error || `the capture upload failed (HTTP ${response.status})`);
            }
            return { artifact: payload.artifact, state };
        },

        /** The agent panel when one is up (it shows the latest piece in
         *  place), the dialog otherwise. The ack says which. */
        async show_evidence(args) {
            const panel = agentPanel();
            if (panel) {
                const src = evidenceSource(args);
                panel.showEvidence({
                    src,
                    caption: args.caption ? String(args.caption) : "",
                    title: args.title ? String(args.title) : "",
                    subject: args.subject ? String(args.subject) : "",
                    kind: args.kind ? String(args.kind) : "",
                });
                return { shown: true, src, via: "panel" };
            }
            return Object.assign(showEvidence(args), { via: "dialog" });
        },

        /** Give the viewer back (see the header). Sent by the server's
         *  teardown; a tab with no lease acknowledges `had_lease: false`. */
        async restore_viewer(args) {
            return restore({ reason: args.reason });
        },
    };

    // -- boot ----------------------------------------------------------------

    function boot() {
        if (!datasource()) return;
        // After the boot, succeeded or not: a viewer whose image failed is
        // still a tab an agent may want to ask about.
        Promise.resolve(window.__plexoraReady).catch(() => {}).then(() => start());
    }

    if (typeof window.addEventListener === "function") {
        window.addEventListener("pagehide", () => {
            unloaded = true;
            running = false;
            if (!navigating) leave(intendedTarget);
        });
        window.addEventListener("pageshow", (event) => {
            // Back from the back-forward cache: the session was left, so this
            // is a fresh registration under the same id.
            if (!event || !event.persisted) return;
            unloaded = false;
            navigating = false;
            intendedTarget = null;
            session = null;
            start();
        });
    }

    // Deferred, like main.js and after it in document order is NOT assumed:
    // `__plexoraReady` is created by main.js, which runs after this file. So
    // wait for DOMContentLoaded (every deferred script has run by then) unless
    // it has plainly already happened.
    if (typeof document !== "undefined") {
        if (document.readyState === "complete"
            || (document.readyState === "interactive" && window.__plexoraReady)) {
            boot();
        } else {
            document.addEventListener("DOMContentLoaded", boot, { once: true });
        }
    }

    return {
        /** Something user-visible changed; bump the revision. */
        touch,
        revision: () => revision,
        sessionId: () => session,
        start,
        stop,
        getState,
        /** Give the viewer back as the agent found it: `{reason}` ->
         *  `{had_lease, restored, errors}`. The agent panel's local fallback. */
        restore,
        /** Open evidence in the (non-modal) dialog: `{src}` (a resolved path,
         *  as the panel holds it), or `{artifact_id}` / `{url}`, with
         *  `caption` and `title`. The panel's "enlarge". */
        showEvidence,
        /** Whether a lease is held (the probe's, and the panel's, question). */
        hasLease: () => Boolean(lease),
        /** The command types this page runs, as registered. */
        commands: () => Object.keys(HANDLERS),
        //: Test seams: the probe drives these against a stand-in page.
        _pollOnce: pollOnce,
        /** Mark the loop as running WITHOUT starting it, so a probe can drive
         *  `_pollOnce` itself and not race the real loop for each answer. */
        _arm: () => { generation += 1; running = true; stopped = false; },
        _run: runCommand,
        _handleEvent: handleEvent,
        _register: register,
        _status: () => ({ attached, running, stopped, navigating, cursor: Object.assign({}, cursor) }),
    };
})();
