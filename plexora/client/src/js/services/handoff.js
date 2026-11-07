/**
 * handoff.js -- what the website says where only the desktop app could act.
 *
 * A web page cannot open a TCP socket, run a command-line tool or stop an
 * installed program, so on the website a few routes are answered by a notice
 * instead (plexora/browser/routes.py `HANDOFF`; services/backend.js answers
 * them `501 {success: false, error: why, handoff: {to, title, why, actions}}`).
 * This file draws that notice, and never leaves a blank panel or a click that
 * does nothing:
 *
 * - **inline**, in the panel or dialog that would have asked: a short title,
 *   one line, and every button on one row (`render(container, handoff)`).
 *   A caller that knows ahead of time asks `forPath(path)` and draws the
 *   notice instead of a form that would fail;
 * - **a notice in the corner** (PlexoraToast) for any other caller: backend.js
 *   tells this page about every handoff it answers (`plexora:handoff`), and
 *   unless the same notice is already on screen inline the toast says it
 *   once per route per page.
 *
 * The actions (`ACTIONS`, the same four routes.py allows):
 *   open_desktop  the desktop app, on this page's project when it has one
 *                 (services/desktopHandoff.js, in the site's host page)
 *   get_desktop   the desktop app's download page
 *   pair_node     pair a node started on the server (`plexora node serve
 *                 --pair` prints the link; services/connectorClient.js pairs)
 *   google_cloud  the Google Cloud connection, which needs no SSH on the
 *                 website (plan L9): a saved one opened, else the form
 *                 (services/connectionModal.js)
 *
 * Nothing here runs for a page a Plexora served (desktop app, server):
 * `forPath` is null there and no handoff is ever answered.
 *
 * Classic script, `window.PlexoraWebHandoff`.
 */
(function (global) {
    "use strict";

    const ACTIONS = Object.freeze(["open_desktop", "get_desktop", "pair_node", "google_cloud"]);
    const LABELS = Object.freeze({
        open_desktop: "Open Desktop App",
        get_desktop: "Get the Desktop App",
        pair_node: "Pair a Node",
        google_cloud: "Google Cloud",
    });
    const DOWNLOAD = "https://github.com/nirmallab/plexora/releases/latest";

    const website = () => Boolean(global.PlexoraBackend && global.PlexoraBackend.kind === "browser");

    //: The site's host page (services/shell.js): it holds the desktop
    //: hand-over and the node pairing, which outlive this page.
    function shell() {
        try {
            const parent = global.parent;
            if (parent && parent !== global && parent.PlexoraShellHost) return parent;
        } catch (error) { /* another origin */ }
        return global.PlexoraShellHost ? global : null;
    }

    function href(path) {
        const text = String(path || "");
        if (/^[a-z]+:/i.test(text) || text.startsWith("/")) return text;
        return typeof global.plexoraUrl === "function" ? global.plexoraUrl(text) : "/" + text;
    }

    /** The notice the website answers `path` with, or null (always null on a
     *  page a Plexora served). */
    function forPath(path, method = "GET") {
        if (!website() || typeof global.PlexoraBackend.classify !== "function") return null;
        const row = global.PlexoraBackend.classify(href(path), method);
        return row && row.class === "handoff" ? row.handoff : null;
    }

    /** The notice in an answer's body (or the body itself), or null. */
    function of(body) {
        if (!body || typeof body !== "object") return null;
        const notice = body.handoff && typeof body.handoff === "object" ? body.handoff : null;
        return notice && notice.why ? notice : null;
    }

    const keyOf = (handoff) => `${handoff.title || ""}|${handoff.why || ""}`;

    function el(tag, className, text) {
        const node = global.document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    // -- the actions --------------------------------------------------------------

    function openDesktop() {
        const host = shell();
        const project = global.flaskVariables && global.flaskVariables.datasource;
        if (host && host.PlexoraHandoff) {
            if (project) return host.PlexoraHandoff.open(project, global);
            if (host.PlexoraHandoff.openApp) return host.PlexoraHandoff.openApp(global);
        }
        global.location.href = "plexora://open";
        return null;
    }

    function getDesktop() {
        const host = shell();
        const url = (host && host.PlexoraHandoff && host.PlexoraHandoff.DOWNLOAD) || DOWNLOAD;
        global.open(url, "_blank", "noopener");
    }

    //: The command a server runs to pair with this page: the default origin
    //: is the site's own (`--pair` alone); any other names itself.
    function pairCommand() {
        const host = shell();
        if (host && host.PlexoraConnector) return host.PlexoraConnector.command();
        return "plexora node serve --dynamic --pair";
    }

    /** Pair a node: the command to run on the server, and the link it prints
     *  pasted here (or clicked there: the link opens this site and pairs). */
    async function pairNode() {
        const host = shell();
        const confirm = global.PlexoraConfirm;
        if (!confirm || !host || !host.PlexoraConnector) return null;
        const content = el("div", "plx-handoff-pair");
        const row = el("div", "plx-handoff-command");
        const code = el("code", null, pairCommand());
        const copy = el("button", "plx-button", "Copy");
        copy.type = "button";
        copy.addEventListener("click", () => {
            global.navigator.clipboard?.writeText(code.textContent).then(() => {
                copy.textContent = "Copied";
            }).catch(() => null);
        });
        row.append(code, copy);
        const link = el("input", "plx-prompt-input plx-handoff-link");
        link.type = "url";
        link.placeholder = "Paste the link it prints";
        link.autocomplete = "off";
        link.spellcheck = false;
        link.setAttribute("aria-label", "Pairing link");
        content.append(row, link);
        const answer = await confirm.choose({
            title: "Pair a node",
            body: "Run this on the server, then open the link it prints.",
            content,
            choices: [{ value: "pair", label: "Pair", kind: "primary" }],
            close: true,
            inline: true,
        });
        if (answer !== "pair") return null;
        const pairing = host.PlexoraConnector.parse(link.value);
        if (!pairing) {
            global.PlexoraToast?.show({ title: "That is not a pairing link", tone: "warning",
                                        note: "It starts with your Plexora address and ends in #v=1&e=…&t=…" });
            return null;
        }
        return host.PlexoraConnector.pair(pairing, global);
    }

    //: Google Cloud on the website: the saved connection opened (its VM
    //: started and handed over), or the form for a first one.
    async function googleCloud() {
        const modal = global.PlexoraConnectionModal;
        if (!modal) return null;
        let saved = null;
        const remotes = global.PlexoraRemotes;
        if (remotes) {
            const known = remotes.snapshot().loaded ? remotes.snapshot() : await remotes.refresh();
            saved = ((known && known.entries) || []).find((entry) => entry.gcloud) || null;
        }
        return saved ? modal.open({ name: saved.name })
            : modal.open({ view: "recipe", recipe: "gcloud" });
    }

    const RUN = { open_desktop: openDesktop, get_desktop: getDesktop, pair_node: pairNode,
                  google_cloud: googleCloud };

    function run(action) {
        const fn = RUN[action];
        if (!fn) return null;
        try {
            const done = fn();
            if (done && typeof done.catch === "function") {
                done.catch((error) => console.warn("[handoff]", action, error));
            }
            return done;
        } catch (error) {
            console.warn("[handoff]", action, error);
            return null;
        }
    }

    function actionsOf(handoff) {
        return (handoff.actions || []).filter((action) => ACTIONS.includes(action));
    }

    // -- drawing ------------------------------------------------------------------

    /**
     * The notice's buttons, in order, the first the primary: its actions and,
     * with `options.close`, a Close for a dialog with nothing else to do.
     * `options.before(action)` runs first (a dialog closing itself before the
     * pairing question opens).
     */
    function buttons(handoff, options = {}) {
        const out = actionsOf(handoff).map((action, index) => {
            const button = el("button", index === 0 ? "btn btn-sm btn-primary" : "btn btn-sm btn-outline-light",
                              LABELS[action]);
            button.type = "button";
            button.dataset.handoffAction = action;
            button.addEventListener("click", () => {
                if (options.before) options.before(action);
                run(action);
            });
            return button;
        });
        if (options.close) {
            const close = el("button", "btn btn-sm btn-outline-light", "Close");
            close.type = "button";
            close.dataset.handoffAction = "close";
            close.addEventListener("click", () => options.close());
            out.push(close);
        }
        return out;
    }

    /** The notice as an element: a short title, one line, every button on
     *  one row (`buttons`' options). */
    function notice(handoff, options = {}) {
        const box = el("div", "plx-handoff");
        box.setAttribute("role", "note");
        box.dataset.handoffKey = keyOf(handoff);
        const icon = el("span", `plx-handoff-icon fas ${handoff.to === "desktop" ? "fa-desktop" : "fa-circle-info"}`);
        icon.setAttribute("aria-hidden", "true");
        const text = el("div", "plx-handoff-text");
        text.append(el("strong", "plx-handoff-title", handoff.title || "Not on the website"),
                    el("span", "plx-handoff-why", handoff.why || ""));
        box.append(icon, text);
        const row = el("div", "plx-handoff-actions");
        row.append(...buttons(handoff, options));
        if (row.childElementCount) box.append(row);
        return box;
    }

    /** The notice in place of whatever `container` held. */
    function render(container, handoff, options = {}) {
        if (!container || !handoff) return null;
        const box = notice(handoff, options);
        container.replaceChildren(box);
        return box;
    }

    /** Every `[data-handoff-path]` placeholder under `root` (a template's
     *  server-drawn notice, without buttons) made the full notice. */
    function upgrade(root) {
        const scope = root || global.document;
        scope.querySelectorAll("[data-handoff-path]").forEach((slot) => {
            const handoff = forPath(slot.dataset.handoffPath, slot.dataset.handoffMethod || "GET");
            if (handoff) slot.replaceWith(notice(handoff));
        });
    }

    /** The notice in the corner, for a caller that has no panel of its own. */
    function toast(handoff) {
        const toaster = global.PlexoraToast;
        if (!toaster || !handoff) return null;
        return toaster.show({
            title: handoff.title || "Not on the website",
            note: handoff.why,
            actions: actionsOf(handoff).map((action, index) => ({
                label: LABELS[action], primary: index === 0, onSelect: () => { run(action); },
            })),
        });
    }

    //: What the corner notice already said on this page, by route.
    const told = new Set();

    function onScreen(handoff) {
        const key = keyOf(handoff);
        return Array.from(global.document.querySelectorAll(".plx-handoff"))
            .some((node) => node.isConnected && node.dataset.handoffKey === key);
    }

    //: A handoff the page did not draw itself: said once per route, a moment
    //: after the answer, so the caller that asked can draw it first.
    function heard(detail) {
        const handoff = detail && detail.handoff;
        if (!handoff) return;
        const route = detail.endpoint || detail.path || keyOf(handoff);
        if (told.has(route)) return;
        global.setTimeout(() => {
            if (told.has(route) || onScreen(handoff)) return;
            told.add(route);
            toast(handoff);
        }, 300);
    }

    if (typeof global.addEventListener === "function") {
        // backend.js keeps what it answered until this file hears it: the
        // ones answered before it ran are taken here, and each one heard
        // after is taken off the list as it is.
        const unheard = () => (global.PlexoraBackend && Array.isArray(global.PlexoraBackend.handoffs)
            ? global.PlexoraBackend.handoffs : []);
        global.addEventListener("plexora:handoff", (event) => {
            const list = unheard();
            const at = list.indexOf(event.detail);
            if (at >= 0) list.splice(at, 1);
            heard(event.detail);
        });
        unheard().splice(0).forEach(heard);
    }

    // A template's placeholder notices, made whole on every page the app's
    // router shows (services/pageBoot.js).
    if (global.PlexoraPage && typeof global.PlexoraPage.register === "function") {
        global.PlexoraPage.register(() => {
            upgrade();
            return null;
        });
    }

    global.PlexoraWebHandoff = Object.freeze({
        ACTIONS, LABELS, forPath, of, buttons, notice, render, upgrade, toast, run,
        pairNode, pairCommand, website,
    });
})(window);
