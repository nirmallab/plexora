/**
 * The Layers panel: one card per layer, in the order they are drawn.
 *
 * THE SIDEBAR IS THE STACK. Not a list of layers with their controls
 * elsewhere -- the card IS where a layer is configured. The base image's
 * channels are inside the base image's card; a registered slide's opacity,
 * blend and colour are inside its own; the transcripts plugin's gene tree is
 * inside the transcript layer's. Before this they were three unrelated
 * sections over one scene, and the panel that called itself "Layers" offered
 * an eye and a slider for rasters that no renderer read.
 *
 * WHAT EVERY CARD HAS, whatever the modality: a grip to reorder, an eye to
 * show and hide, a chevron to configure, and an X to remove. What is INSIDE
 * is the modality's own and core knows nothing about it -- which is the same
 * boundary as always. Nothing here interprets data. No clusters, no
 * phenotypes, no expression: a plugin computes those and asks core to draw
 * them, and this panel is one of the places that rule has to hold or the
 * viewer quietly becomes an analysis application.
 *
 * WHAT HAS NO CARD. The cell mask and the centroids. There is exactly one
 * segmentation in a project and the Cells footer owns how it is drawn -- mode,
 * point size, opacity -- so a "Cell boundaries" card was a second eye for a
 * control that already existed, wired to nothing. The mask is `pinned` in the
 * stack instead, which keeps it above every raster whatever the cards are
 * dragged into, because there is no card that could put it back.
 *
 * TWO THINGS THE PANEL SAYS THAT THE VIEWER NEVER USED TO
 *
 *   "Aligned by assumption." A layer with no transform has not been registered
 *   against the reference image -- Plexora asserted the identity everywhere and
 *   never said so. For a single image that was harmless; for a scene it is the
 *   difference between "these line up" and "nobody has checked".
 *
 *   "Cannot be drawn: shear." OpenSeadragon places a TiledImage with x, y,
 *   width, degrees and flipped, so shear and anisotropic scale have no way to be
 *   expressed. Drawn without its shear a layer looks entirely plausible and is
 *   wrong by a few microns everywhere -- which is exactly the error a
 *   registration exists to remove. Refused on the card, where somebody can see it.
 *
 * ONE ORDER, TWO SURFACES. Rasters composite inside OSD's world; points and
 * shapes are drawn on a canvas above it. So a points layer cannot be dragged
 * underneath an image, whatever the list says -- which is the one place the
 * stack is not literally the z-order, and it is stated on a divider rather
 * than hidden. Allowing the drag and ignoring it would be a reorder that
 * silently does nothing.
 *
 * RECONCILED, NOT REBUILT. The list used to be wiped and rebuilt on every
 * change, which is safe only while no card holds anything it did not build.
 * Cards hold adopted markup now -- the channel slots with their d3 sliders and
 * colour pickers, a plugin's whole panel with its controller's handles -- and
 * a wipe would leave every one of those pointing at a node no longer on the
 * page, with nothing reporting it. So cards are built once, kept by id, and
 * re-appended to reorder.
 *
 * Served as a classic script (see base.html).
 */
window.PlexoraLayerManager = (function () {
    "use strict";

    const LIST_ID = "layer_card_list";
    const SECTION_ID = "layer_manager_section";
    const COLLAPSE_ID = "layer_manager_collapse";
    const CARD_ATTR = "data-layer-card";

    /** Cards read top-down and the top card is the TOP of the picture, so the
     *  surfaces are listed in reverse composite order here. */
    const SURFACE_ORDER = ["gl", "overlay", "tiles"];

    /** What each kind is called on its card when the layer has no label. */
    const KIND_LABEL = {
        image: "Image",
        labels: "Segmentation",
        points: "Points",
        shapes: "Shapes",
    };

    /**
     * The kinds that GET a card, before anything else is taken into account.
     *
     * Rasters. A card offers an eye, a place in the stack and a body of
     * controls, and for an image layer core draws it, so core can turn it off,
     * fade it and restack it.
     *
     * `labels` is NOT here, and that is the change: the only labels layer is
     * the cell mask, and the Cells footer owns how cells are drawn. `points`
     * and `shapes` are not here either, for the older reason -- core draws
     * neither, so an eye on one would hide nothing.
     *
     * A layer without a card is still in `/config` and still in the stack:
     * this is about what the panel claims it can do, not about what exists.
     *
     * UNLESS SOMETHING SAYS IT DRAWS IT, OR STAGED A PANEL FOR IT.
     * `LayerStack.claim` is how a plugin says the first; a
     * `data-layer-body` mount in `#layer_section_slot` is how it says the
     * second, and that one arrives with the page rather than with the
     * plugin's JavaScript -- so a transcript layer still being built gets its
     * card, and its "Preparing…" line, before anything has claimed anything.
     */
    const CARDED_KINDS = new Set(["image"]);

    function kindOf(layer) { return String(layer?.kind || ""); }

    let stack = null;
    let sortable = null;
    let unsubscribe = null;
    const collapsed = new Set();
    /** Layers the user has pinned in place. Held here rather than on the stack
     *  record: it is a fact about this panel, and the renderer has no use for it. */
    const locked = new Set();
    /** id -> the card element, built once. See RECONCILED, NOT REBUILT. */
    const cards = new Map();
    /** Layers whose card has swallowed markup staged in `#layer_section_slot`.
     *  Once adopted, that markup is no longer IN the slot, so the lookup that
     *  found it stops answering -- and this is what remembers it was there. */
    const adopted = new Set();
    /** id -> show this opacity, for a card whose opacity is a number on a
     *  button rather than a handle on a track. `paint` calls it, so a fade
     *  from viewerControls or a plugin reaches the word the user reads. */
    const opacityReadouts = new Map();

    /** id -> the mounted channel panel for that layer (views/
     *  layerChannelPanel.js). Held here rather than on the card because the
     *  card is rebuilt and the panel must not be: it owns a ViewerSidebar
     *  instance with its slots, its sliders and its restore already done. */
    const panels = new Map();

    /** id -> the mounted ground dot for that layer, for the same reason: it
     *  owns a ColorSwatchPicker whose popover is parked in the portal, and a
     *  card rebuilt around a fresh one would leave the old popover behind. */
    const grounds = new Map();

    /** id -> the compact opacity control on that layer's action line (see
     *  buildOpacityControl), for the same reason again: its track is a
     *  popover parked in the portal. The reference image's is held here too,
     *  though nothing ever drops it. */
    const opacities = new Map();

    /** How long a slider has to stop moving before the server is told. Long
     *  enough that a drag across the whole travel is one request; short enough
     *  that nobody reloads inside it. */
    const PERSIST_DELAY = 500;

    //: id -> the write waiting to go out for that layer. See `persist`.
    const pending = new Map();

    function el(id) { return document.getElementById(id); }

    function sections() { return window.PlexoraLayerSections || null; }

    /** The base image layer's id, spelled once. */
    function baseId() { return PlexoraLayerStack.REFERENCE_LAYER_ID; }

    /**
     * Whether this layer gets the reference image's channel controls.
     *
     * Deliberately broad: ANY image layer whose pixels are channel planes,
     * one channel or forty. A layer is not a lesser kind of image, so it does
     * not get a lesser kind of control.
     *
     * Three things can say no. An rgb layer has no channel to pick -- its
     * bytes are already the picture. The base image already HAS these
     * controls, staged in index.html. And a build without the panel module
     * (the layer-manager probe's, for one) falls back to the single-select
     * row this replaces rather than to nothing.
     */
    function hasChannelPanel(layer) {
        if (layer.kind !== "image" || layer.id === baseId()) return false;
        if ((layer.spec?.render || {}).rgb) return false;
        if (!window.PlexoraLayerChannels) return false;
        return (layer.spec?.channels || []).some((c) => c?.name && c?.src);
    }

    /**
     * This layer's channel panel, mounted once.
     *
     * MOUNTED ONCE is the requirement, not an optimisation. The panel owns a
     * ViewerSidebar: its slots, its sliders, the restore it has already done
     * and the stats it has already fetched. `refreshBody` wipes and rebuilds a
     * raster card's body on every change to the layer -- so the node is kept
     * here and `appendChild` MOVES it back in, which is the same trick
     * `buildBaseFooter` uses for the channel counter.
     */
    function channelPanelFor(layer) {
        const held = panels.get(layer.id);
        if (held) return held;
        const mounted = window.PlexoraLayerChannels?.mount({
            layer,
            // Resolved on every call, the way `restyle` does it: a card can be
            // built before `seaDragonViewer` is assigned -- a stack change
            // during the viewer's own construction is enough -- and a handle
            // taken at mount would be undefined for the life of the panel.
            draw: (list) => window.__plexora?.seaDragonViewer?.viewerManagerVMain
                ?.tiledLayers?.get(layer.id)?.setChannels?.(list),
            persist: (patch) => persist(layer.id, patch),
            // Absent in a build without the modal, and the panel then draws no
            // button rather than one that cannot finish what it starts.
            rename: window.PlexoraChannelNames
                ? () => renameChannels(layer.id) : null,
        });
        if (!mounted) return null;
        panels.set(layer.id, mounted);
        return mounted;
    }

    /**
     * Name this layer's channels from a file the user has.
     *
     * The SAME dialog and the same route the reference image's "Upload channel
     * names" uses (views/channelNamesUpload.js, POST /upload_channels), told
     * which layer -- because it is the same question about a different image,
     * and a second copy of it would be a second place for a spreadsheet to be
     * read differently.
     */
    function renameChannels(id) {
        const sample = window.flaskVariables?.datasource;
        //: Looked up now rather than held, for the reason `draw` resolves the
        //: viewer now: the panel is mounted once and outlives the stack record
        //: it was built from -- a `/config` poll registers a fresh spec -- and
        //: the names have to be written onto the one the viewer is reading.
        const layer = stack?.get(id);
        if (!sample || !layer) return;
        window.PlexoraChannelNames.open({
            datasource: sample,
            layer: id,
            label: layer.label || id,
            onApplied: (names) => adoptChannelNames(id, names),
        });
    }

    /**
     * Take on the names the server has just applied to this layer.
     *
     * REBUILT rather than patched in place, unlike the reference image's
     * `adoptChannelNames` in main.js. The names are the keys here: the panel
     * holds `name -> index` and `name -> src` maps, and the layer's
     * `LayerChannelSet` holds one world item per channel NAME, so patching
     * would mean rewriting four maps in two modules and getting all four
     * right. A rename is a deliberate, once-per-layer act, so the whole layer
     * is dropped and drawn again from the new list instead.
     *
     * What is NOT lost is which channels were on, in what colour and window:
     * `render.channels` carries the index beside each name and the panel
     * resolves by index first, so the slots come back exactly as they were --
     * wearing the new names. A rename moves no index, which is the same
     * property the reference image's rename relies on.
     *
     * `layer.spec` IS the entry in `config.layers` (see
     * imageViewer.syncLayers, which registers the spec by reference), so
     * writing the names here is what makes the re-sync below draw them.
     */
    function adoptChannelNames(id, names) {
        const layer = stack?.get(id);
        if (!layer) return;
        const spec = layer.spec || {};
        const list = names || [];
        spec.channels = (spec.channels || []).map((channel, index) => (
            list[index] === undefined ? channel : {
                ...channel,
                name: String(list[index]),
                fullname: String(list[index]),
            }));
        dropChannelPanel(id);
        const manager = window.__plexora?.seaDragonViewer?.viewerManagerVMain;
        const handle = manager?.tiledLayers?.get(id);
        if (handle) {
            handle.remove();
            manager.tiledLayers.delete(id);
            manager.syncLayerImages(
                window.__plexora?.seaDragonViewer?.config?.layers || []);
        }
        render();
    }

    /** Forget a layer's panel and take its listeners back off. */
    function dropChannelPanel(id) {
        const held = panels.get(id);
        if (held) {
            held.destroy();
            panels.delete(id);
        }
        // The ground dot goes the same way and for the same reason: its
        // popover lives in the portal, not in the card, so dropping the card
        // alone would leave it on the page with nothing to open it.
        const ground = grounds.get(id);
        if (ground) {
            ground.picker.destroy?.();
            grounds.delete(id);
        }
        // And the compact opacity control on the panel's action line, whose
        // track is a popover in the portal too. Its destroy takes it out of
        // `opacities` itself.
        opacities.get(id)?.destroy();
    }

    /**
     * The markup this layer's card body was staged from, or null.
     *
     * By id first, because that is exact. By modality second, for a layer that
     * did not exist when the page was rendered -- imported mid-session, or
     * registered after `/config` was first served -- whose id could not have
     * been written into the mount. Answers only once: adopting MOVES the node
     * out of the slot, which is what makes it safe to ask on every render.
     */
    function stagedFor(layer) {
        const api = sections();
        if (!api || !layer) return null;
        const modality = layer.spec?.modality || layer.modality || "";
        return api.bodyFor(layer.id) || api.bodyForModality(modality) || null;
    }

    /** Whether this layer earns a card. See CARDED_KINDS. */
    function isCarded(layer) {
        if (!layer) return false;
        if (CARDED_KINDS.has(kindOf(layer))) return true;
        if (layer.drawnBy) return true;
        if (adopted.has(layer.id)) return true;
        return Boolean(stagedFor(layer));
    }

    /** Which of the three cards this layer gets. */
    function cardStyle(layer) {
        if (layer.id === baseId()) return "base";
        if (adopted.has(layer.id) || stagedFor(layer)) return "plugin";
        return "raster";
    }

    /**
     * "+ Add Layer" -- the progressive half of the import story.
     *
     * The same dialog the library page opens, scoped to this sample: no name,
     * no dataset, everything proposed as a layer of what is already open. It
     * is here rather than on the edit page because this is where somebody
     * realises a layer is missing -- they are looking at the stack.
     */
    function bindAdd() {
        const button = el("layer_add_button");
        if (!button || button.dataset.bound) return;
        button.dataset.bound = "1";
        button.addEventListener("click", () => {
            window.PlexoraImportSample?.open({
                sample: window.flaskVariables?.datasource,
            });
        });
    }

    /**
     * What a card is titled.
     *
     * The base image is titled by what the layer IS, and its own `label` is
     * ignored -- the server fills that with the PROJECT's name (see
     * `Project.reference_layer`), which is the right name for the sample and
     * the wrong one for the bottom row of a stack. Read down the list, the
     * name has one job: to say how this layer differs from "Segmentation" and
     * "Transcripts" two rows above it. The project's name is already on the
     * page, in the navbar, where it names the whole view rather than one
     * layer of it.
     */
    function labelFor(layer) {
        if (layer.id === baseId()) return "Image";
        return layer.label || KIND_LABEL[layer.kind] || layer.id;
    }

    /**
     * What this layer's registration amounts to, in one line.
     *
     * Four states, and the first is the one that matters: it is what the viewer
     * has always silently assumed.
     */
    function alignmentNote(layer) {
        if (layer.id === baseId()) {
            return { text: "Reference layer", tone: "ok" };
        }
        if (layer.transformUnsupported) {
            return {
                text: `Cannot be drawn: ${layer.transformUnsupported}`,
                tone: "bad",
            };
        }
        if (!layer.transform) {
            return { text: "Aligned by assumption", tone: "warn" };
        }
        const parts = PlexoraLayerStack.decomposeTransform(layer.transform);
        const bits = [];
        if (Math.abs(parts.translateX) > 0.5 || Math.abs(parts.translateY) > 0.5) {
            bits.push(`moved ${Math.round(parts.translateX)}, ${Math.round(parts.translateY)} px`);
        }
        if (Math.abs(parts.rotation) > 0.01) bits.push(`rotated ${parts.rotation.toFixed(2)}°`);
        if (Math.abs(parts.scaleX - 1) > 1e-6) bits.push(`scaled ${parts.scaleX.toFixed(4)}×`);
        if (parts.flipped) bits.push("mirrored");
        return { text: bits.length ? `Registered: ${bits.join(", ")}` : "Registered", tone: "ok" };
    }

    /** How strongly this layer is drawn. The one control every drawn layer has. */
    function buildOpacityRow(layer) {
        const row = document.createElement("div");
        row.className = "layer-card-row";
        const label = document.createElement("label");
        const sliderId = `layer_opacity_${layer.id}`;
        label.setAttribute("for", sliderId);
        label.textContent = "Opacity";
        // The readout is the slider's own number box now, and typeable: 0.35
        // is a value somebody copies from one layer to another, and there was
        // no way to state it except by dragging until the <output> agreed.
        const slider = new PlexoraSlider(null, {
            id: sliderId, min: 0, max: 1, step: 0.01,
            value: layer.opacity, decimals: 2, ariaLabel: "Opacity",
            // On `onInput`, not `onChange`: opacity is a blend on the live
            // world item, so it costs nothing per tick and a slider that only
            // moved the picture on release would feel broken. The write to the
            // server on the same path is already debounced by `persist`.
            onInput: (value) => {
                stack?.setOpacity(layer.id, value);
                // NOT for a layer a plugin draws. Its own panel already
                // persists its opacity, through its own state route -- two
                // writers for one number would disagree on the next reload,
                // and the plugin's is the one that knows what else it belongs
                // with.
                if (!stack?.get(layer.id)?.drawnBy) persist(layer.id, { render: { opacity: value } });
            },
        });
        row.appendChild(label);
        row.appendChild(slider.el);
        return row;
    }

    /**
     * The same number, in the width of a word: `Opacity 100%`, with the track
     * behind it.
     *
     * For any card that has something else to put on that line. An image's
     * whole-picture actions share a row -- opacity and "Upload channel names"
     * -- and a label, a full track and a number box cannot share anything in
     * a 300px sidebar. What moves into the popover is the part nobody looks
     * at: this number is 100% on almost every project, and the slider is
     * there for the day it is not. The reference image's card has that line
     * staged in index.html; a registered layer's channel panel builds the
     * same line (views/layerChannelPanel.js); both mark it
     * `data-layer-opacity-slot`. A layer is not a lesser kind of image, and
     * it does not get a bigger kind of opacity control either -- a multiplex
     * imported as a layer looked like a different widget from one imported as
     * the reference image, for no reason the pixels could account for.
     *
     * THE TRACK IS PORTALED, for the reason ColorSwatchPicker's palette is: a
     * dimmed row has `opacity` below 1, which makes a stacking context, which
     * would trap a popover parked inside it behind the next card. See
     * PopoverPortal for why <body> is not unconditionally the host.
     *
     * BUILT ONCE PER LAYER AND HELD, in `opacities`, the way the ground dot is
     * held in `grounds`: `refreshBody` rebuilds a layer card's body on every
     * change to the layer, and a control rebuilt with it would leave its
     * popover behind on the portal each time -- the orphan ColorSwatchPicker
     * warns about. The trigger lives inside the channel panel's node, which
     * is itself kept and moved back in, so a rebuilt card finds it already on
     * its line (see buildBody); `destroy` goes with the panel's, in
     * dropChannelPanel. The reference image's is never dropped, because its
     * card is never rebuilt.
     */
    function buildOpacityControl(layer) {
        const sliderId = `layer_opacity_${layer.id}`;

        const trigger = document.createElement("button");
        trigger.type = "button";
        trigger.className = "layer-opacity-trigger";
        trigger.title = "How strongly this layer is drawn";
        trigger.setAttribute("aria-haspopup", "true");
        trigger.setAttribute("aria-expanded", "false");
        const name = document.createElement("span");
        name.textContent = "Opacity";
        const readout = document.createElement("span");
        readout.className = "layer-opacity-value";
        trigger.append(name, readout);

        const popover = document.createElement("div");
        popover.className = "layer-opacity-popover";
        popover.hidden = true;
        // Or the document-click handler below would close it on the way to the
        // handle the user just grabbed.
        popover.addEventListener("click", (event) => event.stopPropagation());
        const label = document.createElement("label");
        label.className = "layer-opacity-popover-label";
        label.setAttribute("for", sliderId);
        label.textContent = "Opacity";
        popover.appendChild(label);

        const say = (value) => {
            const fraction = Number.isFinite(value) ? value : 1;
            readout.textContent = `${Math.round(fraction * 100)}%`;
        };
        const slider = new PlexoraSlider(popover, {
            id: sliderId, min: 0, max: 1, step: 0.01,
            value: layer.opacity, decimals: 2, ariaLabel: "Opacity",
            // On `onInput`, not `onChange`: opacity is a blend on the live
            // world item, so it costs nothing per tick and a slider that only
            // moved the picture on release would feel broken. The write to the
            // server on the same path is already debounced by `persist`.
            onInput: (value) => {
                say(value);
                stack?.setOpacity(layer.id, value);
                // NOT for a layer a plugin draws -- see buildOpacityRow.
                if (!stack?.get(layer.id)?.drawnBy) persist(layer.id, { render: { opacity: value } });
            },
        });
        say(layer.opacity);
        PopoverPortal.attach(popover);

        let open = false;
        const close = () => {
            if (!open) return;
            open = false;
            popover.classList.remove("is-open");
            trigger.setAttribute("aria-expanded", "false");
            window.setTimeout(() => { if (!open) popover.hidden = true; }, 150);
            document.removeEventListener("click", onDocumentClick);
            document.removeEventListener("keydown", onEscape);
            window.removeEventListener("resize", close);
            window.removeEventListener("scroll", close, true);
        };
        const onDocumentClick = (event) => {
            if (!trigger.contains(event.target)) close();
        };
        const onEscape = (event) => { if (event.key === "Escape") close(); };
        trigger.addEventListener("click", (event) => {
            event.stopPropagation();
            if (open) { close(); return; }
            open = true;
            // Right-aligned under the trigger: this control sits at the left
            // of its row in a panel pinned to the right of the window, so a
            // popover hung from its left edge has the whole sidebar to grow
            // into and a wider one would still fit.
            const rect = trigger.getBoundingClientRect();
            popover.style.left = `${rect.left}px`;
            popover.style.top = `${rect.bottom + 6}px`;
            popover.hidden = false;
            requestAnimationFrame(() => popover.classList.add("is-open"));
            trigger.setAttribute("aria-expanded", "true");
            document.addEventListener("click", onDocumentClick);
            document.addEventListener("keydown", onEscape);
            window.addEventListener("resize", close);
            window.addEventListener("scroll", close, true);
        });

        // Silent: this is the panel being told what the number already is,
        // not the user setting it, and emitting here would write the value
        // straight back to the server on every repaint.
        opacityReadouts.set(layer.id, (value) => {
            if (open) return;
            say(value);
            slider.set(value, { silent: true });
        });

        // Off the page for good: shut (which unbinds the document listeners),
        // then out of the portal, or a rebuilt panel's control would sit
        // beside a dead popover that the portal keeps re-parenting on every
        // fullscreen toggle.
        const destroy = () => {
            close();
            PopoverPortal.detach(popover);
            opacityReadouts.delete(layer.id);
            opacities.delete(layer.id);
        };
        opacities.set(layer.id, { trigger, destroy });
        return trigger;
    }

    /** The alignment line, as any card's last word about itself. */
    function buildAlignment(layer) {
        const note = alignmentNote(layer);
        const alignment = document.createElement("p");
        alignment.className = `layer-card-alignment is-${note.tone}`;
        alignment.textContent = note.text;
        return alignment;
    }

    /**
     * The contrast window this layer's tiles are quantized through.
     *
     * NUMBERS AND NOT A SLIDER, deliberately. A slider needs a domain, and the
     * client has none for a registered layer: its pyramid is its own, it has
     * no entry in `config.imageData`, and the window the file declares is
     * known only to `layer_sources.serve_layer_tile`. A 16-bit slider whose
     * useful travel is the first two percent is worse than a box to type in --
     * and leaving a box EMPTY is how you get the file's own window back, which
     * a slider cannot express at all.
     *
     * Only for a windowable layer. A brightfield or H&E tile is already the
     * picture the scanner recorded, with nothing to re-window (see
     * `parse_style`, which applies lo/hi only to a channel plane), so the row
     * is absent rather than present and inert.
     */
    function buildWindowRow(layer) {
        // Rasters only, and the same reason: a window is a quantization the
        // tile route applies to a channel plane.
        if (layer.kind !== "image" || layer.id === baseId()) return null;
        const render = layer.spec?.render || {};
        if (render.rgb) return null;
        // A panelled layer has a logarithmic slider per channel instead, with
        // Auto behind it. Two ways to set one window is what this whole change
        // is undoing.
        if (hasChannelPanel(layer)) return null;

        const row = document.createElement("div");
        row.className = "layer-card-row";
        const label = document.createElement("label");
        label.textContent = "Window";
        row.appendChild(label);

        const pair = document.createElement("div");
        pair.className = "layer-card-window";
        const [lo, hi] = render.range || [];
        const inputs = [];
        [["Low", lo], ["High", hi]].forEach(([name, value]) => {
            const input = document.createElement("input");
            input.type = "number";
            input.className = "plx-number layer-card-number";
            input.min = "0";
            input.step = "1";
            input.setAttribute("aria-label", `${name} end of the window`);
            input.placeholder = name === "Low" ? "auto" : "auto";
            if (Number.isFinite(value)) input.value = String(value);
            // On `change`, not `input`: a new window is a new tile ADDRESS, so
            // applying it re-adds the world item and refetches the viewport.
            // Per keystroke that would be a request per digit.
            input.addEventListener("change", () => applyWindow(layer, inputs));
            inputs.push(input);
            pair.appendChild(input);
        });
        row.appendChild(pair);
        return row;
    }

    function applyWindow(layer, inputs) {
        const values = inputs.map((input) => {
            const parsed = Number(input.value);
            return input.value === "" || !Number.isFinite(parsed) ? null : parsed;
        });
        // Both or neither: a half-stated window is the file's own window with
        // one end moved, and `parse_style` already fills an absent end in from
        // it -- so `null` here means exactly what leaving the box empty means.
        restyle(layer, { range: values.every((v) => v === null) ? null : values });
    }

    /** The opacity control, the alignment line and a raster's own controls. */
    function buildBody(layer) {
        const body = document.createElement("div");
        body.className = "layer-card-controls";

        // Where the opacity goes is the question buildBaseBody asks of the
        // staged markup, with the same answer. A channel panel builds the
        // reference card's action line -- "Upload channel names", marked
        // `data-layer-opacity-slot` -- and the compact control goes first on
        // it, exactly where it goes on the reference card. A card with no
        // such line (an rgb layer, a build without the panel module) gets the
        // ordinary row every other card has.
        const panel = hasChannelPanel(layer) ? channelPanelFor(layer) : null;
        const slot = panel?.node?.querySelector?.("[data-layer-opacity-slot]");
        if (slot) {
            // Held, not rebuilt -- see buildOpacityControl. The panel's node
            // is kept across rebuilds and the trigger sits inside it, so on a
            // rebuild this finds it already first on its line and leaves it
            // alone (a re-insert would blur whatever is focused inside it).
            const trigger = opacities.get(layer.id)?.trigger || buildOpacityControl(layer);
            if (trigger.parentNode !== slot || slot.firstChild !== trigger) {
                slot.insertBefore(trigger, slot.firstChild);
            }
        } else {
            body.appendChild(buildOpacityRow(layer));
        }

        const channels = buildChannelRow(layer);
        if (channels) body.appendChild(channels);

        const window_ = buildWindowRow(layer);
        if (window_) body.appendChild(window_);

        // The channel panel carries the card's last line with it -- the
        // counter has to stay inside the sidebar instance's root -- so the
        // alignment note goes onto that line rather than standing alone.
        if (panel) {
            body.appendChild(panel.node);
            panel.setAlignment(buildAlignment(layer));
        } else {
            body.appendChild(buildAlignment(layer));
        }

        const state = buildState(layer);
        if (state) body.appendChild(state);

        return body;
    }

    /**
     * The base image layer's card body: its channels, or its adjustments.
     *
     * The markup is the sidebar's own, staged in `#layer_section_slot` and
     * moved in here once -- see views/layerSections.js. Built in the template
     * rather than here because the things that drive it (channelList's slots,
     * brightfieldAdjust's sliders) take their handles by id and keep them, and
     * a node moved keeps working where a node rebuilt does not.
     *
     * A brightfield project's staged markup already carries an opacity slider,
     * writing the same number to the same stack, so this does not add a second
     * one. Two sliders for one fact is exactly what this panel is undoing.
     */
    function buildBaseBody(layer) {
        const body = document.createElement("div");
        body.className = "layer-card-controls";
        const staged = stagedFor(layer);
        // Three ways the opacity gets onto this card, and the staged markup
        // decides which. A LINE TO SHARE (`data-layer-opacity-slot`, the
        // fluorescence card's action row) takes the compact control; a
        // CONTROL OF ITS OWN (`data-layer-opacity`, what a brightfield project
        // stages beside its brightness and gamma) takes nothing, because two
        // sliders for one number is what this panel is undoing; anything else
        // gets the ordinary row every other card has.
        const slot = staged?.querySelector?.("[data-layer-opacity-slot]");
        if (slot) slot.insertBefore(buildOpacityControl(layer), slot.firstChild);
        else if (!hasStagedOpacity(staged)) body.appendChild(buildOpacityRow(layer));
        if (staged) {
            adopted.add(layer.id);
            body.appendChild(staged);
        }
        body.appendChild(buildBaseFooter(layer, staged));
        return body;
    }

    /**
     * The card's last line: what this layer is aligned to, and how much of it
     * is switched on.
     *
     * "Reference layer" and "1 active · 15 max" are the same kind of sentence
     * -- a muted caption stating a fact the user cannot click -- and they were
     * at opposite ends of the card, the counter crowded into the header beside
     * the eye where it read as a control that had lost its button. One line,
     * one at each end.
     *
     * The counter is MOVED out of the staged markup rather than built here:
     * viewerSidebar and channelList both write `#num-selected-channels` by id
     * and take that handle early, and a node moved keeps working where a node
     * rebuilt does not. Appended after the staged block for the same reason
     * appendChild is the reorder everywhere else in this file -- it takes the
     * node out of wherever it was.
     */
    function buildBaseFooter(layer, staged) {
        const footer = document.createElement("div");
        footer.className = "layer-card-footer";
        footer.appendChild(buildAlignment(layer));
        const count = staged?.querySelector?.("[data-layer-count]");
        if (count) footer.appendChild(count);
        return footer;
    }

    /** Whether staged markup brings an opacity control of its own. */
    function hasStagedOpacity(staged) {
        return Boolean(staged?.querySelector?.("[data-layer-opacity]"));
    }

    /**
     * A plugin's panel, as the body of its layer's card.
     *
     * NO CORE OPACITY SLIDER. The plugin's own controls already write the
     * stack (`ctx.layers.setOpacity`) and read it back, so a second slider
     * here would be two controls for one number -- and the plugin's knows
     * what its number means, which core's cannot.
     */
    function buildPluginBody(layer) {
        const staged = stagedFor(layer);
        if (!staged) return buildBody(layer);
        adopted.add(layer.id);
        return staged;
    }

    /**
     * Which channel of a registered image layer is drawn, and in what colour.
     *
     * WHAT IS LEFT of this row. It used to be every registered layer's only
     * channel control -- one `<select>`, one swatch, one channel at a time,
     * and a refetch of the viewport per change. A layer whose pixels are
     * channel planes now gets the reference image's own channel panel instead
     * (views/layerChannelPanel.js), which is several channels at once with a
     * colour and a contrast window each, all of them free repaints.
     *
     * So this is for the one image layer that has no channel to pick and
     * nothing for that panel to control: an rgb slide, whose tiles arrive
     * already coloured. It also stands in on a build with no panel module, so
     * such a build degrades rather than going blank.
     */
    function buildChannelRow(layer) {
        const spec = layer.spec || {};
        const channels = spec.channels || [];
        if (layer.kind !== "image") return null;
        if (layer.id === baseId()) return null;
        if (!channels.length) return null;
        // Superseded for any layer that gets the reference image's own
        // channel controls, which is every layer whose pixels are channel
        // planes. This row is what is left for the one case that has no
        // channel to pick: an rgb layer, whose bytes are already the picture.
        if (hasChannelPanel(layer)) return null;

        const row = document.createElement("div");
        row.className = "layer-card-row";

        if (channels.length > 1) {
            const label = document.createElement("label");
            label.textContent = "Channel";
            label.setAttribute("for", `layer_channel_${layer.id}`);
            const select = document.createElement("select");
            select.id = `layer_channel_${layer.id}`;
            select.className = "layer-card-channel";
            channels.forEach((channel, index) => {
                const option = document.createElement("option");
                option.value = String(index);
                option.textContent = channel.fullname || channel.name;
                if (index === (spec.render?.channelIndex ?? 0)) option.selected = true;
                select.appendChild(option);
            });
            select.addEventListener("change", () => {
                restyle(layer, { channelIndex: Number(select.value) });
            });
            row.appendChild(label);
            row.appendChild(select);
        }

        const swatch = document.createElement("span");
        swatch.className = "layer-card-swatch";
        row.appendChild(swatch);
        // The same picker the channel list uses, so a colour chosen here and
        // one chosen there come from one palette.
        new ColorSwatchPicker(swatch, {
            value: spec.render?.color || "#94a3b8",
            title: "Layer colour",
            onChange: (hex) => restyle(layer, { color: hex }),
        });

        return row;
    }

    /**
     * Redraw one registered layer with a changed style.
     *
     * Through the tiled handle rather than by rebuilding the stack: a colour
     * change is a new tile url, and `setStyle` removes and re-adds exactly
     * that one world item. Writing `render` back onto the spec first is what
     * makes the change survive the next `syncLayerImages`.
     */
    function restyle(layer, change) {
        const spec = layer.spec || {};
        spec.render = { ...(spec.render || {}), ...change };
        // Before the handle is looked up, and whether or not there is one: a
        // layer still being built has a card and a colour and no world item,
        // and the choice has to survive anyway.
        persist(layer.id, { render: change });
        const manager = window.__plexora?.seaDragonViewer?.viewerManagerVMain;
        const handle = manager?.tiledLayers?.get(layer.id);
        if (!handle) return;
        if (change.channelIndex !== undefined) {
            // A different channel is a different ADDRESS, which `setStyle`
            // cannot express -- it only rewrites the query. Dropped and re-added
            // through the one primitive that owns the world item.
            handle.remove();
            manager.tiledLayers.delete(layer.id);
            manager.syncLayerImages(window.__plexora.seaDragonViewer.config.layers);
            return;
        }
        handle.setStyle(styleQueryFor(spec.render));
    }

    /** The colour and window as a tile-url query. Mirrors viewerManager's. */
    function styleQueryFor(render) {
        const colour = String((render || {}).color || "").replace(/^#/, "");
        if (!/^[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$/.test(colour)) return "";
        const parts = [`color=${colour}`];
        const [lo, hi] = (render || {}).range || [];
        if (Number.isFinite(lo)) parts.push(`lo=${Math.round(lo)}`);
        if (Number.isFinite(hi)) parts.push(`hi=${Math.round(hi)}`);
        return parts.join("&");
    }

    /**
     * What this layer is still doing, when it is doing anything.
     *
     * Null for the ordinary case -- a layer that is ready and has nothing
     * outstanding says nothing, which is what keeps the card the short thing
     * it is. The two states that DO speak are the ones a user would otherwise
     * read as an empty layer: one still being built, and one whose build
     * failed. A failure carries a Retry, because it usually is one -- a
     * dependency installed since, a file that was on a disconnected drive.
     */
    function buildState(layer) {
        const spec = layer.spec || {};
        const status = spec.status || "ready";
        const unresolved = spec.unresolved || [];
        if (status === "ready" && !unresolved.length) return null;

        const line = document.createElement("p");
        line.className = "layer-card-state";
        if (status === "pending") {
            line.classList.add("is-pending");
            line.textContent = "Preparing…";
        } else if (status === "failed") {
            line.classList.add("is-bad");
            line.textContent = "Could not be prepared";
            const retry = document.createElement("button");
            retry.type = "button";
            retry.className = "layer-card-retry";
            retry.textContent = "Retry";
            retry.addEventListener("click", () => retryBuild(layer, line));
            line.appendChild(retry);
        }
        if (unresolved.length) {
            const needs = document.createElement("span");
            needs.className = "layer-card-needs";
            // Named rather than counted: "Needs: which table" is actionable and
            // "1 unanswered question" is not.
            needs.textContent = `Needs: ${unresolved.join(", ")}`;
            line.appendChild(needs);
        }
        return line;
    }

    async function retryBuild(layer, line) {
        const sample = window.flaskVariables?.datasource;
        if (!sample) return;
        line.textContent = "Retrying…";
        try {
            await fetch(plexoraUrl("import/layers"), {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                // The layer is already registered, so re-posting its own source
                // is the retry: registration replaces by id and starts the
                // build again. Nothing is duplicated -- `with_layer` keeps a
                // layer's position when it already exists.
                body: JSON.stringify({sample, paths: [layer.spec?.src]}),
            });
            window.__plexora?.watchLayers?.();
        } catch (error) {
            line.textContent = "Could not be prepared";
        }
    }

    /** The ids `all_layers` synthesizes, which store nothing of their own. */
    function isReserved(id) {
        return [
            PlexoraLayerStack.REFERENCE_LAYER_ID,
            PlexoraLayerStack.MASK_LAYER_ID,
            PlexoraLayerStack.CENTROID_LAYER_ID,
        ].includes(id);
    }

    /**
     * The ids the server will not hear a word about.
     *
     * Narrower than `isReserved`, and the difference is the reference image.
     * It is still synthesized -- it has no entry in `spatialLayers` and never
     * will -- but two things about it are now the user's rather than the
     * file's, and both are kept beside the project: where in the stack it is
     * drawn, and the ground it is drawn on. The mask and the centroids have
     * neither; where those composite is a fact about the viewer, and an
     * opacity for the mask would be a second answer to what the Cells footer
     * already says.
     */
    function isUnstorable(id) {
        return [
            PlexoraLayerStack.MASK_LAYER_ID,
            PlexoraLayerStack.CENTROID_LAYER_ID,
        ].includes(id);
    }

    /**
     * Remember how a layer is drawn, on the server.
     *
     * Every one of these controls used to write memory and nothing else, so a
     * colour, a window, an eye and a place in the stack all survived exactly
     * until the page reloaded. A control that works and then quietly forgets
     * is worse than one that refuses.
     *
     * Coalesced per layer and delayed, because a slider is DRAGGED: without
     * this a single gesture is sixty requests, each one a file rewrite. The
     * picture has already changed by then -- this is only the remembering.
     *
     * The synthesized layers are skipped: they have no stored record to write
     * to, which is why the route refuses them too. So the base image's opacity
     * is deliberately a session choice, and the one thing here that does not
     * survive a reload.
     */
    function persist(id, patch) {
        const sample = window.flaskVariables?.datasource;
        if (!sample || !id || isUnstorable(id)) return;
        // The reference image stores how it is DRAWN and nothing else. Its eye
        // is about this tab: it is the one layer whose absence leaves the
        // viewer with no world at all, and a project that remembered the image
        // switched off would open on nothing.
        if (id === PlexoraLayerStack.REFERENCE_LAYER_ID && !patch.render) return;
        const queued = pending.get(id) || { render: {} };
        if (patch.visible !== undefined) queued.visible = patch.visible;
        if (patch.render) Object.assign(queued.render, patch.render);
        clearTimeout(queued.timer);
        queued.timer = setTimeout(() => {
            pending.delete(id);
            const body = {};
            if (queued.visible !== undefined) body.visible = queued.visible;
            if (Object.keys(queued.render).length) body.render = queued.render;
            fetch(
                plexoraUrl(`project/${encodeURIComponent(sample)}/layers/`
                           + encodeURIComponent(id)),
                {
                    method: "PATCH",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(body),
                },
            ).catch((error) => {
                console.error("layerManager: saving the layer failed", error);
            });
        }, PERSIST_DELAY);
        pending.set(id, queued);
    }

    /**
     * Remember the stack order.
     *
     * Only the registered layers: where the image, the mask and the centroids
     * composite is `all_layers`' answer every time it is read, and the route
     * refuses to be told otherwise.
     */
    function persistOrder() {
        const sample = window.flaskVariables?.datasource;
        if (!sample || !stack) return;
        // The reference image is named too, at the position it is drawn in.
        // `with_layer_order` keeps a depth for it rather than a place in a
        // list it is not in -- see the route.
        const ids = stack.order().filter((id) => !isUnstorable(id));
        fetch(
            plexoraUrl(`project/${encodeURIComponent(sample)}/layers/order`),
            {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ ids }),
            },
        ).catch((error) => {
            console.error("layerManager: saving the layer order failed", error);
        });
    }

    function isRemovable(layer) {
        // The synthesized layers are the image, the mask and the table's
        // coordinates. The way to remove one is to remove what it is made of,
        // and an X here that refused would be worse than no X at all. Only the
        // first still has a card; the other two are named anyway, because what
        // is reserved is a fact about the ids and not about this panel.
        return ![
            PlexoraLayerStack.REFERENCE_LAYER_ID,
            PlexoraLayerStack.MASK_LAYER_ID,
            PlexoraLayerStack.CENTROID_LAYER_ID,
        ].includes(layer.id);
    }

    function bodyFor(layer) {
        const style = cardStyle(layer);
        if (style === "base") return buildBaseBody(layer);
        if (style === "plugin") return buildPluginBody(layer);
        return buildBody(layer);
    }

    /**
     * The ground a layer's channels composite onto.
     *
     * OFFERED WHEREVER IT CAN BE SEEN, which is any image whose channels carry
     * their coverage in their alpha: they stopped being drawn on opaque black
     * tiles, so the thing that shows through where a channel has no signal is
     * this. Black is what every composite has always been read against; white
     * is what a figure and a printer want, and what makes a fluorescence
     * composite legible beside an H&E.
     *
     * The two are drawn by different mechanisms -- the reference's ground is
     * the canvas, a registered layer's is a world item its own shape -- and
     * `setGround` is where that parts. The dot is the same control either way.
     */
    function hasGroundDot(layer) {
        if (layer.kind !== "image") return false;
        if (layer.id === baseId()) return true;
        // An rgb layer is the picture the scanner recorded -- opaque, edge to
        // edge. There is nothing for a ground to show through.
        return !((layer.spec?.render || {}).rgb)
            && (layer.spec?.channels || []).some((c) => c?.name && c?.src);
    }

    /**
     * How many swatches a row of the ground popover holds.
     *
     * Handed to the popover as `--ground-columns` rather than written down
     * twice, because the palette below has to come out a whole number of
     * ROWS: a last row with three colours and a hole in it reads as a swatch
     * that failed to load. The grid it feeds is
     * `.color-swatch-popover.is-ground .color-swatch-grid` in viewer.css.
     */
    const GROUND_COLUMNS = 4;

    /** No ground at all: a registered layer is transparent where its channels
     *  have no signal, which is its default and is not a colour. */
    const NO_GROUND = { label: "None", hex: "transparent" };

    /** The reference image cannot have none -- it is the scene's frame, and
     *  the canvas behind it is always SOME colour -- so the same slot hands
     *  back the colour viewer.css picks when nothing is stored, which is black
     *  for a composite and an off-white for a slide. Without it a ground set
     *  once could never be unset. */
    const DEFAULT_GROUND = { label: "Default", hex: "transparent" };

    /**
     * What a ground can be set to: the slash and the two ends, then every
     * colour a channel swatch already offers.
     *
     * TAKEN FROM THE CHANNEL PALETTE, not copied out of it, so the two cannot
     * drift apart and no colour can appear twice -- white is on both lists,
     * and the filter is what keeps it off this one twice. The bespoke greys
     * this used to carry (slide white, charcoal, paper) are gone: they were
     * near-repeats of the two ends, they left the grid three short of a full
     * row, and the custom input under it reaches any of them exactly.
     */
    function groundPresets(base) {
        const head = [
            base ? DEFAULT_GROUND : NO_GROUND,
            { label: "Black", hex: "#000000" },
            { label: "White", hex: "#ffffff" },
        ];
        //: Not `window.ColorSwatchPicker`: a `class` at the top level of a
        //: classic script is a global BINDING and never a property of window.
        const shared = (ColorSwatchPicker.DEFAULT_PRESETS || []).filter(
            (preset) => !head.some((held) =>
                held.hex.toLowerCase() === String(preset.hex).toLowerCase()));
        return head.concat(shared);
    }

    /**
     * The colour this layer's ground is ALREADY, which is what the dot shows.
     *
     * Read back off the element rather than guessed, because the default is
     * two different colours and viewer.css is the one that knows which: an
     * unset `--plexora-viewer-ground` falls back to black for a composite and
     * to an off-white for a slide, and a dot that guessed would be a second
     * answer that disagrees with the picture on the first brightfield project.
     */
    function groundValue(layer) {
        const stored = (layer.spec?.render || {}).background;
        if (/^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$/.test(String(stored || ""))) {
            return String(stored);
        }
        // A registered layer with no background has none -- it is transparent
        // where its channels have no signal, and that is the default.
        if (layer.id !== baseId()) return NO_GROUND.hex;
        const element = document.getElementById("openseadragon");
        const drawn = element
            && getComputedStyle(element).backgroundColor;
        return toHex(drawn) || "#000000";
    }

    /** `rgb(r, g, b)` as `#rrggbb`, or "" for anything else. */
    function toHex(value) {
        const parts = String(value || "").match(/\d+/g);
        if (!parts || parts.length < 3) return "";
        return `#${parts.slice(0, 3).map((part) => Number(part)
            .toString(16).padStart(2, "0")).join("")}`;
    }

    /**
     * The dot, built once per card and kept, for the reason every other
     * adopted node here is: it owns a ColorSwatchPicker with a popover parked
     * in the portal, and `refreshBody` would otherwise leave one behind on
     * every repaint.
     */
    function groundDotFor(layer) {
        const held = grounds.get(layer.id);
        if (held) return held.mount;
        const mount = document.createElement("span");
        mount.className = "layer-card-ground";
        const base = layer.id === baseId();
        const picker = new ColorSwatchPicker(mount, {
            value: groundValue(layer),
            title: base ? "The ground this image is drawn on"
                        : "The ground this layer is drawn on",
            presets: groundPresets(base),
            //: The slash is `transparent` on both palettes and means "store
            //: nothing" on both -- which is no ground for a layer and the
            //: viewer's own default for the reference. `null` is what the
            //: route reads as "take the key away".
            onChange: (hex) => setGround(layer.id,
                hex === NO_GROUND.hex ? null : hex),
        });
        // BLACK AND WHITE NEED A RING to be findable at all. The channel
        // palette this widget was built for is saturated hues on a dark
        // popover, where a 2px transparent border is fine; the two ends of a
        // ground's range are each a hole in that grid instead, and a hole
        // reads as a swatch that failed to load rather than as the colour it
        // is offering. See `.is-ground` in viewer.css. Set on the popover
        // rather than passed as an option because the popover is parked in
        // the portal, not inside `mount`, so no selector from here can reach
        // it.
        picker.popover?.classList.add("is-ground");
        picker.popover?.style.setProperty("--ground-columns",
            String(GROUND_COLUMNS));
        grounds.set(layer.id, { mount, picker });
        return mount;
    }

    function setGround(id, hex) {
        const layer = stack?.get(id);
        if (layer) {
            const spec = layer.spec || (layer.spec = {});
            spec.render = { ...(spec.render || {}), background: hex };
        }
        // `null` is a real value here and the route knows it: it REMOVES the
        // key, which is how "use the default" is said everywhere else in this
        // file's `render` patches.
        persist(id, { render: { background: hex } });
        const manager = window.__plexora?.seaDragonViewer?.viewerManagerVMain;
        // The reference image's ground is the canvas, because the reference is
        // the scene's frame; a registered layer's is a world item its own
        // shape. Two mechanisms because they are two different things, not one
        // thing with a special case -- see ViewerManager.addLayerGround.
        if (id !== baseId()) {
            manager?.tiledLayers?.get(id)?.setGround?.(hex);
            return;
        }
        manager?.applyViewerGround?.(hex);
        // The slash stores nothing, and what the canvas draws then is
        // viewer.css's own fallback -- which is still a colour. Read it back
        // off the element, or the dot would sit there showing the absence of
        // a choice over a ground that is plainly black or plainly white, and
        // would correct itself on the next reload.
        if (!hex && layer) grounds.get(id)?.picker?.setValue(groundValue(layer));
    }

    /** The header controls this layer brings with it, or null. */
    function extrasFor(layer) {
        const api = sections();
        if (!api) return null;
        if (layer.id === baseId()) return api.extrasFor(layer.id);
        // A plugin stages its own inside its mount, so it is found relative to
        // the body rather than by layer id -- and taken out BEFORE the mount
        // is adopted as the body, or it would ride along into it.
        return api.extrasIn(stagedFor(layer));
    }

    function buildCard(layer) {
        const base = layer.id === baseId();
        // Extras first: a plugin's live inside the markup that is about to
        // become the body, and appending them to the header moves them out.
        const extras = [].concat(extrasFor(layer) || []);
        if (hasGroundDot(layer)) extras.unshift(groundDotFor(layer));
        return PlexoraCardList.buildCard({
            prefix: "layer-card",
            attr: CARD_ATTR,
            key: layer.id,
            label: labelFor(layer),
            body: bodyFor(layer),
            //: Null rather than an empty list: `buildCard` builds the slot for
            //: anything truthy, and `[]` is truthy.
            extras: extras.length ? extras : null,
            attrs: { "data-layer-kind": layer.kind, "data-layer-surface": surfaceOf(layer) },
            titles: {
                grip: "Drag to restack",
                collapse: "Collapse or expand this layer's controls",
                eye: "Show or hide this layer",
                lock: "Pin this layer in place, so a drag cannot move it",
                remove: "Remove this layer",
            },
            onCollapse: () => {
                if (collapsed.has(layer.id)) openOnly(layer.id);
                else collapsed.add(layer.id);
                paint();
            },
            onToggle: () => {
                const next = !stack.get(layer.id)?.visible;
                stack?.setVisible(layer.id, next);
                persist(layer.id, { visible: next });
                paint();
            },
            // The base image has no X -- the way to remove it is to remove the
            // sample -- but its padlock is a real one now. It used to be drawn
            // stuck shut, because the row could not be dragged and the padlock
            // was the only thing on it that accounted for that. The row can be
            // dragged.
            onLock: () => {
                if (locked.has(layer.id)) locked.delete(layer.id);
                else locked.add(layer.id);
                paint();
            },
            lockFixed: false,
            onRemove: isRemovable(layer) ? () => removeLayer(layer) : null,
        });
    }

    /**
     * Take a layer out of the scene, for good.
     *
     * It used to unregister the layer from the stack and nothing else, so the
     * card went away and the layer came back on the next reload -- an X that
     * looked like it worked, which is worse than one that refuses. The server
     * is told first and the client follows, in this order:
     *
     *   DELETE, then drop the world item, then the stack record, then re-read
     *   `/config`. The re-read matters: `syncLayers` never unregisters an id
     *   the config no longer lists, and `watchLayers` would otherwise put the
     *   layer straight back on its next poll.
     *
     * Asked about first, because the file this layer was read from stays
     * exactly where it is and the dialog is the only place that says so.
     */
    async function removeLayer(layer) {
        const sample = window.flaskVariables?.datasource;
        const confirm = window.PlexoraConfirm;
        if (sample && confirm) {
            const sure = await confirm.ask({
                title: `Remove ${labelFor(layer)}?`,
                body: "The layer stops being drawn and leaves this sample's "
                    + "stack. The file it was read from is left exactly where "
                    + "it is.",
                confirm: "Remove",
                danger: true,
            });
            if (!sure) return;
            try {
                await fetch(
                    plexoraUrl(`project/${encodeURIComponent(sample)}/layers/`
                               + encodeURIComponent(layer.id)),
                    { method: "DELETE" });
            } catch (error) {
                console.error("layerManager: removing the layer failed", error);
                return;
            }
        }
        const manager = window.__plexora?.seaDragonViewer?.viewerManagerVMain;
        manager?.tiledLayers?.get(layer.id)?.remove();
        manager?.tiledLayers?.delete(layer.id);
        stack?.unregister(layer.id);
        cards.delete(layer.id);
        adopted.delete(layer.id);
        dropChannelPanel(layer.id);
        render();
        await window.__plexora?.adoptLayers?.();
    }

    function surfaceOf(layer) {
        return PlexoraLayerStack.LayerStack.surfaceOf(layer.kind);
    }

    /** The layers that should have a card, top of the picture first. */
    function cardedLayers() {
        if (!stack) return [];
        // The order the stack says, and nothing else. The base image's card
        // used to be sorted to the foot whatever the order said, on the
        // reasoning that a list whose bottom row is not the bottom of the
        // picture makes the one thing this panel exists to show a lie. That
        // was true while the image could only ever BE the bottom; now that it
        // can be dragged, pinning the row is what would make the list lie.
        return stack.layers().slice().reverse().filter(isCarded);
    }

    function cardedIds() { return cardedLayers().map((layer) => layer.id); }

    /** The layers that do have one, in the order they are drawn in. */
    function renderedIds(list) {
        return Array.from(list.children)
            .map((card) => card.getAttribute?.(CARD_ATTR))
            .filter(Boolean);
    }

    /**
     * This layer's card, built at most once.
     *
     * A raster card owns nothing it did not build, so its body is refreshed in
     * place when the layer changes -- a transform corrected, a build that
     * finished. A base or plugin card has ADOPTED markup somebody else holds
     * handles into, and refreshing that would be the wipe this panel stopped
     * doing.
     */
    function ensureCard(layer) {
        const existing = cards.get(layer.id);
        if (existing) {
            if (cardStyle(layer) === "raster") refreshBody(existing, layer);
            return existing;
        }
        const card = buildCard(layer);
        cards.set(layer.id, card);
        return card;
    }

    function refreshBody(card, layer) {
        const wrapper = Array.from(card.children)
            .find((child) => child.className?.includes?.("layer-card-body"));
        if (!wrapper) return;
        while (wrapper.firstChild) wrapper.removeChild(wrapper.firstChild);
        wrapper.appendChild(buildBody(layer));
    }

    /**
     * Put the list in the order the stack says, without rebuilding it.
     *
     * `appendChild` MOVES a node the list already holds, so appending every
     * card in order is the reorder. Nothing is cleared: see RECONCILED, NOT
     * REBUILT at the top of this file.
     */
    function render() {
        const list = el(LIST_ID);
        if (!list || !stack) return;
        const layers = cardedLayers();
        const present = SURFACE_ORDER.filter(
            (surface) => layers.some((layer) => surfaceOf(layer) === surface));

        // A card that has just appeared -- a mask that finished building, a
        // layer somebody imported -- has never been in `collapsed`, so it
        // would arrive EXPANDED and break the one-open rule with something
        // nobody clicked. It arrives folded instead, unless there is nothing
        // open yet for it to crowd.
        let open = layers.some((layer) => cards.has(layer.id) && !collapsed.has(layer.id));
        for (const layer of layers) {
            if (cards.has(layer.id) || collapsed.has(layer.id)) continue;
            if (open) collapsed.add(layer.id);
            else open = true;
        }

        // GROUPED BY SURFACE, and nothing drawn to say so. There was a
        // labelled rule between the groups -- "Points and shapes draw over
        // images" -- explaining why a drag across it is refused. It explained
        // a refusal most users never meet, in a sentence, in a panel whose
        // whole problem is that everything in it is competing for a 300px
        // column. The refusal still stands (see ensureSortable's onMove); it
        // is just no longer announced in advance. The grouping itself is what
        // is left of the boundary, and it is what the order means.
        const wanted = [];
        for (const surface of present) {
            for (const layer of layers) {
                if (surfaceOf(layer) === surface) wanted.push(ensureCard(layer));
            }
        }

        const keep = new Set(wanted);
        for (const child of Array.from(list.children)) {
            if (keep.has(child)) continue;
            list.removeChild(child);
            const id = child.getAttribute?.(CARD_ATTR);
            // A card whose layer has gone stops being cached, so a layer
            // re-registered later is built afresh. One merely reordered out of
            // the list cannot happen -- it would still be wanted.
            //
            // UNLESS IT HAS ADOPTED MARKUP. That card holds a plugin's whole
            // panel, and the staged copy it came from is no longer in the slot
            // to be found again -- dropping the card would lose the panel for
            // good. Kept detached instead, so a layer that comes back (a
            // re-adopt after an import, a build that finished) comes back with
            // its controls. `removeLayer` is the one path that really forgets
            // one, because there the user said so.
            if (id && !stack.has(id) && !adopted.has(id)) {
                cards.delete(id);
                // With the card goes its channel panel, which holds a window
                // listener for the HD toggle. A panel with no card on the page
                // would go on remapping slots nobody can see.
                dropChannelPanel(id);
            }
        }
        for (const node of wanted) list.appendChild(node);

        ensureSortable();
        paint();
    }

    /**
     * The classes the cards hang their state off.
     *
     * Classes and not glyphs, for the reason cardList.js spells out: FontAwesome
     * is loaded as JS and rewrites every icon span into an svg before anything
     * can click it, so a JS glyph swap edits a node that is no longer on the page.
     */
    function paint() {
        const list = el(LIST_ID);
        if (!list || !stack) return;
        // MEMBERSHIP NEEDS A RENDER, everything else needs a repaint. A
        // layer adopted mid-session, or a points layer a plugin has just
        // claimed, is a card that does not exist yet -- and repainting a
        // list it is not in would leave the panel silently one card short
        // until something else happened to rebuild it.
        if (cardedIds().join(",") !== renderedIds(list).join(",")) {
            render();
            return;
        }
        Array.from(list.children).forEach((card) => {
            const id = card.getAttribute?.(CARD_ATTR);
            if (!id) return;
            const layer = stack.get(id);
            if (!layer) return;
            card.classList.toggle("is-collapsed", collapsed.has(id));
            card.classList.toggle("is-layer-off", !layer.visible);
            // A number on a button cannot be read off the DOM the way a
            // handle's position can, so the one card that shows its opacity
            // as a word is told what the word is here -- otherwise a fade from
            // viewerControls left the button still saying 100%.
            opacityReadouts.get(id)?.(layer.opacity);
            card.classList.toggle("is-locked", locked.has(id));
            card.classList.toggle("is-unregistered",
                id !== baseId() && !layer.transform);
            card.classList.toggle("is-undrawable", Boolean(layer.transformUnsupported));
        });
    }

    /**
     * ONE CARD OPEN AT A TIME, ACROSS BOTH OF THE SIDEBAR'S LISTS.
     *
     * The panel holds a card per layer and a card per loaded tool, each with a
     * body of controls; expanded together on a 300px column they are a single
     * scroll several screens long, and the thing the user came for is
     * somewhere in the middle of it. Opening one therefore folds the rest.
     *
     * The tool cards are somebody else's list -- toolLoader's -- and it does
     * the mirror of this when one of ITS cards opens. Reached through the
     * global rather than a registered callback because that is how the two
     * modules already talk (see toolLoader's collapseForNewTool, which folds
     * this panel's base image card), and guarded because a page can render
     * this list with no plugins loaded at all.
     */
    function openOnly(id) {
        cardedIds().forEach((other) => collapsed.add(other));
        collapsed.delete(id);
        try {
            window.PlexoraToolLoader?.collapseAllCards?.();
        } catch (error) {
            console.error("layerManager: folding the tool cards failed", error);
        }
    }

    /** Fold every layer card. toolLoader's half of the rule above: a tool card
     *  that has just opened calls this. Silent about tool cards, or the two
     *  modules would fold each other back and forth. */
    function collapseAll() {
        cardedIds().forEach((id) => collapsed.add(id));
        paint();
    }

    /** Fold or unfold one card from outside. The sidebar's own collapse and
     *  toolLoader's "make room for a tool" both come through here now that the
     *  base image is a card rather than a section. Unfolding means unfolding
     *  ONLY this one -- see openOnly. */
    function setCollapsed(id, on) {
        if (on) collapsed.add(id);
        else openOnly(id);
        paint();
    }

    /**
     * Push the card order back onto the stack.
     *
     * The cards are grouped by surface and separated by a rule, so the DOM
     * holds a row that is not a layer. orderFromSlot skips it by keeping only
     * keys the stack knows.
     *
     * EVERY id is named, not just the ones with cards. `setOrder` files what
     * it is not told about at the BOTTOM, so sending only the cards would push
     * the centroids -- and anything else this panel does not show -- beneath
     * the base image and have the model claim they are drawn there. An
     * uncarded layer keeps its surface's place instead: rasters among the
     * cards, overlays above them, which is where they are actually drawn.
     * (The mask is named here too and then lifted past everything by its pin;
     * see LayerStack.setOrder.)
     */
    function syncOrder() {
        const list = el(LIST_ID);
        if (!list || !stack) return;
        const shown = PlexoraCardList.orderFromSlot(
            list, CARD_ATTR, (id) => stack.has(id));
        const carded = new Set(shown);
        const others = stack.layers().map((layer) => layer.id)
            .filter((id) => !carded.has(id));
        const overlay = (id) => stack.surfaceOf(id) !== "tiles";
        stack.setOrder([
            // The base image is no longer hoisted to the front of this list.
            // It is a carded layer like any other and arrives in `shown` at
            // the position its row is in -- which is the whole of what "the
            // reference image can be reordered" means to the model.
            ...others.filter((id) => !overlay(id)),
            ...shown,
            ...others.filter(overlay),
        ]);
        stack.applyWorldOrder();
        persistOrder();
    }

    function ensureSortable() {
        const list = el(LIST_ID);
        if (!list || sortable) return;
        sortable = PlexoraCardList.ensureSortable(list, {
            handle: ".layer-card-grip",
            draggable: ".layer-card",
            onSort: syncOrder,
            /**
             * Refuse a drag that cannot be honoured, at the moment it is made.
             *
             * Two reasons, and each is a real constraint rather than policy:
             * a locked layer is one the user pinned, and a cross-surface move
             * is one the compositor cannot express.
             *
             * There used to be a third -- nothing goes under the base image,
             * because there is nothing under the base image. There can be now,
             * and the image composites as a group over whatever it is, so the
             * floor this defended is gone.
             */
            onMove: (event) => {
                const moving = event.dragged?.getAttribute?.(CARD_ATTR);
                if (moving && locked.has(moving)) return false;
                const target = event.related?.getAttribute?.(CARD_ATTR);
                // Onto something in the list that is not a card at all. There
                // is nothing like that in there today -- the surface divider
                // that used to be was the one case -- but a drop whose target
                // cannot be named is a reorder that cannot be honoured, and
                // refusing is cheaper than working out what it meant.
                if (!target) return false;
                return event.dragged?.getAttribute?.("data-layer-surface")
                    === event.related?.getAttribute?.("data-layer-surface");
            },
        });
    }

    function bindCollapse() {
        const section = el(SECTION_ID);
        const toggle = el(COLLAPSE_ID);
        if (!section || !toggle) return;
        toggle.addEventListener("click", () => {
            const next = !section.classList.contains("is-collapsed");
            section.classList.toggle("is-collapsed", next);
            toggle.setAttribute("aria-expanded", String(!next));
        });
    }

    /**
     * @param layerStack - the viewer's LayerStack. Everything the panel shows is
     *   read off it on every render, so the panel holds no copy to go stale.
     */
    function init(layerStack) {
        stack = layerStack || null;
        bindCollapse();
        bindAdd();
        // One card open from the first paint rather than only after the first
        // click, and the base image is the one left open: it is the card every
        // project has and the one the sidebar is usually opened for. Written
        // straight into the set rather than through `openOnly`, which would
        // reach across to toolLoader mid-boot -- before it has decided which
        // tool the URL asked for.
        cardedIds().forEach((id) => collapsed.add(id));
        collapsed.delete(baseId());
        // Repaint rather than render: a visibility or opacity change from
        // anywhere else -- viewerControls, a plugin -- has to show here, and
        // rebuilding on every opacity tick would drop the slider the user is
        // dragging out from under the pointer.
        unsubscribe = stack?.subscribe(() => paint()) || null;
        render();
        return api;
    }

    const api = { init, render, paint, syncOrder, setCollapsed, collapseAll };
    if (typeof window !== "undefined") window.PlexoraLayerManager = api;
    if (typeof globalThis !== "undefined") globalThis.PlexoraLayerManager = api;
    return api;
}());
