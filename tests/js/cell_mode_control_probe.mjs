/**
 * The Cells control: one representation at a time, and only ones this project
 * can actually draw.
 *
 * This replaced two independent checkboxes, and the reason is the thing worth
 * testing. Two checkboxes could express "Outlines and Centroids", which the
 * renderer then had to arbitrate, and could not express "Filled" at all. A
 * one-of-four control makes both problems structural rather than a rule
 * somebody has to remember.
 *
 * Availability is the other half. Filled needs a mask whose labels are stored
 * whole -- a pyramid pre-reduced to boundaries has no interior pixels, so the
 * button would do nothing. Centroids need coordinates. Offering either where it
 * cannot work is a control that lies, and the failure is silent: the click
 * lands, nothing happens, and there is nothing on screen to explain it.
 *
 * ViewerControls is run from source in a vm against a DOM and an ImageViewer
 * stand-in, so what is exercised is the shipped file.
 *
 * Run directly:  node tests/js/cell_mode_control_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/views/viewerControls.js");

const MODES = ["none", "centroids", "outlines", "filled"];

/** A button that behaves enough like one: identity, classes, disabled, title,
 *  and the inline display the control uses to take an option away entirely. */
function makeButton(mode) {
    const classes = new Set(mode === "none" ? ["cell-mode-option", "is-active"] : ["cell-mode-option"]);
    const attributes = { "aria-checked": mode === "none" ? "true" : "false" };
    return {
        dataset: { cellMode: mode },
        disabled: mode !== "none",
        title: "",
        focused: 0,
        style: { display: "" },
        get shown() { return this.style.display !== "none"; },
        classList: {
            add: (c) => classes.add(c),
            remove: (c) => classes.delete(c),
            toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
            contains: (c) => classes.has(c),
        },
        setAttribute(name, value) { attributes[name] = String(value); },
        getAttribute(name) { return attributes[name] ?? null; },
        removeAttribute(name) { delete attributes[name]; this.title = ""; },
        focus() { this.focused += 1; },
        closest(selector) { return selector === "[data-cell-mode]" ? this : null; },
        get active() { return classes.has("is-active"); },
    };
}

/** The viewer's cell-layer surface, with every side effect recorded.
 *
 *  The registry half is a small but real implementation rather than a set of
 *  stubs: what this file is testing is a control that reads per-layer state and
 *  writes it back, so a registry that forgot what it was told would make every
 *  assertion below vacuous. */
function fakeViewer({ segmentationFails = false } = {}) {
    return {
        noLabel: false,
        centroidsFromFallback: false,
        cellDisplayMode: "outlines",
        centroidPointScale: 1,
        viewerManagerVMain: { sel_outlines: false, setHdMode() {} },
        viewer: { forceRedraw() { this.redraws = (this.redraws || 0) + 1; } },
        calls: [],
        layers: new Map(),
        order: [],
        cellLayerOwner: null,
        // The provider of the ACTIVE layer. main.js stamps `preferredCellMode`
        // onto it, which is how core reads a preference without ever knowing a
        // plugin's name.
        get cellLayer() { return this.layers.get(this.cellLayerOwner)?.provider || null; },
        registerCellLayer(name, provider, options = {}) {
            let layer = this.layers.get(name);
            if (!layer) {
                layer = {
                    name, provider, lut: null, mode: "none", userMode: null,
                    supportedModes: null, opacity: 0.7, visible: true,
                };
                this.layers.set(name, layer);
                this.order.push(name);
            }
            if (options.supportedModes) layer.supportedModes = [...options.supportedModes];
            if (options.makeActive !== false) this.cellLayerOwner = name;
            return layer;
        },
        unregisterCellLayer(name) {
            this.layers.delete(name);
            this.order = this.order.filter((entry) => entry !== name);
            if (this.cellLayerOwner === name) {
                this.cellLayerOwner = this.order[this.order.length - 1] || null;
            }
        },
        setActiveCellLayer(name) { this.cellLayerOwner = name; },
        getCellLayer(name) { return this.layers.get(name) || null; },
        cellLayers() { return this.order.map((name) => this.layers.get(name)); },
        setCellLayerMode(name, mode) {
            const layer = this.layers.get(name);
            if (!layer) return false;
            layer.userMode = mode;
            layer.mode = mode;
            this.calls.push(`layer:${name}:${mode}`);
            return true;
        },
        setCellLayerVisible(name, on) {
            const layer = this.layers.get(name);
            if (!layer || layer.visible === Boolean(on)) return false;
            layer.visible = Boolean(on);
            this.calls.push(`visible:${name}:${layer.visible}`);
            return true;
        },
        setLayerOpacity(name, value) {
            const layer = this.layers.get(name);
            if (!layer) return false;
            layer.opacity = value;
            return true;
        },
        setCellDisplayMode(mode) { this.cellDisplayMode = mode; this.calls.push(`mode:${mode}`); },
        // Hiding the selected cells at BLIT time, keeping every pixel that was
        // built for them. The real one flips one boolean two draw gates read
        // and forces a redraw; what matters here is that nothing else is
        // called, which `calls` is what records.
        overlayMuted: false,
        setOverlayMuted(muted) {
            const next = Boolean(muted);
            if (next === this.overlayMuted) return false;
            this.overlayMuted = next;
            this.calls.push(`muted:${next}`);
            return true;
        },
        // Core's OWN layer's opacity, for a viewer with no plugin at all --
        // the other half of what makes the Opacity control canvas-level.
        cellDisplayOpacity: 1,
        setCellDisplayOpacity(value) { this.cellDisplayOpacity = value; return true; },
        setCentroidPointScale(value) { this.centroidPointScale = value; },
        setLoading() {},
        async ensureSegmentationReady() {
            this.calls.push("ensureSegmentationReady");
            if (segmentationFails) throw new Error("no mask pyramid");
        },
        async updateSegmentationFilter() { this.calls.push("updateSegmentationFilter"); },
        async updateCentroidVisibility(on) { this.calls.push(`centroids:${on}`); },
        updateCentroidFilter() { this.calls.push("updateCentroidFilter"); },
        async updateCentroidFallback(on) {
            this.calls.push(`fallback:${on}`);
            this.centroidsFromFallback = Boolean(on);
            this.viewerManagerVMain.sel_outlines = false;
            // The real one hands the control the mode it just switched to.
            globalThis.__probeWindow.__plexora?.viewerControls?.adoptMode?.("centroids");
        },
    };
}

function build({ segmentation = "/mask.zarr", segmentationMode = "filled",
    hasCentroids = true, segmentationFails = false, cellLayer = null,
    segmentationStatus = "ready", hasTable = hasCentroids,
    answerRequirements = true, maskAttached = false } = {}) {
    const buttons = new Map(MODES.map((mode) => [mode, makeButton(mode)]));
    const handlers = new Map();
    //: Listeners the control puts on the DOCUMENT rather than on the Cells
    //: control -- the overlay key is the only one.
    const docHandlers = new Map();
    const events = [];

    const control = {
        hidden: false,
        querySelectorAll: () => Array.from(buttons.values()),
        addEventListener(type, fn) { handlers.set(type, fn); },
        contains: () => true,
    };
    // The link that names what this project is missing. Hidden until the
    // control decides there is something to name.
    const ctaClasses = new Set(["cell-mode-cta"]);
    const cta = {
        hidden: true,
        textContent: "",
        classList: {
            toggle: (c, on) => (on ? ctaClasses.add(c) : ctaClasses.delete(c)),
            contains: (c) => ctaClasses.has(c),
        },
        get sole() { return ctaClasses.has("is-sole"); },
        // Clicking it no longer follows the href: it opens the requirements
        // modal for exactly what is missing, which is why this needs to be a
        // real enough element to be listened to.
        addEventListener(type, fn) { if (type === "click") this.onclick = fn; },
        async click() {
            const event = { preventDefault: () => { event.defaultPrevented = true; },
                            defaultPrevented: false };
            await this.onclick?.(event);
            return event;
        },
    };
    const hd = { addEventListener() {} };
    // The centroid size slider and the row it lives in. Both are core's: the
    // geometry is, so every colouring plugin gets this without shipping one.
    //
    // Both sliders are `PlexoraSlider`s now, and what these two objects are is
    // the element the template staged, which the slider adopts. The slider
    // itself is stubbed below: what this probe is about is viewerControls, and
    // the primitive has a probe of its own that runs the real file.
    const pointSize = { value: "1" };
    const pointSizeRow = { hidden: false };
    // The per-layer opacity slider, which moved here out of Cell Explorer's own
    // panel: compositing is core's, so one slider serves every plugin.
    const opacity = { value: "70" };
    const opacityRow = { hidden: false };
    // The key printed on the canvas under the filename: a <kbd> cap and a
    // sentence, built by ImageViewer and filled by this control. Enough of an
    // element to be written to the way paintOverlayHint writes to it.
    const hintKey = { textContent: "" };
    const hintLabel = { textContent: "" };
    const hintClasses = new Set();
    const overlayHint = {
        hidden: false,
        dataset: {},
        key: hintKey,
        label: hintLabel,
        classList: {
            toggle: (c, on) => (on ? hintClasses.add(c) : hintClasses.delete(c)),
            contains: (c) => hintClasses.has(c),
        },
        querySelector(selector) {
            if (selector === "kbd") return hintKey;
            if (selector === '[data-role="label"]') return hintLabel;
            return null;
        },
    };

    /** Just enough of views/slider.js to drive the two callbacks apart: a tick
     *  of a drag, and the one commit on release. */
    const sliders = new Map();
    class FakeSlider {
        constructor(mount, options = {}) {
            this.options = options;
            this.value = Number(options.value ?? mount?.value ?? 0);
            sliders.set(mount, this);
        }
        get() { return this.value; }
        set(value, opts = {}) {
            this.value = Number(value);
            if (opts.silent) return;
            this.options.onInput?.(this.value);
            this.options.onChange?.(this.value);
        }
        /** One pixel of a drag. */
        drag(value) { this.value = Number(value); this.options.onInput?.(this.value); }
        /** Letting go, which is the only thing a plugin hears. */
        release() { this.options.onChange?.(this.value); }
        destroy() {}
    }

    // What the CTA asks for, and what it did afterwards. The modal itself is
    // core's and lives elsewhere; what this probe is about is that the button
    // names exactly the missing things and nothing else.
    const asked = [];
    const viewer = fakeViewer({ segmentationFails });
    const win = {
        dispatchEvent(event) { events.push({ type: event.type, detail: event.detail }); },
        addEventListener() {},
        flaskVariables: { datasource: "demo" },
        location: { reload() { asked.push("reload"); } },
        PlexoraRequirements: {
            ask(datasource, keys) {
                asked.push({ datasource, keys });
                return Promise.resolve(answerRequirements);
            },
        },
        __plexora: {
            refreshDataset() {
                asked.push("refresh");
                return Promise.resolve({ maskAttached });
            },
            watchSegmentation() { asked.push("watch"); },
            // Core's own "draw this tool's layer, or stop" (main.js). What the
            // overlay key falls back to when the tool loader is not the one
            // holding the answer -- the eye and the pin are toolLoader's, and
            // this probe is about viewerControls.
            setToolLayerVisible(name, visible) {
                return viewer.setCellLayerVisible(name, visible);
            },
        },
    };
    globalThis.__probeWindow = win;

    const context = createContext({
        console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map,
        Promise, Error,
        CustomEvent: class CustomEvent {
            constructor(type, init) { this.type = type; this.detail = init?.detail; }
        },
        window: win,
        document: {
            //: What has focus, which is what stops a bare letter firing while
            //: somebody is typing a project name. Null unless a check sets it.
            activeElement: null,
            querySelector(selector) {
                if (selector === "#cell_display_control") return control;
                if (selector === "#viewer_controls_hd") return hd;
                if (selector === "#cell_point_size") return pointSize;
                if (selector === "#cell_point_size_row") return pointSizeRow;
                if (selector === "#cell_layer_opacity") return opacity;
                if (selector === "#cell_layer_opacity_row") return opacityRow;
                if (selector === "#cell_data_cta") return cta;
                if (selector === "#viewer_overlay_hint") return overlayHint;
                return null;
            },
            addEventListener(type, fn) { docHandlers.set(type, fn); },
        },
        PlexoraDataset: { hasCentroids: () => hasCentroids },
        PlexoraSlider: FakeSlider,
    });
    runInContext(readFileSync(SOURCE, "utf8"), context, { filename: "viewerControls.js" });
    runInContext("globalThis.__ViewerControls = ViewerControls;", context);

    const controls = new context.__ViewerControls(
        viewer,
        {
            segmentation, segmentationMode, cellLayer,
            segmentation_status: segmentationStatus,
            // No dataset block IS the image-only state, which is the whole
            // input to "is data missing". Kept in step with hasCentroids by
            // default, because a table whose coordinates are unanswered is a
            // deliberately separate case and not the common one.
            dataset: hasTable ? { roles: hasCentroids ? { x: "X", y: "Y" } : {} } : null,
        },
        { trigger() {} });
    win.__plexora.viewerControls = controls;
    win.__plexora.seaDragonViewer = viewer;
    controls.init();
    return {
        controls, viewer, buttons, handlers, events, control, cta, asked,
        pointSize, pointSizeRow, opacity, opacityRow, overlayHint,
        pointSizeSlider: sliders.get(pointSize),
        opacitySlider: sliders.get(opacity),
        /** Press a bare key the way the document would deliver it. */
        press(key, init = {}) {
            const event = { key, preventDefault() { this.defaultPrevented = true; },
                            defaultPrevented: false, ...init };
            docHandlers.get("keydown")?.(event);
            return event;
        },
        document: context.document,
    };
}

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}

const activeModes = (buttons) =>
    MODES.filter((mode) => buttons.get(mode).active);

// -- nothing is drawn on load -------------------------------------------

{
    const { controls, viewer, buttons } = build();
    check("the viewer opens drawing no cells",
        controls.mode === "none" && String(activeModes(buttons)) === "none",
        "a user who opened a project to look at the image wanted the image");
    check("no mask is fetched before something asks for one",
        viewer.calls.length === 0, `got ${viewer.calls}`);
}

// -- availability --------------------------------------------------------

{
    const { buttons } = build({ segmentation: "/mask.zarr", segmentationMode: "filled" });
    check("a whole-label mask enables every option",
        MODES.every((mode) => !buttons.get(mode).disabled));
}

{
    const { buttons } = build({ segmentationMode: "outlines" });
    check("a pre-reduced mask offers outlines but not filled",
        !buttons.get("outlines").disabled && buttons.get("filled").disabled,
        "there are no interior pixels in that pyramid to fill");
    check("and says why",
        /nothing to fill/.test(buttons.get("filled").title),
        `title: ${buttons.get("filled").title}`);
}

{
    const { buttons } = build({ segmentation: null });
    check("no mask leaves outlines and filled unavailable",
        buttons.get("outlines").disabled && buttons.get("filled").disabled);
    check("but centroids stay available when there are coordinates",
        !buttons.get("centroids").disabled);
}

// -- the control reflects what this project HAS ---------------------------
//
// Four project states, four different rows. A mode the project has no resource
// for is not shown at all: a greyed "Outlines" reading "Needs a segmentation
// mask" describes a file the user could supply in four clicks and gives them no
// way to, while taking a quarter of a control that has to fit on one line.

{
    const { buttons, control, cta } = build({ segmentation: null, hasCentroids: false,
        hasTable: false });
    check("with neither a mask nor data the mode buttons go entirely",
        control.hidden === true && MODES.every((m) => !buttons.get(m).shown),
        "None on its own is not a choice -- it is the state the viewer is in");
    check("...and the row offers the way to fix that instead",
        cta.hidden === false && cta.textContent === "Add Seg Mask / Data",
        `cta: ${cta.textContent}`);
    check("...taking the width the buttons would have had",
        cta.sole === true);
}

{
    const { buttons, control, cta } = build({ hasCentroids: false, hasTable: false });
    check("a mask without data offers the two it can draw, and not centroids",
        control.hidden === false
        && buttons.get("none").shown && buttons.get("outlines").shown
        && buttons.get("filled").shown && !buttons.get("centroids").shown,
        "there are no positions to plot, and no explanation worth a tooltip");
    check("...and asks for the half that is missing",
        cta.hidden === false && cta.textContent === "Add Data" && cta.sole === false,
        `cta: ${cta.textContent}`);
}

{
    const { buttons, cta } = build({ segmentation: null });
    check("data without a mask offers centroids, and not outlines or filled",
        buttons.get("none").shown && buttons.get("centroids").shown
        && !buttons.get("outlines").shown && !buttons.get("filled").shown);
    check("...and asks for the mask",
        cta.hidden === false && cta.textContent === "Add Seg Mask",
        `cta: ${cta.textContent}`);
}

{
    const { buttons, cta } = build();
    check("with both, all four are on the row and nothing is asked for",
        MODES.every((m) => buttons.get(m).shown) && cta.hidden === true);
}

// A table that is PRESENT but whose coordinate columns nobody has answered is
// not a project missing data. Sending it to re-supply a file it already has
// would be the wrong instruction; the button stays and says what it needs.

{
    const { buttons, cta } = build({ hasCentroids: false, hasTable: true });
    check("a table with no coordinates keeps centroids, disabled, with a reason",
        buttons.get("centroids").shown && buttons.get("centroids").disabled
        && /no cell coordinates/.test(buttons.get("centroids").title),
        `title: ${buttons.get("centroids").title}`);
    check("...and is not told to add data it already has",
        cta.hidden === true, `cta: ${cta.textContent}`);
}

// Nor is a mask that is still converting a project without a mask -- that is
// the one project that must never be told to go and attach one.

{
    const { buttons, control, cta } = build({ segmentation: null, hasCentroids: false,
        hasTable: false, segmentationStatus: "pending" });
    check("a converting mask keeps its options on the row",
        control.hidden === false
        && buttons.get("outlines").shown && buttons.get("filled").shown
        && buttons.get("outlines").disabled,
        "they are about to work, and will be enabled in place when it lands");
    check("...and says so rather than asking for a mask that is already there",
        /still being prepared/.test(buttons.get("outlines").title)
        && cta.textContent === "Add Data",
        `title: ${buttons.get("outlines").title}, cta: ${cta.textContent}`);
}

// -- exactly one at a time ----------------------------------------------

{
    const { controls, viewer, buttons } = build();
    await controls.selectMode("outlines");
    check("choosing outlines makes it the only active option",
        String(activeModes(buttons)) === "outlines" && controls.mode === "outlines");
    check("choosing outlines turns the label layer on and centroids off",
        viewer.viewerManagerVMain.sel_outlines === true
        && viewer.calls.includes("centroids:false"),
        `${viewer.calls}`);

    await controls.selectMode("centroids");
    check("choosing centroids makes it the only active option",
        String(activeModes(buttons)) === "centroids");
    check("choosing centroids turns the label layer off",
        viewer.viewerManagerVMain.sel_outlines === false
        && viewer.calls.includes("centroids:true"),
        `${viewer.calls}`);

    await controls.selectMode("none");
    check("choosing none draws nothing at all",
        viewer.viewerManagerVMain.sel_outlines === false
        && viewer.calls.filter((c) => c === "centroids:false").length === 2);
}

{
    const { controls, buttons } = build();
    await controls.selectMode("filled");
    check("filled is selectable where the mask allows it",
        controls.mode === "filled" && String(activeModes(buttons)) === "filled");
}

{
    const { controls } = build({ segmentationMode: "outlines" });
    await controls.selectMode("filled");
    check("a disabled option cannot be selected through the API either",
        controls.mode === "none",
        "or the menu and the keyboard would each be a way around the control");
}

// -- the renderer is told, before the tiles are built --------------------

{
    const { controls, viewer } = build();
    await controls.selectMode("filled");
    check("the renderer learns the mode before the mask is loaded",
        viewer.calls.indexOf("mode:filled") < viewer.calls.indexOf("ensureSegmentationReady"),
        `${viewer.calls}`);
    check("and the tiles are rendered filled from the start",
        viewer.cellDisplayMode === "filled",
        "otherwise every tile is drawn as outlines once and then re-rendered");
}

// -- events other views listen for ---------------------------------------

{
    const { controls, events } = build();
    await controls.selectMode("outlines");
    const types = events.map((e) => e.type);
    check("the mode event carries the mode and what is available",
        events.some((e) => e.type === "plexora:cell-mode-changed"
            && e.detail.mode === "outlines" && e.detail.available.filled === true));
    check("the legacy outline event still fires",
        events.some((e) => e.type === "plexora:outlines-changed" && e.detail.enabled === true),
        `${types}`);
    check("filled counts as outlines for anything still listening for that",
        (() => {
            const before = events.length;
            controls.paint("filled");
            controls.announce();
            return events.slice(before).some((e) =>
                e.type === "plexora:outlines-changed" && e.detail.enabled === true);
        })(),
        "the label layer is on either way");

    const before = events.length;
    await controls.selectMode("centroids");
    const after = events.slice(before);
    check("switching to centroids reports outlines off and centroids on",
        after.some((e) => e.type === "plexora:outlines-changed" && e.detail.enabled === false)
        && after.some((e) => e.type === "plexora:centroids-changed" && e.detail.enabled === true));
}

// -- a mask that will not load -------------------------------------------

{
    const { controls, viewer, buttons } = build({ segmentationFails: true });
    await controls.selectMode("outlines");
    check("a mask that fails to load falls back to centroids",
        controls.mode === "centroids" && String(activeModes(buttons)) === "centroids",
        `mode ${controls.mode}, calls ${viewer.calls}`);
    check("and the fallback is remembered as a fallback",
        viewer.centroidsFromFallback === true,
        "so a mask arriving later may take the drawing over, and a user's own choice may not");
}

{
    const { controls, viewer } = build({ segmentationFails: true, hasCentroids: false });
    await controls.selectMode("outlines");
    check("with nothing to fall back to, the control returns to where it was",
        controls.mode === "none" && viewer.cellDisplayMode === "none",
        "rather than showing Outlines selected over an empty image");
}

// -- enableCellLayer ------------------------------------------------------

{
    const { controls } = build();
    await controls.enableCellLayer();
    check("a plugin activating turns on the mask, which is the better view",
        controls.mode === "outlines");
}

{
    const { controls, viewer } = build({ segmentation: null });
    await controls.enableCellLayer();
    check("with no mask ready it falls back to centroids",
        controls.mode === "centroids");
    check("and marks that as a fallback, not a choice",
        viewer.centroidsFromFallback === true);
}

{
    const { controls, viewer } = build({ segmentation: null });
    await controls.enableCellLayer("centroids");
    check("a plugin that asks for centroids is not treated as a fallback",
        controls.mode === "centroids" && viewer.centroidsFromFallback === false,
        "a mask landing later must not overrule what was actually wanted");
}

// -- a mask that is still converting is not a project without one ---------

{
    const { controls, viewer } = build({ segmentation: null, segmentationStatus: "pending" });
    await controls.enableCellLayer("filled");
    // The substitution this replaced was silent and could last minutes: the
    // panel opened showing dots for a project whose whole point was cell shape,
    // and nothing said why or that it would change.
    check("a mask still converting is waited for, not substituted",
        controls.mode === "none",
        "centroids drawn instead would be the wrong representation, not a rougher one");
    check("...and the wait is recorded, so the layer can be turned on later",
        viewer.cellLayerAwaitingMask === true);
    check("...and it is not marked as a centroid fallback",
        viewer.centroidsFromFallback === false,
        "nothing is drawing, so there is no fallback to swap away from");
}

{
    const { controls, viewer } = build({ segmentation: null, segmentationStatus: "pending" });
    await controls.enableCellLayer("centroids");
    check("a plugin that wanted centroids all along does not wait",
        controls.mode === "centroids" && viewer.cellLayerAwaitingMask !== true,
        "it is not waiting for the mask, so the mask's progress is none of its business");
}

{
    const { controls, viewer } = build({
        segmentation: null, segmentationStatus: "pending", hasCentroids: false });
    await controls.enableCellLayer("filled");
    check("with nothing to fall back to it waits just the same",
        controls.mode === "none" && viewer.cellLayerAwaitingMask === true);
}

{
    const { controls, viewer } = build({ segmentation: null, segmentationStatus: "pending" });
    await controls.enableCellLayer("filled");
    const fell = await controls.fallBackToCentroids();
    check("someone who would rather not wait gets centroids",
        fell === true && controls.mode === "centroids");
    check("...and stops waiting", viewer.cellLayerAwaitingMask === false);
    check("...but the mask still replaces them when it lands",
        viewer.centroidsFromFallback === true,
        "'meanwhile' is the whole offer -- see main.js's adoptSegmentation");
}

{
    const { controls } = build({
        segmentation: null, segmentationStatus: "pending", hasCentroids: false });
    const fell = await controls.fallBackToCentroids();
    check("there is no falling back to centroids a project does not have",
        fell === false && controls.mode === "none");
}

// -- how the mask is drawn is the plugin's; which layer is the project's ---

{
    const { controls } = build();
    await controls.enableCellLayer("filled");
    check("a plugin that colours every cell gets a filled mask",
        controls.mode === "filled",
        "an outline shows a phenotype colour as a one-pixel ring, which stops "
        + "being legible past a few hundred cells on screen");
}

{
    const { controls } = build({ segmentationMode: "outlines" });
    await controls.enableCellLayer("filled");
    check("asking for filled where the mask cannot fill lands on outlines",
        controls.mode === "outlines",
        "and never on a mode the control itself has disabled");
}

{
    const { controls } = build({ cellLayer: "centroids" });
    await controls.enableCellLayer("filled");
    check("the project's recorded layer still wins over a plugin's preference",
        controls.mode === "centroids",
        "which layer is the project's answer; how to draw a mask is the plugin's");
}

// -- the mask that arrives after the tool did -----------------------------
//
// A pyramid finishing conversion mid-session turns the mask on WITHOUT any
// plugin activating, so it cannot be handed a preference the way
// enableCellLayer is -- it has to ask whoever holds the layer. That path used
// to hardcode outlines, which is how a project that gained its mask from the
// edit page drew outlines for the rest of the session while every later page
// load drew it filled.

{
    const { controls, viewer } = build();
    check("with nothing holding the cell layer there is no preference to read",
        controls.ownerMaskPreference() === null);

    viewer.registerCellLayer("cell_explorer", { preferredCellMode: "filled" });
    check("the preference is read off whoever holds the layer",
        controls.ownerMaskPreference() === "filled",
        "asked of the viewer, so core never learns which plugins exist");
    check("a late mask is drawn the way the holder asked",
        controls.maskMode(controls.ownerMaskPreference()) === "filled");
}

{
    const { controls, viewer } = build({ segmentationMode: "outlines" });
    viewer.registerCellLayer("cell_explorer", { preferredCellMode: "filled" });
    check("and still not in a way this mask cannot manage",
        controls.maskMode(controls.ownerMaskPreference()) === "outlines");
}

// -- centroid point size --------------------------------------------------

{
    const { controls, pointSizeRow } = build();
    check("the size slider is hidden while nothing is drawn",
        pointSizeRow.hidden === true);
    await controls.selectMode("centroids");
    check("and appears when points are what is on screen",
        pointSizeRow.hidden === false);
    await controls.selectMode("outlines");
    check("and goes again for a mask, which it cannot size",
        pointSizeRow.hidden === true,
        "a control that is present but inert reads as broken, not as N/A");
}

{
    const { controls, viewer, pointSizeSlider } = build();
    await controls.selectMode("centroids");
    pointSizeSlider.drag(2.5);
    check("dragging it resizes the points",
        viewer.centroidPointScale === 2.5,
        "on input rather than change: it is a redraw of what is already in view");
}

{
    const { controls } = build();
    await controls.selectMode("centroids");
    await controls.enableCellLayer();
    check("a second tool activating does not undo what is already showing",
        controls.mode === "centroids",
        "the user's own choice outranks a tool's opinion");
}

// -- a mask arriving late -------------------------------------------------

{
    const config = { segmentation: null, segmentationMode: "filled" };
    const { controls, buttons, cta } = build(config);
    await controls.enableCellLayer();
    check("while there is no mask, the two modes it would unlock are off the row",
        !buttons.get("outlines").shown && !buttons.get("filled").shown
        && cta.textContent === "Add Seg Mask");
    controls.config.segmentation = "/mask.zarr";
    controls.seaDragonViewer.noLabel = false;
    controls.refreshAvailability();
    check("a mask finishing conversion enables the options it unlocks",
        !buttons.get("outlines").disabled && !buttons.get("filled").disabled,
        "without a page reload, minutes into a session");
    check("and they come back onto the row, with nothing left to ask for",
        buttons.get("outlines").shown && buttons.get("filled").shown
        && cta.hidden === true,
        "the buttons stay in the DOM while hidden precisely so this can happen");
    await controls.selectMode("outlines");
    check("and the fallback can then be swapped for the real thing",
        controls.mode === "outlines");
}

// -- one control, several layers ------------------------------------------
//
// The Cells control edits ONE layer -- the active one -- and every plugin that
// colours cells shares it rather than shipping its own. Everything below is
// about that seam: what it offers, what it writes to, and what it leaves alone.

{
    const { controls, viewer, buttons } = build();
    viewer.registerCellLayer("cell_explorer", {});
    controls.syncToActiveLayer();
    check("with a layer active, None is taken off the control",
        !buttons.get("none").shown && controls.offeredModes().none === false,
        "the plugin's own card is what turns its layer off; two controls for one "
        + "question makes both of them feel broken");

    await controls.selectMode("filled");
    check("choosing a mode writes it to the active layer, not to core",
        viewer.getCellLayer("cell_explorer").mode === "filled"
        && viewer.cellDisplayMode === "outlines",
        `core is ${viewer.cellDisplayMode}`);
    check("and the layer remembers that the user chose it",
        viewer.getCellLayer("cell_explorer").userMode === "filled",
        "so a plugin restoring a stored preference can tell 'not chosen yet' apart");
}

{
    const { controls, viewer, buttons } = build();
    viewer.registerCellLayer("marker_tool", {}, { supportedModes: ["outlines"] });
    controls.syncToActiveLayer();
    check("a plugin's declared modes narrow what the control offers",
        buttons.get("outlines").shown && !buttons.get("filled").shown
        && !buttons.get("centroids").shown,
        "a tool that marks a handful of cells has no use for Filled, and offering "
        + "it is offering a result the tool did not design for");
    check("a mode this project cannot draw is still shown, disabled, with a reason",
        (() => {
            const { buttons: b } = build({ segmentationMode: "outlines" });
            return b.get("filled").shown && b.get("filled").disabled
                && /nothing to fill/.test(b.get("filled").title);
        })(),
        "that is a fact about the dataset, not about the open tool");
}

{
    const { controls, viewer } = build();
    viewer.registerCellLayer("cell_explorer", {});
    controls.syncToActiveLayer();
    await controls.selectMode("filled");
    viewer.registerCellLayer("gating", {});
    controls.syncToActiveLayer();
    check("selecting another tool re-points the control at ITS mode",
        controls.mode === "none",
        "the second layer is drawing nothing yet, and the control has to say so "
        + "rather than showing the first layer's Filled over it");

    await controls.enableCellLayer("outlines", "gating");
    check("and a second plugin still gets the mode it asked for",
        viewer.getCellLayer("gating").mode === "outlines"
        && viewer.getCellLayer("cell_explorer").mode === "filled",
        "asked per layer: 'something is already showing' was true as soon as any "
        + "tool had turned the mask on, so the second plugin never got its answer");
}

{
    const { controls, viewer } = build();
    viewer.registerCellLayer("cell_explorer", {});
    controls.syncToActiveLayer();
    await controls.selectMode("outlines");
    viewer.registerCellLayer("gating", {});
    controls.syncToActiveLayer();
    await controls.selectMode("centroids");
    check("the mask stays on while any OTHER layer is still drawing one",
        viewer.viewerManagerVMain.sel_outlines === true,
        "the label item is one item shared by every layer, so this is not a "
        + "question about the mode that was just clicked");
    check("and the points go on at the same time",
        viewer.calls.includes("centroids:true"), `${viewer.calls}`);
}

{
    const { controls, viewer } = build();
    viewer.registerCellLayer("cell_explorer", {});
    viewer.setCellLayerVisible("cell_explorer", false);
    controls.syncToActiveLayer();
    await controls.selectMode("outlines");
    check("choosing a mode for a switched-off layer turns it back on",
        viewer.getCellLayer("cell_explorer").visible === true,
        "otherwise the click visibly does nothing");
}

{
    // The card's eye, which does not go through selectMode at all.
    const { controls, viewer } = build();
    viewer.registerCellLayer("cell_explorer", {});
    controls.syncToActiveLayer();
    await controls.selectMode("filled");
    viewer.setCellLayerVisible("cell_explorer", false);
    await controls.refreshLayerSurfaces();
    check("switching every layer off takes the mask item down with them",
        viewer.viewerManagerVMain.sel_outlines === false,
        "nothing is drawn from it, and it is the expensive thing to keep loaded");

    viewer.setCellLayerVisible("cell_explorer", true);
    await controls.refreshLayerSurfaces();
    check("and turning one back on brings it up again",
        viewer.viewerManagerVMain.sel_outlines === true
        && viewer.calls.filter((c) => c === "ensureSegmentationReady").length === 2,
        "a visible layer with its canvases built and no mask item to blit them "
        + "onto is an eye toggle that does nothing anyone can see");

    const before = viewer.calls.filter((c) => c === "ensureSegmentationReady").length;
    await controls.refreshLayerSurfaces();
    check("and a layer that was already drawing does not re-read the pyramid",
        viewer.calls.filter((c) => c === "ensureSegmentationReady").length === before);
}

{
    const { controls, viewer, events } = build();
    viewer.registerCellLayer("cell_explorer", {});
    controls.syncToActiveLayer();
    await controls.selectMode("outlines");
    check("the mode event names the layer it is about",
        events.some((e) => e.type === "plexora:cell-mode-changed"
            && e.detail.layer === "cell_explorer" && e.detail.mode === "outlines"),
        "a plugin that stores the user's choice must ignore the other tools'");
}

// -- the shared opacity slider --------------------------------------------
//
// CANVAS-LEVEL, not a plugin's. It appears exactly while something is drawn
// over the image, and it moves whatever that something is -- the active
// plugin's layer, or core's own when no plugin holds one. It used to be keyed
// on a plugin having registered a layer, which left the commonest way to get a
// mask on screen (this control, no tool open) with no opacity at all and made
// it look like a feature Thresholding owned.

{
    const { controls, viewer, opacityRow, opacitySlider } = build();
    check("the opacity row is hidden while nothing is drawn over the image",
        opacityRow.hidden === true,
        "there is nothing to fade against");

    await controls.selectMode("outlines");
    check("turning the mask on with no tool open brings the control with it",
        opacityRow.hidden === false && opacitySlider.get() === 100,
        `row hidden ${opacityRow.hidden}, value ${opacitySlider.get()}`);

    opacitySlider.drag(40);
    check("and it fades core's own layer",
        viewer.cellDisplayOpacity === 0.4,
        "a mask can be turned on with no plugin at all, and fading it is the same wish");

    await controls.selectMode("none");
    check("taking the mask off takes the control away again",
        opacityRow.hidden === true);
}

{
    const { controls, viewer, opacityRow, opacitySlider, events } = build();
    viewer.registerCellLayer("cell_explorer", {});
    controls.syncToActiveLayer();
    check("a registered layer nobody has drawn yet does not bring it back",
        opacityRow.hidden === true,
        "the question is what is on screen, not what is loaded");

    await controls.selectMode("outlines");
    check("and it appears with the active layer's own value on it",
        opacityRow.hidden === false && opacitySlider.get() === 70,
        `row hidden ${opacityRow.hidden}, value ${opacitySlider.get()}`);

    opacitySlider.drag(30);
    check("dragging it moves the active layer and nothing else",
        viewer.getCellLayer("cell_explorer").opacity === 0.3
            && viewer.cellDisplayOpacity === 1,
        "on input rather than change: it is a blit argument, not a re-render");

    const before = events.length;
    opacitySlider.release();
    check("releasing it announces the value, tagged with the layer",
        events.slice(before).some((e) => e.type === "plexora:cell-layer-opacity-changed"
            && e.detail.layer === "cell_explorer" && e.detail.value === 0.3),
        "which is how a plugin persists it without owning a slider");
}

// -- the overlay key ------------------------------------------------------
//
// One bare letter that HIDES the selected cells and shows the same ones again.
// Not a mode change and not the card's eye: both of those mean "I am done with
// this", so they unload the mask and drop every tile's canvases, which made
// hiding instant and showing slow. This changes one blit-time boolean.

{
    const { controls, viewer, overlayHint, press, document: doc } = build();
    await controls.selectMode("outlines");
    check("the key is printed on the canvas as a cap and a sentence",
        overlayHint.hidden === false
            && overlayHint.key.textContent === "T"
            && overlayHint.label.textContent === "Toggle selected cells",
        `hint ${overlayHint.key.textContent} ${JSON.stringify(overlayHint.label.textContent)}`);

    const before = viewer.calls.length;
    const event = press("t");
    check("pressing it hides the selected cells",
        viewer.overlayMuted === true && event.defaultPrevented === true);
    check("and does nothing else at all",
        viewer.calls.slice(before).join(",") === "muted:true",
        `did ${JSON.stringify(viewer.calls.slice(before))}`);
    check("the mode is untouched, so what comes back is what went away",
        controls.mode === "outlines");
    check("and the caption says so, since nothing else on the page does",
        overlayHint.label.textContent === "Selected cells hidden"
            && overlayHint.classList.contains("is-off"));

    press("t");
    check("pressing it again shows the same cells",
        viewer.overlayMuted === false
            && viewer.calls.slice(before).join(",") === "muted:true,muted:false",
        "no pyramid read, no filter round trip, no re-render");
    check("and the caption goes back to offering the key",
        overlayHint.label.textContent === "Toggle selected cells"
            && overlayHint.classList.contains("is-off") === false);

    doc.activeElement = { tagName: "INPUT", isContentEditable: false };
    press("t");
    check("a field with focus outranks it",
        viewer.overlayMuted === false,
        "a project named Tonsil must not blink the cells on every T of it");
    doc.activeElement = null;

    press("t", { ctrlKey: true });
    check("and a modified chord is somebody else's",
        viewer.overlayMuted === false,
        "services/keyboardShortcuts.js owns those");
}

{
    // Choosing how the cells are drawn is also asking to see them, including
    // when the mode chosen is the one already selected -- which is what
    // somebody who has forgotten about the key will reach for.
    const { controls, viewer, overlayHint, press } = build();
    await controls.selectMode("outlines");
    press("t");
    await controls.selectMode("outlines");
    check("picking a mode brings hidden cells back",
        viewer.overlayMuted === false
            && overlayHint.label.textContent === "Toggle selected cells");
}

{
    // Nothing drawn: the key would do nothing anybody could see, so it is not
    // printed either. The project CAN draw outlines -- that is not the
    // question. On None the picture is the picture, and a caption offering to
    // toggle cells nobody asked for is a control that does nothing.
    const { controls, viewer, overlayHint, press } = build();
    press("t");
    check("with nothing on screen the key is inert",
        viewer.overlayMuted === false,
        "hiding what is already not there is a keystroke that looks broken");
    check("and on None the hint is not printed, though the project could draw",
        overlayHint.hidden === true && controls.offeredModes().outlines === true,
        `hidden ${overlayHint.hidden}, outlines offered ${controls.offeredModes().outlines}`);

    await controls.selectMode("centroids");
    check("choosing a way to draw them brings the key with it",
        overlayHint.hidden === false);
    await controls.selectMode("none");
    check("and going back to None takes it away again",
        overlayHint.hidden === true,
        "the hint follows what is on screen, not what the project has");
}

{
    // With a plugin holding the layer the hint follows the LAYER, because that
    // is what the key acts on: None is not even offered there, so keying on the
    // row's modes would have printed it for a layer whose eye is off.
    const { controls, viewer, overlayHint } = build();
    viewer.registerCellLayer("gating", {});
    controls.syncToActiveLayer();
    await controls.selectMode("outlines");
    check("a tool's layer prints the key",
        overlayHint.hidden === false);
    viewer.setCellLayerVisible("gating", false);
    await controls.refreshLayerSurfaces();
    check("and an eye that takes it off the canvas takes the key too",
        overlayHint.hidden === true,
        "nothing is drawn, so there is nothing for T to hide");
}

{
    // Nothing to toggle at all: no mask, no centroids. A key that does nothing
    // is worse than no key, so the hint is not printed.
    const { overlayHint } = build({ segmentation: null, hasCentroids: false,
                                    hasTable: false, segmentationStatus: null });
    check("a project with no cells to draw is not offered the key",
        overlayHint.hidden === true);
}

{
    // With a plugin holding the layer it is the same one boolean: the card's
    // eye is a different verb (it unloads), and using it here is what made
    // showing slow.
    const { controls, viewer, press } = build();
    viewer.registerCellLayer("gating", {});
    controls.syncToActiveLayer();
    await controls.selectMode("outlines");
    press("t");
    check("a tool's layer is hidden the same way, and stays loaded",
        viewer.overlayMuted === true
            && viewer.getCellLayer("gating").visible === true
            && viewer.getCellLayer("gating").mode === "outlines",
        "dropping the layer's canvases is what a rebuild on the way back costs");
    press("t");
    check("and comes back with its colours, its gate and its mode intact",
        viewer.overlayMuted === false
            && viewer.getCellLayer("gating").mode === "outlines");
}

// -- keyboard -------------------------------------------------------------

{
    const { controls, handlers, buttons } = build();
    let prevented = 0;
    handlers.get("keydown")({ key: "ArrowRight", preventDefault: () => { prevented += 1; } });
    check("arrow keys move through the enabled options",
        controls.mode === "centroids" && prevented === 1 && buttons.get("centroids").focused === 1,
        `mode ${controls.mode}`);
    handlers.get("keydown")({ key: "ArrowLeft", preventDefault: () => {} });
    check("and back again", controls.mode === "none");
    handlers.get("keydown")({ key: "Enter", preventDefault: () => { prevented += 1; } });
    check("other keys are left alone", prevented === 1 && controls.mode === "none");
}

{
    const { controls, handlers } = build({ segmentationMode: "outlines" });
    handlers.get("keydown")({ key: "ArrowLeft", preventDefault: () => {} });
    check("the keyboard skips options this project cannot draw",
        controls.mode === "outlines",
        "wrapping from None goes to Outlines, not to the disabled Filled");
}

// -- clicking -------------------------------------------------------------

{
    const { controls, viewer, buttons, handlers } = build();
    viewer.centroidsFromFallback = true;
    handlers.get("click")({ target: buttons.get("centroids") });
    await Promise.resolve();
    check("a click is a decision, and outranks the automatic fallback",
        viewer.centroidsFromFallback === false,
        "so a mask arriving later leaves it alone");
    check("clicking selects", controls.mode === "centroids");

    handlers.get("click")({ target: buttons.get("filled") });
    buttons.get("filled").disabled = true;
    handlers.get("click")({ target: buttons.get("filled") });
    await Promise.resolve();
    check("clicking a disabled option does nothing",
        controls.mode === "filled", "it was enabled for the first click only");
}

// -- the CTA asks rather than navigating ---------------------------------

{
    const { cta, asked } = build({ segmentation: null, hasTable: false });
    const event = await cta.click();
    check("clicking Add Seg Mask / Data asks for exactly what is missing",
        event.defaultPrevented === true
        && asked[0]?.datasource === "demo"
        && String(asked[0]?.keys) === "segmentation,table",
        `asked: ${JSON.stringify(asked[0])}`);
    check("a project that answered is re-read before the control repaints",
        asked.includes("refresh"),
        "the control would otherwise decide from the state it just changed");
    check("the wait for a converting mask is started",
        asked.includes("watch"),
        "a mask attached mid-session finishes into a page that never notices");
}

{
    const { cta, asked } = build({ segmentation: "/mask.zarr", hasTable: false });
    await cta.click();
    check("only the missing half is asked for",
        String(asked[0]?.keys) === "table", `asked: ${JSON.stringify(asked[0])}`);
}

{
    const { cta, asked } = build({ segmentation: null, hasTable: false,
                                   answerRequirements: false });
    await cta.click();
    check("backing out of the form changes nothing",
        asked.length === 1, `asked: ${JSON.stringify(asked)}`);
}

{
    const { cta, asked } = build({ segmentation: null, hasTable: false,
                                   maskAttached: true });
    await cta.click();
    check("a mask attached mid-session reloads rather than being patched in",
        asked.includes("reload") && !asked.includes("watch"),
        "attaching one inserts the Area channel at imageData[0], so every "
        + "channel index this page holds has just moved");
}

console.log(`\n${failures.length ? `FAILURES: ${failures.join(", ")}` : "all checks passed"}`);
process.exit(failures.length ? 1 : 0);
