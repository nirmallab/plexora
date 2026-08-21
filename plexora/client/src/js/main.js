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
// Data prevent caching on the config file, as it may have been modified
window.__plexoraReady = d3.json(`${plexoraUrl("config")}?t=${Date.now()}`).then(function (config) {
    return init(config[datasource]);
});

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
    if (Array.isArray(config.imageData)) {
        config.imageData.forEach(function (channel) {
            if (channel.src && channel.src.startsWith("/")) {
                channel.src = plexoraUrl(channel.src);
            }
        });
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
    const viewerManager = new ViewerManager(seaDragonViewer, channelList);

    // Core viewer toggles (Centroids/HD/Outlines) -- unconditional, independent of
    // whichever tool (if any) is active, so they work on a plain base viewer too.
    const viewerControls = new ViewerControls(seaDragonViewer, config, eventHandler);
    __plexora.viewerControls = viewerControls;
    viewerControls.init();

    // Fire the database description request without awaiting it here --
    // ImageViewer.init() below never reads dd (only WebGL/OSD setup), so
    // there's no reason to serialize tile-viewer construction behind this
    // network round trip. Only channelList.init(dd) and each plugin's own
    // init(dd) below actually need dd, and they run after it resolves anyway.
    const ddPromise = dataLayer.getDatabaseDescription();

    const imageInit = [viewerManager, channelList, null, [], []];
    const [dd] = await Promise.all([ddPromise, dataLayer.init(), seaDragonViewer.init(...imageInit)]);
    __plexora.databaseDescription = dd;
    // The dataset handed to every plugin -- same shape as the server's
    // plexora.api dataset, so a plugin reads roles rather than column names.
    __plexora.dataset = PlexoraDataset.build(config, imageChannels, dd);
    channelList.init(dd);

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
        if (!entry) return;
        // Only the read spec: attaching a mask also rewrites imageData, and
        // adopting a new channel list mid-session would shift every index the
        // tile path and the channel sliders are keyed on.
        config.dataset = entry.dataset;
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
    if (config.segmentation_status === 'pending') {
        const pollSegmentationStatus = async () => {
            const status = await dataLayer.getSegmentationStatus();
            if (status?.status === 'pending') {
                window.setTimeout(pollSegmentationStatus, 3000);
            } else if (status?.status === 'ready') {
                adoptSegmentation(status.segmentation);
            }
            // status === 'error': leave the viewer as-is (no segmentation), no poll loop.
        };
        window.setTimeout(pollSegmentationStatus, 3000);
    }

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

        // Swap the drawing over only when what is showing is the fallback this
        // very absence caused. A user who chose Centroids themselves gets to
        // keep them, and a viewer drawing nothing was drawing nothing on
        // purpose -- neither is a state a finished background job should
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
