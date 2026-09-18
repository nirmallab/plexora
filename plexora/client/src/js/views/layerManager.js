/**
 * The Layers panel: one card per layer, in the order they are drawn.
 *
 * What a card carries is what is true of ANY layer -- whether it is drawn, how
 * strongly, where it sits in the stack, whether it can be moved, and whether
 * anybody actually registered it. Nothing here interprets data. No clusters, no
 * phenotypes, no expression, no metadata: a plugin computes those and asks core
 * to draw them, and this panel is one of the places that rule has to hold or the
 * viewer quietly becomes an analysis application.
 *
 * Deliberately NOT a second channel UI. The reference image's channels have
 * their own section above this one, built by channelList.js, and duplicating it
 * inside a card would be two controls for one fact.
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
 * ONE ORDER, THREE SURFACES. Tiles composite inside OSD's world; overlays are
 * drawn on a canvas above it. So a points layer cannot be dragged underneath an
 * image, whatever the list says. The drag is refused at the boundary, with a
 * separator row saying where the boundary is -- allowing the drag and ignoring
 * it would be a reorder that silently does nothing.
 *
 * Served as a classic script (see base.html).
 */
window.PlexoraLayerManager = (function () {
    "use strict";

    const LIST_ID = "layer_card_list";
    const SECTION_ID = "layer_manager_section";
    const COLLAPSE_ID = "layer_manager_collapse";
    const CARD_ATTR = "data-layer-card";
    const BREAK_ATTR = "data-surface-break";

    /** Cards read top-down and the top card is the TOP of the picture, so the
     *  surfaces are listed in reverse composite order here. */
    const SURFACE_ORDER = ["gl", "overlay", "tiles"];

    const SURFACE_LABEL = {
        tiles: "Drawn into the image",
        overlay: "Drawn over the image",
        gl: "Drawn over the image",
    };

    /** What each kind is called on its card when the layer has no label. */
    const KIND_LABEL = {
        image: "Image",
        labels: "Segmentation",
        points: "Points",
        shapes: "Shapes",
    };

    let stack = null;
    let sortable = null;
    let unsubscribe = null;
    const collapsed = new Set();
    /** Layers the user has pinned in place. Held here rather than on the stack
     *  record: it is a fact about this panel, and the renderer has no use for it. */
    const locked = new Set();

    function el(id) { return document.getElementById(id); }

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

    function labelFor(layer) {
        return layer.label || KIND_LABEL[layer.kind] || layer.id;
    }

    /**
     * What this layer's registration amounts to, in one line.
     *
     * Four states, and the first is the one that matters: it is what the viewer
     * has always silently assumed.
     */
    function alignmentNote(layer) {
        if (layer.id === PlexoraLayerStack.REFERENCE_LAYER_ID) {
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

    /** The opacity slider and the alignment line. Everything a layer has in
     *  common with every other layer, and nothing it does not. */
    function buildBody(layer) {
        const body = document.createElement("div");
        body.className = "layer-card-controls";

        const row = document.createElement("div");
        row.className = "layer-card-row";
        const label = document.createElement("label");
        const sliderId = `layer_opacity_${layer.id}`;
        label.setAttribute("for", sliderId);
        label.textContent = "Opacity";
        const slider = document.createElement("input");
        slider.type = "range";
        slider.id = sliderId;
        slider.min = "0";
        slider.max = "1";
        slider.step = "0.01";
        slider.value = String(layer.opacity);
        const output = document.createElement("output");
        output.setAttribute("for", sliderId);
        output.textContent = Number(layer.opacity).toFixed(2);
        slider.addEventListener("input", () => {
            output.textContent = Number(slider.value).toFixed(2);
            stack?.setOpacity(layer.id, Number(slider.value));
        });
        row.appendChild(label);
        row.appendChild(slider);
        row.appendChild(output);
        body.appendChild(row);

        const note = alignmentNote(layer);
        const alignment = document.createElement("p");
        alignment.className = `layer-card-alignment is-${note.tone}`;
        alignment.textContent = note.text;
        body.appendChild(alignment);

        const state = buildState(layer);
        if (state) body.appendChild(state);

        return body;
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
            line.textContent = "Preparing\u2026";
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
        line.textContent = "Retrying\u2026";
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

    function isRemovable(layer) {
        // The synthesized layers are the image, the mask and the table's
        // coordinates. The way to remove one is to remove what it is made of,
        // and an X here that refused would be worse than no X at all.
        return ![
            PlexoraLayerStack.REFERENCE_LAYER_ID,
            PlexoraLayerStack.MASK_LAYER_ID,
            PlexoraLayerStack.CENTROID_LAYER_ID,
        ].includes(layer.id);
    }

    function buildCard(layer) {
        const card = PlexoraCardList.buildCard({
            prefix: "layer-card",
            attr: CARD_ATTR,
            key: layer.id,
            label: labelFor(layer),
            body: buildBody(layer),
            attrs: { "data-layer-kind": layer.kind, "data-layer-surface": surfaceOf(layer) },
            titles: {
                grip: "Drag to restack",
                collapse: "Collapse or expand this layer's controls",
                eye: "Show or hide this layer",
                lock: "Pin this layer in place, so a drag cannot move it",
                remove: "Remove this layer",
            },
            onCollapse: () => {
                if (collapsed.has(layer.id)) collapsed.delete(layer.id);
                else collapsed.add(layer.id);
                paint();
            },
            onToggle: () => {
                stack?.setVisible(layer.id, !stack.get(layer.id)?.visible);
                paint();
            },
            onLock: () => {
                if (locked.has(layer.id)) locked.delete(layer.id);
                else locked.add(layer.id);
                paint();
            },
            onRemove: isRemovable(layer)
                ? () => { stack?.unregister(layer.id); render(); }
                : null,
        });
        return card;
    }

    function surfaceOf(layer) {
        return PlexoraLayerStack.LayerStack.surfaceOf(layer.kind);
    }

    /** A labelled rule where one surface gives way to the next. */
    function buildBreak(surface) {
        const row = document.createElement("div");
        row.className = "layer-surface-break";
        row.setAttribute(BREAK_ATTR, surface);
        row.textContent = SURFACE_LABEL[surface] || surface;
        return row;
    }

    /**
     * Rebuild the list.
     *
     * Rebuilt rather than patched, because a layer card owns no controller and
     * holds no state of its own -- the stack holds all of it, and the two things
     * the panel remembers (folded, locked) are keyed by id up here. That is what
     * makes this safe to call on every change, which a card list that cost
     * something to rebuild would not be.
     */
    function render() {
        const list = el(LIST_ID);
        if (!list || !stack) return;
        list.innerHTML = "";
        // Top-down, so the reverse of the stack's bottom-first order.
        const layers = stack.layers().slice().reverse();
        let lastSurface = null;
        for (const surface of SURFACE_ORDER) {
            const inSurface = layers.filter((layer) => surfaceOf(layer) === surface);
            if (!inSurface.length) continue;
            if (SURFACE_LABEL[surface] !== lastSurface) {
                list.appendChild(buildBreak(surface));
                lastSurface = SURFACE_LABEL[surface];
            }
            for (const layer of inSurface) list.appendChild(buildCard(layer));
        }
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
        Array.from(list.children).forEach((card) => {
            const id = card.getAttribute?.(CARD_ATTR);
            if (!id) return;
            const layer = stack.get(id);
            if (!layer) return;
            card.classList.toggle("is-collapsed", collapsed.has(id));
            card.classList.toggle("is-layer-off", !layer.visible);
            card.classList.toggle("is-locked", locked.has(id));
            card.classList.toggle("is-unregistered",
                id !== PlexoraLayerStack.REFERENCE_LAYER_ID && !layer.transform);
            card.classList.toggle("is-undrawable", Boolean(layer.transformUnsupported));
        });
    }

    /**
     * Push the card order back onto the stack.
     *
     * The cards are grouped by surface and separated by rules, so the DOM holds
     * rows that are not layers. orderFromSlot skips them by keeping only keys the
     * stack knows.
     */
    function syncOrder() {
        const list = el(LIST_ID);
        if (!list || !stack) return;
        const ids = PlexoraCardList.orderFromSlot(list, CARD_ATTR, (id) => stack.has(id));
        stack.setOrder(ids);
        stack.applyWorldOrder();
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
             * Two reasons a move is refused, and both are real constraints
             * rather than policy: a locked layer is one the user pinned, and a
             * cross-surface move is one the compositor cannot express.
             */
            onMove: (event) => {
                const moving = event.dragged?.getAttribute?.(CARD_ATTR);
                if (moving && locked.has(moving)) return false;
                const target = event.related?.getAttribute?.(CARD_ATTR);
                // Dropping onto a separator, or onto a card on another surface.
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
        // Repaint rather than rebuild: a visibility or opacity change from
        // anywhere else -- viewerControls, a plugin -- has to show here, and
        // rebuilding the list on every opacity tick would drop the slider the
        // user is dragging out from under the pointer.
        unsubscribe = stack?.subscribe(() => paint()) || null;
        render();
        return api;
    }

    const api = { init, render, paint, syncOrder };
    if (typeof window !== "undefined") window.PlexoraLayerManager = api;
    if (typeof globalThis !== "undefined") globalThis.PlexoraLayerManager = api;
    return api;
}());
