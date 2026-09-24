/**
 * main.js Initializes main client/interface setup, and distributes events to respective views
 */

//EVENTHANDLER
const eventHandler = new SimpleEventHandler(d3.select("body").node());
const datasource = flaskVariables.datasource;

//VIEWS
const __plexora = window.__plexora = {
  dataLayer: null,
  channelList: null,
  seaDragonViewer: null,
  viewerSidebar: null,
  // Activated plugins by name: { definition, instance, sidebarController,
  // cleanups }. Replaces the single csv_gatingList slot -- core no longer has
  // a place that can only hold one plugin, or that names a particular one.
  plugins: new Map()
}

//DATA MANAGEMENT
const dataSrcIndex = 0;

//CHANNELS
const imageChannels = {}; // lookup table between channel id and channel name (for image viewer)
const imageChannelsIdx = {};

//OTHER SETTINGS
document.getElementById("openseadragon").addEventListener("contextmenu", (event) => event.preventDefault()); //Disable right clicking on element

//LOAD DATA
/**
 * Ask the server why this sample's image would not open, and say so.
 *
 * Every way an image can be unreadable -- moved, unreadable, truncated --
 * arrives in the browser the same way: a 500 per tile, or, when the load fails
 * before any tile is asked for, nothing at all. The canvas was simply blank
 * and the reason was in a terminal. `/image_status` classifies it and
 * viewerErrorState.js puts the answer where the picture should have been.
 *
 * Best-effort and never throws: it runs on paths that are already failing.
 */
async function reportImageFailure() {
    try {
        const response = await fetch(
            `${plexoraUrl("image_status")}?datasource=${encodeURIComponent(datasource)}`,
            { credentials: "same-origin" });
        if (!response.ok) return false;
        const report = await response.json();
        // `unavailable` -- a node that is not answering -- deliberately falls
        // through to nothing here. It already has a surface of its own
        // (services/resourceStatus.js), and that one can offer to reconnect the
        // machine, which is a better answer than a card that only explains.
        return Boolean(window.PlexoraViewerError?.show?.(report));
    } catch (error) {
        console.error("main: could not ask why the image failed", error);
        return false;
    }
}

// Data prevent caching on the config file, as it may have been modified
window.__plexoraReady = d3.json(`${plexoraUrl("config")}?t=${Date.now()}`).then(function (config) {
    return init(config[datasource]);
}).catch(async (error) => {
    // A boot that failed is very often a boot whose IMAGE failed -- /config
    // answers out of the project record and never opens a file, so the first
    // thing to actually touch the image is somewhere inside init(). Say which
    // before passing the rejection on.
    await reportImageFailure();
    throw error;
// From here on, an empty viewer means "showing nothing on purpose" rather than
// "has not started yet", and the centre spinner can stop on its own. `finally`
// rather than `then` so a boot that failed also takes it down -- the navbar
// chip reports the failure, and a spinner on top of that says the app is still
// trying. The rejection is passed on unchanged: toolLoader.js awaits this.
}).finally(() => window.PlexoraViewerLoader?.settle());

/**
 * Put back whatever the sample next door had arranged.
 *
 * Runs AFTER `__plexoraReady`, never inside it, and that is not a stylistic
 * choice: toolLoader's lazy path awaits `window.__plexoraReady` before it
 * activates a plugin, so reopening a tool from inside init() would be a
 * promise waiting on itself -- the boot would never finish and the spinner
 * would never stop. (loadTool's `started` option is the other half of this.)
 *
 * Channels and HD are NOT here; they have to happen before the channel slots
 * are built, so they are inside init() where that ordering exists.
 *
 * Every step is independent and every step is guarded. A tool that will not
 * open on this sample does not stop the layers being restored, and a layer
 * that no longer exists does not stop the cells being drawn the way they were.
 */
async function restoreCarriedState() {
    const carried = window.PlexoraCarryOver?.current?.();
    if (!carried || !__plexora.viewerSidebar) return;
    const components = carried.components || {};

    // Every module's own per-sample state first. THIS is what keeps a carried
    // marker from arriving with the previous sample's numbers: gating loads
    // this project's saved gates here, and only then is the marker re-imposed
    // on top of them.
    try {
        await __plexora.viewerSidebar.whenModulesApplied();
    } catch (error) {
        console.error("main: waiting for the sidebar's modules failed", error);
    }

    // -- tools ------------------------------------------------------------
    if (components.tools && window.PlexoraToolLoader?.restore) {
        try {
            const { skipped } = await window.PlexoraToolLoader.restore(
                components.tools, { started: true });
            if (skipped?.length) window.PlexoraCarryOver.report("tools", skipped);
            if (window.PlexoraToolLoader.loadedTools().length) {
                window.PlexoraCarryOver.applied();
            }
        } catch (error) {
            console.error("main: restoring the open tools failed", error);
        }
    }

    // -- each plugin's own selection --------------------------------------
    // After the tools are open and after this sample's own state has loaded,
    // so a plugin is re-imposing a CHOICE over numbers that are already this
    // image's. A plugin that cannot honour the choice says so and is skipped;
    // one that throws is skipped too, and costs the others nothing.
    for (const entry of (components.tools?.loaded || [])) {
        if (!entry?.state) continue;
        const controller = __plexora.plugins.get(entry.name)?.sidebarController;
        if (!controller?.applyCarryState) continue;
        try {
            const outcome = await controller.applyCarryState(entry.state);
            if (outcome?.skipped?.length) {
                window.PlexoraCarryOver.report(entry.name, outcome.skipped);
            } else {
                window.PlexoraCarryOver.applied();
            }
        } catch (error) {
            console.error(`main: "${entry.name}" could not take its state on`, error);
            window.PlexoraCarryOver.report(entry.name,
                [`${entry.name}: could not be restored on this sample`]);
        }
    }

    // -- layer sections ----------------------------------------------------
    // Transcripts and anything like it. Never a "loaded tool" -- a layer
    // section is part of what the viewer is for a sample that has the data, so
    // it is already mounted and only its selection has to be re-imposed.
    for (const [name, state] of Object.entries(components.sections || {})) {
        const controller = __plexora.plugins.get(name)?.sidebarController;
        if (!controller?.applyCarryState) continue;
        try {
            const outcome = await controller.applyCarryState(state);
            if (outcome?.skipped?.length) {
                window.PlexoraCarryOver.report(name, outcome.skipped);
            } else {
                window.PlexoraCarryOver.applied();
            }
        } catch (error) {
            console.error(`main: "${name}" could not take its state on`, error);
            window.PlexoraCarryOver.report(name,
                [`${name}: could not be restored on this sample`]);
        }
    }

    // -- cells and layers --------------------------------------------------
    // After the tools, deliberately. `selectMode` acts on the ACTIVE cell
    // layer, and which layer that is moves when a tool is shown and when each
    // plugin registers its own -- a mode applied before all that is repainted
    // over by syncToActiveLayer a moment later.
    restoreCarriedCells(components.cells);
    restoreCarriedLayers(components.layers);

    window.PlexoraCarryOver.flush();
}

/** How cells were drawn next door, where this sample can draw them that way. */
function restoreCarriedCells(cells) {
    if (!cells || !__plexora.viewerControls) return;
    const controls = __plexora.viewerControls;
    try {
        if (cells.mode && cells.mode !== controls.mode) {
            const button = document.querySelector(`[data-cell-mode="${cells.mode}"]`);
            if (button && !button.disabled) {
                // The same field a real click sets. Without it a background job
                // finishing (adoptSegmentation) would treat the restored mode
                // as a default nobody chose and overrule it.
                controls.userChose = true;
                controls.selectMode(cells.mode);
                window.PlexoraCarryOver.applied();
            } else {
                window.PlexoraCarryOver.report("cells",
                    [`Cells: this sample cannot draw ${cells.mode}`]);
            }
        }
        // Through the slider objects rather than the inputs: a non-silent set
        // raises the same input/change the user's drag does, which is what the
        // viewer is listening for.
        if (Number.isFinite(cells.pointSize)) {
            controls.pointSizeSlider?.set?.(cells.pointSize);
        }
        if (Number.isFinite(cells.opacity)) {
            controls.opacitySlider?.set?.(cells.opacity);
        }
    } catch (error) {
        console.error("main: restoring the cell display failed", error);
    }
}

/**
 * Which layers were drawn, and how strongly.
 *
 * Matched by id first and by what the layer IS second: two samples of one run
 * carry the same modalities under ids generated per sample, so a transcripts
 * layer should recognise its opposite number even when neither id matches. The
 * fallback only fires when exactly ONE layer here fits -- with two, "the
 * transcripts one" does not name either of them.
 */
function restoreCarriedLayers(layers) {
    if (!Array.isArray(layers) || !__plexora.layers) return;
    const stack = __plexora.layers;
    const missing = [];
    layers.forEach((carried) => {
        try {
            let target = carried.id ? stack.get(carried.id) : null;
            if (!target && carried.modality) {
                const matches = stack.layers().filter(
                    (layer) => layer.kind === carried.kind
                        && layer.spec?.modality === carried.modality);
                if (matches.length === 1) target = matches[0];
            }
            if (!target) {
                if (carried.id !== "__image__") missing.push(carried);
                return;
            }
            stack.setVisible(target.id, carried.visible);
            if (Number.isFinite(carried.opacity)) {
                stack.setOpacity(target.id, carried.opacity);
            }
            window.PlexoraCarryOver.applied();
        } catch (error) {
            console.error("main: restoring a layer failed", error);
        }
    });
    if (missing.length) {
        window.PlexoraCarryOver.report("layers", [
            missing.length === 1
                ? `Layer ${missing[0].modality || missing[0].id} is not in this sample`
                : `${missing.length} layers are not in this sample`,
        ]);
    }
}

// Consumed here, at the top level, so it is gone before anything can apply it
// twice -- and so a plain reload is a fresh open rather than a second arrival.
// Reading it is what makes `current()` answer for the rest of the page.
window.PlexoraCarryOver?.take?.(datasource);

// The continuation. Not awaited by anything and deliberately not part of
// `__plexoraReady`: a sample that arrives with nothing to restore must not
// wait on this, and a restore that fails must not make the boot look failed.
window.__plexoraReady
    .then(() => restoreCarriedState())
    .catch((error) => console.error("main: carrying the previous sample over failed", error));

//INITS

/**
 * Init all views.
 *
 * @param conf - The configuration json file
 */
async function init(config) {
    // Flat RGB quick-view datasource (no channels, no gating, no feature
    // table worth loading) -- hand off to the minimal pan/zoom-only viewer
    // and skip DataLayer/ChannelList/ViewerSidebar/module setup entirely.
    if (config.image_kind === "rgb") {
        const rgbViewer = new RgbImageViewer(config);
        __plexora.seaDragonViewer = rgbViewer;
        await rgbViewer.init();
        return;
    }

    //maximum selections
    config.maxSelections = 4;
    config.extraZoomLevels = 0;

    // Where each layer's tiles come from. For an ordinary project this resolves
    // to "everything is on this server" without a single probe, and the rewrite
    // below is exactly what it has always been. For a project reading from a
    // data node it decides, once per tab, whether this browser can reach that
    // node directly -- see services/resourceRouting.js. `let`, because a
    // reconnect mid-session replaces it -- see repairRouting below.
    let routing = await PlexoraRouting.load(datasource);

    // A layer that could not be loaded at all is already absent from `config`
    // by this point, so nothing below would ever mention it. Not awaited: the
    // banner is a note about something that has already happened, and making
    // the viewer wait for it would trade a working page for a message.
    if (window.PlexoraResourceStatus) {
        PlexoraResourceStatus.report(datasource, routing);
    }

    if (Array.isArray(config.imageData)) {
        config.imageData.forEach(function (channel) {
            if (channel.src && channel.src.startsWith("/")) {
                channel.src = plexoraUrl(channel.src);
            }
            // The address THIS server serves the layer at, kept apart from the
            // routed rewrite below so routing can be applied again later. A
            // reconnect changes a node's port and token, and re-deriving the
            // direct address from the previous direct address would strand the
            // one case that has to work: a route that has fallen back to the
            // proxy and later becomes direct again.
            channel.origSrc = channel.src;
        });
        applyRouting(routing);
    }

    /**
     * Point every layer's tile address at what `routing` says, from scratch.
     *
     * Idempotent over `origSrc`, so it can run again when the answer changes
     * -- at boot, and on every repair after a node reconnects or goes away.
     */
    function applyRouting(resolved) {
        const imageRoute = PlexoraRouting.tileSource(resolved, "image");
        const segRoute = PlexoraRouting.tileSource(resolved, "segmentation");
        // Which imageData entry the mask's tiles are served as, straight from
        // the layer list, instead of re-deriving "index 0, but only when the
        // project has a segmentation" here as well. One statement of the rule,
        // made by the side that owns it. -1 when there is no mask, which no
        // index equals.
        const labelIndex = (config.layers || [])
            .find((layer) => layer.kind === "labels")?.channelIndex ?? -1;
        (config.imageData || []).forEach(function (channel, index) {
            if (!channel.origSrc) return;
            const isLabel = index === labelIndex;
            const route = isLabel ? segRoute : imageRoute;
            if (!route) {
                // This server's own address: the proxy path, which is also the
                // only path for a local layer.
                channel.src = channel.origSrc;
                channel.srcQuery = "";
                return;
            }
            // An image names which channel in the path; a mask has one plane
            // and names nothing. The key is the last segment of the address
            // this server would serve the entry at.
            const key = channel.origSrc.replace(/\/+$/, "").split("/").pop();
            channel.src = route.appendKey ? route.base + key + "/" : route.base;
            // Carried apart from `src` rather than appended to it, because
            // `channel_add` reads the key back out of `src` by position and a
            // query string would move it.
            channel.srcQuery = route.query;
        });
    }
    // The blank frame's tile address. Built here rather than in ViewerManager
    // because this is where every other address is built, and through
    // `plexoraUrl` because a Plexora served under a base path has to reach its
    // own routes -- the channel addresses get away without it only because the
    // server writes them absolute.
    function blankFrameSrc() {
        return plexoraUrl(`generated/blank/${datasource}/`);
    }

    //channel information
    for (let idx = 0; idx < config["imageData"].length; idx++) {
        imageChannels[config["imageData"][idx].fullname] = idx;
        let name = config["imageData"][idx].name;
        if (name !== "Area") {
            imageChannelsIdx[idx] = name;
        }
    }

    //initialize metadata
    const dataLayer = new DataLayer(config, imageChannels);
    const numericData = new NumericData(config, dataLayer);
    const columns = await dataLayer.getChannelNames(true);
    // The first request in this function that actually opens the image, and so
    // the first that a missing, unreadable or corrupt file can fail. DataLayer
    // swallows its own transport errors and answers undefined, which used to
    // reach `new ChannelList(config, undefined, ...)` and throw a TypeError on
    // a spread -- a stack trace about a channel list, for a problem with a
    // file. Stopping here instead leaves a page whose navbar, status chip and
    // Prev/Next all still work, with the reason on the canvas.
    if (!Array.isArray(columns)) {
        await reportImageFailure();
        return;
    }
    let imgMetadata = null;
    try {
        imgMetadata = await dataLayer.getMetadata();
    } catch (error) {
        console.error("Error getting metadata:", error);
    }

    //Create channel panels
    channelList = new ChannelList(config, columns, dataLayer, eventHandler);
    __plexora.channelList = channelList;
    __plexora.dataLayer = dataLayer;

    // Plugins whose scripts this page loaded (see the server's Plugin.scripts).
    // Any number may be registered; each is activated below.
    const pluginDefs = window.Plexora?.plugins?.all() ?? [];

    //Create image viewer
    const imageArgs = [imgMetadata, numericData, eventHandler];
    const seaDragonViewer = new ImageViewer(config, dataLayer, ...imageArgs);
    __plexora.seaDragonViewer = seaDragonViewer;
    // Before any channel is added, so the world items ViewerManager claims land
    // in layers that already exist and already know their order.
    seaDragonViewer.syncLayers(config.layers);
    // The structural readback the browser harness asserts against. Pixel hashes
    // are proven insufficient for a change of this shape -- a rename once left
    // every hash identical with no GL error, because everything drew the same
    // while nothing was wired.
    __plexora.layers = seaDragonViewer.layerStack;
    // The Layers panel reads the stack and nothing else, so it holds no copy to
    // go stale -- and it subscribes, so a visibility change made anywhere shows
    // up here too.
    window.PlexoraLayerManager?.init(seaDragonViewer.layerStack);
    const viewerManager = new ViewerManager(seaDragonViewer, channelList);
    // A node that finishes converting this project's mask while it is open:
    // the status report watches it, and this redraws the layer from the
    // pyramid, at a new tile address.
    window.PlexoraResourceStatus?.onMaskReady?.(
        (version) => viewerManager.reloadLabelLayer(version));

    // Core viewer toggles (Centroids/HD/Outlines) -- unconditional, independent of
    // whichever tool (if any) is active, so they work on a plain base viewer too.
    const viewerControls = new ViewerControls(seaDragonViewer, config, eventHandler);
    __plexora.viewerControls = viewerControls;
    viewerControls.init();

    // Handed the metadata main.js already fetched rather than fetching its own:
    // the scale bar and this control are two readings of one payload, and the
    // first paint should not cost a second request to say so.
    const scaleCalibration = new ScaleCalibration(datasource, seaDragonViewer);
    __plexora.scaleCalibration = scaleCalibration;
    scaleCalibration.init(imgMetadata);

    // Say a mask is being built the moment the viewer is up, rather than when
    // the first poll answers. That poll is at the bottom of this function,
    // behind the sidebar's init and every plugin's -- and a user who attached a
    // mask seconds ago and was sent straight here would spend that whole stretch
    // looking at a viewer with no cells and no reason given. The panel it opens
    // takes its readings from the poll's announcements; it asks the server
    // nothing of its own.
    if (config.segmentation_status === 'pending') {
        window.PlexoraSegmentationWait?.start();
    }

    // Fire the database description request without awaiting it here --
    // ImageViewer.init() below never reads dd (only WebGL/OSD setup), so
    // there's no reason to serialize tile-viewer construction behind this
    // network round trip. Only channelList.init(dd) and each plugin's own
    // init(dd) below actually need dd, and they run after it resolves anyway.
    const ddPromise = dataLayer.getDatabaseDescription();

    const imageInit = [viewerManager, channelList, null, [], []];
    const [dd] = await Promise.all([ddPromise, dataLayer.init(), seaDragonViewer.init(...imageInit)]);
    __plexora.databaseDescription = dd;

    // HD, if the sample this walk came from had it on. Here and not earlier:
    // ImageViewer.init() is what assigns `viewerManagerVMain`, which the HD
    // checkbox's own handler reaches through. And through `presetHdMode`
    // rather than `setHdMode`, because the world is still empty -- see that
    // method for what remembering a viewport of nothing costs.
    const carriedChannelState = window.PlexoraCarryOver?.current?.()?.components?.channels;
    if (carriedChannelState?.hd) {
        try {
            viewerManager.presetHdMode(true);
            const hdCheckbox = document.querySelector("#viewer_controls_hd");
            if (hdCheckbox) hdCheckbox.checked = true;
        } catch (error) {
            console.error("main: could not carry HD mode over", error);
        }
    }
    // The dataset handed to every plugin -- same shape as the server's
    // plexora.api dataset, so a plugin reads roles rather than column names.
    __plexora.dataset = PlexoraDataset.build(config, imageChannels, dd);
    channelList.init(dd);

    // A brightfield project's one layer, added here rather than by the channel
    // sidebar: there is no channel to switch on, and nothing else would ever
    // put an image on screen. Everything after this point -- the mask, the
    // centroids, plugins -- works exactly as it does for a channel stack.
    if (config.image_kind === "brightfield") {
        viewerManager.load_brightfield_base();
        window.PlexoraBrightfieldAdjust?.init(seaDragonViewer);
    }

    // A sample with no image of its own -- transcripts, a table and a mask,
    // spots. Same move as the brightfield base and for the same reason:
    // something has to be in OSD's world for the viewport to have bounds and
    // for registered layers to have a position to stack against. What makes
    // the picture is whatever is registered on top of it.
    if (config.image_kind === "blank") {
        viewerManager.load_blank_base(blankFrameSrc());
    }

    // Registered image layers -- a second slide, an H&E beside a Xenium
    // morphology image. After the reference frame is in the world, because
    // `placementFor` sizes each one against it and `applyWorldOrder` needs
    // something to stack them relative to.
    viewerManager.syncLayerImages(config.layers);

    /**
     * Rebuild every tiled image off the addresses `config` now holds.
     *
     * The same remove-and-re-add the HD toggle uses, and for the same reason
     * (see ViewerManager.setHdMode): invalidating tiles in place leaves stale
     * canvases on screen. `currentChannels` still holds each layer's OLD url,
     * which is exactly what channel_remove matches world items against -- so
     * the order is fixed: applyRouting first, this second.
     */
    function rebuildTileLayers() {
        const world = seaDragonViewer.viewer && seaDragonViewer.viewer.world;
        if (!world) return;
        // A repair is not a reason to lose the user's place. Emptying the world
        // is what makes OpenSeadragon re-frame the whole image on the next add,
        // and this rebuild empties it exactly as the HD toggle does -- see
        // ViewerManager.rememberView, whose restore rides the first layer back.
        viewerManager.rememberView();
        Object.keys(channelList.currentChannels).map(Number).forEach((srcIdx) => {
            viewerManager.channel_remove(srcIdx);
            viewerManager.channel_add(srcIdx);
        });
        if (config.image_kind === "brightfield" || config.image_kind === "blank") {
            // The brightfield layer and the blank frame are in no
            // `currentChannels` map, so the loop above cannot have rebuilt
            // either. Dropped and re-added for the same reason the channels
            // are: their tile URLs now point somewhere else.
            for (let i = world.getItemCount() - 1; i >= 0; i -= 1) {
                const item = world.getItemAt(i);
                if (item?.source?.tileFormat === RGB_TILE_FORMAT) world.removeItem(item);
            }
            if (config.image_kind === "blank") viewerManager.load_blank_base(blankFrameSrc());
            else viewerManager.load_brightfield_base();
        }
        // Registered layers address their own tiles, so a routing repair moved
        // them too. Dropped wholesale and re-added rather than restyled: their
        // urls changed, which is exactly what `setStyle` cannot express.
        for (const handle of (viewerManager.tiledLayers || new Map()).values()) {
            handle.remove();
        }
        viewerManager.tiledLayers = new Map();
        viewerManager.syncLayerImages(config.layers);
        if (config.segmentation) {
            viewerManager.reloadLabelLayer();
        }
    }

    /**
     * Say again what is missing, now that the answer may have changed.
     *
     * One banner at a time: whatever strip is up is about the addresses that
     * were just replaced, and report() draws a fresh one when there is still
     * something to say (and clears its own per-tab memories when there is not).
     */
    function rereportResources(resolved) {
        if (!window.PlexoraResourceStatus) return;
        document.querySelectorAll(".resource-status-banner")
            .forEach((strip) => strip.remove());
        PlexoraResourceStatus.report(datasource, resolved);
    }

    //: The in-flight repair, shared: a burst of failing tiles and a reconnect
    //: event landing together is one re-resolution, not several racing ones.
    let repairing = null;

    /**
     * Re-resolve where tiles come from, and take the answer on in place.
     *
     * What a reconnect used to require a page reload for: the node comes back
     * on a new port with a new token, the server's own providers re-resolve on
     * their next call (nodes.address_generation), and this is the browser-side
     * counterpart -- without it every direct tile URL on the page kept the
     * dead address, which made each reconnect look like it had done nothing.
     */
    function repairRouting() {
        if (!window.PlexoraRouting) return Promise.resolve(null);
        if (repairing) return repairing;
        repairing = (async () => {
            const before = JSON.stringify(routing && routing.routes || {});
            const fresh = await PlexoraRouting.refresh(datasource);
            routing = fresh;
            rereportResources(fresh);
            if (JSON.stringify(fresh.routes || {}) === before) return null;
            applyRouting(fresh);
            rebuildTileLayers();
            return fresh;
        })().finally(() => { repairing = null; });
        return repairing;
    }
    __plexora.repairRouting = repairRouting;

    // A data node connecting or going away is the moment tile addresses can
    // change. remoteState announces the transition (it is already watching
    // whenever one can happen in this tab); repairing on it is what lets a
    // reconnect heal an open viewer instead of needing a reload nobody was
    // told about.
    window.addEventListener("plexora:remote-nodes-changed", () => {
        repairRouting();
    });

    //: How often failing tiles may trigger a repair attempt. A dead node fails
    //: tiles for as long as it is dead; asking the server once per window is
    //: what keeps that from becoming a poll.
    const TILE_FAILURE_REPAIR_MS = 30000;
    let lastTileRepair = 0;
    //: Whether the server has already been asked why the image is failing. The
    //: answer is a fact about a file and does not change while the page is
    //: open, so once is enough for a burst of hundreds.
    let askedWhyTilesFail = false;
    // A burst of failing tiles is how a moved or dead node actually presents
    // mid-session -- nothing else on the page is watching when no dialog is
    // open. Repairing answers both cases: an address that changed is taken on
    // in place, and a node that is gone from the map gets the resource-status
    // report (the modal with the Connect button) instead of silent timeouts.
    if (seaDragonViewer.viewer && seaDragonViewer.viewer.addHandler) {
        seaDragonViewer.viewer.addHandler("tile-load-failed", () => {
            const now = Date.now();
            if (now - lastTileRepair < TILE_FAILURE_REPAIR_MS) return;
            lastTileRepair = now;
            repairRouting();
            // Tiles can also start failing because the FILE went away under a
            // viewer that opened fine -- a drive unmounted, a file replaced
            // while being written. Asked once per page: the answer cannot
            // change without a reload, and a dead image fails tiles for as
            // long as it is dead.
            if (!askedWhyTilesFail) {
                askedWhyTilesFail = true;
                reportImageFailure();
            }
        });
        // An item that cannot be added at all -- the tile source itself would
        // not build. OpenSeadragon raises this rather than `open-failed` for
        // the inline sources every layer here is added with.
        seaDragonViewer.viewer.addHandler("add-item-failed", () => {
            if (askedWhyTilesFail) return;
            askedWhyTilesFail = true;
            reportImageFailure();
        });
    }

    /**
     * Re-fetch what the server says this project's numbers are.
     *
     * Both of the things a tool is drawn from -- the read spec in `config` and
     * the per-column statistics in `dd` -- are fetched once at page load. That
     * is fine right up until the user changes which matrix is read, or turns
     * the log transform on, from the requirements modal: the server re-reads
     * the table and clears its own caches, but the page is still holding the
     * ranges and histograms of the matrix it is no longer reading. Thresholding
     * then opens on a panel of X values with a read spec pointing at a layer.
     *
     * Both `config` and `dd` are updated in place rather than replaced. Each is
     * held by reference by things that outlive this call -- `config` by the
     * viewer, the channel list and ViewerControls; `dd` by ChannelList and
     * ViewerSidebar, which took it at boot (channelList.init(dd) /
     * viewerSidebar.init(dd)) and read their ranges and histograms straight out
     * of it ever after. Handing them a new object leaves them on the old one:
     * rebinding only __plexora's reference is what left Thresholding drawing a
     * log-valued slider over an X-valued histogram, since the gating panel
     * reads the sidebar's copy.
     */
    __plexora.refreshDataset = async function refreshDataset() {
        const fresh = await d3.json(`${plexoraUrl("config")}?t=${Date.now()}`);
        const entry = fresh?.[datasource];
        if (!entry) return null;
        // Whether a mask has been attached since this page was drawn -- which
        // is the one change that CANNOT be taken on in place. Attaching one
        // inserts the "Area" placeholder at imageData[0], and the label layer
        // is loaded from exactly that position (viewerManager.load_label_image),
        // so a page that adopted the new record without the new channel list
        // would draw a marker channel as the segmentation. Reported rather than
        // acted on: the caller knows whether the user just asked for this and
        // is expecting something to happen.
        const grew = (entry.imageData || []).length > (config.imageData || []).length;
        // Only the read spec: attaching a mask also rewrites imageData, and
        // adopting a new channel list mid-session would shift every index the
        // tile path and the channel sliders are keyed on.
        config.dataset = entry.dataset;
        // The conversion job's state, though, is safe and necessary: it is what
        // `watchSegmentation` starts a poll from, and a mask attached
        // mid-session is pending at this point with no Area channel adopted
        // yet -- so `segmentation` itself is deliberately NOT copied. See
        // adoptSegmentation, which is the only thing that may set it.
        if (!config.segmentation && entry.segmentation_status) {
            config.segmentation_status = entry.segmentation_status;
        }
        const description = await dataLayer.getDatabaseDescription();
        for (const [column, stats] of Object.entries(description || {})) {
            // Merged into the existing entry, not assigned over it: a channel's
            // image_min/image_max/image_histogram and its quantization window
            // are fetched lazily on activation (ChannelList.ensureChannelStats)
            // and live in these same entries. Only which numbers the feature
            // table holds has changed -- the image has not -- so they stay.
            dd[column] = { ...dd[column], ...stats };
        }
        __plexora.dataset = PlexoraDataset.build(config, imageChannels, dd);
        return { maskAttached: grew };
    };

    //: The keys in `dd` that describe the IMAGE rather than the feature table.
    //: They belong to the channel, not to a column of that name -- see
    //: adoptChannelNames below, which is the only thing that has to tell them
    //: apart. Fetched lazily per channel by ChannelList.ensureChannelStats.
    const IMAGE_SIDE_STATS = ["image_min", "image_max", "image_histogram",
                              "qmin", "qmax", "vmin_hint", "vmax_hint"];

    /**
     * Take on channel names the user has just uploaded, without a reload.
     *
     * A rename changes what each channel is CALLED and nothing else. The image
     * is the same file, imageData keeps its order, and so every index the tile
     * path, the channel slots and the sliders are keyed on still points where
     * it did. That is what makes this safe to do in place -- the same argument
     * adoptSegmentation below makes for a finished mask.
     *
     * Names, though, are keys all over the page: `imageChannels`, `dd`, the
     * channel list's per-name maps, the sidebar's slots. They have to move
     * together. A page that moved only some of them shows the renamed channel
     * twice -- once under its new name, once as a slot still naming a channel
     * the server no longer has -- and that slot's stats request is the one that
     * used to come back as a StopIteration (see data_model.real_channel_index).
     *
     * Mutated in place, never replaced, for the reason refreshDataset above
     * records: `config`, `imageChannels` and `dd` are held by reference by
     * things that outlive this call, including every plugin's `ctx.dataset`,
     * which reads all three live through getters (services/datasetContext.js).
     * Handing anyone a fresh object would leave them on the old names.
     *
     * @param names one per non-Area channel, in imageData order -- exactly
     *              what POST /upload_channels returns.
     * @returns whether it was applied. False means the image is not the shape
     *          this page thinks it is, and the caller should reload rather
     *          than rename things by guesswork.
     */
    __plexora.adoptChannelNames = async function adoptChannelNames(names) {
        const channels = (config.imageData || []).filter((c) => c.fullname !== "Area");
        if (!Array.isArray(names) || names.length !== channels.length) return false;

        // `index` is the position among the REAL channels -- the order `names`
        // arrives in, and the order `columns` and the channel list rows are in.
        const renames = [];
        channels.forEach((channel, index) => {
            const to = String(names[index]);
            if (channel.name === to && channel.fullname === to) return;
            renames.push({ index, fromShort: channel.name, fromFull: channel.fullname, to });
            channel.name = to;
            channel.fullname = to;
        });
        if (!renames.length) return true;

        // Rebuilt in place: DataLayer, ViewerSidebar and every plugin's
        // dataset.image.index hold these exact objects.
        Object.keys(imageChannels).forEach((key) => delete imageChannels[key]);
        Object.keys(imageChannelsIdx).forEach((key) => delete imageChannelsIdx[key]);
        config.imageData.forEach((channel, idx) => {
            imageChannels[channel.fullname] = idx;
            if (channel.name !== "Area") imageChannelsIdx[idx] = channel.name;
        });
        renames.forEach(({ index, to }) => { columns[index] = to; });

        // `dd` holds two different things under one key. The image side belongs
        // to the channel, so it moves with the rename -- the pixels did not
        // change, and re-fetching a histogram the page already has would blank
        // every open slider. The table side belongs to the column, so it is
        // re-read: which marker each channel now matches is the entire point of
        // renaming them.
        const carried = renames.map(({ fromFull }) => {
            const entry = dd[fromFull] || {};
            const kept = {};
            IMAGE_SIDE_STATS.forEach((key) => {
                if (entry[key] !== undefined) kept[key] = entry[key];
            });
            return kept;
        });
        // Every old key goes before any new one lands, so two channels that
        // swapped names do not delete what the other has just taken on.
        renames.forEach(({ fromFull }) => delete dd[fromFull]);
        const description = await dataLayer.getDatabaseDescription();
        for (const [column, stats] of Object.entries(description || {})) {
            dd[column] = { ...dd[column], ...stats };
        }
        renames.forEach(({ to }, i) => { dd[to] = { ...dd[to], ...carried[i] }; });

        channelList.renameChannels(renames);
        __plexora.viewerSidebar?.renameChannels(renames);
        // For anything else that keyed something by channel name and is not
        // core's to reach into. Nothing in core listens; a plugin can.
        window.dispatchEvent(new CustomEvent("plexora:channels-renamed", {
            detail: { renames: renames.map(({ fromFull, to }) => ({ from: fromFull, to })) },
        }));
        return true;
    };

    // The three calls toolLoader makes while painting a card, defined HERE
    // rather than beside activatePlugin below: a page opened with ?tool=<name>
    // reports its already-live tool through registerLoaded partway down this
    // function, which goes through show() and reaches all three. Assigned later,
    // they would be undefined at exactly that moment -- and every call site
    // guards with `?.`, so the boot path would silently skip them instead of
    // failing.

    /**
     * Point the shared controls at a tool the user has just switched to.
     *
     * Every layer that was visible STAYS visible -- that is the whole point of
     * layers. What moves is which one the Cells control, the opacity slider,
     * picking and the gate flows act on.
     *
     * Only for plugins that declared ownsCellLayer: a tool that draws its own
     * overlay (ROI) must not take the active layer off the one that legitimately
     * holds it merely by being looked at. The controls are re-synced either way,
     * because opening ROI does not change what they should show.
     */
    __plexora.setActiveTool = function setActiveTool(name) {
        const record = name ? __plexora.plugins.get(name) : null;
        if (record?.instance && record.definition?.ownsCellLayer) {
            seaDragonViewer.setActiveCellLayer(name);
        }
        viewerControls.syncToActiveLayer();
        return seaDragonViewer.cellLayerOwner === name;
    };

    /**
     * Draw a tool's layer, or stop drawing it, without unloading the tool.
     *
     * Returns whether this plugin has a layer at all -- a tool that draws its
     * own overlay answers false, and its card's toggle is its controller's to
     * honour (see the onVisibilityChange hook in pluginRegistry.js).
     */
    __plexora.setToolLayerVisible = function setToolLayerVisible(name, visible) {
        const record = __plexora.plugins.get(name);
        if (!record?.definition?.ownsCellLayer) return false;
        // Only on a real change, and only then: the mask item and the point
        // overlay are shared, so whether they are needed at all is a question
        // about the whole stack rather than about this one layer.
        if (seaDragonViewer.setCellLayerVisible(name, visible)) {
            viewerControls.refreshLayerSurfaces();
        }
        return true;
    };

    /** Restack the cell layers, bottom of the sidebar order first. */
    __plexora.setToolLayerOrder = function setToolLayerOrder(names) {
        return seaDragonViewer.setCellLayerOrder(names);
    };

    // Instantiate plugins after the viewer exists but before tile loading gets
    // going, matching the order the single-module path used to run in.
    for (const definition of pluginDefs) {
        activatePluginInstance(definition, dd);
    }

    //EVENT HANDLING

    /**
     * Listen to Color Transfer Change Events and forwards it to respective views.
     *
     * @param d - The color map object
     */
    const actionColorTransferChange = (d) => {
        //map to full name
        d.name = dataLayer.getFullChannelName(d.name);
        // d3.select('body').style('cursor', 'progress');
        seaDragonViewer.updateChannelColors(d.name, d.color, d.type);
        // d3.select('body').style('cursor', 'default');
    };
    eventHandler.bind(ChannelList.events.COLOR_TRANSFER_CHANGE, actionColorTransferChange);

    /**
     * Listen to Render Mode Events and forwards it to respective views.
     *
     * @param d - The render mode object
     */
    const actionRenderingModeChange = (d) => {
        seaDragonViewer.updateRenderingMode(d);
    };
    eventHandler.bind(ImageViewer.events.renderingMode, actionRenderingModeChange);

    /**
     * Listen to Channels set for Rendering and forwards it to respective views.
     *
     * @param d - The channel package object
     */
    const actionChannelsToRenderChange = (d) => {
        //map to full name
        d.name = dataLayer.getFullChannelName(d.name);

        //send to image viewer
        const action = ["remove", "add"][+d.status];
        seaDragonViewer.updateActiveChannels(d.name, action);
        // The tiles this kicks off are reported by the status indicator via
        // OSD's fully-loaded-change (see appStatus.js watchViewer). The
        // cursor:progress that used to bracket this call was set and cleared
        // in the same synchronous turn, so it was never actually on screen
        // while the work it described was running.
    };
    eventHandler.bind(ChannelList.events.CHANNELS_CHANGE, actionChannelsToRenderChange);

    /**
     * Listen to regional or single cell selection.
     *
     * @param d - The selections
     */
    const actionImageClickedMultiSel = (d) => {
        // Nothing to select against without a feature table, or without a
        // cell id naming the column that identifies a row.
        const idField = __plexora.dataset?.schema?.cellId;
        if (!idField) return;
        const task = window.PlexoraStatus?.begin("Selecting");
        try {
            // add newly clicked item to selection
            if (!Array.isArray(d.item)) {
                dataLayer.addToCurrentSelection(d.item, true, d.clearPriors);
                const picked = [d.item[idField]];
                updateSeaDragonSelection({ picked });
            } else {
                dataLayer.addAllToCurrentSelection(d.item);
                const picked = d.item.map(i => i[idField]);
                updateSeaDragonSelection({ picked });
            }
        } finally {
            task?.done();
        }
    };
    eventHandler.bind(ImageViewer.events.imageClickedMultiSel, actionImageClickedMultiSel);

    /**
     * Listen to Channel Select Click Events.
     *
     * @param sels - The selected/deselected channels
     */
    const channelSelect = async (sels) => {
        updateSeaDragonSelection();
        let channelCells = await dataLayer.getChannelCellIds(sels);
        dataLayer.addAllToCurrentSelection(channelCells);
    };
    eventHandler.bind(ChannelList.events.CHANNEL_SELECT, channelSelect);

    /**
     * Listens to and updates based on selection changes (specific for seadragon).
     *
     * @param props - may contain cell id
     */
    function updateSeaDragonSelection(props = {}) {
        if ("picked" in props) {
          seaDragonViewer.pickedIds = props.picked;
        }
        seaDragonViewer.forceRepaint();
    }

    /**
     * Ranges one layer's plugin wants drawn, or {} when there is none.
     *
     * @param name - whose layer to ask. Null means the ACTIVE one, which is what
     *   core's own paths want; a plugin's gate flow passes its own name so a
     *   tool the user has switched away from still gates its OWN cells rather
     *   than whichever layer happens to be active.
     *
     * Core asks the viewer rather than reading a named plugin's state, so these
     * paths work for any plugin -- or none.
     */
    function cellFilterFor(name = null) {
        const provider = name
            ? seaDragonViewer.getCellLayer(name)?.provider
            : seaDragonViewer.cellLayer;
        return provider?.getColorCodedRanges?.() ?? {};
    }

    function currentCellFilter() {
        return cellFilterFor(null);
    }

    let centroidGateRequest = 0;
    const updateCentroidsForGate = async (name = null) => {
        if (!seaDragonViewer.shouldDrawCentroids()) return;
        const requestId = ++centroidGateRequest;
        seaDragonViewer.setLoading(true);
        try {
            await seaDragonViewer.ensureCentroidsReady(false);
            if (requestId === centroidGateRequest) {
                seaDragonViewer.updateCentroidFilter(cellFilterFor(name), true);
            }
        } finally {
            seaDragonViewer.setLoading(false);
        }
    };
    let segmentationGateRequest = 0;
    const updateSegmentationForGate = async (showSpinner = true, name = undefined) => {
        if (!seaDragonViewer.viewerManagerVMain?.sel_outlines) return;
        const requestId = ++segmentationGateRequest;
        await seaDragonViewer.updateSegmentationFilter(
            cellFilterFor(name ?? null), showSpinner, name);
        if (requestId !== segmentationGateRequest) {
            seaDragonViewer.forceRepaint();
        }
    };
    // Runs on every move tick, as fast as the network+render round trip allows, rather than
    // waiting on a fixed interval: if a request is still in flight when the next tick arrives,
    // it's marked pending and replayed immediately (with the latest gate values) as soon as the
    // in-flight one finishes, so the mask keeps following the handle continuously while dragging.
    let segmentationGateRunning = false;
    let segmentationGatePending = false;
    const runSegmentationGate = async (showSpinner, name = undefined) => {
        if (!seaDragonViewer.viewerManagerVMain?.sel_outlines) return;
        if (segmentationGateRunning) {
            segmentationGatePending = true;
            return;
        }
        segmentationGateRunning = true;
        try {
            await updateSegmentationForGate(showSpinner, name);
            while (segmentationGatePending) {
                segmentationGatePending = false;
                await updateSegmentationForGate(false, name);
            }
        } finally {
            segmentationGateRunning = false;
        }
    };

    eventHandler.bind(ChannelList.events.BRUSH_MOVE, (d) => {
        const fullName = dataLayer.getFullChannelName(d.name);
        seaDragonViewer.updateChannelRange(fullName, d.dataRange[0], d.dataRange[1]);
    });

    /**
     * Reset the (core) channel list to its initial values. Add-on modules hook their own
     * reset behavior onto this same event via bindEvents() below.
     */
    const reset_lists = () => {
        channelList.resetChannelList();
        seaDragonViewer.forceRepaint();
    };
    eventHandler.bind(ChannelList.events.RESET_LISTS, reset_lists);

    for (const definition of pluginDefs) {
        bindPluginEvents(definition);
    }

    if (typeof ViewerSidebar !== "undefined" && document.getElementById("viewer_sidebar")) {
        const viewerSidebar = new ViewerSidebar(config, columns, dataLayer, eventHandler, channelList);
        __plexora.viewerSidebar = viewerSidebar;
        for (const definition of pluginDefs) {
            const controller = createPluginSidebar(definition, viewerSidebar);
            if (!controller) continue;
            viewerSidebar.registerModule(controller);
            // A LAYER section is not a tool and must not be registered as one:
            // toolLoader's standDown() folds whatever is showing the moment any
            // tool opens, so opening Gating would have switched the transcripts
            // off. It gets the same lifecycle from viewerSidebar above, and its
            // chrome -- grip, chevron, title, eye, X -- from the card its panel
            // was moved into. See views/layerSections.js and layerManager.js.
            if (window.PlexoraLayerSections?.isLayerSection(definition.name)) {
                window.PlexoraLayerSections.register(definition.name, controller);
                continue;
            }
            // Tell toolLoader.js this tool is already live (rendered server-side via
            // a direct/bookmarked ?tool= link) so its close button and any later
            // Tools-menu click work off the real state instead of re-fetching and
            // re-activating a plugin that is already registered.
            if (window.PlexoraToolLoader) {
                const slotIds = Array.from(
                    document.querySelectorAll(`[data-tool-mount="${definition.name}"]`)
                ).map((el) => el.id);
                window.PlexoraToolLoader.registerLoaded(definition.name, slotIds, controller);
            }
        }
        await viewerSidebar.init(dd);
    }

    // Segmentation-mask processing (pyramid/outline generation) runs in a
    // background job on upload -- see data_model.start_segmentation_job -- so
    // the viewer can open before it's done. Poll until it's ready (or errors
    // out), then take the finished mask on in place.
    //
    // This used to call location.reload(). On a large mask that job runs for
    // minutes, so the reload landed well into a working session: the viewer
    // went blank and came back at the default viewport with every channel,
    // tool, gate and pan/zoom gone, and nothing on screen said why. It is also
    // avoidable -- see adoptSegmentation below for why nothing needs rebuilding.
    //
    // Each reading is announced as well as acted on. A plugin that is waiting
    // for this mask rather than drawing a substitute (viewerControls'
    // enableCellLayer) has to be able to say how far along it is, and the poll
    // that already knows is the only thing that should be asking the server.
    //: Whether a poll loop is already running, so starting one twice does not
    //: double the request rate. See watchLayers below.
    let watchingLayers = false;

    /**
     * Whether anything about this sample is still being prepared.
     *
     * The mask is the case that already existed. A transcript layer whose
     * tiles are still being built is the same shape of thing, and this is the
     * one gate both go through -- a sample with nothing pending never starts a
     * poll at all, which is what keeps an ordinary project paying nothing for
     * any of this.
     */
    function anythingPending() {
        if (config.segmentation_status === 'pending') return true;
        return (config.layers || []).some((layer) => layer.status === 'pending');
    }

    /**
     * Write a layer's build outcome onto the config every surface reads.
     *
     * So `syncLayerImages` can tell a layer that is ready to be drawn from one
     * that is not, and so the Layers panel's card stops saying "Preparing"
     * once it is not.
     */
    function markLayerStatus(id, status) {
        for (const list of [config.layers, config.spatialLayers]) {
            const found = (list || []).find((layer) => layer.id === id);
            if (found) found.status = status;
        }
        const record = seaDragonViewer.layerStack?.get(id);
        if (record?.spec) record.spec.status = status;
    }

    /**
     * Start (or keep) the wait for whatever this sample is still preparing.
     *
     * ONE loop, over `/import/status`, which composes the mask's conversion
     * job and every layer builder's into a single document. It replaces the
     * two progress vocabularies that used to exist side by side -- the
     * segmentation job's percentage bands, and the transcripts plugin's
     * private `{stage, done, total}`, which no other surface could read and
     * which a server restart lost entirely.
     *
     * The `plexora:segmentation-*` events are emitted exactly as before, with
     * the same detail, because Cell Explorer, the viewer controls and
     * `segmentationWait` all listen for them and none of them has any reason
     * to know that the poll behind them moved.
     *
     * Idempotent, and cheap to call when nothing is pending: it starts a loop
     * only when something actually is and no loop is already going.
     */
    __plexora.watchLayers = function watchLayers() {
        if (watchingLayers || !anythingPending()) return;
        watchingLayers = true;
        const announce = (what, detail) => window.dispatchEvent(
            new CustomEvent(`plexora:segmentation-${what}`, { detail }));
        //: The per-layer counterpart. Same three verbs plus a `layer`, so a
        //: card can follow one build without every listener having to filter
        //: the mask out of the events above.
        const announceLayer = (what, detail) => window.dispatchEvent(
            new CustomEvent(`plexora:layer-${what}`, { detail }));
        //: Layers already reported as finished, so a loop still running for
        //: the mask does not announce the same transcript build every 1.5 s.
        const settled = new Set();
        //: Consecutive polls that came back with nothing. getLayerStatus
        //: swallows its own errors and returns undefined, so this is the only
        //: way to tell a dead server from a job that is simply still running.
        let silent = 0;

        const pollLayerStatus = async () => {
            const document_ = await dataLayer.getLayerStatus();
            const layers = document_?.layers || null;
            if (!layers) {
                if ((silent += 1) > 10) {
                    // Long enough that no ordinary hiccup reaches it, short
                    // enough that a panel waiting on this loop is not left
                    // waiting on a server that is never going to answer.
                    watchingLayers = false;
                    announce('failed', { error: '' });
                    return;
                }
                // One bad answer must not abandon a job that is still running
                // server-side, which is why the count has to run out first.
                window.setTimeout(pollLayerStatus, 1500);
                return;
            }
            silent = 0;

            const mask = layers['__mask__'];
            if (mask && !settled.has('__mask__')) {
                if (mask.status === 'ready' && config.segmentation_status === 'pending') {
                    settled.add('__mask__');
                    adoptSegmentation(mask.segmentation);
                    announce('ready', { segmentation: mask.segmentation });
                } else if (mask.status === 'failed') {
                    settled.add('__mask__');
                    announce('failed', { error: mask.error || '' });
                } else if (mask.status === 'pending') {
                    announce('progress', {
                        progress: typeof mask.progress === 'number' ? mask.progress : null,
                        message: mask.message || '',
                        //: Which phase, so a surface can show the rail as well
                        //: as the bar. Absent from a restarted server's
                        //: answer, which is why every consumer treats it as
                        //: optional.
                        stage: mask.stage || null,
                        stageLabel: mask.stage_label || '',
                    });
                }
            }

            for (const [id, entry] of Object.entries(layers)) {
                if (id === '__mask__' || settled.has(id)) continue;
                if (entry.status === 'ready') {
                    settled.add(id);
                    markLayerStatus(id, 'ready');
                    announceLayer('ready', { layer: id });
                    // Drawn the moment it is drawable, rather than at the next
                    // adopt: a transcript density that appears while the user
                    // watches is the whole reason the import opens early.
                    viewerManager.syncLayerImages(config.layers);
                } else if (entry.status === 'failed') {
                    settled.add(id);
                    markLayerStatus(id, 'failed');
                    announceLayer('failed', {
                        layer: id,
                        error: entry.error || '',
                        //: The command that would make this build possible, when
                        //: the failure was a missing dependency. Offered rather
                        //: than a stack trace shown to somebody who cannot act
                        //: on one.
                        install: entry.install || '',
                    });
                } else {
                    announceLayer('progress', {
                        layer: id,
                        progress: typeof entry.progress === 'number' ? entry.progress : null,
                        message: entry.message || '',
                        stage: entry.stage || null,
                        stageLabel: entry.stage_label || '',
                    });
                }
            }

            if (!document_.pending) {
                // Everything landed. Stopping here rather than running the
                // silent counter out: a sample whose only pending thing was a
                // layer would otherwise poll ten more times for nothing.
                watchingLayers = false;
                return;
            }
            window.setTimeout(pollLayerStatus, 1500);
        };
        // Asked straight away rather than after a first interval: something may
        // be showing a progress bar with nothing in it until this answers.
        pollLayerStatus();
    };

    //: The name this poll had when a mask was the only thing it waited for.
    //: Kept because the Cells control and the requirements modal call it by
    //: name, and neither has any reason to know that it now waits for a
    //: transcript build as well.
    __plexora.watchSegmentation = __plexora.watchLayers;

    /**
     * Take on layers that were registered after this page opened.
     *
     * Where "+ Add Layer" and the requirements modal both end. It re-reads
     * `/config` rather than trusting what the import route handed back,
     * deliberately: the server computes `layers` from the project record, and
     * a client that assembled its own copy would be a second implementation of
     * `Project.all_layers` waiting to disagree with the first.
     *
     * `imageData` is NEVER adopted -- the rule `refreshDataset` already
     * follows. Channel indices are wired into the GL pass, the channel list
     * and every open plugin, and swapping them under a live page is what a
     * reload is for. A new MASK is the one case that needs one, and the import
     * route says so in its answer rather than leaving this to guess.
     */
    __plexora.adoptLayers = async function adoptLayers() {
        const fresh = await d3.json(`${plexoraUrl("config")}?t=${Date.now()}`);
        const entry = fresh?.[datasource];
        if (!entry) return null;
        config.layers = entry.layers || [];
        config.spatialLayers = entry.spatialLayers || [];
        config.bundles = entry.bundles || [];
        seaDragonViewer.syncLayers(config.layers);
        viewerManager.syncLayerImages(config.layers);
        __plexora.watchLayers();
        return config.layers;
    };

    __plexora.watchLayers();

    /**
     * Take on the mask the background job just finished, without a reload.
     *
     * Nothing about the page is stale except one fact. The "Area" placeholder
     * channel is inserted into imageData when the mask is *attached* (see
     * import_routes.attach_segmentation), not when its pyramid lands, so no
     * channel index shifts here and nothing keyed off one has to be rebuilt.
     * The only thing that changes is config.segmentation going from null to a
     * path -- and the label layer is loaded lazily from that, on demand, by
     * viewerManager.load_label_image().
     *
     * @param path the derived pyramid the job produced.
     */
    function adoptSegmentation(path) {
        if (!path || config.segmentation) return;
        config.segmentation = path;
        config.segmentation_status = 'ready';
        // Both were set when the viewer opened and found no mask to fetch;
        // clearing them is what lets the layer be requested now. `noLabel` also
        // short-circuits ensureSegmentationReady(), so it has to go first.
        seaDragonViewer.noLabel = false;
        if (seaDragonViewer.viewerManagerVMain) {
            seaDragonViewer.viewerManagerVMain.labelLayerRequested = false;
        }

        // Outlines and Filled were disabled while there was no mask to draw,
        // and now there is one. Done before the swap below so the control is
        // right either way -- a user who is drawing nothing on purpose still
        // gains the option they were previously not offered.
        viewerControls.refreshAvailability();

        // A tool asked for this mask and was told to wait rather than given
        // centroids instead (viewerControls.enableCellLayer). Nothing is drawing
        // yet, so this is the moment the layer it asked for actually turns on.
        if (seaDragonViewer.cellLayerAwaitingMask) {
            seaDragonViewer.cellLayerAwaitingMask = false;
            viewerControls.selectMode(
                viewerControls.maskMode(viewerControls.ownerMaskPreference()));
            return;
        }

        // Nothing is drawn, and nobody chose that. Through the whole conversion
        // None was the only enabled button -- Outlines and Filled were greyed
        // with "still being prepared" on them -- so "none" here is where the
        // control started rather than an answer anyone gave, and the mask the
        // user just attached is what they attached it to see. `userChose` is
        // what keeps this from overruling a real click on None.
        if (viewerControls.mode === 'none' && !viewerControls.userChose) {
            viewerControls.selectMode(
                viewerControls.maskMode(viewerControls.ownerMaskPreference()));
            return;
        }

        // Swap the drawing over only when what is showing is the fallback this
        // very absence caused. A user who chose Centroids themselves gets to
        // keep them -- that is not a state a finished background job should
        // overrule.
        if (viewerControls.mode !== 'centroids') return;
        if (!seaDragonViewer.centroidsFromFallback) return;
        // Drawn the way whatever holds the cell layer asks for, not always as
        // outlines. A project that gained its mask from the edit page reaches
        // this path rather than enableCellLayer's, and hardcoding outlines here
        // meant the tool's own preference was honoured on every later page load
        // and never on the one where the mask actually arrived.
        viewerControls.selectMode(
            viewerControls.maskMode(viewerControls.ownerMaskPreference()));
    }

    /**
     * Record of one activated plugin. Cleanups let a plugin release globals it
     * registered (window listeners, timers) when it is torn down -- the old
     * single-module world never needed this because the one module lived for
     * the life of the page.
     */
    function pluginRecord(definition) {
        let record = __plexora.plugins.get(definition.name);
        if (!record) {
            record = { definition, instance: null, sidebarController: null, cleanups: [] };
            __plexora.plugins.set(definition.name, record);
        }
        return record;
    }

    /**
     * `ctx.layers` -- the viewer's rendering primitives, scoped to one plugin.
     *
     * Every id a plugin registers is prefixed with its name, so two plugins
     * cannot collide and a plugin being torn down can have everything it drew
     * removed without knowing what that was. The prefix is added here rather
     * than asked for, because "remember to namespace your ids" is a rule that
     * gets followed until it does not.
     */
    /**
     * One layer as a plugin sees it.
     *
     * The stack record's presentation facts plus the four the server sends
     * that say what this layer IS and whether it is usable yet. Flattened off
     * `spec` rather than handed through, so a plugin reads `layer.modality`
     * and not `layer.spec.modality` -- the nesting is core's storage detail.
     */
    function layerRecord(layer) {
        const spec = layer.spec || {};
        return {
            id: layer.id,
            kind: layer.kind,
            label: layer.label,
            visible: layer.visible,
            opacity: layer.opacity,
            transform: layer.transform ? [...layer.transform] : null,
            modality: spec.modality || null,
            source: spec.source || null,
            status: spec.status || "ready",
            unresolved: spec.unresolved || [],
            src: spec.src || null,
            width: spec.width ?? null,
            height: spec.height ?? null,
            maxLevel: spec.maxLevel ?? null,
            tileWidth: spec.tileWidth ?? null,
            tileHeight: spec.tileHeight ?? null,
        };
    }

    function pluginLayerApi(definition, record) {
        const stack = () => seaDragonViewer.layerStack;
        const own = (id) => `${definition.name}:${id}`;
        const handles = new Map();
        const claimed = new Set();
        let unsubscribe = null;

        record.cleanups.push(() => {
            handles.forEach((handle) => handle.remove());
            handles.clear();
            claimed.forEach((id) => stack()?.claim(id, null));
            claimed.clear();
            unsubscribe?.();
        });

        return {
            // -- read ----------------------------------------------------
            list: () => stack().layers().map(layerRecord),
            /**
             * Layers matching a kind, a modality, or both.
             *
             * `kind` is the rendering strategy core owns; `modality` is what
             * the data MEANS, which the plugin owns. A transcripts panel asks
             * for `{modality: "transcripts"}` and does not care that it is
             * drawn as points -- which is the whole reason both are on the
             * record. Same vocabulary as `plexora.api.layers` on the server.
             */
            find: (query) => stack().layers()
                .filter((layer) => {
                    const spec = layer.spec || {};
                    if (query?.kind && layer.kind !== query.kind) return false;
                    if (query?.modality && spec.modality !== query.modality) return false;
                    return true;
                })
                .map(layerRecord),
            get: (id) => stack().get(id),
            describe: () => stack().describe(),
            viewport: () => seaDragonViewer.viewportImageBounds(),
            /** The cell ids currently on screen. Nearly free: the centroid tile
             *  cache already holds them, so this is a walk over what is loaded
             *  rather than a request. */
            visibleCells: () => seaDragonViewer.visibleCellIds(),

            // -- style ---------------------------------------------------
            // The rule setCellColorLUT has always followed: the plugin computes
            // a table, core draws it. Same LUT shape as ever.
            setColorLUT: (layerId, lut) => seaDragonViewer.setCellColorLUT(
                layerId === "cells" ? definition.name : layerId, lut),
            setVisible: (id, on) => stack().setVisible(id, on),
            setOpacity: (id, value) => stack().setOpacity(id, value),
            /**
             * "I draw this layer" -- which is what earns it a card.
             *
             * The Layers panel gives a card to anything core draws and, until
             * this existed, to nothing else: an eye and an opacity slider on
             * a layer nobody renders are three controls that move nothing,
             * which is exactly the shallow card that panel is kept clear of.
             * A plugin that HAS drawn it says so here and gets the ordinary
             * card, with the eye, the opacity and the drag all arriving back
             * through `onLayerChange` like any other layer's.
             *
             * Released when the plugin is torn down, so a tool switched away
             * cannot leave a card behind that nothing answers.
             */
            claim: (id) => {
                if (!id) return false;
                claimed.add(id);
                return stack().claim(id, definition.name);
            },
            release: (id) => {
                claimed.delete(id);
                return stack().claim(id, null);
            },
            setOrder: (ids) => {
                const changed = stack().setOrder(ids);
                if (changed) stack().applyWorldOrder();
                return changed;
            },
            setTransform: (id, transform) => stack().setTransform(id, transform),

            // -- temporary overlays --------------------------------------
            addOverlay: (spec) => {
                const id = own(spec?.id || "overlay");
                handles.get(id)?.remove();
                const handle = seaDragonViewer.addOverlay({ ...spec, id });
                if (handle) handles.set(id, handle);
                return handle;
            },
            removeOverlay: (id) => {
                const key = own(id);
                handles.get(key)?.remove();
                handles.delete(key);
            },
            /**
             * A TILED layer this plugin draws. The counterpart of addOverlay.
             *
             * Core owns the world item, the placement (an affine turned into
             * OSD's five controls), the z-order and the `layerId` tag; the
             * plugin owns what is drawn and where its tiles come from. That is
             * the same split addOverlay already makes, extended to the surface
             * that was missing it -- and it is why a transcript density raster
             * is the transcripts plugin's picture rather than a modality core
             * had to learn about.
             *
             * The id is prefixed with the plugin's name and the handle is torn
             * down with the plugin, so a tool that is switched away cannot
             * leave a layer behind.
             *
             * @returns `{remove, setStyle, setVisible}` or null when the
             *   transform is one OpenSeadragon cannot express.
             */
            addTiled: (spec) => {
                const id = own(spec?.id || "tiled");
                handles.get(id)?.remove();
                const handle = viewerManager.addTiledLayer({
                    ...spec, layerId: spec?.layerId || id,
                });
                if (handle) handles.set(id, handle);
                return handle;
            },
            removeTiled: (id) => {
                const key = own(id);
                handles.get(key)?.remove();
                handles.delete(key);
            },
            repaint: () => seaDragonViewer.repaintOverlay(),

            // -- events --------------------------------------------------
            /** Throttled in core to one call per animation frame, because the
             *  alternative is every plugin discovering that OSD raises
             *  `animation` per pointer move and writing its own rAF gate. */
            onViewportChange: (fn) => {
                const off = seaDragonViewer.onViewportChange(fn);
                record.cleanups.push(off);
                return off;
            },
            onLayerChange: (fn) => {
                unsubscribe = stack().subscribe(fn);
                return unsubscribe;
            },
        };
    }

    /** The context every plugin hook receives. */
    function pluginContext(definition, extra = {}) {
        const record = pluginRecord(definition);
        return {
            // What this plugin is given: image data always, plus segmentation
            // and a feature table when the project has them.
            dataset: __plexora.dataset,
            config,
            columns,
            dataLayer,
            eventHandler,
            channelList,
            viewer: seaDragonViewer,
            datasource,
            // Core event names, so a plugin can hook core behaviour without
            // reaching for a concrete core class off window.
            coreEvents: ChannelList.events,
            url: plexoraUrl,
            // Ask the user for something this plugin declared in its Requires
            // but the project has not got. Resolves true once it is recorded --
            // centrally, so another plugin needing the same thing finds it
            // already answered. A plugin should never build its own "type a
            // column name" input; declare the requirement and call this.
            requirements: {
                require: (keys) => window.PlexoraRequirements.require(
                    datasource, definition.name, keys),
            },
            // The rendering API. A plugin says "draw these cells in these
            // colours" or "draw this geometry" and core does it; the plugin
            // never builds a spatial renderer of its own. See layerStack.js and
            // ImageViewer.addOverlay.
            //
            // What is deliberately NOT here: anything that interprets data.
            // Clusters, phenotypes, expression and metadata are the plugin's,
            // and the viewer only ever receives the table they produce.
            layers: pluginLayerApi(definition, record),
            // What this SAMPLE is, beside what its table holds. Live getters
            // for the same reason `ctx.dataset` has them: layers are adopted
            // mid-session, and a snapshot taken at activation would be wrong
            // the moment somebody pressed "+ Add Layer".
            sample: {
                get name() { return datasource; },
                get bundles() { return config.bundles || []; },
                get reference() {
                    return (config.layers || []).find(
                        (layer) => layer.id === "__image__") || null;
                },
                get modalities() {
                    return [...new Set((config.layers || [])
                        .map((layer) => layer.modality).filter(Boolean))];
                },
                get blank() { return config.image_kind === "blank"; },
            },
            onCleanup: (fn) => record.cleanups.push(fn),
            instance: record.instance,
            ...extra,
        };
    }

    function activatePluginInstance(definition, databaseDescription) {
        const record = pluginRecord(definition);
        record.instance = definition.createInstance
            ? definition.createInstance(pluginContext(definition))
            : null;

        // Only a plugin that says it colours cells gets a layer. Registering is
        // explicit because it is what puts a card in the sidebar and a pass in
        // the compositor -- see ImageViewer.registerCellLayer.
        if (record.instance && definition.ownsCellLayer) {
            record.instance.pluginName = definition.name;
            // Carried on the provider, so core can ask the viewer who holds the
            // layer instead of learning plugin names. The paths that turn the
            // mask on without a plugin activating -- a pyramid finishing
            // conversion mid-session -- read it from there.
            record.instance.preferredCellMode = definition.preferredCellMode || null;
            seaDragonViewer.registerCellLayer(definition.name, record.instance, {
                supportedModes: definition.supportedCellModes || null,
            });
            // The shared Cells control follows whichever layer is active, and
            // this one just became it.
            viewerControls.syncToActiveLayer();
            // Nothing is drawn over the image until something needs it -- this
            // is that moment. WHICH layer is the project's recorded choice, not
            // this plugin's: the next plugin to register must get the same
            // answer. HOW the mask is drawn -- filled or outlines -- is the
            // plugin's, because it depends on what it is showing. See
            // viewerControls.enableCellLayer.
            viewerControls.enableCellLayer(definition.preferredCellMode, definition.name);
        }
        if (record.instance?.init) {
            record.instance.init(databaseDescription, seaDragonViewer);
        }
        return record.instance;
    }

    function bindPluginEvents(definition) {
        if (!definition.bindEvents) return;
        // The two gate actions are bound to THIS plugin's own layer. A plugin
        // that gates cells while another tool is the active one must move its
        // own cells, not the active layer's -- passing the name here is what
        // keeps a plugin from having to know it exists.
        definition.bindEvents(pluginContext(definition, {
            seaDragonViewer,
            moduleInstance: pluginRecord(definition).instance,
            updateSeaDragonSelection,
            updateCentroidsForGate: () => updateCentroidsForGate(definition.name),
            runSegmentationGate: (showSpinner) => runSegmentationGate(showSpinner, definition.name),
        }));
    }

    function createPluginSidebar(definition, sidebar) {
        if (!definition.createSidebarController) return null;
        const record = pluginRecord(definition);
        const controller = definition.createSidebarController(pluginContext(definition, {
            sidebar,
            moduleInstance: record.instance,
        }));
        record.sidebarController = controller || null;
        return controller || null;
    }

    /**
     * Activate a plugin discovered after boot (see toolLoader.js), once the
     * viewer, dataLayer and ViewerSidebar are already up. The boot path above
     * is left running its own sequence rather than routed through here: its
     * timing is load-order sensitive (instance.init() must precede tile
     * loading), and both paths end in the same registered state.
     */
    __plexora.activatePlugin = async function activatePlugin(definition) {
        const instance = activatePluginInstance(definition, __plexora.databaseDescription);
        bindPluginEvents(definition);

        let sidebarController = null;
        if (__plexora.viewerSidebar) {
            sidebarController = createPluginSidebar(definition, __plexora.viewerSidebar);
            if (sidebarController) {
                await __plexora.viewerSidebar.registerModuleLate(sidebarController);
            }
        }
        return { moduleInstance: instance, instance, sidebarController };
    };

    /**
     * Tear a plugin down: run its cleanups, drop its cell layer, unhook its
     * sidebar controller, and forget it. Without this a deactivated plugin
     * leaves window-level listeners behind and a dead controller still receiving
     * the sidebar's lifecycle calls -- neither of which could happen while a
     * plugin lived for the life of the page.
     */
    __plexora.deactivatePlugin = function deactivatePlugin(name) {
        const record = __plexora.plugins.get(name);
        if (!record) return false;
        for (const fn of record.cleanups) {
            try {
                fn();
            } catch (error) {
                console.error(`Plexora: cleanup failed for plugin "${name}"`, error);
            }
        }
        try {
            record.definition.destroy?.();
        } catch (error) {
            console.error(`Plexora: destroy() failed for plugin "${name}"`, error);
        }
        if (record.sidebarController) {
            __plexora.viewerSidebar?.unregisterModule?.(record.sidebarController);
        }
        if (seaDragonViewer.unregisterCellLayer(name)) {
            viewerControls.refreshLayerSurfaces();
        }
        __plexora.plugins.delete(name);
        viewerControls.syncToActiveLayer();
        return true;
    };
}
