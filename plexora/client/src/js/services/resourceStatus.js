/**
 * PlexoraResourceStatus -- turning a missing layer into a sentence, and into a
 * button when there is one to press.
 *
 * A project whose image is on this machine and whose cell table is on a data
 * node opens even when that node is asleep. That is deliberate and it is the
 * right behaviour: the pixels are still there, the figures still open, and
 * refusing to load the whole project because one table is unreachable would
 * make a laptop that closed its lid look like data loss.
 *
 * But it left the user with a viewer that had silently lost its cell colours
 * and nothing anywhere saying why. The server has known the answer since the
 * project loaded -- `/resource_status` has listed it all along -- and until
 * this file nothing asked.
 *
 * Two different absences, and they are not the same problem:
 *
 *   unavailable   The layer is not readable: the node could not be reached
 *                 when the project loaded, or it has left this machine's map
 *                 since -- disconnecting between two looks at the same project
 *                 is an ordinary afternoon, and the load is skipped the second
 *                 time. Fixing it needs a reconnect and a reload. This is the
 *                 one worth interrupting for.
 *   unreachable   The layer loaded fine through this server, but the BROWSER
 *                 could not reach the node directly, so tiles take one extra
 *                 hop (see resourceRouting.js). Nothing is missing and nothing
 *                 is broken; it is slower. Mentioned in the disconnection
 *                 notice only when there is one, never on its own.
 *
 * **ONLY A MACHINE THAT WAS UP IN THIS TAB IS "DISCONNECTED".** This used to
 * draw a strip across the top of the viewer for any node in the project's
 * record that was not on the map -- and the map outlives a server restart, so
 * a fresh session opened on a warning about a connection that belonged to
 * yesterday. Now the notice is a small toast in the corner, with a Reconnect
 * button when a saved connection can do it, and it is raised only for a node
 * this tab has seen working (`upThisSession`): the project loaded with it, or
 * the globe or a dialog connected it. A reload is a new session and starts the
 * set empty, on purpose.
 *
 * A node that was never up in this tab is not a disconnection. When a saved
 * connection THIS server could open can bring it back, the project asks once
 * with a modal (`offerToConnect`) -- a machine one button away is a question
 * with an answer. Otherwise nothing is drawn: the navbar globe already shows
 * each machine's state, and a warning about something nobody did this
 * session is the noise this replaced.
 *
 * And a third thing, about a cell mask a data node serves (`masks`). Nothing
 * is missing -- the node draws the mask it was given while it converts it,
 * slower -- but the user deserves to know why it is slow, how far along the
 * conversion is, and why it failed if it did. Opening the project is what
 * retries a failed conversion (the server does that once per load); this
 * watches it in the navbar chip and redraws the mask when it lands.
 *
 * Two memories, both per project and per tab, because they answer different
 * questions. `asked` means the modal has been shown and answered, so moving
 * around inside the app does not re-ask; `dismissed` means the user closed the
 * disconnection notice, and wants nothing further said about it. Both are
 * dropped the
 * moment the project opens whole -- they record an answer about a situation,
 * and a project that is fine has ended the one they were about.
 */
window.PlexoraResourceStatus = (function () {
    "use strict";

    const DISMISS_PREFIX = "plexora.resourceStatus.dismissed.";
    const ASKED_PREFIX = "plexora.resourceStatus.asked.";
    const MASK_NOTE_PREFIX = "plexora.resourceStatus.maskNote.";
    //: How often a converting mask is asked about. The conversion is minutes;
    //: the chip only has to move.
    const MASK_POLL_MS = 2000;

    //: What each resource kind is called in a sentence. The route's keys are
    //: the server's words for them; these are the user's.
    const LABELS = {
        image: "The image",
        segmentation: "The cell mask",
        table: "The cell table",
    };

    //: The same three, in the middle of a sentence rather than starting one.
    const NOUNS = {
        image: "image",
        segmentation: "cell mask",
        table: "cell table",
    };

    function remembered(prefix, datasource) {
        try {
            return window.sessionStorage.getItem(prefix + datasource) === "1";
        } catch (e) {
            return false;
        }
    }

    function remember(prefix, datasource) {
        try {
            window.sessionStorage.setItem(prefix + datasource, "1");
        } catch (e) { /* a tab with no storage simply asks again */ }
    }

    //: Node names seen working in this tab: bound in a project that opened
    //: without them missing, or reported up by remoteState. In memory and never
    //: in storage -- a reload is a new session, and "disconnected" is only ever
    //: said about something that was connected in this one. appRouter's page
    //: swaps keep module state, so moving around the app keeps the set.
    const upThisSession = new Set();

    //: The disconnection notice on screen, `{key, handle}`, or null. The key is
    //: what it says, so the 30-second tile-failure re-report does not animate
    //: an identical notice in again.
    let notice = null;

    //: The mask conversion note on screen, or null.
    let maskToast = null;

    // A node connected from the globe, a dialog or Settings counts as seen up
    // before the next report gets round to it.
    window.addEventListener?.("plexora:remote-nodes-changed", (event) => {
        const changed = (event && event.detail && event.detail.changed) || [];
        changed.forEach((row) => {
            if (row && row.up && row.node) upThisSession.add(row.node);
        });
    });

    /** The node each resource kind is read from, as routing resolved it. */
    function boundNodes(routing) {
        const routes = (routing && routing.routes) || {};
        return Object.keys(routes)
            .map((kind) => routes[kind] && routes[kind].node)
            .filter(Boolean);
    }

    function isDismissed(datasource) {
        return remembered(DISMISS_PREFIX, datasource);
    }

    /** Forget both answers for one project, so the next problem is asked afresh. */
    function forget(datasource) {
        try {
            window.sessionStorage.removeItem(DISMISS_PREFIX + datasource);
            window.sessionStorage.removeItem(ASKED_PREFIX + datasource);
        } catch (e) { /* a tab with no storage had nothing to forget */ }
    }

    /** Take the disconnection notice down, if it is about this project. */
    function dropNotice(datasource) {
        if (!notice) return;
        if (datasource && !notice.key.startsWith(datasource + "|")) return;
        const { handle } = notice;
        notice = null;
        handle?.dismiss?.();
    }

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

    /** “a”, or “a” and “b”, or “a”, “b” and “c”. */
    function listOf(names) {
        const quoted = (names || []).map((name) => "“" + name + "”");
        if (quoted.length < 2) return quoted[0] || "";
        return quoted.slice(0, -1).join(", ") + " and "
            + quoted[quoted.length - 1];
    }

    /** `{unavailable, nodes, profiles, reconnect}`, or null if it cannot be had. */
    function load(datasource) {
        if (!datasource) return Promise.resolve(null);
        const url = plexoraUrl("/resource_status?datasource="
            + encodeURIComponent(datasource));
        return fetch(url)
            .then((response) => (response.ok ? response.json() : null))
            .catch(() => null);
    }

    /**
     * Read the whole project again, now that something has changed.
     *
     * A page reload is not enough and this is the reason: the server keys
     * "which project is loaded" on the NAME, so a project that opened with its
     * image missing keeps that shape until something asks for it again. See
     * `/reload_datasource`.
     */
    function reload(datasource) {
        return fetch(plexoraUrl("/reload_datasource?datasource="
            + encodeURIComponent(datasource)), { method: "POST" })
            .then((response) => (response.ok ? response.json() : null))
            .catch(() => null);
    }

    /**
     * The sentence for one missing resource.
     *
     * The node's name is in it because that is what the user has to act on --
     * it is the name in Settings, and the name in the profile that reconnects
     * it. "A data source was unavailable" would be true and useless.
     */
    function sentence(kind, why, nodes) {
        const what = LABELS[kind] || "A data source";
        const where = nodes && nodes.length
            ? " from data node “" + nodes.join("”, “") + "”"
            : "";
        return what + " for this project could not be loaded" + where + ": "
            + (why || "the machine holding it did not answer") + ".";
    }

    // -- the modal: a machine this Plexora can connect ------------------------

    /**
     * Ask whether to connect the machine this project reads from.
     *
     * Interrupting is the point. Everything else this file draws is a note
     * about something already settled; this is a project that has just opened
     * missing the thing the user came for, where one button restores it -- and
     * a dismissible strip at the top of a viewer is not how you ask a question.
     *
     * Resolves true when the page is on its way to being reloaded, so the
     * caller knows not to draw a banner behind a page that is leaving.
     */
    function offerToConnect(datasource, status) {
        const profiles = status.profiles || [];
        const names = profiles.map((row) => row.profile);
        const kinds = Object.keys(status.unavailable)
            .map((kind) => NOUNS[kind] || kind);

        const dialog = el("dialog", "connect-modal resource-modal");
        const head = el("div", "connect-modal-head");
        const heading = el("div", "connect-modal-heading");
        heading.append(
            el("h2", "connect-modal-title",
               listOf(names) + (names.length > 1 ? " are" : " is")
               + " not connected"),
            el("p", "connect-modal-subtitle",
               "This project reads its " + listOfWords(kinds) + " from "
               + (names.length > 1 ? "those machines" : "that machine") + "."));
        head.append(heading);

        const body = el("div", "connect-modal-body");
        const list = el("ul", "resource-modal-list");
        Object.keys(status.unavailable).forEach((kind) => {
            const item = el("li");
            item.append(el("span", "resource-modal-kind",
                           LABELS[kind] || "A data source"));
            // The server's own words for why. Not rewritten: "connection
            // refused" and "is not connected to this Plexora" are different
            // situations with the same button, and only one of them is the
            // user having pressed Disconnect.
            item.append(el("span", "resource-modal-why",
                           status.unavailable[kind]));
            list.append(item);
        });
        body.append(list);
        body.append(el("p", "resource-modal-note",
                       "Connecting reopens the data node on that machine and "
                       + "loads this project again. Everything already on this "
                       + "computer — your ROIs, figures and gates — is "
                       + "untouched either way."));

        const actions = el("div", "connect-modal-actions");
        //: Whether a connection attempt is in flight. It is what stops this
        //: promise resolving when the window closes: pressing Connect closes
        //: this dialog on the way to the next one, and the ANSWER -- whether
        //: to leave a note behind -- is not known until that one is finished
        //: with. Resolving on close alone meant a connection that failed or was
        //: cancelled left the user with no modal, no banner and no explanation
        //: for the layer that was still missing.
        let connecting = false;
        let settled = false;
        let finish = null;

        function close() {
            remember(ASKED_PREFIX, datasource);
            if (dialog.open) dialog.close();
        }

        actions.append(button("btn btn-outline-light", "Continue without it",
                              close));
        actions.append(el("span", "connect-modal-spacer"));
        profiles.forEach((row) => {
            actions.append(button("btn btn-primary",
                                  "Connect “" + row.profile + "”", () => {
                // This dialog closes FIRST. Two <dialog>s in the top layer
                // means two backdrops and two dim passes over the same page,
                // and the connection dialog is the one with something to say.
                connecting = true;
                close();
                connectAndReload(datasource, row.profile, kinds).then(finish);
            }));
        });

        dialog.append(head, body, actions);
        document.body.appendChild(dialog);

        return new Promise((resolve) => {
            finish = (leaving) => {
                if (settled) return null;
                settled = true;
                resolve(leaving);
                return null;
            };
            dialog.addEventListener("close", () => {
                dialog.remove();
                if (!connecting) finish(false);
            });
            // Escape means "stop showing me this", the same reading the
            // connection dialog gives it.
            dialog.addEventListener("cancel", () => {
                remember(ASKED_PREFIX, datasource);
            });
            dialog.showModal();
        });
    }

    /**
     * Open one saved connection, and on success read the project again and
     * reload the page. Resolves true when the page is on its way out.
     *
     * The project, then the page: a browser reload alone would find the server
     * still holding the project in exactly the shape it opened in -- see
     * `reload()`. Shared by the modal's Connect and the notice's Reconnect.
     */
    function connectAndReload(datasource, profile, kinds) {
        if (!window.PlexoraConnectionModal) return Promise.resolve(false);
        return Promise.resolve(window.PlexoraConnectionModal.open({
            name: profile,
            kind: "node",
            intent: "This project reads its " + listOfWords(kinds)
                    + " from that machine.",
        })).then((outcome) => {
            if (!outcome || !outcome.connected) return false;
            return reload(datasource).then(() => {
                window.location.reload();
                return true;
            });
        }).catch(() => false);
    }

    /** image, cell mask and cell table -- lower case, no quotes. */
    function listOfWords(words) {
        if (words.length < 2) return words[0] || "data";
        return words.slice(0, -1).join(", ") + " and " + words[words.length - 1];
    }

    // -- the notice: a machine that was up in this tab and is not now ----------

    /**
     * A small warning in the corner, until it is dismissed. Not a dialog: the
     * page goes on working around the missing layer, and a modal over a
     * viewer somebody is in the middle of using is the disruption this is.
     */
    function announce(datasource, status, gone, slow) {
        const toast = window.PlexoraToast;
        if (!toast) return null;
        const kinds = Object.keys(status.unavailable)
            .map((kind) => NOUNS[kind] || kind);
        const what = listOfWords(kinds);
        const note = listOf(gone) + " stopped"
            + " answering. The " + what + " for this project can't be read"
            + " until " + (gone.length > 1 ? "they are" : "it is") + " reconnected.";
        const profiles = status.profiles || [];
        const actions = profiles.length && window.PlexoraConnectionModal
            ? profiles.map((row, index) => ({
                label: "Reconnect “" + row.profile + "”",
                primary: index === 0,
                onSelect: () => {
                    // Closed by the press; if the connection does not come
                    // up, the notice comes back so the button is still there.
                    notice = null;
                    connectAndReload(datasource, row.profile, kinds).then((leaving) => {
                        if (!leaving) announce(datasource, status, gone, slow);
                    });
                },
            }))
            : [{
                label: "Open Settings",
                onSelect: () => { window.location.href = plexoraUrl("/settings"); },
            }];
        const lines = [];
        if (!profiles.length && status.reconnect) lines.push(status.reconnect);
        if (slow && slow.length) {
            lines.push("Tiles from “" + slow.join("”, “") + "” are being relayed "
                       + "through this server, which is slower.");
        }
        const key = datasource + "|" + gone.join(",");
        const handle = toast.show({
            tone: "warning",
            timeout: 0,
            title: gone.length > 1 ? "Remote servers disconnected"
                                   : "Remote server disconnected",
            note,
            lines,
            actions,
            onDismiss: (why) => {
                if (notice && notice.handle === handle) notice = null;
                // The ×, and only the ×: a newer notice taking its place, or
                // Reconnect being pressed, is not "stop telling me".
                if (why === "user") remember(DISMISS_PREFIX, datasource);
            },
        });
        notice = handle ? { key, handle } : null;
        return handle;
    }

    /**
     * Ask, and say something if there is anything to say. Never throws.
     *
     * `routing` is the already-resolved answer from PlexoraRouting, passed in
     * rather than re-fetched: it has been decided by the time a viewer exists,
     * and probing a second time would put another 1.5-second timeout in front
     * of the page. It also names the node each resource is read from, which is
     * how this knows which machines were working when the page loaded.
     */
    function report(datasource, routing) {
        return load(datasource).then((status) => {
            try {
                watchMasks(datasource, status);
            } catch (e) { /* a note about speed never stops the report */ }
            const bound = boundNodes(routing);
            if (!status || !status.unavailable
                || !Object.keys(status.unavailable).length) {
                // A project that opens whole ends the conversation about it.
                // Both memories are per tab and were keyed on the project
                // alone, so a project connected, used, disconnected and opened
                // again in one sitting -- which is an afternoon, not an edge
                // case -- was met with the silence of an answer given about a
                // situation that has since been fixed and broken again.
                forget(datasource);
                if (status) bound.forEach((node) => upThisSession.add(node));
                dropNotice(datasource);
                return null;
            }
            const missing = status.nodes || [];
            // The nodes this project reads from that are still fine.
            bound.filter((node) => !missing.includes(node))
                .forEach((node) => upThisSession.add(node));

            const gone = missing.filter((node) => upThisSession.has(node));
            if (gone.length) {
                // A disconnection IN THIS SESSION: something that was working
                // in this tab is not now.
                const key = datasource + "|" + gone.join(",");
                if (notice && notice.key === key && notice.handle?.isLive?.()) {
                    return notice.handle;
                }
                if (isDismissed(datasource)) return null;
                const slow = window.PlexoraRouting
                    ? PlexoraRouting.unreachable(routing) : [];
                return announce(datasource, status, gone, slow);
            }

            // Nothing missing was ever up in this tab -- typically a node left
            // on the map by a previous run. Not a disconnection, so no notice;
            // but a saved connection that can bring it back is worth one
            // question.
            const askable = (status.profiles || []).length
                && window.PlexoraConnectionModal
                && !remembered(ASKED_PREFIX, datasource);
            if (!askable) return null;
            return offerToConnect(datasource, status).then(() => null);
        }).catch(() => null);
    }

    // -- a mask a data node is converting --------------------------------------

    let maskReloader = null;
    let maskWatch = null;

    /** Register what redraws the label layer (main.js, once the viewer exists). */
    function onMaskReady(reload) {
        maskReloader = typeof reload === "function" ? reload : null;
    }

    function percentOf(progress) {
        if (!progress || !progress.total) return null;
        return Math.round(100 * progress.done / progress.total);
    }

    /**
     * What a node-served mask is doing, as a notice in the corner that stays
     * until it is dismissed. One at a time by construction: the finished
     * conversion repeats the warning the first report showed, and replaces it.
     */
    function maskNote(datasource, lines) {
        if (!lines.length || remembered(MASK_NOTE_PREFIX, datasource)) return null;
        const toast = window.PlexoraToast;
        if (!toast) return null;
        if (maskToast) maskToast.dismiss();
        const [first, ...rest] = lines;
        maskToast = toast.show({
            title: "About the cell mask",
            note: first,
            lines: rest,
            timeout: 0,
            onDismiss: (why) => {
                maskToast = null;
                if (why === "user") remember(MASK_NOTE_PREFIX, datasource);
            },
        });
        return maskToast;
    }

    function failedSentence(mask) {
        return "The cell mask could not be prepared on “" + mask.node + "”: "
            + (mask.error || "no reason was given")
            + ". Showing the unconverted mask, which is slower.";
    }

    /**
     * Say what a node-served mask is doing, and follow it while it converts.
     *
     * One watch per page: a report run again (a routing repair) finds the
     * watch already polling and leaves it be.
     */
    function watchMasks(datasource, status) {
        const masks = (status && status.masks) || [];
        const lines = [];
        masks.forEach((mask) => {
            if (mask.warning) lines.push(mask.warning);
            if (mask.state === "error") lines.push(failedSentence(mask));
        });
        maskNote(datasource, lines);
        const converting = masks.find((mask) => mask.state === "preparing");
        if (!converting || maskWatch) return;
        const wait = window.PlexoraSegmentationWait;
        const label = "Preparing the cell mask on " + converting.node + "…";
        wait?.start({ modal: false, label,
                      message: "The unconverted mask is shown meanwhile, "
                               + "which is slower." });
        const show = (mask) => {
            const percent = percentOf(mask.progress);
            wait?.progress({
                progress: percent,
                stageLabel: percent === null ? label
                    : label.replace("…", "") + " " + percent + "%",
                message: "Converting the cell mask on “" + mask.node
                    + "” into a pyramid. The unconverted mask is shown "
                    + "meanwhile, which is slower.",
            });
        };
        show(converting);
        maskWatch = window.setInterval(() => {
            load(datasource).then((next) => {
                const mask = ((next && next.masks) || [])
                    .find((row) => row.id === converting.id);
                if (!mask || mask.state === "preparing") {
                    if (mask) show(mask);
                    return;
                }
                window.clearInterval(maskWatch);
                maskWatch = null;
                if (mask.state === "error") {
                    wait?.failed(failedSentence(mask));
                    return;
                }
                wait?.ready();
                if (maskReloader) maskReloader(mask.version);
                if (mask.warning) maskNote(datasource, [mask.warning]);
                else if (maskToast) maskToast.dismiss();
            }).catch(() => null);
        }, MASK_POLL_MS);
    }

    return { report, load, reload, sentence, isDismissed, forget,
             offerToConnect, onMaskReady, connectAndReload,
             // For a probe: what this tab has seen working.
             _seenUp: () => [...upThisSession] };
})();
