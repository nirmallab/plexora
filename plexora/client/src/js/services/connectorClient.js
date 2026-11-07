/**
 * PlexoraConnector -- pairing a data node with this browser.
 *
 * A web page cannot open an SSH connection (plan decision 1), so a server's
 * data reaches the website through a node started there: `plexora node serve
 * --pair` prints a link to this site,
 *
 *     <site>/connect#v=1&e=<endpoint>&t=<token>
 *
 * and opening it pairs that node in one click. Everything is in the fragment,
 * which a browser never sends to a server; the loader (services/shell.js
 * `takePairingLink`) takes it before anything else runs and clears it from
 * the address bar and the history (`history.replaceState`), so the token is
 * in neither. Pairing then
 *
 * 1. asks the node's `/hello` from the page itself -- the request Chrome's
 *    local-network prompt is attached to when the node is on this computer
 *    and the site is not (P5: the bubble asks once per origin, and the
 *    permission then covers the worker's requests too);
 * 2. registers it with Plexora's Python (`POST /settings/nodes`), which checks
 *    it from the worker over urllib3 (plexora/browser/stages.py `nodes`) and
 *    keeps it with the projects, as the desktop keeps its nodes.
 *
 * The node must allow this site's origin: `--pair` does that for the origin
 * it names (the hosted viewer by default; `command()` names any other).
 *
 *   parse(text)           {endpoint, token} from a link or its fragment, or null
 *   command()             what to run on the server for this site
 *   pair(pairing, app)    -> {ok, name, error}; tells the app's page
 *
 * Classic script, `window.PlexoraConnector`, in the site's host page
 * (services/shell.js); needs PlexoraPython.
 */
(function (global) {
    "use strict";

    const VERSION = "1";
    const DEFAULT_ORIGIN = "https://viewer.plexoraapp.com";
    const TOKEN_HEADER = "X-Plexora-Node-Token";
    const DEFAULT_PORT = "8642";
    const HELLO_MS = 4000;
    //: How long a pairing waits on Chrome's local-network question.
    const PROMPT_MS = 60000;

    /** {endpoint, token} from a pairing link, its fragment, or null. */
    function parse(text) {
        const raw = String(text || "").trim();
        if (!raw) return null;
        const hash = raw.includes("#") ? raw.slice(raw.indexOf("#") + 1) : raw;
        const fields = new URLSearchParams(hash);
        if (fields.get("v") !== VERSION) return null;
        const endpoint = String(fields.get("e") || "").trim().replace(/\/+$/, "");
        const token = String(fields.get("t") || "").trim();
        if (!endpoint || !token) return null;
        let url;
        try {
            url = new URL(endpoint);
        } catch (error) {
            return null;
        }
        if (!["http:", "https:"].includes(url.protocol) || url.username || url.password
            || url.search || url.hash) {
            return null;
        }
        return { endpoint, token };
    }

    //: The site's base path: "" at a site's root, the prefix when a node
    //: serves the app under one (`/rnode/<host>/<port>/app`).
    const base = () => String(global.PLEXORA_SITE_BASE || "");

    /** What to run on the server: a node that may be asked for files, paired
     *  with this site (`--pair` alone means the hosted viewer). */
    function command() {
        const here = global.location.origin + base();
        return "plexora node serve --dynamic --pair" + (here === DEFAULT_ORIGIN ? "" : ` ${here}`);
    }

    function loopback(host) {
        return host === "localhost" || host === "[::1]" || host === "::1" || /^127\./.test(host);
    }

    //: The node's own name for itself, as Settings lists it: its host, and
    //: the port when it is not the default (two nodes on one machine).
    function nameFor(hello, endpoint) {
        const url = new URL(endpoint);
        let host = String((hello && hello.node_id) || "").replace(/-[0-9a-f]{8}$/i, "")
            || url.hostname;
        host = host.toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 32)
            || "node";
        const port = url.port || (url.protocol === "https:" ? "443" : "80");
        return port === DEFAULT_PORT ? host : `${host}-${port}`;
    }

    //: Chrome's answer to "may this site reach apps on this computer?"
    //: (`loopback-network` in Chrome 156, P5), or "" where it does not say.
    async function loopbackPermission() {
        for (const name of ["loopback-network", "local-network-access"]) {
            try {
                return (await global.navigator.permissions.query({ name })).state;
            } catch (error) { /* not a permission this browser knows */ }
        }
        return "";
    }

    //: One /hello: `{hello}`, `{error}`, or `{error, unreached}` when nothing
    //: answered -- refused, held or blocked by the prompt, or no node there.
    async function helloOnce(pairing, prompted) {
        const options = { headers: { [TOKEN_HEADER]: pairing.token }, cache: "no-store" };
        // A declaration, not a key (P5): "loopback", never "local", for a
        // node on this computer asked from a public page.
        if (prompted) options.targetAddressSpace = "loopback";
        const controller = typeof AbortController === "function" ? new AbortController() : null;
        if (controller) options.signal = controller.signal;
        const timer = controller ? global.setTimeout(() => controller.abort(), HELLO_MS) : null;
        try {
            const response = await global.fetch(`${pairing.endpoint}/node/v1/hello`, options);
            const body = await response.json().catch(() => ({}));
            if (response.status === 403) {
                return { error: "The node refused the link's token: use the link it printed last." };
            }
            if (!response.ok || !body.api_version) return { error: body.error || `The node answered ${response.status}.` };
            return { hello: body };
        } catch (error) {
            return { unreached: true,
                     error: "The node could not be reached: is it running, and its address reachable from here?" };
        } finally {
            if (timer) global.clearTimeout(timer);
        }
    }

    //: The node's /hello, asked by the page: the request Chrome's local-
    //: network prompt belongs to, for a node on this computer and a site that
    //: is not. The prompt is announced first, and a request it held is asked
    //: again while the question is open -- one held by the prompt stays held
    //: after it is answered (P5).
    async function hello(pairing, app) {
        const url = new URL(pairing.endpoint);
        const prompted = loopback(url.hostname) && !loopback(global.location.hostname);
        if (prompted && (await loopbackPermission()) !== "granted") {
            tell(app, { title: "Allow it when Chrome asks",
                        note: "Chrome asks once whether this site may reach apps on this computer.", timeout: 0 });
        }
        const until = Date.now() + (prompted ? PROMPT_MS : 0);
        for (;;) {
            const asked = await helloOnce(pairing, prompted);
            if (!asked.unreached || Date.now() > until) return asked;
            if ((await loopbackPermission()) === "denied") {
                return { error: "Chrome was told this site may not reach apps on this computer: allow it in the site's settings, then open the link again." };
            }
            await new Promise((resolve) => global.setTimeout(resolve, 1500));
        }
    }

    function tell(app, options) {
        try {
            app?.PlexoraToast?.show(options);
        } catch (error) { /* the frame is between pages */ }
    }

    /**
     * Pair `pairing` ({endpoint, token}) with this browser. `app` is the
     * app's frame window, for the notice that says how it went.
     */
    async function pair(pairing, app) {
        if (!pairing || pairing.invalid) {
            tell(app, { title: "That pairing link is not complete", tone: "warning",
                        note: "Copy the whole link the node printed." });
            return { ok: false, error: "invalid" };
        }
        const asked = await hello(pairing, app);
        if (asked.error) {
            tell(app, { title: "Could not pair the node", tone: "warning", note: asked.error, timeout: 0 });
            return { ok: false, error: asked.error };
        }
        const name = nameFor(asked.hello, pairing.endpoint);
        let body = {};
        try {
            const response = await global.PlexoraPython.fetch("/settings/nodes", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name, endpoint: pairing.endpoint, token: pairing.token }),
            });
            body = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(body.error || `Plexora answered ${response.status}`);
        } catch (error) {
            const message = String(error.message || error);
            tell(app, { title: "Could not pair the node", tone: "warning", note: message, timeout: 0 });
            return { ok: false, error: message };
        }
        tell(app, { title: `Paired with ${name}`, note: "Its files are under Remote in every file picker." });
        try {
            app?.dispatchEvent(new app.CustomEvent("plexora:node-paired", { detail: { name } }));
        } catch (error) { /* the frame is between pages */ }
        return { ok: true, name, node: body.node || null };
    }

    global.PlexoraConnector = Object.freeze({ parse, command, pair, nameFor });
})(window);
