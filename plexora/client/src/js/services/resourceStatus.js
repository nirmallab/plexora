/**
 * PlexoraResourceStatus -- turning a missing layer into a sentence.
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
 *   unavailable   The node could not be reached AT LOAD TIME, so the layer is
 *                 not in the project at all. Fixing it needs a reconnect and a
 *                 reload. This is the one worth a banner.
 *   unreachable   The layer loaded fine through this server, but the BROWSER
 *                 could not reach the node directly, so tiles take one extra
 *                 hop (see resourceRouting.js). Nothing is missing and nothing
 *                 is broken; it is slower. Mentioned in the same banner only
 *                 when there is already a banner, never on its own.
 *
 * Dismissible, and dismissal is remembered per project for the tab: somebody
 * who knows their laptop node is off and is working on the images anyway
 * should not be told again on every navigation inside the app.
 */
window.PlexoraResourceStatus = (function () {
    "use strict";

    const STORAGE_PREFIX = "plexora.resourceStatus.dismissed.";

    //: What each resource kind is called in a sentence. The route's keys are
    //: the server's words for them; these are the user's.
    const LABELS = {
        image: "The image",
        segmentation: "The cell mask",
        table: "The cell table",
    };

    function dismissKey(datasource) {
        return STORAGE_PREFIX + datasource;
    }

    function isDismissed(datasource) {
        try {
            return window.sessionStorage.getItem(dismissKey(datasource)) === "1";
        } catch (e) {
            return false;
        }
    }

    function remember(datasource) {
        try {
            window.sessionStorage.setItem(dismissKey(datasource), "1");
        } catch (e) { /* a tab with no storage simply asks again */ }
    }

    /** `{unavailable, nodes}` for this project, or null if it cannot be had. */
    function load(datasource) {
        if (!datasource) return Promise.resolve(null);
        const url = plexoraUrl("/resource_status?datasource="
            + encodeURIComponent(datasource));
        return fetch(url)
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
        if (status.reconnect) {
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
            remember(datasource);
            if (banner.parentNode) banner.parentNode.removeChild(banner);
        });
        banner.appendChild(close);
        return banner;
    }

    /**
     * Ask, and draw a banner if there is anything to say. Never throws.
     *
     * `routing` is the already-resolved answer from PlexoraRouting, passed in
     * rather than re-fetched: it has been decided by the time a viewer exists,
     * and probing a second time would put another 1.5-second timeout in front
     * of the page.
     */
    function report(datasource, routing, host) {
        const target = host || document.body;
        if (!target || isDismissed(datasource)) return Promise.resolve(null);
        return load(datasource).then((status) => {
            if (!status || !status.unavailable
                || !Object.keys(status.unavailable).length) {
                return null;
            }
            const slow = window.PlexoraRouting
                ? PlexoraRouting.unreachable(routing) : [];
            const banner = build(datasource, status, slow);
            target.insertBefore(banner, target.firstChild);
            return banner;
        }).catch(() => null);
    }

    return { report, load, sentence, isDismissed };
})();
