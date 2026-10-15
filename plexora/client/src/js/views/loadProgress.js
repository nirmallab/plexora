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
 *
 * A large cell table is not part of that wait any more. It loads BEHIND the
 * viewer (data_model._load_table_behind): the image and the channel list are
 * there in a second, and the status names the table's own step while its
 * cells are read -- "Preparing cells · Mapping cells to images", a bar that
 * fills. So the chip stays up past the boot for exactly as long as the status
 * carries `table`, and when it goes the page takes the cells on in place
 * (`refreshDataset`, the Cells control, `plexora:table-ready` for any tool
 * that drew before them) -- no reload, and nothing the user had arranged in
 * the meantime is lost.
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

    /**
     * After the boot: the project is open and its cells are still coming.
     * Polls until the status stops carrying `table`, then takes them on --
     * or says why they never came. A reload of the open project is pending
     * too (a mask that finished converting reloads it), and a poll that did
     * not get through is asked again: neither is the cells arriving.
     */
    function watchTable(datasource, first) {
        const title = "The image is ready. The cell table is still being "
            + "prepared — cell tools turn on when it is.";
        const show = (table) => {
            const label = table?.stage_label || "Reading the cell table";
            paint(`Preparing cells · ${label}`,
                  typeof table?.progress === "number" ? table.progress : null, title);
        };
        if (first?.table?.status === "error") {
            failTable(first.table);
            return;
        }
        show(first?.table);
        // The Cells control shows Centroids as coming rather than offering a
        // click that would wait (views/viewerControls.js cellsPending).
        if (window.__plexora) {
            window.__plexora.cellsPending = true;
            window.__plexora.viewerControls?.refreshAvailability?.();
        }
        const check = async () => {
            const status = await read(datasource);
            if (status?.table?.status === "error") {
                failTable(status.table);
                return;
            }
            if (!status || status.table || status.status === "pending") {
                if (status?.table) show(status.table);
                window.setTimeout(check, POLL_MS);
                return;
            }
            await adoptTable(datasource);
            if (status.remote) {
                watchTiles(datasource);
                return;
            }
            paint("Cells ready", 100, "The cell table is loaded");
            window.setTimeout(hide, 1200);
        };
        window.setTimeout(check, POLL_MS);
    }

    /** The cells were being read behind the viewer and could not be. */
    function failTable(table) {
        const page = window.__plexora;
        if (page) page.cellsPending = false;
        try {
            page?.viewerControls?.refreshAvailability?.();
        } catch (error) {
            console.error("loadProgress: the Cells control could not repaint", error);
        }
        paint("The cell table could not be read", 100,
              table?.error || "Check the data file on the Edit page");
        window.setTimeout(hide, 8000);
    }

    /**
     * A cell table just attached to the open project -- the import dialog's
     * "Add data". Followed in when it is loading behind the viewer, taken on
     * at once when it was read in place.
     *
     * @param {string} datasource
     */
    async function track(datasource) {
        if (!datasource) return;
        const status = await read(datasource);
        if (status?.table || status?.status === "pending") {
            watchTable(datasource, status);
            return;
        }
        await adoptTable(datasource);
    }

    /** The cells have landed: the page re-reads what it holds about them. */
    async function adoptTable(datasource) {
        const page = window.__plexora;
        if (page) page.cellsPending = false;
        try {
            await page?.refreshDataset?.();
        } catch (error) {
            console.error("loadProgress: could not take the cells on", error);
        }
        try {
            page?.viewerControls?.refreshAvailability?.();
        } catch (error) {
            console.error("loadProgress: the Cells control could not repaint", error);
        }
        window.dispatchEvent(new CustomEvent("plexora:table-ready",
                                             { detail: { datasource } }));
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
                if (status && status.table) {
                    watchTable(datasource, status);
                } else if (window.__plexora?.cellsPending) {
                    // The boot's description came back without the cells
                    // (services/dataLayer.js), and they landed before this
                    // asked: taken on now rather than never.
                    await adoptTable(datasource);
                    if (status && status.remote) watchTiles(datasource);
                    else hide();
                } else if (status && status.remote) {
                    watchTiles(datasource);
                } else {
                    hide();
                }
            },
            () => { stopped = true; hide(); });
    }

    /**
     * A table being prepared by a request somebody stopped watching -- the
     * requirements form's "Continue in background". The chip reads the
     * table's own stages (`/get_table_status`) until `work` settles, then the
     * page takes the cells on, or says the save failed.
     *
     * @param {string} datasource
     * @param {Promise<boolean>} work  resolves true once the save succeeded
     */
    function follow(datasource, work) {
        if (!datasource || !work) return;
        const title = "The cell table is being prepared — keep working; cell "
            + "tools turn on when it is ready.";
        let settled = false;
        paint("Preparing cells…", null, title);
        const poll = async () => {
            if (settled) return;
            try {
                const response = await fetch(plexoraUrl("get_table_status") + "?"
                    + new URLSearchParams({ datasource }), { cache: "no-store" });
                const status = await response.json();
                if (!settled && status?.status === "pending") {
                    paint(`Preparing cells · ${status.stage_label || "Reading"}`,
                          typeof status.progress === "number" ? status.progress : null,
                          title);
                }
            } catch (error) { /* the save reports its own failure */ }
            if (!settled) window.setTimeout(poll, POLL_MS);
        };
        window.setTimeout(poll, POLL_MS);
        Promise.resolve(work).then(async (ok) => {
            settled = true;
            if (!ok) {
                paint("The cell table could not be prepared", 100,
                      "Open the form again to see why");
                window.setTimeout(hide, 6000);
                return;
            }
            await adoptTable(datasource);
            paint("Cells ready", 100, "The cell table is loaded");
            window.setTimeout(hide, 1200);
        }, () => { settled = true; hide(); });
    }

    return { watch, follow, track };
})();
