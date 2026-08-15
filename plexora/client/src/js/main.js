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
  csv_gatingList: null,
  seaDragonViewer: null,
  viewerSidebar: null
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

    // Active add-on module (gating today; 0 or 1 entries in practice, since only the
    // active module's scripts are ever loaded -- see appModules.js/base.html).
    const activeModuleDef = AppModules.registry[0] || null;
    const activeModuleInstance = activeModuleDef?.createInstance
        ? activeModuleDef.createInstance({ config, columns, dataLayer, eventHandler })
        : null;
    csv_gatingList = activeModuleInstance;
    __plexora.csv_gatingList = csv_gatingList;

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
    // network round trip. Only channelList.init(dd)/csv_gatingList.init(dd)
    // below actually need dd, and they run after it resolves either way.
    const ddPromise = dataLayer.getDatabaseDescription();

    const imageInit = [viewerManager, channelList, csv_gatingList, [], []];
    const [dd] = await Promise.all([ddPromise, dataLayer.init(), seaDragonViewer.init(...imageInit)]);
    __plexora.databaseDescription = dd;
    channelList.init(dd);
    if (csv_gatingList) csv_gatingList.init(dd, seaDragonViewer);

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
        d3.select("body").style("cursor", "progress");

        //map to full name
        d.name = dataLayer.getFullChannelName(d.name);

        //send to image viewer
        const action = ["remove", "add"][+d.status];
        seaDragonViewer.updateActiveChannels(d.name, action);

        d3.select("body").style("cursor", "default");
    };
    eventHandler.bind(ChannelList.events.CHANNELS_CHANGE, actionChannelsToRenderChange);

    /**
     * Listen to regional or single cell selection.
     *
     * @param d - The selections
     */
    const actionImageClickedMultiSel = (d) => {
        // No real per-cell data to select against for a quick-view (no
        // feature data) datasource -- config.featureData is empty there.
        if (config?.has_feature_data === false) return;
        d3.select("body").style("cursor", "progress");
        const { idField } = config.featureData[0];
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
        d3.select("body").style("cursor", "default");
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

    let centroidGateRequest = 0;
    const updateCentroidsForGate = async () => {
        if (!seaDragonViewer.shouldDrawCentroids()) return;
        const requestId = ++centroidGateRequest;
        seaDragonViewer.setLoading(true);
        try {
            await seaDragonViewer.ensureCentroidsReady(false);
            if (requestId === centroidGateRequest) {
                seaDragonViewer.updateCentroidFilter(csv_gatingList.selections, true);
            }
        } finally {
            seaDragonViewer.setLoading(false);
        }
    };
    let segmentationGateRequest = 0;
    const updateSegmentationForGate = async (showSpinner = true) => {
        if (!seaDragonViewer.viewerManagerVMain?.sel_outlines) return;
        const requestId = ++segmentationGateRequest;
        await seaDragonViewer.updateSegmentationFilter(csv_gatingList.selections, showSpinner);
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
    const runSegmentationGate = async (showSpinner) => {
        if (!seaDragonViewer.viewerManagerVMain?.sel_outlines) return;
        if (segmentationGateRunning) {
            segmentationGatePending = true;
            return;
        }
        segmentationGateRunning = true;
        try {
            await updateSegmentationForGate(showSpinner);
            while (segmentationGatePending) {
                segmentationGatePending = false;
                await updateSegmentationForGate(false);
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

    if (activeModuleDef?.bindEvents) {
        activeModuleDef.bindEvents({
            eventHandler,
            dataLayer,
            channelList,
            seaDragonViewer,
            moduleInstance: activeModuleInstance,
            updateSeaDragonSelection,
            updateCentroidsForGate,
            runSegmentationGate,
        });
    }

    if (typeof ViewerSidebar !== "undefined" && document.getElementById("viewer_sidebar")) {
        const viewerSidebar = new ViewerSidebar(config, columns, dataLayer, eventHandler, channelList);
        __plexora.viewerSidebar = viewerSidebar;
        if (activeModuleDef?.createSidebarController) {
            const moduleSidebarController = activeModuleDef.createSidebarController({
                sidebar: viewerSidebar,
                moduleInstance: activeModuleInstance,
                dataLayer,
                eventHandler,
                config,
            });
            if (moduleSidebarController) {
                viewerSidebar.registerModule(moduleSidebarController);
                // Tell toolLoader.js this tool is already live (rendered server-side via
                // a direct/bookmarked ?tool= link) so its close button and any later
                // Tools-menu click work off the real state instead of re-fetching/
                // re-activating a module that's already registered.
                if (window.PlexoraToolLoader) {
                    const slotIds = Array.from(
                        document.querySelectorAll(`[data-tool-mount="${activeModuleDef.name}"]`)
                    ).map((el) => el.id);
                    window.PlexoraToolLoader.registerLoaded(activeModuleDef.name, slotIds, moduleSidebarController);
                }
            }
        }
        await viewerSidebar.init(dd);
    }

    // Segmentation-mask processing (pyramid/outline generation) runs in a
    // background job on upload -- see data_model.start_segmentation_job --
    // so the viewer can open before it's done. Poll until it's ready (or
    // errors out) and reload, which picks up the now-real segmentation path
    // from a fresh /config fetch and goes through the normal segmentation-
    // loading path with no extra client state to reconcile.
    if (config.segmentation_status === 'pending') {
        const pollSegmentationStatus = async () => {
            const status = await dataLayer.getSegmentationStatus();
            if (status?.status === 'pending') {
                window.setTimeout(pollSegmentationStatus, 3000);
            } else if (status?.status === 'ready') {
                window.location.reload();
            }
            // status === 'error': leave the viewer as-is (no segmentation), no reload loop.
        };
        window.setTimeout(pollSegmentationStatus, 3000);
    }

    // Reusable "a module became available after boot" sequence -- mirrors the
    // createInstance/module.init/bindEvents/createSidebarController steps above,
    // but for a module discovered later (see toolLoader.js), once the viewer,
    // dataLayer, and ViewerSidebar are already fully initialized. The boot
    // sequence above is left untouched (different, load-order-sensitive timing:
    // module.init() runs before tile loading starts) rather than rewritten to
    // call this too, so this addition can't regress the eager/bookmarked-tool-link
    // path -- both paths just end up in the same registered state.
    __plexora.activateAddonModule = async function activateAddonModule(moduleDef) {
        const moduleInstance = moduleDef.createInstance
            ? moduleDef.createInstance({ config, columns, dataLayer, eventHandler })
            : null;
        csv_gatingList = moduleInstance;
        __plexora.csv_gatingList = moduleInstance;
        // seaDragonViewer.init() (boot sequence above) only ever binds selectionProvider
        // once, from whatever csv_gatingList existed at that moment -- null for a plain-
        // viewer boot, since no module's scripts are loaded yet. Without this, a lazily
        // activated module's gates/selections would set correctly but
        // updateSegmentationFilter()/updateCentroidFilter() would keep treating
        // this.selectionProvider as absent and never actually subset segmentation
        // outlines (or legacy-mode centroids) to the gated cells.
        seaDragonViewer.selectionProvider = moduleInstance;
        if (moduleInstance) moduleInstance.init(__plexora.databaseDescription, seaDragonViewer);

        if (moduleDef.bindEvents) {
            moduleDef.bindEvents({
                eventHandler,
                dataLayer,
                channelList,
                seaDragonViewer,
                moduleInstance,
                updateSeaDragonSelection,
                updateCentroidsForGate,
                runSegmentationGate,
            });
        }

        let moduleSidebarController = null;
        if (moduleDef.createSidebarController && __plexora.viewerSidebar) {
            moduleSidebarController = moduleDef.createSidebarController({
                sidebar: __plexora.viewerSidebar,
                moduleInstance,
                dataLayer,
                eventHandler,
                config,
            });
            if (moduleSidebarController) {
                await __plexora.viewerSidebar.registerModuleLate(moduleSidebarController);
            }
        }
        return { moduleInstance, sidebarController: moduleSidebarController };
    };
}
