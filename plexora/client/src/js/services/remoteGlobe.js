/**
 * remoteGlobe.js -- where the connections are, from anywhere in Plexora.
 *
 * Every other surface that shows a remote connection is somewhere you have to
 * go: the Settings page, or a dialog you opened on purpose. Which is fine
 * until you are three hours into a session and the tunnel has died -- then the
 * first sign is a tile that will not load, and the place that would explain it
 * is a page away.
 *
 * So one icon, in the navbar, on every page. Grey when nothing is connected,
 * lit when something is, and pulsing while something is on its way up. Opening
 * it lists every machine and what it is doing, and says which of them the
 * project on screen is actually reading from.
 *
 * **It costs nothing when nothing is happening.** It subscribes to
 * `PlexoraRemotes` passively, which means: no timer while every connection is
 * settled and this panel is closed. That is the state a viewer sits in for
 * hours, and an icon that cost a request a second for the privilege of being
 * grey would not be worth having on the viewer at all. Opening the panel
 * subscribes actively, and closing it goes back to nothing.
 *
 * **It reports, it does not probe.** What it shows is the session state the
 * server already keeps plus, once per panel open, which node the current
 * project's image is routed to. There is no health check of its own: a second
 * opinion polled from here would disagree with Settings at some point, and the
 * disagreement would be the thing people remembered.
 */
window.PlexoraRemoteGlobe = (function () {
    "use strict";

    const Remotes = () => window.PlexoraRemotes;

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function button(className, text, onClick) {
        const node = el("button", className, text);
        node.type = "button";
        if (onClick) node.addEventListener("click", onClick);
        return node;
    }

    function icon(name) {
        const node = el("span", "fas fa-" + name);
        node.setAttribute("aria-hidden", "true");
        return node;
    }

    //: The one mount there will ever be. This icon lives in the NAVBAR, which
    //: the router does not swap -- only the page below it is replaced -- so
    //: `PlexoraPage.register` runs again on every internal navigation against
    //: the very same button. Without this guard each navigation would add a
    //: second click handler, and the globe would open and immediately close.
    //: (views/segmentationWait.js guards its navbar chip for the same reason.)
    let mounted = null;

    function mount(root) {
        if (!root) return null;
        if (mounted === root) return null;

        const glyph = icon("globe");
        root.replaceChildren(glyph);
        root.setAttribute("aria-haspopup", "dialog");
        root.setAttribute("aria-expanded", "false");
        root.hidden = false;

        let panel = null;
        //: The passive watch: alive for the whole page, telling the icon what
        //: colour to be. Costs no polling while everything is settled.
        let watching = null;
        //: The active watch, alive only while the panel is open.
        let watchingOpen = null;
        //: Which node the project on screen is reading its image from, asked
        //: once per panel open. Not polled: it changes when a project is
        //: loaded, not while one is being looked at. `null` with `routingRead`
        //: set means "this server's own filesystem", which is a real answer
        //: and a different one from "we have not asked".
        let imageNode = null;
        let routingRead = false;

        function summarise(snapshot) {
            let connected = 0;
            let opening = 0;
            (snapshot.entries || []).forEach((entry) => {
                if (entry.connected) connected += 1;
                if (entry.opening) opening += 1;
            });
            return { connected, opening };
        }

        function paintIcon(snapshot) {
            const { connected, opening } = summarise(snapshot);
            root.classList.toggle("is-live", connected > 0);
            root.classList.toggle("is-opening", opening > 0);
            const what = opening
                ? "Connecting to another machine"
                : (connected
                    ? `${connected} machine${connected === 1 ? "" : "s"} connected`
                    : "No other machine connected");
            root.setAttribute("aria-label", what);
            root.setAttribute("title", what);
        }

        // -- the panel ---------------------------------------------------------

        function toggle() {
            if (panel) return closePanel();
            openPanel();
        }

        function openPanel() {
            panel = el("div", "remote-panel");
            panel.setAttribute("role", "dialog");
            panel.setAttribute("aria-label", "Remote connections");
            // Through the portal, never straight onto <body>: a fullscreen
            // viewer paints an opaque ::backdrop over every sibling of the
            // fullscreen element, and a panel parked on <body> would open
            // underneath it -- laid out, focusable, and invisible.
            window.PopoverPortal.attach(panel);
            position();
            root.setAttribute("aria-expanded", "true");
            document.addEventListener("keydown", onKey, true);
            document.addEventListener("mousedown", onClickAway, true);
            window.addEventListener("resize", position);

            imageNode = null;
            routingRead = false;
            readRouting();
            watchingOpen = Remotes().subscribe(draw, { active: true });
        }

        function closePanel() {
            if (!panel) return;
            if (watchingOpen) watchingOpen();
            watchingOpen = null;
            window.PopoverPortal.detach(panel);
            panel = null;
            root.setAttribute("aria-expanded", "false");
            document.removeEventListener("keydown", onKey, true);
            document.removeEventListener("mousedown", onClickAway, true);
            window.removeEventListener("resize", position);
        }

        function onKey(event) {
            if (event.key === "Escape") {
                closePanel();
                if (root.focus) root.focus();
            }
        }

        function onClickAway(event) {
            if (!panel) return;
            const inside = panel.contains(event.target)
                || root.contains(event.target);
            if (!inside) closePanel();
        }

        /** Under the icon, right-aligned to it, clamped to the viewport. */
        function position() {
            if (!panel || !root.getBoundingClientRect) return;
            const box = root.getBoundingClientRect();
            const width = panel.offsetWidth || 320;
            const left = Math.max(8, Math.min(box.right - width,
                                              window.innerWidth - width - 8));
            panel.style.top = `${box.bottom + 8}px`;
            panel.style.left = `${left}px`;
        }

        /**
         * Which machine the project on screen is actually reading from.
         *
         * The one thing this panel knows that Settings does not, and the
         * question somebody has when a tile will not load. Asked once, when
         * the panel opens: it is a property of the project that is loaded, not
         * something that changes while it is being looked at.
         */
        function readRouting() {
            const datasource = (window.flaskVariables || {}).datasource;
            if (!datasource) return;
            fetch(plexoraUrl("resource_routing?datasource="
                             + encodeURIComponent(datasource)))
                .then((response) => (response.ok ? response.json() : null))
                .then((payload) => {
                    const routes = (payload && payload.routes) || {};
                    imageNode = (routes.image && routes.image.node) || null;
                    routingRead = true;
                    if (panel) draw(Remotes().snapshot());
                })
                .catch(() => {});
        }

        function draw(snapshot) {
            paintIcon(snapshot);
            if (!panel) return;
            panel.replaceChildren();

            const head = el("div", "remote-panel-head");
            head.append(el("h2", "remote-panel-title", "Connections"));
            panel.append(head);

            const list = el("ul", "remote-panel-list");
            list.append(localRow(snapshot));
            if (snapshot.serverIsRemote && snapshot.server) {
                list.append(serverRow(snapshot.server));
            }
            (snapshot.entries || []).forEach(
                (entry) => list.append(entryRow(entry)));
            panel.append(list);

            if (snapshot.error) {
                panel.append(el("div", "remote-panel-error", snapshot.error));
            }

            const foot = el("div", "remote-panel-foot");
            foot.append(button("btn btn-primary remote-panel-add",
                               "Connect to another machine", addOne));
            foot.append(link("Manage saved servers", "settings#remotes"));
            panel.append(foot);
            position();
        }

        function link(text, href) {
            const anchor = el("a", "remote-panel-link", text);
            anchor.href = plexoraUrl(href);
            return anchor;
        }

        function row(label, detail, stateText, stateClass) {
            const item = el("li", "remote-panel-row");
            const main = el("div", "remote-panel-main");
            main.append(el("span", "remote-panel-name", label));
            if (detail) main.append(el("span", "remote-panel-detail", detail));
            item.append(main);
            const chip = el("span", "remote-panel-chip " + stateClass, stateText);
            item.append(chip);
            return item;
        }

        /**
         * This computer, which is a different thing depending on where Plexora
         * is running -- and the difference is the whole reason the switch on
         * every data field exists.
         */
        function localRow(snapshot) {
            const attached = Boolean(snapshot.clientNode);
            const item = row(
                "This computer",
                snapshot.serverIsRemote
                    ? (attached ? `reached through “${snapshot.clientNode}”`
                                : "not attached to the server")
                    : "Plexora is running here",
                snapshot.serverIsRemote ? (attached ? "Attached" : "Detached")
                                        : "Local",
                snapshot.serverIsRemote
                    ? (attached ? "is-ready" : "is-idle") : "is-ready");
            if (snapshot.serverIsRemote && !attached) {
                // The fix is a command on the user's OWN computer, which is
                // precisely the thing a page served from a cluster cannot do
                // for them -- so it says it rather than offering a button that
                // could not work.
                item.append(el("div", "remote-panel-note",
                               "Run `plexora connect <you>@<server>` in a "
                               + "terminal here and this computer's files "
                               + "become available."));
            }
            return item;
        }

        function serverRow(server) {
            const item = row("This Plexora server", server.detail || "",
                             "Connected", "is-ready");
            // `routingRead` with no image node means the project's image is on
            // this server's own filesystem, which is a real answer. Before the
            // fetch lands, nothing is claimed.
            if (routingRead && !imageNode) markIfViewing(item);
            return item;
        }

        function entryRow(entry) {
            const node = entry.node;
            const viewer = entry.viewer;
            const ready = Boolean(node.node) || viewer.state === "connected";
            const busy = entry.opening;
            const item = row(entry.label, entry.detail,
                             busy ? Remotes().label(
                                 Remotes().isOpening(node.state)
                                     ? node.state : viewer.state)
                                  : (ready ? "Connected" : "Not connected"),
                             busy ? "is-busy" : (ready ? "is-ready" : "is-idle"));

            // Which of the two things is up. One profile can be running both,
            // and they are separate allocations on a site that schedules --
            // somebody should not discover that from `squeue`.
            const kinds = [];
            if (node.node) kinds.push("data node");
            if (viewer.state === "connected") kinds.push("viewer");
            if (kinds.length) {
                item.append(el("div", "remote-panel-note",
                               "Running: " + kinds.join(" and ") + "."));
            }
            const phase = (Remotes().isOpening(node.state) ? node.phase : "")
                || (Remotes().isOpening(viewer.state) ? viewer.phase : "");
            if (phase) item.append(el("div", "remote-panel-note", phase));
            const error = node.error || viewer.error;
            if (error && !busy) {
                item.append(el("div", "remote-panel-row-error", error));
            }
            // Matched on the NODE's name, not the profile's: a profile with a
            // `node_name` registers its node under that, and the routing table
            // names nodes.
            if (node.node && node.node === imageNode) markIfViewing(item);

            const actions = el("div", "remote-panel-actions");
            if (node.node) {
                actions.append(button(
                    "btn btn-outline-light btn-sm", "Disconnect",
                    () => Remotes().disconnect(entry.name, "node")
                        .catch(() => {})));
            } else if (!busy) {
                // A data node, not a viewer: a viewer connection replaces the
                // page you are looking at, which is not something to offer
                // from a navbar icon. It stays a Settings action.
                actions.append(button(
                    "btn btn-primary btn-sm", "Connect", () => {
                        closePanel();
                        window.PlexoraConnectionModal.open({
                            name: entry.name, kind: "node",
                            intent: "Opens a data node on that machine, so "
                                    + "files on it can be used here.",
                        });
                    }));
            }
            if (actions.children.length) item.append(actions);
            return item;
        }

        /** The one thing this panel knows that the Settings page does not. */
        function markIfViewing(item) {
            const note = el("div", "remote-panel-viewing");
            note.append(icon("image"));
            note.append(el("span", null,
                           "The image on screen is being read from here."));
            item.append(note);
        }

        async function addOne() {
            closePanel();
            await window.PlexoraConnectionModal.open({
                kind: "node",
                intent: "Opens a data node on another machine, so files on it "
                        + "can be used in this Plexora.",
            });
        }

        root.addEventListener("click", toggle);
        watching = Remotes().subscribe(paintIcon);
        mounted = root;

        // Deliberately NO teardown handed back to PlexoraPage. A teardown is
        // for state that outlives the markup it was built against, and this
        // button's markup is the navbar's -- which the router never swaps. So
        // there is nothing here that a page swap invalidates, and tearing down
        // on one would mean the globe went grey and re-read the whole state on
        // every internal navigation, for no reason. `mounted` above is what
        // makes running again a no-op instead.
        //
        // Returned for tests and for a caller that really is disposing of the
        // button; PlexoraPage is not such a caller.
        return function dispose() {
            closePanel();
            if (watching) watching();
            watching = null;
            root.removeEventListener("click", toggle);
            if (mounted === root) mounted = null;
        };
    }

    return { mount };
})();

// Through PlexoraPage rather than DOMContentLoaded, because a router swap
// never fires one -- and the globe has to be there on the first page load
// whether that page is the viewer, the import form or Settings. Nothing is
// returned: see the note at the end of mount().
PlexoraPage.register(() => {
    if (!window.PlexoraRemotes || !window.PlexoraRemoteGlobe) return null;
    window.PlexoraRemoteGlobe.mount(document.getElementById("remote_globe"));
    return null;
});
