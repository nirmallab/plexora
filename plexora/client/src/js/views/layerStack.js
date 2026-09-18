/**
 * The layer stack: one ordered list of everything the viewer draws.
 *
 * Plexora already had a layer stack, twice. OpenSeadragon's `world` is one --
 * one TiledImage per channel, z-order by `setItemIndex` -- and ImageViewer's
 * cell-layer registry is another, with visibility, opacity, mode, a lookup table
 * and drag-to-reorder. Neither knew about the other, so "which layer is on top"
 * had two answers and a second image had nowhere to go.
 *
 * This is the one list. Four kinds, because a kind is a rendering strategy and
 * nothing else: a channel group IS an image layer with more than one channel,
 * spots and bins ARE points with a render hint, and an annotation IS a shape
 * somebody can edit.
 *
 *   image   tiles    N OSD TiledImages, `lighter` blend
 *   labels  tiles    one TiledImage (tileFormat 32) plus a per-layer canvas stack
 *   points  overlay  canvas2d, or WebGL at transcript scale
 *   shapes  overlay  Path2D
 *
 * ONE ORDER, THREE SURFACES -- and the seam is honest rather than hidden.
 * Surfaces composite in a fixed sequence (`tiles`, inside OSD's world, then
 * `overlay`, then `gl`), so a single global order cannot be honoured while masks
 * live in the world and points are drawn above it. The Layer Manager draws a
 * labelled separator at the boundary and refuses cross-surface drags, which says
 * out loud what would otherwise be a reorder that silently did nothing.
 *
 * What the stack deliberately does NOT hold: anything about what the data means.
 * No clusters, no phenotypes, no expression, no metadata. A plugin computes a
 * table and core draws it -- the rule `setCellColorLUT` has always followed.
 *
 * Served as a classic script (see base.html) and must load BEFORE imageViewer.js.
 */

/** Composite order. Not configurable: it is a fact about how the page is built. */
const LAYER_SURFACES = ["tiles", "overlay", "gl"];

/** kind -> the surface it draws on. The whole of the kind system. */
const LAYER_KIND_SURFACE = {
    image: "tiles",
    labels: "tiles",
    points: "overlay",
    shapes: "overlay",
};

const LAYER_KINDS = Object.keys(LAYER_KIND_SURFACE);

/** The ids project.py synthesizes rather than stores. Kept in step by hand
 *  because the client never sees project.py; `layer_stack_probe.mjs` asserts
 *  the two lists still agree. */
const REFERENCE_LAYER_ID = "__image__";
const MASK_LAYER_ID = "__mask__";
const CENTROID_LAYER_ID = "__centroids__";

const IDENTITY_TRANSFORM = [1, 0, 0, 1, 0, 0];


/**
 * Invert a canvas-order affine, or null where it does not invert.
 *
 * Needed on every hover: a pointer arrives in the reference layer's pixel space
 * and has to be put back into the layer's own to be hit-tested. Computed once at
 * registration rather than per event -- it is six multiplies, but the event is
 * every mouse move.
 */
function invertTransform(t) {
    const [a, b, c, d, e, f] = t || IDENTITY_TRANSFORM;
    const det = a * d - b * c;
    if (!det || !Number.isFinite(det)) return null;
    return [
        d / det,
        -b / det,
        -c / det,
        a / det,
        (c * f - d * e) / det,
        (b * e - a * f) / det,
    ];
}


/**
 * One layer's sub-stack: several styles of the same geometry, drawn in order.
 *
 * This IS the cell-layer registry, which predates the rest of this file and is
 * unchanged in behaviour. It belongs to the labels layer because a plugin's cell
 * layer is a STYLING of the one segmentation mask, not a layer of its own: there
 * is one set of boundaries on screen, and a phenotype map and a gate are two
 * colourings of it that must be able to be looked at together.
 *
 * Order is held separately from membership because it is the z-order the
 * sub-layers composite in (bottom first) and the user sets it by dragging cards;
 * the two change independently.
 */
class SubLayerStack {
    /**
     * @param defaults - { opacity } applied to a newly created record
     * @param makeRecord - builds the record for a new name. Passed in rather
     *   than built here because what a sub-layer holds is the owner's business:
     *   this class owns the ORDER and the membership, nothing else.
     */
    constructor({ defaults = {}, makeRecord } = {}) {
        this._records = new Map();
        this._order = [];
        this._active = null;
        this._defaults = defaults;
        this._makeRecord = makeRecord || ((name) => ({ name }));
    }

    get size() { return this._records.size; }

    has(name) { return this._records.has(name); }

    get(name) { return this._records.get(name) || null; }

    /** Every record, bottom of the stack first. */
    all() {
        return this._order.map((name) => this._records.get(name)).filter(Boolean);
    }

    order() { return [...this._order]; }

    get active() { return this._active; }

    /**
     * Add, or re-adopt what is already there.
     *
     * Re-registering the SAME name keeps everything the record holds -- colours,
     * gate, mode, opacity -- which is what makes switching a tool away and back
     * instant rather than a reload. Only `unregister` throws state away.
     *
     * A new record goes on TOP: a tool the user just opened is the one they are
     * looking at.
     */
    register(name) {
        if (!name) return null;
        let record = this._records.get(name);
        if (!record) {
            record = { ...this._defaults, ...this._makeRecord(name) };
            this._records.set(name, record);
            this._order.push(name);
        }
        return record;
    }

    /** @returns the name that took over as active, or undefined if nothing was
     *  removed. Null means nothing is active any more. */
    unregister(name) {
        if (!this._records.has(name)) return undefined;
        this._records.delete(name);
        this._order = this._order.filter((entry) => entry !== name);
        if (this._active === name) {
            // The topmost survivor takes over rather than leaving nothing
            // active, so removing the tool being looked at does not strand the
            // shared controls while other layers are still on screen.
            this._active = this._order.length ? this._order[this._order.length - 1] : null;
        }
        return this._active;
    }

    /** @returns the displaced name, or null when nothing changed. */
    setActive(name) {
        const next = name && this._records.has(name) ? name : null;
        const previous = this._active;
        if (previous === next) return null;
        this._active = next;
        return previous;
    }

    /**
     * Restack, bottom first.
     *
     * Names that are not registered are ignored, and registered names the caller
     * did not mention keep their relative places underneath -- so a partial order
     * can never drop a sub-layer off the stack.
     *
     * @returns whether the order actually changed
     */
    setOrder(names) {
        const wanted = (names || []).filter((name) => this._records.has(name));
        const mentioned = new Set(wanted);
        const rest = this._order.filter((name) => !mentioned.has(name));
        const next = [...rest, ...wanted];
        const same = next.length === this._order.length
            && next.every((name, index) => name === this._order[index]);
        if (same) return false;
        this._order = next;
        return true;
    }
}


/**
 * The world layers: what the viewer draws, in the order it draws them.
 */
class LayerStack {
    /**
     * @param viewer - the OpenSeadragon viewer, or null in a test harness. Only
     *   ever read through optional chaining: the stack is the model, and it has
     *   to be usable before a viewer exists and after one is torn down.
     * @param onChange - called after any mutation that changes what is drawn.
     *   One callback rather than an event bus: there is exactly one consumer
     *   (ImageViewer), and a bus here would be a second way to schedule a redraw.
     */
    constructor({ viewer = null, onChange = null } = {}) {
        this._viewer = viewer;
        this._onChange = onChange;
        this._layers = new Map();
        this._order = [];
    }

    setViewer(viewer) { this._viewer = viewer; }

    // -- membership -------------------------------------------------------

    /**
     * Add a layer, or update the one already holding that id.
     *
     * Re-registering keeps the record, for the same reason SubLayerStack does:
     * a layer list rebuilt from a fresh /config must not reset the opacity and
     * the order the user just set.
     *
     * @param spec - { kind, label, visible, opacity, transform, source, ... }
     *   Anything not named here lands on the record untouched, so a caller can
     *   carry per-kind facts (a gene vocabulary's manifest url, a mask's
     *   segmentationMode) without this class growing a field for each.
     */
    register(id, spec = {}) {
        if (!id) return null;
        let layer = this._layers.get(id);
        if (!layer) {
            layer = {
                id,
                kind: "image",
                label: id,
                visible: true,
                opacity: 1,
                transform: null,
                inverse: null,
                //: The sub-stack, built on demand. Only the labels layer has one
                //: today (it is the cell-layer registry); nothing stops a points
                //: layer growing one when per-gene styling needs it.
                sub: null,
                //: The OSD world items this layer owns, when it has any. Set by
                //: whoever calls addTiledImage, because that is who knows.
                items: [],
            };
            this._layers.set(id, layer);
            this._order.push(id);
        }
        const { transform, ...rest } = spec;
        Object.assign(layer, rest);
        if (!LAYER_KINDS.includes(layer.kind)) layer.kind = "image";
        if (transform !== undefined) this._assignTransform(layer, transform);
        this._changed();
        return layer;
    }

    unregister(id) {
        if (!this._layers.has(id)) return false;
        this._layers.delete(id);
        this._order = this._order.filter((entry) => entry !== id);
        this._changed();
        return true;
    }

    has(id) { return this._layers.has(id); }

    get(id) { return this._layers.get(id) || null; }

    /** Every layer, bottom of the stack first. */
    layers() {
        return this._order.map((id) => this._layers.get(id)).filter(Boolean);
    }

    order() { return [...this._order]; }

    /** The surface a layer draws on, from its kind. */
    static surfaceOf(kind) { return LAYER_KIND_SURFACE[kind] || "tiles"; }

    surfaceOf(id) { return LayerStack.surfaceOf(this._layers.get(id)?.kind); }

    /**
     * The visible layers on one surface, bottom first.
     *
     * THE RETURNED ORDER IS THE Z-ORDER. Whoever draws from this list draws in
     * sequence, and the last one wins wherever they overlap.
     */
    drawList(surface) {
        return this.layers().filter(
            (layer) => layer.visible && LayerStack.surfaceOf(layer.kind) === surface);
    }

    // -- ordering ---------------------------------------------------------

    /**
     * Restack, bottom first. Same partial-order rule as SubLayerStack: an id the
     * caller did not mention keeps its place underneath rather than falling off.
     *
     * Cross-surface moves are NOT rejected here. The stack holds one order and
     * this is it; what the surfaces can honour is the Layer Manager's problem,
     * and it refuses those drags at the UI where the user can see why.
     */
    setOrder(ids) {
        const wanted = (ids || []).filter((id) => this._layers.has(id));
        const mentioned = new Set(wanted);
        const rest = this._order.filter((id) => !mentioned.has(id));
        const next = [...rest, ...wanted];
        const same = next.length === this._order.length
            && next.every((id, index) => id === this._order[index]);
        if (same) return false;
        this._order = next;
        this._changed();
        return true;
    }

    // -- properties -------------------------------------------------------

    setVisible(id, visible) {
        const layer = this._layers.get(id);
        if (!layer) return false;
        const next = Boolean(visible);
        if (layer.visible === next) return false;
        layer.visible = next;
        this._changed();
        return true;
    }

    setOpacity(id, value) {
        const layer = this._layers.get(id);
        if (!layer) return false;
        const next = Math.max(0, Math.min(1, Number(value)));
        if (!Number.isFinite(next) || next === layer.opacity) return false;
        layer.opacity = next;
        this._changed();
        return true;
    }

    /**
     * Where this layer sits in the reference layer's pixel grid.
     *
     * null means "already there", which is a different claim from the identity
     * matrix: a layer with no transform has never been registered. Both draw the
     * same; only the Layer Manager tells them apart, and it says "aligned by
     * assumption" for the first, which is the thing today's viewer never says.
     */
    setTransform(id, transform) {
        const layer = this._layers.get(id);
        if (!layer) return false;
        this._assignTransform(layer, transform);
        this._changed();
        return true;
    }

    _assignTransform(layer, transform) {
        if (transform == null) {
            layer.transform = null;
            layer.inverse = null;
            return;
        }
        const values = Array.from(transform, Number);
        if (values.length !== 6 || values.some((v) => !Number.isFinite(v))) {
            layer.transform = null;
            layer.inverse = null;
            return;
        }
        layer.transform = values;
        layer.inverse = invertTransform(values);
    }

    /** The affine as six numbers, identity where none is stored. For drawing. */
    affineOf(id) {
        return this._layers.get(id)?.transform || IDENTITY_TRANSFORM;
    }

    // -- the OSD world ----------------------------------------------------

    /**
     * Which world item's pass overlay layers draw on.
     *
     * CanvasOverlayHd calls `onRedraw` once per world item, each time with the
     * context pre-transformed into THAT item's image space. Every overlay must
     * therefore claim exactly one pass and ignore the rest, or it is drawn once
     * per channel -- seven times at seven channels, which today is invisible only
     * because every item shares one transform. The moment a layer has its own it
     * becomes N ghosts at N positions.
     *
     * `opts.index !== 0` is NOT this test, though it is what the code used to
     * say. For a brightfield project item 0 is the RGB base by construction; for
     * a fluorescence one it is whichever channel was added first; and after any
     * `setItemIndex` it is whatever the user last dragged.
     *
     * @returns the index, or -1 when there is nothing to draw on.
     */
    anchorIndex() {
        const world = this._viewer?.world;
        const count = world?.getItemCount?.() || 0;
        if (!count) return -1;
        let firstNonLabel = -1;
        for (let i = 0; i < count; i += 1) {
            const source = world.getItemAt(i)?.source;
            if (!source) continue;
            // The reference layer if it says so. Items are tagged as they are
            // added; an untagged world (a plain channel stack) falls through.
            if (source.layerId === REFERENCE_LAYER_ID) return i;
            if (source.tileFormat !== 32 && firstNonLabel < 0) firstNonLabel = i;
        }
        // Never the label layer if there is anything else: it is the one item
        // guaranteed NOT to be the reference image.
        return firstNonLabel >= 0 ? firstNonLabel : 0;
    }

    /**
     * Push this stack's order onto OSD's world.
     *
     * Replaces ViewerManager.raiseLabelLayer, which could only express one rule
     * ("the mask goes on top") because there was nowhere to say anything else.
     * Walking the stack bottom-first and calling `setItemIndex` says all of them,
     * and is what lets a second image layer sit above or below the reference.
     *
     * Costs one redraw however many tiles are on screen -- `setItemIndex` moves
     * an item in a list, it does not refetch or re-decode anything. That is why
     * this is preferred over rebuilding the world wherever both would work: a
     * rebuild empties the world, and an empty world makes OSD call goHome() and
     * the user loses their place.
     */
    applyWorldOrder() {
        const world = this._viewer?.world;
        if (!world) return false;
        const count = world.getItemCount();
        if (!count) return false;
        // Item -> the layer that owns it, so an item nobody claimed (a channel
        // added before its layer registered) keeps its relative place rather
        // than being shuffled to the bottom.
        const rank = new Map();
        this._order.forEach((id, index) => {
            const layer = this._layers.get(id);
            for (const item of layer?.items || []) rank.set(item, index);
        });
        if (!rank.size) return false;
        const items = [];
        for (let i = 0; i < count; i += 1) items.push(world.getItemAt(i));
        const ranked = items
            .map((item, index) => ({ item, index, rank: rank.has(item) ? rank.get(item) : -1 }))
            .filter((entry) => entry.rank >= 0)
            .sort((a, b) => (a.rank - b.rank) || (a.index - b.index));
        let moved = false;
        ranked.forEach((entry) => {
            // Bottom first, each one raised to the top in turn: after the last
            // pass the stack reads bottom-to-top exactly as `_order` does.
            const current = world.getIndexOfItem(entry.item);
            const target = count - 1;
            if (current !== target) {
                world.setItemIndex(entry.item, target);
                moved = true;
            }
        });
        return moved;
    }

    /**
     * The structural readback: what is stacked, where, and on what.
     *
     * Exists because pixel hashes are PROVEN insufficient for this kind of
     * change -- a rename left all fourteen of them identical with no GL error,
     * because everything drew the same while nothing was wired. This is what
     * catches that, and it is what the probes and the browser harness assert
     * against.
     */
    describe() {
        const world = this._viewer?.world;
        return this.layers().map((layer, index) => ({
            id: layer.id,
            kind: layer.kind,
            surface: LayerStack.surfaceOf(layer.kind),
            order: index,
            visible: layer.visible,
            opacity: layer.opacity,
            transform: layer.transform ? [...layer.transform] : null,
            worldIndex: (layer.items || [])
                .map((item) => (world?.getIndexOfItem ? world.getIndexOfItem(item) : -1))
                .filter((i) => i >= 0),
            sub: layer.sub ? layer.sub.order() : null,
        }));
    }

    _changed() {
        if (this._onChange) this._onChange(this);
    }
}


if (typeof window !== "undefined") {
    window.PlexoraLayerStack = {
        LayerStack, SubLayerStack, invertTransform,
        LAYER_KINDS, LAYER_SURFACES, LAYER_KIND_SURFACE, IDENTITY_TRANSFORM,
        REFERENCE_LAYER_ID, MASK_LAYER_ID, CENTROID_LAYER_ID,
    };
}
if (typeof globalThis !== "undefined" && !globalThis.PlexoraLayerStack) {
    globalThis.PlexoraLayerStack = {
        LayerStack, SubLayerStack, invertTransform,
        LAYER_KINDS, LAYER_SURFACES, LAYER_KIND_SURFACE, IDENTITY_TRANSFORM,
        REFERENCE_LAYER_ID, MASK_LAYER_ID, CENTROID_LAYER_ID,
    };
}
