/**
 * PlexoraWebRemote -- the website's connections to servers, held by the page
 * (docs/internal/WEB_REMOTE_RELAY.md).
 *
 * Python on the website cannot open a connection of its own, so its
 * `remote_sessions` asks this page (server/models/web_remote.py), at
 *
 *     /__plexora/remote/v1/sessions            GET (list), POST (start)
 *     /__plexora/remote/v1/sessions/<id>       GET, DELETE (disconnect)
 *     /__plexora/remote/v1/sessions/<id>/answer  POST {id, answer}
 *     /__plexora/remote/v1/files/<name>          POST {op, path, start, end}
 *
 * and registers each node a connection starts at `/__plexora/relay/<room>`,
 * which this page answers through the room (services/relayRoom.js). The
 * service worker hands both prefixes here (workers/backendRelay.js,
 * services/backend.js), whichever frame or worker asked.
 *
 * A connection, start to finish:
 *  1. the room is created on the relay (the page's socket is its first), with
 *     a password and an encryption key made here and kept in this browser;
 *  2. SSH, in the page, through the relay (services/../ssh/sshEntry.js): the
 *     server's key is checked against the ones this browser has accepted, and
 *     every question it asks -- a password, a Duo choice -- is put to the
 *     dialog through Python's status, answered through `answer`;
 *  3. the start script Python built runs, the relay file on its stdin; it
 *     starts the node detached and shows its log;
 *  4. the node joins the room and says hello: connected. SSH is closed; it
 *     is not needed again.
 * After that the connection is the room alone, and a room reconnects by
 * itself: a relay restart, a network blip or a reload of this tab costs a
 * second, never a sign-in. Only Disconnect, the node's own idle limit or its
 * job's time limit ends it.
 *
 * A files-only server (an SFTP server where nothing of ours runs: HMS O2's
 * transfer node) has no node to hand the connection to, so its SSH connection
 * is the connection: signed in as above, its SFTP side opened, and held. The
 * website's Python reads files through it at `files/<name>`, as the desktop's
 * reads them through its system ssh (server/utils/sftp.py). Nothing on the
 * server can resume a dropped SSH connection, so when one drops -- a relay
 * restart, a network change, a reload of this tab -- the page signs in again
 * by itself: the dialog comes back for whatever the server asks (`attention`,
 * services/connectionModal.js), and reads wait for it meanwhile.
 *
 *   configure({relay, bundle, licence})   the relay's wss:// address, the SSH
 *                                         bundle's URL, a licence getter
 *   answer(request) -> Promise<Response>  the two prefixes above
 *   handles(pathname) -> boolean
 *   takeAttention() -> [name]             connections signing in again by
 *                                         themselves since last asked (also
 *                                         the window event `plexora-remote-attention`)
 *
 * Classic script, `window.PlexoraWebRemote`, in the site's host page
 * (services/shell.js), so a connection outlives the app frame's pages.
 */
(function (global) {
    "use strict";

    const API = "/__plexora/remote/v1";
    const RELAY = "/__plexora/relay/";
    const ROOMS_KEY = "plexora.webRemote.rooms";
    const HOSTS_KEY = "plexora.webRemote.knownHosts";
    const ROOM_PLACEHOLDER = "__PLEXORA_ROOM__";
    const KEEP_LINES = 400;
    //: How long a node may be absent from its room before the connection is
    //: called ended: past a relay restart and a node's own reconnect.
    const GONE_MS = 120000;
    //: How long a reopened tab waits for a stored room's node to say hello.
    const REATTACH_MS = 60000;
    //: How long a read of a files-only server waits for its connection to come
    //: back -- a relay restart, then whatever sign-in the server asks for --
    //: before it is answered "not connected".
    const FILES_WAIT_MS = 300000;
    //: A files connection that drops again this soon after signing in again is
    //: not signed in a third time unasked: something beyond a restart is wrong.
    const FLAPPING_MS = 60000;
    //: Signing in again after a drop: tries while the way to the server is
    //: down (a relay restarting), this far apart times the attempt.
    const RETRIES = 6;
    const RETRY_MS = 5000;
    //: This tab's files-only connections, so a reload signs in to them again.
    const FILES_KEY = "plexora.webRemote.files";
    const OPENING = ["preparing_compute", "connecting", "authenticating", "mounting_data",
                     "installing", "waiting_for_job", "tunneling", "waiting_for_app"];

    let relayUrl = "wss://relay.plexoraapp.com";
    let bundleUrl = null;
    let licenceOf = () => null;
    const sessions = new Map();
    const needed = new Set();
    let attached = false;
    let bundle = null;

    function configure(options = {}) {
        if (options.relay) relayUrl = String(options.relay).replace(/\/+$/, "");
        if (options.bundle) bundleUrl = options.bundle;
        if (typeof options.licence === "function") licenceOf = options.licence;
        reattach();
    }

    // -- storage (this browser only; never synced, never sent) ---------------

    function read(key) {
        try {
            return JSON.parse(global.localStorage.getItem(key) || "{}") || {};
        } catch (error) {
            return {};
        }
    }

    function write(key, value) {
        try {
            global.localStorage.setItem(key, JSON.stringify(value));
        } catch (error) { /* private window: this tab only */ }
    }

    function keepRoom(room, record) {
        const rooms = read(ROOMS_KEY);
        rooms[room] = record;
        write(ROOMS_KEY, rooms);
    }

    function dropRoom(room) {
        const rooms = read(ROOMS_KEY);
        delete rooms[room];
        write(ROOMS_KEY, rooms);
    }

    // -- one connection ------------------------------------------------------

    const secret = (bytes) => global.PlexoraRelayRoom.newSecret(bytes);

    function view(session, count = 25) {
        return {
            id: session.id,
            name: session.name,
            kind: session.kind,
            state: session.state,
            detail: session.detail || "",
            lines: session.lines.slice(-Math.max(0, count)),
            prompt: session.prompt ? { id: session.prompt.id, text: session.prompt.text } : null,
            error: session.error,
            ended_by_script: Boolean(session.endedByScript),
            room: session.room,
            node: session.node,
            // A files-only connection's home folder, kept while it signs in
            // again, so Python keeps its reads waiting rather than failing.
            files: session.kind === "files" && session.home ? { home: session.home } : null,
            started_at: session.startedAt,
            job_started_at: session.jobStartedAt,
        };
    }

    function line(session, text) {
        session.lines.push(String(text));
        if (session.lines.length > KEEP_LINES) session.lines.splice(0, session.lines.length - KEEP_LINES);
    }

    function setState(session, state, detail = "") {
        session.state = state;
        session.detail = detail;
    }

    const ended = (session) => session.state === "failed" || session.state === "exited";

    function fail(session, message, { byScript = false } = {}) {
        if (["connected", "failed", "exited"].includes(session.state)) return;
        // A question nobody will now answer: its sign-in gives up.
        if (session.answer) session.answer(null);
        session.error = message;
        session.endedByScript = byScript;
        setState(session, "failed");
        finishSsh(session);
        if (session.roomClient) session.roomClient.close();
        if (session.room) dropRoom(session.room);
        if (session.kind === "files") forgetFiles(session);
    }

    function finishSsh(session) {
        clearTimeout(session.timer);
        const ssh = session.ssh;
        const sftp = session.sftp;
        session.ssh = null;
        session.sftp = null;
        if (sftp) sftp.close();
        if (session.proc) session.proc.close();
        session.proc = null;
        if (ssh) ssh.close();
    }

    function ask(session, text, secretAnswer) {
        return new Promise((resolve) => {
            const id = secret(6);
            const before = session.state;
            session.prompt = { id, text, secret: secretAnswer };
            setState(session, "authenticating");
            session.answer = (value) => {
                session.prompt = null;
                session.answer = null;
                setState(session, before === "authenticating" ? "connecting" : before);
                resolve(value);
            };
        });
    }

    async function hostKey(session, host, port, shown) {
        const known = read(HOSTS_KEY);
        const id = `${host}:${port}`;
        const had = known[id];
        if (had && had.fingerprint === shown.fingerprint) return true;
        if (had) {
            throw new Error(`The key ${host} presented has changed since this browser last connected `
                            + `(${had.fingerprint}, now ${shown.fingerprint}). Someone may be `
                            + "intercepting the connection, or the server was reinstalled. Check "
                            + "with the server's administrators before connecting.");
        }
        const answer = await ask(session,
            `The authenticity of host '${host}' can't be established.\n`
            + `${shown.algorithm} key fingerprint is ${shown.fingerprint}.\n`
            + "Are you sure you want to continue connecting (yes/no)?", false);
        if (!/^y(es)?$/i.test(String(answer || "").trim())) return false;
        known[id] = { algorithm: shown.algorithm, fingerprint: shown.fingerprint, at: Date.now() };
        write(HOSTS_KEY, known);
        line(session, `Accepted the key of ${host} (${shown.fingerprint}).`);
        return true;
    }

    async function interactive(session, request) {
        if (request.name) line(session, request.name);
        if (request.instruction) line(session, request.instruction);
        const answers = [];
        for (const item of request.prompts || []) {
            const text = [request.instruction, item.prompt].filter(Boolean).join("\n").trim();
            answers.push(await ask(session, text || item.prompt, !item.echo));
        }
        return answers;
    }

    function loadBundle() {
        if (global.PlexoraSshLib) return Promise.resolve(global.PlexoraSshLib);
        if (bundle) return bundle;
        bundle = new Promise((resolve, reject) => {
            const script = global.document.createElement("script");
            script.src = bundleUrl || "/client/dist/ssh_bundle.js";
            script.onload = () => (global.PlexoraSshLib ? resolve(global.PlexoraSshLib)
                : reject(new Error("The SSH client did not load.")));
            script.onerror = () => reject(new Error("The SSH client could not be downloaded."));
            global.document.head.appendChild(script);
        });
        bundle.catch(() => { bundle = null; });
        return bundle;
    }

    function waitFor(test, ms) {
        return new Promise((resolve) => {
            const started = Date.now();
            const tick = () => {
                if (test()) return resolve(true);
                if (Date.now() - started > ms) return resolve(false);
                setTimeout(tick, 100);
            };
            tick();
        });
    }

    //: The room's node said hello: the connection is up, SSH is done.
    function connected(session, hello) {
        if (session.state === "connected") {
            session.node = hello;
            return;
        }
        if (["failed", "exited"].includes(session.state)) return;
        session.node = hello;
        if (session.plan && session.plan.queued && !session.jobStartedAt) session.jobStartedAt = Date.now();
        setState(session, "connected");
        session.error = null;
        line(session, `The data node is up${hello.hostname ? ` on ${hello.hostname}` : ""}.`);
        finishSsh(session);
    }

    function watchRoom(session) {
        let absentSince = null;
        session.roomClient.on((state) => {
            if (state.hello && (state.node || session.state !== "connected")) connected(session, state.hello);
            if (state.error) {
                if (session.state === "connected") {
                    session.error = state.error;
                    setState(session, "exited");
                    dropRoom(session.room);
                } else {
                    fail(session, state.error);
                }
                return;
            }
            // The node gone for longer than any restart takes: its idle limit,
            // its job's time limit, or Disconnect from another tab.
            if (session.state === "connected" && state.socket === "open" && !state.node) {
                absentSince = absentSince || Date.now();
                clearTimeout(session.goneTimer);
                session.goneTimer = setTimeout(() => {
                    const now = session.roomClient.state();
                    if (session.state === "connected" && now.socket === "open" && !now.node) {
                        session.error = "The data node stopped (its job may have reached its time limit).";
                        setState(session, "exited");
                        session.roomClient.close();
                        dropRoom(session.room);
                    }
                }, GONE_MS);
            } else if (state.node) {
                absentSince = null;
                clearTimeout(session.goneTimer);
            }
        });
    }

    function openRoom(session, { auth, key }) {
        session.roomClient = global.PlexoraRelayRoom.join({
            relay: session.relay, room: session.room, auth, key, licence: licenceOf(),
            log: (text) => line(session, text),
        });
        watchRoom(session);
    }

    //: SSH to the plan's server through the relay, every question it asks put
    //: to the dialog. The connection, or an Error saying why not (`transport`
    //: when the way there failed rather than the sign-in).
    async function signIn(session) {
        const plan = session.plan;
        setState(session, "connecting", `Signing in to ${plan.host}…`);
        const lib = await loadBundle();
        const licence = licenceOf();
        const protocols = ["plexora-relay.v1", ...(licence ? [`l.${licence}`] : [])];
        const port = plan.port || 22;
        return lib.open({
            url: `${session.relay}/v1/ssh?host=${encodeURIComponent(plan.host)}&port=${port}`,
            protocols, username: plan.user,
            hostKey: (shown) => hostKey(session, plan.host, port, shown),
            ask: (request) => interactive(session, request),
            log: (text) => line(session, text),
        });
    }

    async function run(session) {
        const plan = session.plan;
        const auth = secret(32);
        const key = secret(32);
        keepRoom(session.room, { relay: session.relay, auth, key, name: session.name, at: Date.now() });
        openRoom(session, { auth, key });
        // The room exists once this page is in it; only then may the node join.
        if (!(await waitFor(() => session.roomClient.state().socket === "open"
                                  || session.state === "failed", 20000))) {
            return fail(session, session.roomClient.state().error
                        || "The relay could not be reached. Check this computer's internet connection.");
        }
        try {
            session.ssh = await signIn(session);
        } catch (error) {
            return fail(session, String(error.message || error));
        }
        if (ended(session)) return finishSsh(session);
        line(session, `Signed in to ${plan.host}.`);
        setState(session, plan.install ? "installing" : (plan.queued ? "waiting_for_job" : "waiting_for_app"));
        const relayFile = JSON.stringify({ v: 1, relay: session.relay, room: session.room, auth, key,
                                           idle_exit: plan.idle_exit ?? 1800 });
        let launched = false;
        try {
            session.proc = await session.ssh.exec(plan.command.split(ROOM_PLACEHOLDER).join(session.room),
                                                  { stdin: `${relayFile}\n` });
        } catch (error) {
            return fail(session, String(error.message || error));
        }
        session.proc.onLine((text) => {
            line(session, text);
            if (/^\[plexora-start\]/.test(text)) launched = true;
            if (plan.marks && text.trim() === plan.marks.installed) {
                setState(session, plan.queued ? "waiting_for_job" : "waiting_for_app");
            } else if (/queued and waiting for resources/.test(text)) {
                setState(session, "waiting_for_job");
            } else if (/has been allocated resources/.test(text)) {
                session.jobStartedAt = Date.now();
                setState(session, "waiting_for_app");
            }
        });
        session.timer = setTimeout(() => {
            if (session.state !== "connected") {
                fail(session, `The data node did not start within ${Math.round(plan.timeout / 60)} minutes.`);
            }
        }, Math.max(60, plan.timeout || 600) * 1000);
        session.proc.done.then(() => {
            if (session.state === "connected" || session.state === "failed" || session.state === "exited") return;
            if (launched) {
                // SSH went away after the node was started: it joins its room
                // without it, and the timer above still applies.
                line(session, "The SSH connection closed; waiting for the data node to join.");
                return;
            }
            fail(session, "The start command on the server ended before the data node started.",
                 { byScript: true });
        });
    }

    // -- a files-only server ---------------------------------------------------

    function keptFiles() {
        try {
            return JSON.parse(global.sessionStorage.getItem(FILES_KEY) || "{}") || {};
        } catch (error) {
            return {};
        }
    }

    function keepFiles(session) {
        const kept = keptFiles();
        kept[session.name] = { plan: session.plan, home: session.home };
        try {
            global.sessionStorage.setItem(FILES_KEY, JSON.stringify(kept));
        } catch (error) { /* this page's life only */ }
    }

    function forgetFiles(session) {
        const kept = keptFiles();
        if (!(session.name in kept)) return;
        delete kept[session.name];
        try {
            global.sessionStorage.setItem(FILES_KEY, JSON.stringify(kept));
        } catch (error) { /* nothing kept */ }
    }

    //: A connection is signing in again by itself and may need somebody: the
    //: app's connection dialog brings itself back for its questions.
    function attention(name) {
        needed.add(name);
        try {
            global.dispatchEvent(new CustomEvent("plexora-remote-attention", { detail: { name } }));
        } catch (error) { /* nobody listening yet: takeAttention() has it */ }
    }

    function takeAttention() {
        reattach();
        const names = [...needed];
        needed.clear();
        return names;
    }

    const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

    async function runFiles(session) {
        const plan = session.plan;
        const again = session.reconnects > 0 || session.resumed;
        clearTimeout(session.timer);
        // Whatever the server asks is answered within its time, or not at all.
        session.timer = setTimeout(() => {
            if (session.state !== "connected") {
                fail(session, `Not signed in to ${plan.host} within ${Math.round((plan.timeout || 300) / 60)} minutes.`);
            }
        }, Math.max(60, plan.timeout || 300) * 1000);
        let ssh = null;
        for (let attempt = 0; ; attempt += 1) {
            try {
                ssh = await signIn(session);
                break;
            } catch (error) {
                const message = String(error.message || error);
                if (ended(session)) return;
                // After a drop the way back can be down for as long as a relay
                // restart takes: tried again before anybody is told.
                if (again && error.transport && attempt < RETRIES) {
                    line(session, `${message} Trying again…`);
                    setState(session, "connecting", "The connection closed; reaching the server again…");
                    await pause(RETRY_MS * (attempt + 1));
                    if (ended(session)) return;
                    continue;
                }
                return fail(session, message);
            }
        }
        if (ended(session)) return ssh.close();
        let sftp;
        try {
            sftp = await ssh.sftp();
            if (!session.home) session.home = await sftp.realpath(".");
        } catch (error) {
            ssh.close();
            return fail(session, String(error.message || error));
        }
        if (ended(session)) {
            sftp.close();
            return ssh.close();
        }
        clearTimeout(session.timer);
        session.ssh = ssh;
        session.sftp = sftp;
        session.connectedAt = Date.now();
        session.error = null;
        line(session, again ? `Signed in to ${plan.host} again.`
            : `Connected for files. Home folder: ${session.home}`);
        setState(session, "connected");
        keepFiles(session);
        ssh.closed.then(() => dropped(session, ssh));
        // The SFTP side ending on its own is the connection ending too.
        sftp.closed.then(() => {
            if (session.ssh === ssh) ssh.close();
        });
    }

    function dropped(session, ssh) {
        if (session.ssh !== ssh) return;  // Disconnect, or already replaced
        session.ssh = null;
        const sftp = session.sftp;
        session.sftp = null;
        if (sftp) sftp.close();
        if (session.state !== "connected") return;
        const host = session.plan.host;
        if (session.reconnects && Date.now() - session.connectedAt < FLAPPING_MS) {
            session.error = `The connection to ${host} keeps closing. Connect it again from Settings › Remote servers.`;
            setState(session, "exited");
            forgetFiles(session);
            return;
        }
        session.reconnects += 1;
        line(session, `The connection to ${host} closed. Signing in again…`);
        setState(session, "connecting", "The connection closed; signing in again…");
        attention(session.name);
        runFiles(session).catch((error) => fail(session, String(error.message || error)));
    }

    //: The connection's SFTP side, once it is up: a read made while it signs in
    //: again waits for it.
    async function sftpOf(session) {
        const usable = () => (session.sftp && !session.sftp.failure ? session.sftp : null);
        if (!usable() && !ended(session)) {
            await waitFor(() => usable() || ended(session), FILES_WAIT_MS);
        }
        const sftp = usable();
        if (!sftp) {
            throw Object.assign(new Error(session.error || `Not connected to “${session.name}”.`),
                                { notConnected: true });
        }
        return sftp;
    }

    //: Python's reads (server/models/web_remote.py `PageFiles`): {op, path,
    //: start, end}. Every operation only reads, so one the connection dropped
    //: under is asked again once it is back.
    async function files(request, name) {
        const session = listed().find((item) => item.name === name && item.kind === "files");
        if (!session || ended(session)) {
            return json({ error: (session && session.error) || `Not connected to “${name}”.`,
                          not_connected: true }, 503);
        }
        const body = await request.json().catch(() => ({}));
        const path = String(body.path || "");
        for (let attempt = 0; ; attempt += 1) {
            let sftp;
            try {
                sftp = await sftpOf(session);
            } catch (error) {
                return json({ error: error.message, not_connected: true }, 503);
            }
            try {
                if (body.op === "stat" || body.op === "lstat") return json({ attrs: await sftp[body.op](path) });
                if (body.op === "realpath") return json({ path: await sftp.realpath(path || ".") });
                if (body.op === "listdir") return json({ entries: await sftp.listdir(path) });
                if (body.op === "read") {
                    let end = body.end;
                    if (end === undefined || end === null) end = (await sftp.stat(path)).size || 0;
                    const bytes = await sftp.read(path, body.start || 0, end);
                    return new Response(bytes, { status: 200, headers: { "Content-Type": "application/octet-stream" } });
                }
                return json({ error: `No such operation: ${body.op}` }, 400);
            } catch (error) {
                if (error.lost && attempt === 0 && !ended(session)) continue;
                const status = error.fx === 2 ? 404 : error.fx === 3 ? 403 : error.lost ? 503 : 500;
                return json({ error: String(error.message || error), fx: error.fx ?? 4,
                              not_connected: Boolean(error.lost) }, status);
            }
        }
    }

    function start(name, plan, { home = null, resumed = false } = {}) {
        for (const session of sessions.values()) {
            if (session.name === name && (OPENING.includes(session.state) || session.state === "connected")) {
                throw Object.assign(new Error(`“${name}” is already connected or connecting.`), { status: 409 });
            }
        }
        const kind = plan.kind === "files" ? "files" : "node";
        const session = {
            id: secret(9), name, plan, kind, relay: relayUrl, room: kind === "node" ? secret(16) : null,
            state: "connecting", detail: "", lines: [], prompt: null, answer: null, error: null, node: null,
            startedAt: Date.now(), jobStartedAt: null, endedByScript: false,
            home, resumed, reconnects: 0, connectedAt: 0, ssh: null, sftp: null,
        };
        sessions.set(session.id, session);
        (kind === "files" ? runFiles : run)(session)
            .catch((error) => fail(session, String(error.message || error)));
        return session;
    }

    async function stop(session) {
        if (session.answer) session.answer(null);
        finishSsh(session);
        if (session.roomClient) {
            const state = session.roomClient.state();
            if (state.socket === "open") await session.roomClient.stop().catch(() => null);
            session.roomClient.close();
        }
        if (session.room) dropRoom(session.room);
        if (session.kind === "files") forgetFiles(session);
        session.error = null;
        setState(session, "exited");
    }

    //: A reopened tab: every room this browser still holds the key to, back
    //: as a connection that is reconnecting, and connected once its node says
    //: hello.
    function reattach() {
        if (attached) return;
        attached = true;
        const rooms = read(ROOMS_KEY);
        for (const [room, record] of Object.entries(rooms)) {
            if (!record || !record.auth || !record.key) continue;
            const session = {
                id: secret(9), name: record.name, plan: null, kind: "node", relay: record.relay || relayUrl, room,
                state: "connecting", detail: "Reconnecting to the data node…", lines: [],
                prompt: null, answer: null, error: null, node: null, startedAt: record.at || Date.now(),
                jobStartedAt: null, endedByScript: false,
            };
            sessions.set(session.id, session);
            openRoom(session, record);
            setTimeout(() => {
                if (session.state === "connecting") {
                    session.error = "The data node is no longer running.";
                    setState(session, "exited");
                    session.roomClient.close();
                    dropRoom(room);
                }
            }, REATTACH_MS);
        }
        // Files-only servers this tab was connected to: signed in to again,
        // the dialog brought back for the questions.
        for (const [name, record] of Object.entries(keptFiles())) {
            if (!record || !record.plan) continue;
            const session = start(name, record.plan, { home: record.home || null, resumed: true });
            line(session, "This tab was reloaded: signing in again.");
            attention(name);
        }
    }

    // -- the page's two prefixes ---------------------------------------------

    function json(value, status = 200) {
        return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
    }

    function routeOf(pathname) {
        const base = String(global.PLEXORA_SITE_BASE || "");
        if (base && (pathname === base || pathname.startsWith(`${base}/`))) return pathname.slice(base.length) || "/";
        return pathname;
    }

    function handles(pathname) {
        const route = routeOf(pathname);
        return route.startsWith(`${API}/`) || route === API || route.startsWith(RELAY);
    }

    //: The newest connection for each profile: an ended one stays listed
    //: until a newer one replaces it, so its last state and log can be read.
    function listed() {
        const newest = new Map();
        for (const session of sessions.values()) {
            const had = newest.get(session.name);
            if (!had || had.startedAt <= session.startedAt) newest.set(session.name, session);
        }
        return [...newest.values()];
    }

    async function api(request, route, url) {
        const parts = route.slice(API.length).split("/").filter(Boolean);
        const count = Number(url.searchParams.get("log") || 25);
        if (parts[0] === "files" && parts.length === 2 && request.method === "POST") {
            return files(request, decodeURIComponent(parts[1]));
        }
        if (parts[0] !== "sessions") return json({ error: "Not found." }, 404);
        if (parts.length === 1 && request.method === "GET") {
            return json({ sessions: listed().map((session) => view(session, count)),
                          rooms: Object.keys(read(ROOMS_KEY)) });
        }
        if (parts.length === 1 && request.method === "POST") {
            const body = await request.json().catch(() => ({}));
            if (!body.name || !body.plan || !(body.plan.command || body.plan.kind === "files")) {
                return json({ error: "A name and a plan are needed." }, 400);
            }
            try {
                return json(view(start(body.name, body.plan)), 202);
            } catch (error) {
                return json({ error: error.message }, error.status || 500);
            }
        }
        const session = sessions.get(parts[1]);
        if (!session) return json({ error: "That connection is no longer running." }, 404);
        if (parts.length === 2 && request.method === "GET") return json(view(session, count));
        if (parts.length === 2 && request.method === "DELETE") {
            await stop(session);
            return json(view(session));
        }
        if (parts.length === 3 && parts[2] === "answer" && request.method === "POST") {
            const body = await request.json().catch(() => ({}));
            if (!session.answer || (body.id && session.prompt && body.id !== session.prompt.id)) {
                return json({ error: "Nothing is waiting for an answer." }, 409);
            }
            session.answer(body.answer === undefined ? "" : body.answer);
            return json({ ok: true });
        }
        return json({ error: "Not found." }, 404);
    }

    //: Request headers the node has no use for, or that would be wrong there.
    const DROPPED = new Set(["host", "origin", "referer", "cookie", "connection", "content-length",
                             "accept-encoding", "transfer-encoding", "upgrade", "keep-alive"]);
    //: Response headers a Response built here must not repeat.
    const UNSENT = new Set(["content-length", "transfer-encoding", "connection", "content-encoding",
                            "keep-alive"]);

    async function relay(request, route, url) {
        const rest = route.slice(RELAY.length);
        const slash = rest.indexOf("/");
        const room = slash < 0 ? rest : rest.slice(0, slash);
        const path = (slash < 0 ? "/" : rest.slice(slash)) + url.search;
        const session = [...sessions.values()].find((item) => item.room === room && item.roomClient
                                                          && item.state !== "failed");
        if (!session || session.state === "exited") {
            return json({ error: "This server's connection is not open in this browser. Connect to it again "
                                 + "from Settings › Remote servers." }, 502);
        }
        const headers = [];
        request.headers.forEach((value, name) => {
            if (!DROPPED.has(name.toLowerCase())) headers.push([name, value]);
        });
        const body = ["GET", "HEAD"].includes(request.method) ? null
            : new Uint8Array(await request.arrayBuffer());
        let answer;
        try {
            answer = await session.roomClient.fetch({ method: request.method, path, headers, body });
        } catch (error) {
            return json({ error: String(error.message || error) }, 504);
        }
        const out = new Headers();
        let encoding = "";
        for (const [name, value] of answer.headers || []) {
            if (String(name).toLowerCase() === "content-encoding") encoding = String(value).trim().toLowerCase();
            if (!UNSENT.has(String(name).toLowerCase())) out.append(name, value);
        }
        const empty = [204, 304].includes(answer.status) || request.method === "HEAD";
        // The node compresses some answers whatever it is asked (a table's
        // `all_cells`), and its link carries them as served. A Response made
        // here is never decoded by the browser: still encoded, its bytes would
        // reach their reader -- the website's Python -- as gzip.
        let payload = answer.body;
        if (!empty && payload && payload.length && /^(gzip|deflate)$/.test(encoding)) {
            payload = await inflate(payload, encoding);
        }
        return new Response(empty ? null : payload, { status: answer.status, headers: out });
    }

    //: Bytes through the browser's own decompressor (pythonClient.js's).
    async function inflate(bytes, format) {
        const stream = new DecompressionStream(format);
        const writer = stream.writable.getWriter();
        writer.write(bytes).catch(() => {});
        writer.close().catch(() => {});
        return new Uint8Array(await new Response(stream.readable).arrayBuffer());
    }

    async function answer(request) {
        reattach();
        const url = new URL(request.url, global.location.href);
        const route = routeOf(url.pathname);
        if (route.startsWith(RELAY)) return relay(request, route, url);
        return api(request, route, url);
    }

    global.PlexoraWebRemote = Object.freeze({ configure, answer, handles, takeAttention });
})(window);
