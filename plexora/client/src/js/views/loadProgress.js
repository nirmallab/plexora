/**
 * loadProgress.js -- opening a project, reported in the navbar.
 *
 * The viewer's first request that touches data (/get_channel_names) waits on
 * the server's load_datasource: the cell table, the mask, the image. Locally
 * that is usually a second. For a large table, or an image read from the web,
 * it is ten seconds to minutes -- and the page used to show an empty canvas
 * the whole time with nothing to say anything was happening, which reads as
 * a page that has failed and gets reloaded.
 *
 * So while the boot is waiting, this polls `/get_load_status` and shows the
 * step in the same chip the mask conversion uses (#segmentation_chip's look,
 * its own mount #load_chip, since both can be up at once): which step, a bar
 * that fills through the table's own stages, and for web data how much has
 * been downloaded. An image read from the web keeps the chip a little longer
 * -- until the first view has drawn -- because the tiles still have to come
 * over the network after the load itself is done.
 *
 * Asks nothing of the rest of the page. main.js calls `watch(promise)` with
 * the boot promise; the poll stops when that settles, whatever the server
 * says -- a "ready" can be a previous load's answer, so it is not an ending.
 */
window.PlexoraLoadProgress = (function () {
    "use strict";

    const POLL_MS = 500;
    //: Nothing is shown for a load that finishes inside this. A local project
    //: opening in 300 ms should not flash a chip.
    const SHOW_AFTER_MS = 600;
    //: The tiles-after-load phase gives up on its own after this, so a view
    //: that never reports fully loaded cannot leave the chip up for good.
    const TILES_MAX_MS = 120000;

    let root = null;
    let label = null;
    let fill = null;

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function mount() {
        if (root) return root;
        const host = document.getElementById("load_chip");
        if (!host) return null;
        label = el("span", "segmentation-chip-label", "Opening…");
        fill = el("span", "segmentation-chip-fill is-indeterminate");
        const track = el("span", "segmentation-chip-track");
        track.appendChild(fill);
        const body = el("span", "segmentation-chip-body");
        body.appendChild(label);
        body.appendChild(track);
        host.appendChild(body);
        root = host;
        return root;
    }

    function megabytes(bytes) {
        const mb = bytes / (1024 * 1024);
        return mb >= 10 ? `${Math.round(mb)} MB` : `${mb.toFixed(1)} MB`;
    }

    function paint(text, percent, title) {
        if (!mount()) return;
        root.hidden = false;
        label.textContent = text;
        root.title = title || text;
        if (typeof percent === "number") {
            fill.classList.remove("is-indeterminate");
            fill.style.width = `${Math.max(0, Math.min(100, percent))}%`;
        } else {
            fill.classList.add("is-indeterminate");
            fill.style.width = "";
        }
    }

    function hide() {
        if (root) root.hidden = true;
    }

    /** One reading of /get_load_status as the chip's words and bar. */
    function describe(status) {
        let text = status.message || status.stage_label || "Opening…";
        if (status.remote && typeof status.downloaded_bytes === "number"
                && status.downloaded_bytes > 0) {
            text += ` · ${megabytes(status.downloaded_bytes)} downloaded`;
        }
        // Only the table reports how far through it is; the mask and the image
        // are one call each, and a bar parked at a number would read as stuck.
        const percent = status.stage === "table" ? status.progress : null;
        const title = status.remote
            ? "Opening this project — reading data from the web"
            : "Opening this project";
        return { text, percent, title };
    }

    /**
     * After the load: keep the chip until the first view of a web image has
     * drawn. Tracked on OSD's own fully-loaded-change, per tiled image, the
     * same signal appStatus.js uses for its "Loading" state.
     */
    function watchTiles(datasource) {
        const viewer = window.__plexora?.seaDragonViewer?.viewer;
        if (!viewer?.world) { hide(); return; }
        const title = "The first view is being downloaded; later views of the "
            + "same area are served from the local cache";
        paint("Fetching image tiles from the web…", null, title);
        const started = Date.now();
        const check = async () => {
            const status = await read(datasource);
            if (status && typeof status.downloaded_bytes === "number"
                    && status.downloaded_bytes > 0) {
                paint(`Fetching image tiles · ${megabytes(status.downloaded_bytes)} downloaded`,
                      null, title);
            }
            const world = viewer.world;
            let pending = 0;
            for (let i = 0; i < world.getItemCount(); i++) {
                const item = world.getItemAt(i);
                if (item?.getFullyLoaded && !item.getFullyLoaded()) pending += 1;
            }
            if ((world.getItemCount() && !pending) || Date.now() - started > TILES_MAX_MS) {
                hide();
                return;
            }
            window.setTimeout(check, POLL_MS);
        };
        window.setTimeout(check, POLL_MS);
    }

    async function read(datasource) {
        try {
            const response = await fetch(plexoraUrl("get_load_status") + "?"
                + new URLSearchParams({ datasource }), { cache: "no-store" });
            return await response.json();
        } catch (error) {
            // The boot is what reports failure; a poll that cannot get
            // through is not worth saying anything about.
            return null;
        }
    }

    /**
     * Report the load `work` is waiting on, until it settles.
     *
     * @param {string} datasource
     * @param {Promise} work   the viewer's boot
     */
    function watch(datasource, work) {
        if (!datasource || !work) return;
        const began = Date.now();
        let stopped = false;

        const poll = async () => {
            if (stopped) return;
            const status = await read(datasource);
            if (stopped) return;
            if (status && status.status === "pending"
                    && Date.now() - began >= SHOW_AFTER_MS) {
                const view = describe(status);
                paint(view.text, view.percent, view.title);
            }
            if (!stopped) window.setTimeout(poll, POLL_MS);
        };
        poll();

        // Asked once more when the boot is done: a load quick enough to beat
        // the first poll never said whether it read from the web, and the
        // finished record still does.
        Promise.resolve(work).then(
            async () => {
                stopped = true;
                const status = await read(datasource);
                if (status && status.remote) watchTiles(datasource);
                else hide();
            },
            () => { stopped = true; hide(); });
    }

    return { watch };
})();
