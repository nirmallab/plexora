/**
 * The shapes the picker offers, as node tables.
 *
 * There is no parametric layer here and there is not meant to be one. A
 * pentagon is five nodes, an ellipse is four nodes and KAPPA, a rectangle is
 * four corners -- the same representation a freehand stroke ends up in, so Edit
 * Points opens any of them without a conversion step and the renderers only
 * ever draw one kind of thing. `preset` survives on the stored shape as a
 * LABEL: it names what the shape started as, so the icon grid and the aspect
 * defaults have something to key on, and entering Edit Points rewrites it to
 * "custom" because the nodes no longer describe a pentagon.
 *
 * Every table is normalised to tight [0, 1] ink bounds at definition time, by
 * `fit()` below. That is not tidiness -- `w_mm`/`h_mm` are what all three
 * renderers rotate about and what the resize handles grab, so a shape whose ink
 * does not fill its box is a shape that rotates about the wrong point.
 *
 * The icons ARE the definitions: `icon()` draws the same node table the canvas
 * will. A separate icon set is a second drawing of every shape, and the first
 * time one of them is adjusted the picker starts lying about what it inserts.
 * They are also inline SVG rather than Font Awesome spans on purpose --
 * FontAwesome replaces `<span class="fas">` once at boot, so a span injected
 * into a menu opened later never becomes anything and draws nothing at all.
 */
class FigureShapeDefs {

    /**
     * The tables, built on first use and kept.
     *
     * Lazy rather than computed at load, and that is not an optimisation:
     * `build()` reads `FigureShapeGeometry.KAPPA`, and every file in this
     * plugin is a plain script whose load order is deliberately not
     * significant (`test_the_order_of_the_declared_scripts_does_not_matter`).
     * Doing the work at load would make this the one file that has to come
     * second, which is exactly the constraint that test exists to refuse.
     */
    static get PRESETS() {
        if (!FigureShapeDefs.presetCache) FigureShapeDefs.presetCache = FigureShapeDefs.build();
        return FigureShapeDefs.presetCache;
    }

    /** One preset, or null. */
    static byId(id) { return FigureShapeDefs.PRESETS[id] || null; }

    /**
     * The picker's grid, in reading order. Five to a row; the separators are a
     * CSS border on every cell past the first row, so this stays one flat list
     * and nobody has to keep rows in sync with a layout.
     */
    static get GRID() {
        return ["rect", "rounded_rect", "bar", "pill", "capsule",
                "ellipse", "triangle", "right_triangle", "diamond", "parallelogram",
                "trapezoid", "pentagon", "hexagon", "octagon", "star5",
                "star6", "burst"];
    }

    /**
     * The drawing tools, which are modes rather than geometry -- there is
     * nothing to insert until the user has drawn it.
     *
     * Named, not a second icon grid: four tools whose difference is BEHAVIOUR
     * cannot be told apart by their outlines, and a polygon and an open path
     * drawn as icons are the same picture. The `hint` says what gesture each
     * one takes; the card puts it in the tooltip rather than beside the label,
     * because it is read once and the height it costs is paid every time.
     */
    static get CUSTOM_TOOLS() {
        return [
            { id: "polygon", label: "Polygon", hint: "Click to place corners" },
            { id: "curve", label: "Curved shape", hint: "Click to place smooth points" },
            { id: "freehand", label: "Freehand", hint: "Drag to draw" },
            { id: "path", label: "Open path", hint: "Click to place points" },
        ];
    }

    /**
     * A preset's icon: the shape's own path, drawn in a 24x24 box.
     *
     * Wide presets are drawn wide -- the group is scaled by the preset's
     * natural aspect, so the bar and the pill look like a bar and a pill in the
     * grid instead of both looking like a rectangle. `non-scaling-stroke` is
     * what keeps the outline even under that non-uniform scale.
     */
    static icon(id) {
        const preset = FigureShapeDefs.byId(id);
        if (!preset) return "";
        const span = 20;
        const aspect = preset.aspect;
        const w = aspect >= 1 ? span : span * aspect;
        const h = aspect >= 1 ? span / aspect : span;
        const d = FigureShapeGeometry.pathD(preset.nodes, preset.closed);
        return `<svg class="fb-shape-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">`
            + `<g transform="translate(${(24 - w) / 2} ${(24 - h) / 2}) scale(${w} ${h})">`
            + `<path d="${d}" fill="none" stroke="currentColor" stroke-width="1.6"`
            + ` stroke-linejoin="round" vector-effect="non-scaling-stroke"/></g></svg>`;
    }

    /** A drawing tool's icon. Hand-drawn rather than generated: these depict an
     *  action, and there is no node table for "drag to draw". */
    static customIcon(id) {
        const glyph = FigureShapeDefs.CUSTOM_GLYPHS[id] || "";
        return `<svg class="fb-shape-icon" viewBox="0 0 24 24" aria-hidden="true"`
            + ` focusable="false" fill="none" stroke="currentColor" stroke-width="1.6"`
            + ` stroke-linejoin="round" stroke-linecap="round">${glyph}</svg>`;
    }

    /**
     * The tables themselves. Called once, through `PRESETS`.
     *
     * `aspect` is the shape's natural width/height -- what a bare click
     * inserts and what Shift constrains a drag to. For the generated polygons
     * and stars it is whatever regularity implies (a flat-top hexagon is 1.155
     * wide), which is why the generators return it rather than a hand-typed
     * list that would be right for about five of them.
     */
    static build() {
        const K = FigureShapeGeometry.KAPPA;

        const node = (x, y, type, into, out) =>
            ({ x, y, type, in: into || null, out: out || null });
        const corner = (x, y) => node(x, y, "corner", null, null);

        /** Raw vertices scaled into tight [0, 1], plus the aspect that keeps
         *  them the shape they were drawn as. */
        const fit = (raw) => {
            const xs = raw.map((p) => p.x);
            const ys = raw.map((p) => p.y);
            const x0 = Math.min(...xs), y0 = Math.min(...ys);
            const w = Math.max(...xs) - x0, h = Math.max(...ys) - y0;
            return {
                nodes: raw.map((p) => corner((p.x - x0) / w, (p.y - y0) / h)),
                aspect: w / h,
            };
        };

        /** A regular polygon. Angles run clockwise from `start` in screen
         *  coordinates (y down), so -90 puts a vertex at the top. */
        const polygon = (sides, start) => fit(Array.from({ length: sides }, (unused, index) => {
            const angle = (start + index * 360 / sides) * Math.PI / 180;
            return { x: Math.cos(angle), y: Math.sin(angle) };
        }));

        const star = (points, inner, start) =>
            fit(Array.from({ length: points * 2 }, (unused, index) => {
                const angle = (start + index * 180 / points) * Math.PI / 180;
                const radius = index % 2 === 0 ? 1 : inner;
                return { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius };
            }));

        /** Two flat sides and two semicircular ends. The cap's x radius is
         *  scaled by the aspect so the ends stay round at the shape's natural
         *  proportions -- the box is drawn with `preserveAspectRatio="none"`,
         *  so a cap that assumed a square box would be an oval. */
        const capsule = (aspect) => {
            const rx = 0.5 / aspect, ry = 0.5;
            const left = rx, right = 1 - rx;
            return [
                node(left, 0, "corner", { x: left - rx * K, y: 0 }, null),
                node(right, 0, "corner", null, { x: right + rx * K, y: 0 }),
                node(1, ry, "smooth", { x: 1, y: ry - ry * K }, { x: 1, y: ry + ry * K }),
                node(right, 1, "corner", { x: right + rx * K, y: 1 }, null),
                node(left, 1, "corner", null, { x: left - rx * K, y: 1 }),
                node(0, ry, "smooth", { x: 0, y: ry + ry * K }, { x: 0, y: ry - ry * K }),
            ];
        };

        const roundedRect = (r) => [
            node(r, 0, "corner", { x: r - r * K, y: 0 }, null),
            node(1 - r, 0, "corner", null, { x: 1 - r + r * K, y: 0 }),
            node(1, r, "corner", { x: 1, y: r - r * K }, null),
            node(1, 1 - r, "corner", null, { x: 1, y: 1 - r + r * K }),
            node(1 - r, 1, "corner", { x: 1 - r + r * K, y: 1 }, null),
            node(r, 1, "corner", null, { x: r - r * K, y: 1 }),
            node(0, 1 - r, "corner", { x: 0, y: 1 - r + r * K }, null),
            node(0, r, "corner", null, { x: 0, y: r - r * K }),
        ];

        const ellipse = () => {
            const c = 0.5 * K;
            return [
                node(0.5, 0, "smooth", { x: 0.5 - c, y: 0 }, { x: 0.5 + c, y: 0 }),
                node(1, 0.5, "smooth", { x: 1, y: 0.5 - c }, { x: 1, y: 0.5 + c }),
                node(0.5, 1, "smooth", { x: 0.5 + c, y: 1 }, { x: 0.5 - c, y: 1 }),
                node(0, 0.5, "smooth", { x: 0, y: 0.5 + c }, { x: 0, y: 0.5 - c }),
            ];
        };

        const rectNodes = () => [corner(0, 0), corner(1, 0), corner(1, 1), corner(0, 1)];

        const table = {};
        const add = (id, label, aspect, nodes, closed) => {
            table[id] = { id, label, aspect, closed: closed !== false, nodes };
        };
        const addFitted = (id, label, built) => add(id, label, built.aspect, built.nodes);

        add("rect", "Rectangle", 1, rectNodes());
        add("rounded_rect", "Rounded rectangle", 1, roundedRect(0.12));
        add("bar", "Bar", 8, rectNodes());
        add("pill", "Pill", 6, capsule(6));
        add("capsule", "Capsule", 2.5, capsule(2.5));
        add("ellipse", "Ellipse", 1, ellipse());
        addFitted("triangle", "Triangle", polygon(3, -90));
        add("right_triangle", "Right triangle", 1, [corner(0, 0), corner(1, 1), corner(0, 1)]);
        addFitted("diamond", "Diamond", polygon(4, -90));
        add("parallelogram", "Parallelogram", 1.4, [corner(0.25, 0), corner(1, 0),
                                                    corner(0.75, 1), corner(0, 1)]);
        add("trapezoid", "Trapezoid", 1.4, [corner(0.22, 0), corner(0.78, 0),
                                            corner(1, 1), corner(0, 1)]);
        addFitted("pentagon", "Pentagon", polygon(5, -90));
        addFitted("hexagon", "Hexagon", polygon(6, 0));
        addFitted("octagon", "Octagon", polygon(8, 22.5));
        addFitted("star5", "Star", star(5, 0.382, -90));
        addFitted("star6", "6-point star", star(6, 0.5, -90));
        addFitted("burst", "Burst", star(12, 0.76, -90));
        return table;
    }
}

FigureShapeDefs.CUSTOM_GLYPHS = {
    polygon: '<path d="M12 3.5 20.5 10 17 20h-10L3.5 10Z"/>'
        + '<circle cx="12" cy="3.5" r="1.6" fill="currentColor" stroke="none"/>'
        + '<circle cx="20.5" cy="10" r="1.6" fill="currentColor" stroke="none"/>'
        + '<circle cx="3.5" cy="10" r="1.6" fill="currentColor" stroke="none"/>',
    curve: '<path d="M12 3.5C18 3.5 20.5 7 20.5 12s-3 8.5-8.5 8.5S3.5 17 3.5 12 6 3.5 12 3.5Z"/>'
        + '<circle cx="12" cy="3.5" r="1.6" fill="currentColor" stroke="none"/>'
        + '<circle cx="20.5" cy="12" r="1.6" fill="currentColor" stroke="none"/>'
        + '<circle cx="3.5" cy="12" r="1.6" fill="currentColor" stroke="none"/>',
    freehand: '<path d="M3 21l1.2-4.2L15.4 5.6a2 2 0 0 1 2.8 0l.2.2a2 2 0 0 1 0 2.8L7.2 19.8Z"/>'
        + '<path d="M14.2 6.8 17.2 9.8"/>',
    path: '<path d="M3.5 18 9 8l5 6 6.5-8"/>'
        + '<circle cx="3.5" cy="18" r="1.6" fill="currentColor" stroke="none"/>'
        + '<circle cx="9" cy="8" r="1.6" fill="currentColor" stroke="none"/>'
        + '<circle cx="14" cy="14" r="1.6" fill="currentColor" stroke="none"/>'
        + '<circle cx="20.5" cy="6" r="1.6" fill="currentColor" stroke="none"/>',
};
