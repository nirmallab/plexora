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
 * lit when something is, pulsing while something is on its way up, and marked
 * when something has failed. Opening it lists every saved machine, what it is
 * doing, whether it is answering right now, and which of them the image on
 * screen is actually being read from.
 *
 * **It is a status board and a switch, not a settings page.** No usernames, no
 * hostnames, no ssh options, nothing configurable. One line of identity and
 * one line of condition per machine; anything that involves typing is a link
 * to the Remote servers page, which is where that belongs.
 *
 * **It costs nothing when nothing is happening.** It subscribes to
 * `PlexoraRemotes` passively, which means: no timer while every connection is
 * settled and this panel is closed. That is the state a viewer sits in for
 * hours, and an icon that cost a request a second for the privilege of being
 * grey would not be worth having on the viewer at all. Opening the panel
 * subscribes actively, and closing it goes back to nothing.
 *
 * **It probes once, when asked.** Session state says what Plexora did -- it
 * started an ssh, the node announced. "Is it answering now" is a different
 * claim, and the gap between the two is where a slept laptop and an expired
 * job live. So opening the panel asks `/remote_health` once, and that is the
 * only health check in Plexora: a background poll would be a second opinion
 * running forever, and its first act would be to disagree with the session
 * state at a moment nobody was watching.
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

    function icon(name, className) {
        const node = el("span", (className ? className + " " : "")
                        + "fas fa-" + name);
        node.setAttribute("aria-hidden", "true");
        return node;
    }

    /**
     * The condition of one machine, as the second line of its row says it.
     *
     * Deliberately three separate ideas rather than one word. "Connected" is
     * what Plexora did; "Healthy" is what the machine said thirty seconds ago
     * when we asked; the millisecond figure is how long it took to say it.
     * Collapsing them loses the case that matters -- a session that still
     * reads connected against a node that has stopped answering.
     */
    function healthOf(entry, health) {
        if (!entry.node.node) {
            return { word: "Unknown", glyph: "circle-minus", cls: "is-unknown",
                     ms: null, detail: "" };
        }
        const probe = health[entry.name];
        if (!probe) {
            return { word: "Checking", glyph: "circle-notch",
                     cls: "is-checking", ms: null, detail: "" };
        }
        if (probe.state === "healthy") {
            return { word: "Healthy", glyph: "circle-check", cls: "is-healthy",
                     ms: probe.ms, detail: "" };
        }
        return {
            word: probe.state === "unreachable" ? "Not answering" : "Unknown",
            glyph: "triangle-exclamation", cls: "is-degraded", ms: null,
            detail: probe.detail || "",
        };
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
        const dot = el("span", "remote-globe-dot");
        dot.setAttribute("aria-hidden", "true");
        root.replaceChildren(glyph, dot);
        root.setAttribute("aria-haspopup", "dialog");
        root.setAttribute("aria-expanded", "false");
        // The fast tooltip rather than `title`: this is a navbar icon with no
        // label, so the ~1s delay the browser enforces on `title` is the whole
        // difference between finding out what it is and not.
        root.setAttribute("data-tooltip-placement", "bottom");
        root.setAttribute("data-tooltip-align", "right");
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
        //: What `/remote_health` last said, by profile name, and which set of
        //: connected machines that answer was about -- so becoming connected
        //: while the panel is open re-asks, and a poll that changed nothing
        //: does not.
        let health = {};
        let healthFor = null;

        function summarise(snapshot) {
            let connected = 0;
            let opening = 0;
            let failed = 0;
            let first = null;
            (snapshot.entries || []).forEach((entry) => {
                if (entry.connected) {
                    connected += 1;
                    if (!first) first = entry;
                }
                if (entry.opening) {
                    opening += 1;
                    if (!first || !first.opening) first = entry;
                }
                if (entry.node.state === "failed") failed += 1;
            });
            return { connected, opening, failed, first };
        }

        function paintIcon(snapshot) {
            const sum = summarise(snapshot);
            root.classList.toggle("is-live", sum.connected > 0);
            root.classList.toggle("is-opening", sum.opening > 0);
            root.classList.toggle("is-problem", sum.opening === 0
                                   && sum.failed > 0);
            let what = "Remote connections";
            if (sum.opening) {
                what = `Connecting to “${sum.first.label}”…`;
            } else if (sum.connected === 1) {
                what = `${sum.first.label} · Connected`;
            } else if (sum.connected > 1) {
                what = `${sum.connected} machines connected`;
            } else if (sum.failed) {
                what = "A connection failed";
            }
            root.setAttribute("aria-label", what);
            root.setAttribute("data-tooltip", what);
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
            health = {};
            healthFor = null;
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

        /**
         * Ask every open node whether it is answering, and how fast.
         *
         * Once per set of connected machines, not once per tick. The set is
         * the key rather than a timestamp because the thing that makes a stale
         * answer wrong is a connection appearing or going away, and that is
         * exactly what changes the key.
         */
        function readHealth(snapshot) {
            const key = (snapshot.entries || [])
                .filter((entry) => entry.node.node)
                .map((entry) => entry.name).join(" ");
            if (key === healthFor) return;
            healthFor = key;
            if (!key) {
                health = {};
                return;
            }
            fetch(plexoraUrl("remote_health"))
                .then((response) => (response.ok ? response.json() : null))
                .then((payload) => {
                    health = (payload && payload.health) || {};
                    if (panel) draw(Remotes().snapshot());
                })
                .catch(() => {});
        }

        function draw(snapshot) {
            paintIcon(snapshot);
            if (!panel) return;
            readHealth(snapshot);
            panel.replaceChildren();

            // No title bar. The panel hangs off an icon whose tooltip has
            // just said "Remote connections", and repeating that inside would
            // spend the top of a deliberately compact dropdown restating what
            // the user read on the way in. The dialog keeps the name in
            // `aria-label`, where a screen reader still gets it.
            const where = whereFrom(snapshot);
            if (where) panel.append(el("p", "remote-panel-where", where));

            const entries = snapshot.entries || [];
            if (!entries.length) {
                panel.append(el("p", "remote-panel-empty",
                                "No machines saved yet."));
            } else {
                const list = el("ul", "remote-panel-list");
                entries.forEach((entry) => list.append(connectionRow(entry)));
                panel.append(list);
            }

            if (snapshot.error) {
                panel.append(el("div", "remote-panel-error", snapshot.error));
            }
            panel.append(foot());
            position();
        }

        /**
         * One sentence about this computer, and only when there is one to say.
         *
         * Plexora running on the machine in front of you is the ordinary case
         * and needs no announcement. A Plexora served from somewhere else does
         * -- and when nothing has attached this computer to it, the fix is a
         * command in a terminal HERE, which is exactly what a page served from
         * a cluster cannot do on somebody's behalf.
         */
        function whereFrom(snapshot) {
            if (!snapshot.serverIsRemote) {
                return (routingRead && !imageNode)
                    ? "The image on screen is on this Plexora’s own machine."
                    : "";
            }
            return snapshot.clientNode
                ? `This Plexora is running elsewhere; your computer is `
                  + `attached to it as “${snapshot.clientNode}”.`
                : "This Plexora is running elsewhere. Run "
                  + "`plexora connect <you>@<server>` in a terminal here to "
                  + "make this computer’s files available.";
        }

        /**
         * One saved machine: what it is called and what it is doing, then what
         * condition it is in.
         *
         * Two lines, fixed shape, so a list of them can be read down rather
         * than across. Nothing identifying beyond the name the user chose --
         * an address belongs on the page where it can be edited.
         */
        function connectionRow(entry) {
            const node = entry.node;
            const ready = Boolean(node.node);
            const busy = Remotes().isOpening(node.state);
            const broken = node.state === "failed";
            const item = el("li", "remote-conn "
                            + (ready ? "is-connected"
                               : busy ? "is-opening"
                               : broken ? "is-broken" : "is-idle"));

            const top = el("div", "remote-conn-top");
            top.append(el("span", "remote-conn-name", entry.label));
            const status = el("span", "remote-conn-status");
            status.append(el("span", "remote-conn-dot"));
            status.append(el("span", null,
                             ready ? "Connected"
                             : busy ? Remotes().label(node.state)
                             : broken ? "Failed" : "Disconnected"));
            top.append(status);
            item.append(top);

            const bottom = el("div", "remote-conn-bottom");
            const state = healthOf(entry, health);
            const well = el("span", "remote-conn-health " + state.cls);
            well.append(icon(state.glyph));
            well.append(el("span", null, state.word));
            if (state.detail) well.setAttribute("title", state.detail);
            bottom.append(well);

            bottom.append(el("span", "remote-conn-sep"));
            bottom.append(el("span", "remote-conn-latency",
                             state.ms === null || state.ms === undefined
                                 ? "—" : state.ms + " ms"));

            bottom.append(el("span", "remote-conn-spacer"));
            if (!busy) bottom.append(actionFor(entry, ready));
            bottom.append(viewerMark(entry, ready, busy));
            item.append(bottom);

            // The failure itself, which is the only thing on this panel that
            // is worth more than one line: it is usually the sentence that
            // says what to do.
            if (broken && node.error) {
                item.append(el("div", "remote-conn-error", node.error));
            }
            return item;
        }

        function actionFor(entry, ready) {
            if (ready) {
                const stop = button("remote-conn-act", null, () => {
                    Remotes().disconnect(entry.name, "node").catch(() => {});
                });
                stop.append(icon("power-off"));
                stop.setAttribute("aria-label", "Disconnect " + entry.label);
                // `title`, not the fast tooltip: this panel clips its own
                // overflow to keep its rounded corners, and a positioned
                // pseudo-element inside it would be cut off at the edge.
                stop.setAttribute("title", "Disconnect");
                return stop;
            }
            const go = button("remote-conn-act is-go", null, () => {
                closePanel();
                window.PlexoraConnectionModal.open({
                    name: entry.name, kind: "node",
                    intent: "Opens a data node on that machine, so files on "
                            + "it can be used here.",
                });
            });
            go.append(icon("plug"));
            go.setAttribute("aria-label", "Connect " + entry.label);
            go.setAttribute("title", "Connect");
            return go;
        }

        /**
         * Whether the viewer is reading from this machine.
         *
         * One icon with three readings, because there are exactly three
         * answers and two of them are not failures: lit means the image on
         * screen comes from here, muted means this machine is connected but
         * the picture is not its, and a spinner means it is still on its way.
         * Matched on the NODE's name rather than the profile's -- a profile
         * with a `node_name` registers under that, and the routing table names
         * nodes.
         */
        function viewerMark(entry, ready, busy) {
            const attached = ready && routingRead && entry.node.node === imageNode;
            const mark = el("span", "remote-conn-screen"
                            + (attached ? " is-attached" : "")
                            + (busy ? " is-waiting" : ""));
            mark.append(icon(busy ? "circle-notch" : "desktop"));
            const said = busy ? "Attaching to the viewer"
                : attached ? "Attached to viewer" : "Not attached to viewer";
            mark.setAttribute("title", said);
            mark.setAttribute("aria-label", said);
            mark.setAttribute("role", "img");
            return mark;
        }

        /**
         * The way out to the page where machines are actually configured.
         *
         * A link rather than the Add-a-server dialog: adding one means typing
         * an address, and this panel deliberately holds nothing typeable.
         */
        function foot() {
            const foot = el("div", "remote-panel-foot");
            const anchor = el("a", "remote-panel-add");
            anchor.href = plexoraUrl("settings#remotes");
            anchor.append(icon("plus"));
            anchor.append(el("span", "remote-panel-add-label", "Add connection"));
            anchor.append(icon("chevron-right", "remote-panel-add-go"));
            foot.append(anchor);
            return foot;
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

    return { mount, healthOf };
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
