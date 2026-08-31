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
 *                 is broken; it is slower. Mentioned in the same banner only
 *                 when there is already a banner, never on its own.
 *
 * **A modal when this Plexora can fix it, a banner when it cannot.** The
 * server says which: `profiles` names a saved connection THIS server could
 * open, and a machine that is one button away is a question with an answer,
 * which is not the shape of a banner. Everything else -- a node somebody
 * registered by hand, or one whose tunnel belongs to a computer this server
 * cannot reach -- stays a banner with the command to run, because that is all
 * there honestly is to offer.
 *
 * Two memories, both per project and per tab, because they answer different
 * questions. `asked` means the modal has been shown and answered, so moving
 * around inside the app does not re-ask; `dismissed` means the user closed the
 * banner too, and wants nothing further said about it. Both are dropped the
 * moment the project opens whole -- they record an answer about a situation,
 * and a project that is fine has ended the one they were about.
 */
window.PlexoraResourceStatus = (function () {
    "use strict";

    const DISMISS_PREFIX = "plexora.resourceStatus.dismissed.";
    const ASKED_PREFIX = "plexora.resourceStatus.asked.";

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
                window.PlexoraConnectionModal.open({
                    name: row.profile,
                    kind: "node",
                    intent: "This project reads its " + listOfWords(kinds)
                            + " from that machine.",
                }).then((outcome) => {
                    if (!outcome || !outcome.connected) return finish(false);
                    // The project, then the page. A browser reload alone would
                    // find the server still holding the project in exactly the
                    // shape it opened in -- see `reload()`.
                    return reload(datasource).then(() => {
                        window.location.reload();
                        finish(true);
                    });
                }).catch(() => finish(false));
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

    /** image, cell mask and cell table -- lower case, no quotes. */
    function listOfWords(words) {
        if (words.length < 2) return words[0] || "data";
        return words.slice(0, -1).join(", ") + " and " + words[words.length - 1];
    }

    // -- the banner: everything the modal cannot offer ------------------------

    function build(datasource, status, slowNodes) {
        const banner = document.createElement("div");
        banner.className = "resource-status-banner";
        banner.setAttribute("role", "status");

        const icon = document.createElement("span");
        icon.className = "fas fa-triangle-exclamation";
        icon.setAttribute("aria-hidden", "true");
        banner.appendChild(icon);

        const body = document.createElement("div");
        body.className = "resource-status-body";
        Object.keys(status.unavailable).forEach((kind) => {
            const line = document.createElement("div");
            line.textContent = sentence(kind, status.unavailable[kind],
                                        status.nodes);
            body.appendChild(line);
        });

        const advice = document.createElement("div");
        advice.className = "resource-status-advice";
        const profiles = status.profiles || [];
        if (profiles.length && window.PlexoraConnectionModal) {
            // The same button the modal offered, kept where the user can find
            // it after saying "continue without it" -- otherwise the only way
            // back to it is a page reload, which is exactly what the person
            // who dismissed the question is not going to try.
            advice.appendChild(document.createTextNode(
                "This Plexora can open it for you. "));
            const act = button("resource-status-connect",
                               "Connect “" + profiles[0].profile + "”", () => {
                window.PlexoraConnectionModal.open({
                    name: profiles[0].profile, kind: "node",
                    intent: "This project reads part of its data from that "
                            + "machine.",
                }).then((outcome) => {
                    if (!outcome || !outcome.connected) return null;
                    return reload(datasource).then(() => {
                        window.location.reload();
                    });
                }).catch(() => null);
            });
            advice.appendChild(act);
        } else if (status.reconnect) {
            // A node a saved connection set up has its address and token
            // rewritten every session, so "check the address in Settings" is
            // advice that cannot work -- the entry is not wrong, the tunnel is
            // gone. The server names the command instead, because it is the
            // only actionable thing to say and it has to be run on the user's
            // own computer, which is exactly what is unreachable from here.
            advice.appendChild(document.createTextNode(
                status.reconnect + " Then reload this project. "));
        } else {
            advice.appendChild(document.createTextNode(
                "Reconnect it under Settings, then reload this project. "));
        }
        const link = document.createElement("a");
        link.href = plexoraUrl("/settings");
        link.textContent = "Open Settings";
        advice.appendChild(link);
        body.appendChild(advice);

        if (slowNodes && slowNodes.length) {
            const slow = document.createElement("div");
            slow.className = "resource-status-advice";
            slow.textContent = "Tiles from “" + slowNodes.join("”, “")
                + "” are being relayed through this server, which is slower "
                + "than reading them directly.";
            body.appendChild(slow);
        }
        banner.appendChild(body);

        const close = document.createElement("button");
        close.type = "button";
        close.className = "resource-status-dismiss";
        close.setAttribute("aria-label", "Dismiss");
        close.textContent = "×";
        close.addEventListener("click", () => {
            remember(DISMISS_PREFIX, datasource);
            if (banner.parentNode) banner.parentNode.removeChild(banner);
        });
        banner.appendChild(close);
        return banner;
    }

    /**
     * Ask, and say something if there is anything to say. Never throws.
     *
     * `routing` is the already-resolved answer from PlexoraRouting, passed in
     * rather than re-fetched: it has been decided by the time a viewer exists,
     * and probing a second time would put another 1.5-second timeout in front
     * of the page.
     */
    function report(datasource, routing, host) {
        const target = host || document.body;
        if (!target) return Promise.resolve(null);
        return load(datasource).then((status) => {
            if (!status || !status.unavailable
                || !Object.keys(status.unavailable).length) {
                // A project that opens whole ends the conversation about it.
                // Both memories are per tab and were keyed on the project
                // alone, so a project connected, used, disconnected and opened
                // again in one sitting -- which is an afternoon, not an edge
                // case -- was met with the silence of an answer given about a
                // situation that has since been fixed and broken again.
                forget(datasource);
                return null;
            }
            const slow = window.PlexoraRouting
                ? PlexoraRouting.unreachable(routing) : [];
            const askable = (status.profiles || []).length
                && window.PlexoraConnectionModal
                && !remembered(ASKED_PREFIX, datasource);
            if (!askable) {
                return isDismissed(datasource)
                    ? null : draw(target, datasource, status, slow);
            }
            return offerToConnect(datasource, status).then((leaving) => {
                // Nothing behind a page that is on its way out: the reload
                // will re-ask, and by then the answer should be different.
                if (leaving || isDismissed(datasource)) return null;
                return draw(target, datasource, status, slow);
            });
        }).catch(() => null);
    }

    function draw(target, datasource, status, slow) {
        const banner = build(datasource, status, slow);
        target.insertBefore(banner, target.firstChild);
        return banner;
    }

    return { report, load, reload, sentence, isDismissed, forget,
             offerToConnect };
})();
