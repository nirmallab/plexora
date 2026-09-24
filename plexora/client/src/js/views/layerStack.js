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

/** Added to a pinned layer's rank so it sorts above every ordinary one. Larger
 *  than any layer count a project can reach, which is the only property asked
 *  of it. */
const PINNED_RANK_BIAS = 1e6;

/**
 * The property a world item may carry to say where it sits WITHIN its layer.
 *
 * One layer can own world items that are not interchangeable: a registered
 * layer's channels are drawn as a pair each -- one blit that takes the image
 * underneath away where the channel covers it, one that adds the channel's
 * colour (see ViewerManager.addLayerChannelSet) -- and every blit of the first
 * kind has to stay below every blit of the second, or a later channel dims an
 * earlier channel's colour instead of the base's.
 *
 * Insertion order will not carry that. `addTiledImage` is asynchronous and the
 * HD toggle, a visibility flip and a routing repair all re-add items, so the
 * two halves of a pair can land either way round. A number the item carries
 * survives all of it.
 *
 * Absent means 0, which is every other world item in the viewer.
 */
const ITEM_Z = "_plexoraItemZ";


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
 * How far a decomposed affine may stray from a similarity transform before the
 * viewer refuses to draw it. Must equal ngff_transform.TOLERANCE on the server,
 * which is what decides whether a layer is registered at all --
 * layer_transform_probe.mjs asserts the two agree.
 */
const TRANSFORM_TOLERANCE = 1e-4;


/**
 * An affine as the parts OpenSeadragon has controls for.
 *
 * TiledImage takes x, y, width, degrees and flipped -- translation, UNIFORM
 * scale, rotation and mirror, and nothing else. This says what an affine asks
 * for; `unsupportedReason` says whether the answer is drawable.
 *
 * `flipped` is read off a negative determinant, because that is what a mirror
 * IS, and it is reported apart from the rotation because OSD applies it apart:
 * a flip folded into an angle reads as a half turn of a mirrored image, which
 * is a different picture.
 *
 * The same decomposition as ngff_transform.decompose, kept in step by
 * layer_transform_probe.mjs running both over one table of cases.
 */
function decomposeTransform(t) {
    let [a, b, c, d, e, f] = (t || IDENTITY_TRANSFORM).map(Number);
    const det = a * d - b * c;
    const flipped = det < 0;
    if (flipped) {
        a = -a;
        b = -b;
    }
    const scaleX = Math.hypot(a, b);
    const rotation = scaleX ? Math.atan2(b, a) * 180 / Math.PI : 0;
    const shear = scaleX ? (a * c + b * d) / (scaleX * scaleX) : 0;
    const scaleY = scaleX ? (a * d - b * c) / scaleX : 0;
    return {
        scaleX, scaleY, rotation, shear,
        translateX: e, translateY: f, flipped,
    };
}


/**
 * Why OSD cannot draw this transform, or null when it can.
 *
 * Refused loudly rather than approximated. A sheared layer drawn without its
 * shear looks entirely plausible and is wrong by a few microns everywhere --
 * which is precisely the error a registration exists to remove, reintroduced
 * silently. Rotation is the case that actually occurs (serial sections,
 * re-imaged slides) and OSD handles it exactly; anisotropic pixel size is rare
 * and its escape hatch is resampling the layer on the way in.
 */
function unsupportedReason(t, tol = TRANSFORM_TOLERANCE) {
    if (t == null) return null;
    const parts = decomposeTransform(t);
    if (!Number.isFinite(parts.scaleX) || !parts.scaleX) return "degenerate";
    if (Math.abs(parts.shear) > tol) return "shear";
    const ratio = Math.abs(parts.scaleY) / Math.abs(parts.scaleX);
    if (Math.abs(ratio - 1) > tol) return "anisotropic";
    return null;
}


/**
 * The addTiledImage options that place a layer where its transform says.
 *
 * OSD sizes a TiledImage by its `width` IN VIEWPORT UNITS, and the viewport is
 * normalised so the reference image is exactly 1 wide. So a layer that is
 * `layerWidth` pixels across and scaled by `s` relative to the reference is
 * `layerWidth * s / referenceWidth` viewport units wide -- which is the one
 * conversion in this whole file that is easy to get wrong and impossible to
 * spot afterwards, because a layer at the wrong scale still looks like an image.
 *
 * `x, y` are the top-left of the UNROTATED bounds, which is not where the
 * affine's translation is once the layer is turned or mirrored. OSD rotates a
 * TiledImage about the centre of those bounds (`_getRotationPoint` is
 * `getBoundsNoRotate().getCenter()`) and mirrors it in place inside them
 * (`getTileBounds` reflects the tile grid, the drawer reflects each tile), so
 * a layer pixel p is drawn at  centre + R(degrees) * F * (p - layerCentre) * s
 * with F the horizontal mirror. That equals the affine exactly when the
 * bounds' centre is the affine image of the layer's centre -- so that is what
 * is placed, and x, y are read back off it. With no turn and no mirror the
 * two agree and the translation IS the top-left, which is the branch every
 * layer registered before a rotated one took; it is kept verbatim so those
 * placements do not move by a rounding error.
 *
 * @param transform - the layer's affine, or null for "already in place"
 * @param layerWidth / referenceWidth - full-resolution pixel widths
 * @param layerHeight - the layer's full-resolution pixel height; only a
 *   turned or mirrored layer needs it (its centre), and a square is assumed
 *   without it
 * @returns { x, y, width, degrees, flipped }, or null when the transform is
 *   one OSD cannot express -- the caller shows that on the layer's card rather
 *   than drawing something almost right.
 */
function placementFor(transform, layerWidth, referenceWidth, layerHeight = layerWidth) {
    if (unsupportedReason(transform)) return null;
    const parts = decomposeTransform(transform);
    const scale = Math.abs(parts.scaleX) || 1;
    const width = referenceWidth
        ? (layerWidth || referenceWidth) * scale / referenceWidth
        : 1;
    const norm = referenceWidth || 1;
    if (parts.rotation === 0 && !parts.flipped) {
        return {
            x: parts.translateX / norm,
            y: parts.translateY / norm,
            width,
            degrees: parts.rotation,
            flipped: parts.flipped,
        };
    }
    const w = Number(layerWidth) || Number(referenceWidth) || 1;
    const h = Number(layerHeight) || w;
    // OSD derives the height from the source's aspect ratio.
    const height = width * h / w;
    const [cx, cy] = applyLayerAffine(transform, w / 2, h / 2);
    return {
        x: cx / norm - width / 2,
        y: cy / norm - height / 2,
        width,
        degrees: parts.rotation,
        flipped: parts.flipped,
    };
}


function applyLayerAffine(t, x, y) {
    const [a, b, c, d, e, f] = t.map(Number);
    return [a * x + c * y + e, b * x + d * y + f];
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
        //: Everything else that wants to know. Separate from `onChange` because
        //: that one belongs to the viewer that owns this stack and must never be
        //: displaced by a panel or a plugin registering an interest.
        this._listeners = new Set();
        this._layers = new Map();
        this._order = [];
    }

    /**
     * Be told when what is drawn changes.
     *
     * @returns a function that removes the listener. Returned rather than
     * offering an `off`, because a caller that has to remember its own callback
     * to unsubscribe usually does not, and a panel that outlives its viewer is a
     * repaint into a detached node on every frame.
     */
    subscribe(fn) {
        if (typeof fn !== "function") return () => {};
        this._listeners.add(fn);
        return () => this._listeners.delete(fn);
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
                transformUnsupported: null,
                //: The sub-stack, built on demand. Only the labels layer has one
                //: today (it is the cell-layer registry); nothing stops a points
                //: layer growing one when per-gene styling needs it.
                sub: null,
                //: The OSD world items this layer owns, when it has any. Set by
                //: whoever calls addTiledImage, because that is who knows.
                items: [],
                //: Who draws this layer, when it is not core. See `claim`.
                drawnBy: null,
                //: Held at the top of its surface whatever the order says. The
                //: cell mask is the one that needs it: its card is gone, because
                //: the Cells footer owns how cells are drawn, and a layer with no
                //: card cannot be dragged back up once a raster passes it.
                pinned: false,
            };
            this._layers.set(id, layer);
            this._order.push(id);
        }
        const { transform, ...rest } = spec;
        Object.assign(layer, rest);
        if (!LAYER_KINDS.includes(layer.kind)) layer.kind = "image";
        if (transform !== undefined) this._assignTransform(layer, transform);
        // A layer registered after a pinned one lands above it, so re-lift here
        // as well as in setOrder: /config arrives in server order and the mask is
        // not last in it.
        this._order = this._liftPinned(this._order);
        this._changed();
        return layer;
    }

    /**
     * Say that something is actually drawing this layer.
     *
     * The one fact the Layers panel cannot work out for itself, and the
     * reason it needed telling. A card offers an eye, an opacity slider and a
     * place in the stack, and for an image or a mask those mean something
     * because CORE draws them. For a points layer core draws nothing, so the
     * panel has always refused it a card rather than offer three controls
     * that move nothing -- which was right while nothing drew points, and
     * became wrong the moment the transcripts plugin did.
     *
     * So a drawer claims the layer, and the claim is what earns the card. It
     * is not a permission: the layer is in `/config` and in the stack either
     * way. It is the panel's answer to "will this eye do anything".
     *
     * `__centroids__` is deliberately NOT claimed by anyone. Core draws it,
     * but the Cells section owns the control -- one mask, one set of buttons
     * -- and a second eye for it in the Layers list would be two answers to
     * one question.
     *
     * @param by - the drawer's name, or null to give the claim up
     */
    claim(id, by) {
        const layer = this._layers.get(id);
        if (!layer) return false;
        const next = by || null;
        if (layer.drawnBy === next) return false;
        layer.drawnBy = next;
        this._changed();
        return true;
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
     *
     * Pinned layers are lifted to the top afterwards, in their existing relative
     * order. The partial-order rule alone is not enough for them: the Layers
     * panel sends only the ids it has cards for, so an uncarded mask would be
     * "unmentioned", drop to the bottom, and disappear under the first raster on
     * the first drag.
     */
    setOrder(ids) {
        const wanted = (ids || []).filter((id) => this._layers.has(id));
        const mentioned = new Set(wanted);
        const rest = this._order.filter((id) => !mentioned.has(id));
        const next = this._liftPinned([...rest, ...wanted]);
        const same = next.length === this._order.length
            && next.every((id, index) => id === this._order[index]);
        if (same) return false;
        this._order = next;
        this._changed();
        return true;
    }

    /** `ids` with the pinned ones moved to the end, relative order kept. */
    _liftPinned(ids) {
        const pinned = ids.filter((id) => this._layers.get(id)?.pinned);
        if (!pinned.length) return ids;
        return [...ids.filter((id) => !this._layers.get(id)?.pinned), ...pinned];
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
            layer.transformUnsupported = null;
            return;
        }
        const values = Array.from(transform, Number);
        if (values.length !== 6 || values.some((v) => !Number.isFinite(v))) {
            layer.transform = null;
            layer.inverse = null;
            layer.transformUnsupported = null;
            return;
        }
        layer.transform = values;
        layer.inverse = invertTransform(values);
        //: Why this transform cannot be drawn, or null. Kept on the record
        //: rather than recomputed, because the Layer Manager reads it on every
        //: repaint and the answer never changes while the transform does not.
        layer.transformUnsupported = unsupportedReason(values);
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
            // Pinned ranks are biased clear of every ordinary one, so the mask
            // stays above the rasters even when a stale `_order` has not been
            // lifted yet -- this runs on every redraw and must not depend on it.
            const base = layer?.pinned ? PINNED_RANK_BIAS : 0;
            for (const item of layer?.items || []) rank.set(item, base + index);
        });
        if (!rank.size) return false;
        const items = [];
        for (let i = 0; i < count; i += 1) items.push(world.getItemAt(i));
        const ranked = items
            .map((item, index) => ({
                item,
                index,
                rank: rank.has(item) ? rank.get(item) : -1,
                //: Within one layer, and only one layer has more than one
                //: kind of item -- see ITEM_Z.
                z: Number(item[ITEM_Z]) || 0,
            }))
            .filter((entry) => entry.rank >= 0)
            .sort((a, b) => (a.rank - b.rank) || (a.z - b.z) || (a.index - b.index));
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
            transformUnsupported: layer.transformUnsupported || null,
            drawnBy: layer.drawnBy || null,
            pinned: Boolean(layer.pinned),
            worldIndex: (layer.items || [])
                .map((item) => (world?.getIndexOfItem ? world.getIndexOfItem(item) : -1))
                .filter((i) => i >= 0),
            sub: layer.sub ? layer.sub.order() : null,
        }));
    }

    _changed() {
        if (this._onChange) this._onChange(this);
        // Each listener in its own try: one panel throwing must not stop the
        // next from repainting, and must not stop the viewer redrawing either.
        for (const listener of this._listeners) {
            try {
                listener(this);
            } catch (error) {
                console.error("layerStack: listener failed", error);
            }
        }
    }
}


/**
 * Everything drawn ON TOP of the image, on one canvas, in one pass.
 *
 * Before this, a plugin that wanted to draw its own geometry made its own
 * CanvasOverlayHd -- which is what ROI does. Three things go wrong with that,
 * and all three are the kind that are invisible until they are expensive:
 *
 *   **onRedraw fires once per world item**, i.e. once per active channel. An
 *   overlay that does not claim exactly one pass paints everything N times.
 *   Invisible at full opacity; obvious the moment anything is translucent. Core
 *   owns that guard now, and owns it CORRECTLY -- see LayerStack.anchorIndex,
 *   which is not `index !== 0`.
 *
 *   **Every CanvasOverlayHd registers its own `update-viewport` handler and
 *   offers no way to remove it.** A plugin that came and went left one behind
 *   for the life of the page. ROI's own comments call that unremovable; with one
 *   host there is one handler, and a plugin leaving is one entry removed from a
 *   map.
 *
 *   **Line widths are in IMAGE pixels.** A 2px stroke is 2 image pixels, so at
 *   10x zoom it is a 20px slab and at 0.1x it vanishes. Every overlay has to
 *   divide by the zoom, and every overlay's author has to find that out. `px` is
 *   passed in already inverted, so the idiom is `lineWidth = 1.6 * opts.px`.
 *
 * Core owns the canvas, the transform, the anchor guard, the frame coalescing
 * and a save()/restore() around every draw -- so a plugin leaking canvas state
 * cannot corrupt the next one's pass.
 */
class OverlayHost {
    /**
     * @param stack - the LayerStack, for ordering and for each overlay's
     *   transform. An overlay naming a layer is drawn in that layer's place in
     *   the stack and through that layer's affine; one naming none is drawn in
     *   registration order, in the reference layer's own space.
     * @param repaint - how to make the host canvas redraw itself. Passed in
     *   because the canvas belongs to ImageViewer, and an OverlayHost that
     *   reached for it would be a second thing that knows how CanvasOverlayHd
     *   works.
     */
    constructor({ stack = null, repaint = null } = {}) {
        this._stack = stack;
        this._repaint = repaint;
        this._overlays = new Map();
        this._seq = 0;
        this._frame = null;
    }

    setStack(stack) { this._stack = stack; }

    setRepaint(repaint) { this._repaint = repaint; }

    /**
     * Register something to draw.
     *
     * @param id       - unique; re-adding the same id replaces it
     * @param draw     - (opts) => void. `opts` carries the 2D context, the zoom,
     *                   `px` (= 1 / zoom, in image pixels per screen pixel) and
     *                   the overlay's own record.
     * @param hitTest  - (x, y, opts) => any, in the overlay's own coordinate
     *                   space. Optional; core calls it on demand, never per frame.
     * @param layerId  - the layer this belongs to, for ordering and transform
     * @param order    - a tie-break within one layer; registration order otherwise
     * @param visible  - drawn or not, without being removed
     * @returns a handle: { id, invalidate, remove, setVisible, setOrder }
     */
    add({ id, draw, hitTest = null, layerId = null, order = null, visible = true, kind = "shapes" }) {
        if (!id || typeof draw !== "function") return null;
        const entry = {
            id, draw, hitTest, layerId, kind,
            visible: visible !== false,
            order: order === null ? this._seq : order,
            seq: this._seq,
        };
        this._seq += 1;
        this._overlays.set(id, entry);
        const host = this;
        const handle = {
            id,
            invalidate: () => host.invalidate(),
            remove: () => host.remove(id),
            setVisible: (on) => {
                entry.visible = on !== false;
                host.invalidate();
            },
            setOrder: (value) => {
                entry.order = value;
                host.invalidate();
            },
        };
        this.invalidate();
        return handle;
    }

    remove(id) {
        const had = this._overlays.delete(id);
        if (had) this.invalidate();
        return had;
    }

    get(id) { return this._overlays.get(id) || null; }

    has(id) { return this._overlays.has(id); }

    ids() { return [...this._overlays.keys()]; }

    /**
     * The overlays to draw, bottom first.
     *
     * Ordered by the stack position of the layer each one names, so a plugin's
     * geometry moves when the user drags that layer's card -- which is the whole
     * point of the layer being the unit of ordering rather than the plugin.
     */
    drawList() {
        const rank = new Map();
        (this._stack?.order() || []).forEach((layerId, index) => rank.set(layerId, index));
        return [...this._overlays.values()]
            .filter((entry) => entry.visible && this._layerVisible(entry))
            .sort((a, b) => {
                const ra = rank.has(a.layerId) ? rank.get(a.layerId) : Number.MAX_SAFE_INTEGER;
                const rb = rank.has(b.layerId) ? rank.get(b.layerId) : Number.MAX_SAFE_INTEGER;
                return (ra - rb) || (a.order - b.order) || (a.seq - b.seq);
            });
    }

    _layerVisible(entry) {
        if (!entry.layerId || !this._stack) return true;
        const layer = this._stack.get(entry.layerId);
        // A layer that does not exist does not hide an overlay: a plugin may
        // register its drawing before the layer list arrives, and vanishing in
        // the meantime would look like the plugin failed.
        return !layer || layer.visible;
    }

    /**
     * One frame.
     *
     * Called from ImageViewer's own onRedraw, and only on the anchor item's
     * pass -- so the once-per-channel problem is solved once, here, rather than
     * in every plugin that ever draws anything.
     */
    drawAll(opts) {
        const context = opts?.context;
        if (!context) return 0;
        const zoom = Number(opts.zoom) || 1;
        const px = 1 / Math.max(Math.abs(zoom), 1e-6);
        let drawn = 0;
        for (const entry of this.drawList()) {
            const transform = entry.layerId ? this._stack?.get(entry.layerId)?.transform : null;
            context.save();
            if (transform) context.transform(...transform);
            try {
                entry.draw({ ...opts, px, overlay: entry });
                drawn += 1;
            } catch (error) {
                console.error(`overlay "${entry.id}" failed to draw`, error);
            }
            // Unconditional, and paired with the save above whatever the draw
            // did: a plugin that leaves a clip or a globalAlpha behind would
            // otherwise corrupt every overlay after it, and the symptom would
            // appear in somebody else's code.
            context.restore();
        }
        return drawn;
    }

    /**
     * Repaint on the next frame, however many times this is called first.
     *
     * A pointer-move handler can fire several times between two frames, and a
     * geometry rebuild per event is work whose result is thrown away.
     */
    invalidate() {
        if (this._frame || typeof requestAnimationFrame !== "function") {
            if (typeof requestAnimationFrame !== "function") this._repaint?.();
            return;
        }
        this._frame = requestAnimationFrame(() => {
            this._frame = null;
            this._repaint?.();
        });
    }

    /**
     * Ask each overlay, topmost first, what is under a point.
     *
     * Topmost first because that is what the user means by "this one": the thing
     * they can see. The point arrives in the reference layer's pixel space and
     * is put back into each overlay's own through its layer's inverse, which is
     * why the inverse is computed once at registration rather than per event.
     */
    hitTest(x, y, opts = {}) {
        const list = this.drawList().reverse();
        for (const entry of list) {
            if (typeof entry.hitTest !== "function") continue;
            let px = x;
            let py = y;
            const inverse = entry.layerId ? this._stack?.get(entry.layerId)?.inverse : null;
            if (inverse) {
                const [a, b, c, d, e, f] = inverse;
                px = a * x + c * y + e;
                py = b * x + d * y + f;
            }
            try {
                const hit = entry.hitTest(px, py, { ...opts, overlay: entry });
                if (hit !== null && hit !== undefined && hit !== false) {
                    return { overlay: entry.id, hit };
                }
            } catch (error) {
                console.error(`overlay "${entry.id}" failed to hit-test`, error);
            }
        }
        return null;
    }

    destroy() {
        if (this._frame && typeof cancelAnimationFrame === "function") {
            cancelAnimationFrame(this._frame);
        }
        this._frame = null;
        this._overlays.clear();
    }
}


if (typeof window !== "undefined") {
    window.PlexoraLayerStack = {
        LayerStack, SubLayerStack, OverlayHost, invertTransform,
        decomposeTransform, unsupportedReason, placementFor,
        TRANSFORM_TOLERANCE,
        LAYER_KINDS, LAYER_SURFACES, LAYER_KIND_SURFACE, IDENTITY_TRANSFORM,
        REFERENCE_LAYER_ID, MASK_LAYER_ID, CENTROID_LAYER_ID, ITEM_Z,
    };
}
if (typeof globalThis !== "undefined" && !globalThis.PlexoraLayerStack) {
    globalThis.PlexoraLayerStack = {
        LayerStack, SubLayerStack, OverlayHost, invertTransform,
        decomposeTransform, unsupportedReason, placementFor,
        TRANSFORM_TOLERANCE,
        LAYER_KINDS, LAYER_SURFACES, LAYER_KIND_SURFACE, IDENTITY_TRANSFORM,
        REFERENCE_LAYER_ID, MASK_LAYER_ID, CENTROID_LAYER_ID, ITEM_Z,
    };
}
