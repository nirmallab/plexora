// The documentation's screenshots, as data. tools/docs/screenshots.mjs runs
// each entry: open `url`, run `steps`, wait for the network to go quiet, save
// website/public/media/<name>.webp at 2000x1250.
//
// Steps: {click: css} {at: [x, y]} (CSS px in the 1280x800 page) {hover: css}
// {drag: [[x, y], ...]} (press, move through, release) {move: [x, y]} (hover there)
// {goto: url} (load another page)
// {key: "m", modifiers: 4} (1 Alt, 2 Ctrl, 4 Meta, 8 Shift) {type: "text"}
// {eval: "js"} {waitFor: "js expression"} {wait: ms} {settle: quiet ms}.
// `{data}` in a step is replaced by $PLEXORA_DOCS_DATA, a folder holding the
// public demo files the import shots pick (see CONTRIBUTING_DOCS.md).
// `limit` caps each wait for the network (ms), for pages that poll.
// `manual: true` marks a shot taken by hand (a notebook); the script skips it.

/** Channels with their own colours and auto contrast, on any project whose
 * image has these markers; `soften` widens each window's top by that factor
 * (auto contrast is set for reading one channel, and four at full stretch
 * add up to white). Idempotent: the project remembers them. */
const channels = (want, soften = 2.2) => ({
    eval: `(async () => {
        await window.__plexoraReady;
        const s = __plexora.viewerSidebar;
        const want = ${JSON.stringify(want)};
        for (let i = 0; i < want.length; i += 1) {
            if (!s.channelSlots[i]) s.createAdditionalSlot(s.channelSlots.map((x) => x.name));
            s.setSlotMarker(i, want[i][0], { enable: true, reveal: true });
            if (!s.channelSlots[i].enabled) s.setSlotEnabled(i, true);
            s.setSlotColor(i, want[i][1], true);
        }
        while (s.channelSlots.filter((x) => x.visible).length > want.length) {
            s.removeChannelSlot(s.channelSlots.length - 1);
        }
        for (let i = 0; i < want.length; i += 1) {
            await s.autoChannel(i, { force: true });
            const [lo, hi] = s.channelSlots[i].range;
            s.setSlotRange(i, [lo, Math.round(lo + (hi - lo) * ${soften})], true);
        }
        s.selectSlot(0);
    })()`,
});

const EXEMPLAR = channels([["DNA_6", "#3d7dff"], ["CD45", "#ff3d9a"], ["ECAD", "#ffb020"], ["SMA", "#2bd46f"]]);

/** Fit the view to a rectangle in OpenSeadragon viewport units (image width = 1). */
const view = (x, y, w, h) => ({
    eval: `(() => {
        const v = __plexora.seaDragonViewer.viewer.viewport;
        v.fitBounds(new OpenSeadragon.Rect(${x}, ${y}, ${w}, ${h}), true);
    })()`,
});

const cells = (mode) => ({ click: `#cell_mode_${mode}` });

/** Regions for the ROI shots, imported once as GeoJSON in exemplar-001's
 * image pixels (the import refuses a document without the `plexora` member
 * that says what its coordinates are). */
const ROI_CATEGORIES = [
    ["c-docs-immune", "Immune aggregate", "#ff4fa3"],
    ["c-docs-tumor", "Tumor", "#ffb020"],
    ["c-docs-stroma", "Stroma", "#2bd46f"],
];
const ROIS = {
    type: "FeatureCollection",
    plexora: {
        schema_version: 1,
        coordinate_space: { type: "image_pixels", width: 2511, height: 3139, origin: "top-left", axis_order: "xy" },
        categories: ROI_CATEGORIES.map(([id, label, color], i) => ({ id, label, color, visible: true, locked: false, sort_order: i + 1 })),
    },
    features: [
        ["Immune aggregate", "#ff4fa3", [[1047, 933], [1315, 898], [1404, 1148], [1350, 1540], [1118, 1576], [958, 1326]]],
        ["Tumor", "#ffb020", [[83, 1398], [547, 1326], [690, 1755], [547, 2183], [190, 2273], [47, 1826]]],
        ["Stroma", "#2bd46f", [[1475, 1683], [1886, 1630], [2082, 2005], [1868, 2290], [1529, 2183]]],
    ].map(([category, color, ring], i) => ({
        type: "Feature",
        geometry: { type: "Polygon", coordinates: [[...ring, ring[0]]] },
        properties: { category_id: ROI_CATEGORIES[i][0], category, category_color: color, name: `${category} 1` },
    })),
};

const importRois = (datasource) => ({
    eval: `(async () => {
        const q = new URLSearchParams({ datasource: ${JSON.stringify(datasource)} });
        const state = await (await fetch("/plugins/roi/api/state?" + q)).json();
        if ((state.categories || []).some((c) => c.label === "Tumor")) return;
        await fetch("/plugins/roi/api/import", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ datasource: ${JSON.stringify(datasource)}, base_revision: state.revision,
                                   document: ${JSON.stringify(ROIS)} }),
        });
    })()`,
});

/** Local chip › Add a server… › Add a new server: the preset cards. */
const ADD_SERVER = [
    { click: "#quick_view_where" },
    { wait: 400 },
    { eval: "[...document.querySelectorAll('[role=menuitem], button, li')].find((e) => /Add a server/.test(e.textContent) && e.offsetParent)?.click()" },
    { wait: 800 },
    { eval: "[...document.querySelectorAll('button')].find((b) => /Add a new server/.test(b.textContent) && b.offsetParent)?.click()" },
    { waitFor: "[...document.querySelectorAll('dialog[open], [role=dialog]')].some((d) => /A Slurm cluster/.test(d.innerText))" },
];

/** Open one preset card by its title. */
const preset = (title) => ({
    eval: `[...document.querySelectorAll("button, [role=button], div")]
        .find((e) => e.children.length && e.textContent.trim().startsWith(${JSON.stringify(title)})
                     && e.offsetParent && e.getBoundingClientRect().height < 150)?.click()`,
});

/** Type into the open dialog's fields, found by the start of their label.
 * Nothing is submitted: every server shot ends with the form on screen. */
const fill = (values) => ({
    eval: `(() => {
        const values = ${JSON.stringify(values)};
        const fields = [...document.querySelectorAll("dialog[open] input, [role=dialog] input")].filter((e) => e.offsetParent);
        for (const [label, value] of Object.entries(values)) {
            const field = fields.find((e) => (e.labels?.[0]?.textContent || "").trim().startsWith(label));
            if (!field) continue;
            const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
            set.call(field, value);
            field.dispatchEvent(new Event("input", { bubbles: true }));
            field.dispatchEvent(new Event("change", { bubbles: true }));
        }
        document.activeElement?.blur();
    })()`,
});

/** Three Figure Builder captures of exemplar-001, taken once: C opens the
 * frame, S takes it. Stops at three however often the script runs. */
const CAPTURES = {
    eval: `(async () => {
        const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
        const press = (k) => document.dispatchEvent(new KeyboardEvent("keydown", { key: k, code: "Key" + k.toUpperCase(), bubbles: true }));
        const count = () => Number(document.querySelector(".fb-strip-count")?.textContent || 0);
        const v = __plexora.seaDragonViewer.viewer.viewport;
        for (const [x, y, w, h] of [[0.3, 0.45, 0.4, 0.33], [0.05, 0.5, 0.3, 0.25], [0.55, 0.6, 0.3, 0.25]]) {
            if (count() >= 3) break;
            v.fitBounds(new OpenSeadragon.Rect(x, y, w, h), true);
            await sleep(2500);
            press("c");
            await sleep(800);
            press("s");
            await sleep(3000);
        }
    })()`,
};

/** Open the navbar menu whose toggle has this id. */
const menu = (id) => ({ click: `#${id}` });

/** Cell display opacity, 0..1, through the sidebar's own slider. */
const opacity = (value) => ({
    eval: `(() => {
        const el = document.getElementById("cell_layer_opacity");
        if (!el) return;
        el.value = String(${value} * (Number(el.max) || 1));
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
    })()`,
});

/** Orion crop's two layer cards in a known state: the H&E layer listed but
 * off and folded, the multiplex image on and open. Clicks only what differs. */
const ORION_LAYERS = {
    eval: `(() => {
        const [he, img] = document.querySelectorAll("#layer_card_list > .layer-card");
        const off = (c) => c.classList.contains("is-layer-off");
        const folded = (c) => c.classList.contains("is-collapsed");
        if (!off(he)) he.querySelector(".layer-card-eye").click();
        if (off(img)) img.querySelector(".layer-card-eye").click();
        if (!folded(he)) he.querySelector(".layer-card-collapse").click();
        if (folded(img)) img.querySelector(".layer-card-collapse").click();
    })()`,
};

/** A shot of the whole window with nothing transient on it: no key hint
 * over the image, no toast, and the pointer parked on the empty menu bar so
 * no tooltip opens. */
const QUIET = [
    { eval: `(() => {
        const style = document.createElement("style");
        style.textContent = "#viewer_overlay_hint, .plx-toast { visibility: hidden !important; }";
        document.head.append(style);
    })()` },
    { move: [1100, 20] },
];

/** Close any toast a first visit raises. */
const DISMISS = { eval: "document.querySelectorAll('.plx-toast-dismiss').forEach((b) => b.click())" };

export const SHOTS = [
    // -- getting started -------------------------------------------------------------
    { name: "getting-started/home-page", url: "/" },
    {
        name: "getting-started/import-dialog",
        url: "/open_project",
        steps: [
            { eval: "PlexoraImportSample.open({paths: ['{data}/imc-steinbock/ome/ome/Patient1_001.ome.tiff', '{data}/imc-steinbock/masks_deepcell/masks_deepcell/Patient1_001.tiff', '{data}/imc-steinbock/intensities/intensities/Patient1_001.csv']})" },
            { waitFor: "document.querySelector('dialog[open]')?.innerText.includes('TABLE')" },
        ],
        after: 800,
    },
    { name: "getting-started/samples-page", url: "/open_project" },
    {
        name: "getting-started/interface",
        url: "/exemplar-001",
        steps: [EXEMPLAR, view(0.22, 0.38, 0.62, 0.5), cells("outlines"), opacity(0.6), ...QUIET],
        after: 2500,
    },

    // -- features --------------------------------------------------------------------
    {
        name: "features/opening-local-chip",
        url: "/",
        steps: [{ click: "#quick_view_where" }],
        after: 600,
    },
    {
        name: "features/new-dataset",
        url: "/open_project",
        steps: [
            { eval: "PlexoraDatasetBuilder.open({name: 'IMC cohort', root: '{data}/imc-steinbock'})" },
            { waitFor: "document.querySelector('dialog[open]')?.innerText.includes('Matched')", limit: 60000 },
        ],
        after: 800,
    },
    {
        name: "features/viewer-depth-strip",
        url: "/f8iic-smallchunk",
        steps: [DISMISS],
        after: 2500,
    },
    {
        name: "features/viewer-3d",
        url: "/f8iic-smallchunk",
        steps: [DISMISS, { click: "[data-depth-mode=volume]" }, { wait: 4000 }],
        after: 3000,
        limit: 60000,
    },
    {
        name: "features/channels-panel",
        url: "/exemplar-001",
        steps: [EXEMPLAR, view(0.3, 0.45, 0.4, 0.33), { eval: "__plexora.viewerSidebar.toggleSlotExpanded(1)" }],
        after: 2500,
    },
    {
        name: "features/channels-histogram",
        url: "/exemplar-001",
        steps: [EXEMPLAR, view(0.3, 0.45, 0.4, 0.33), { eval: "__plexora.viewerSidebar.selectSlot(2); __plexora.viewerSidebar.openHistogram()" }],
        after: 2500,
    },
    {
        name: "features/layers-panel",
        url: "/Orion%20crop",
        steps: [ORION_LAYERS],
        after: 2500,
    },
    {
        name: "features/cells-row",
        url: "/exemplar-001",
        steps: [EXEMPLAR, view(0.42, 0.55, 0.18, 0.15), cells("outlines"), opacity(1)],
        after: 2500,
    },
    {
        name: "features/cells-filled",
        url: "/exemplar-001",
        steps: [EXEMPLAR, view(0.42, 0.55, 0.18, 0.15), cells("filled"), opacity(0.5)],
        after: 2500,
    },
    {
        name: "features/projects-samples-list",
        url: "/open_project",
        steps: [{ click: "#project-view-list" }],
        after: 600,
    },
    { name: "features/projects-settings-data-dir", url: "/settings", limit: 8000 },
    {
        name: "features/export-menu",
        url: "/exemplar-001",
        steps: [EXEMPLAR, menu("navbarFileDropdown"), { hover: "#nav_export_menu" }],
        after: 800,
    },
    {
        name: "features/remote-connect",
        url: "/",
        steps: [{ click: "#quick_view_where" }, { wait: 300 }, { eval: "[...document.querySelectorAll('[role=menuitem], button, li')].find((e) => /Add a server/.test(e.textContent) && e.offsetParent)?.click()" },
                { wait: 600 }, { eval: "[...document.querySelectorAll('button')].find((b) => /Add a new server/.test(b.textContent) && b.offsetParent)?.click()" }],
        after: 1000,
    },

    // -- remote servers ----------------------------------------------------------------
    { name: "remote/presets", url: "/", steps: [...ADD_SERVER, ...QUIET, { eval: "document.activeElement?.blur()" }], after: 600, limit: 6000 },
    {
        name: "remote/slurm-form",
        url: "/",
        steps: [...ADD_SERVER, preset("A Slurm cluster"), { wait: 800 },
                fill({ "Name this connection": "cluster", "Your username": "me", "Address": "login.cluster.example.edu",
                       "How long to keep it": "4:00:00", "CPU cores": "4", "Memory": "16G" }), ...QUIET],
        after: 600,
        limit: 6000,
    },
    {
        name: "remote/files-form",
        url: "/",
        steps: [...ADD_SERVER, preset("Files on an SSH server"), { wait: 800 },
                fill({ "Name this connection": "files", "Your username": "me", "Address": "files.university.example.edu" }), ...QUIET],
        after: 600,
        limit: 6000,
    },
    {
        name: "remote/gcloud-form",
        url: "/",
        steps: [...ADD_SERVER, preset("Google Cloud (GCP)"), { wait: 3000 }, ...QUIET],
        after: 800,
        limit: 8000,
    },
    {
        name: "remote/settings-servers",
        url: "/settings",
        steps: [{ click: "[data-section=remotes]" }, { wait: 800 }, ...QUIET],
        after: 600,
        limit: 6000,
    },

    // -- plugins ---------------------------------------------------------------------
    {
        name: "plugins/tools-menu",
        url: "/exemplar-001",
        steps: [EXEMPLAR, menu("navbarToolsDropdown")],
        after: 800,
    },
    {
        name: "plugins/card-anatomy",
        url: "/exemplar-001?tool=roi",
        steps: [EXEMPLAR, importRois("exemplar-001"), { goto: "/exemplar-001?tool=roi" }],
        after: 2000,
    },
    {
        name: "plugins/thresholding",
        url: "/exemplar-001?tool=gating",
        steps: [
            EXEMPLAR, view(0.3, 0.45, 0.4, 0.33),
            { click: "#gate_marker_select input, #gate_marker_select button" }, { type: "CD45" }, { key: "Enter" },
            { wait: 1500 }, { click: "#gate_auto_button" },
        ],
        after: 3000,
    },
    {
        name: "plugins/thresholding-classifier",
        url: "/exemplar-001?tool=gating",
        steps: [EXEMPLAR, view(0.3, 0.45, 0.4, 0.33), { click: "#gating_mode_button" }],
        after: 3000,
    },
    {
        name: "plugins/roi",
        url: "/exemplar-001?tool=roi",
        steps: [EXEMPLAR, importRois("exemplar-001"), { goto: "/exemplar-001?tool=roi" }],
        after: 2500,
    },
    {
        name: "plugins/roi-magic-select",
        url: "/exemplar-001?tool=roi",
        steps: [EXEMPLAR, view(0.3, 0.45, 0.4, 0.33), { click: "#roi_tool_magic" }],
        after: 2500,
    },
    { name: "plugins/qc", url: "/exemplar-001?tool=qc", steps: [EXEMPLAR], after: 2500 },
    {
        name: "plugins/qc-registration",
        url: "/exemplar-001?tool=qc",
        steps: [EXEMPLAR, { click: "#qc_reg_fold" }],
        after: 2500,
    },
    {
        name: "plugins/qc-segmentation",
        url: "/exemplar-001?tool=qc",
        steps: [EXEMPLAR],
        after: 2500,
    },
    {
        name: "plugins/cell-explorer",
        url: "/Orion%20crop?tool=cell_explorer",
        steps: [],
        after: 3000,
    },
    {
        name: "plugins/cell-explorer-numeric",
        url: "/Orion%20crop?tool=cell_explorer",
        steps: [],
        after: 3000,
    },
    {
        name: "plugins/figure-dock",
        url: "/exemplar-001?tool=figure_builder",
        steps: [EXEMPLAR, cells("none"), CAPTURES, view(0.3, 0.45, 0.4, 0.33), ...QUIET],
        after: 2000,
    },
    { name: "plugins/figure-canvas", url: "/plugins/figure_builder/figures", after: 1500 },
    { name: "plugins/figure-export", url: "/plugins/figure_builder/figures", after: 1500 },

    // -- home page ---------------------------------------------------------------------
    {
        // The frame the home page lays its demo clip into: the stack in 3-D,
        // no tool open. What matters is the canvas box, not the tissue.
        name: "home/video-frame",
        url: "/f8iic-smallchunk",
        steps: [DISMISS, { click: "[data-depth-mode=volume]" }, { wait: 4000 }, ...QUIET],
        after: 3000,
        limit: 60000,
    },
    {
        name: "home/explore-viewer",
        url: "/exemplar-001",
        steps: [EXEMPLAR, cells("none"), view(0.3, 0.45, 0.4, 0.33), ...QUIET],
        after: 2500,
    },
    {
        name: "home/explore-plugins",
        url: "/exemplar-001?tool=roi",
        steps: [EXEMPLAR, cells("none"), importRois("exemplar-001"), { goto: "/exemplar-001?tool=roi" }, ...QUIET],
        after: 2500,
    },
    // Taken by tools/docs/notebook_shot.mjs against a running JupyterLab.
    { name: "notebook/cell", manual: true },
    { name: "home/explore-notebook", manual: true },
    { name: "home/explore-python", url: "/open_project", steps: [...QUIET] },
];
