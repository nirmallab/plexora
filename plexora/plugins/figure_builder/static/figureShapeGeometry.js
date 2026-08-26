/**
 * A shape's path: nodes in, geometry out.
 *
 * The browser's half of a rule written twice. `server/shapegeom.py` holds the
 * other half and `server/schema.py normalize_shape` the validation, and
 * `tests/test_figure_builder_shapes.py` re-runs one case table -- written in
 * `tests/js/figure_shape_probe.mjs` -- through both. They have to agree
 * exactly: the canvas draws from what this file produces and the document
 * stores what Python produces, so a disagreement shows the user one shape and
 * exports another, and they find out in the PDF. Same arrangement as
 * `FigureRichText` and the arrowhead pair before it.
 *
 * Everything here is PURE -- no DOM, no measurement, no canvas. That is what
 * lets the node probe exercise it, and it is why the rotation helper is
 * duplicated from `FigureCanvas.turn` rather than reached for: the two are four
 * lines of trigonometry that cannot drift, and the dependency would cost the
 * probe the whole canvas.
 *
 * The model, in one paragraph. A shape is a list of nodes in its own box,
 * normalised 0-1 against the mm geometry the annotation already carries, so
 * resizing rewrites four numbers instead of every point and rotation stays a
 * property of the box. A node is an anchor, a type ("corner" or "smooth"), and
 * up to two bezier handles held as ABSOLUTE positions in that same space --
 * never as offsets, which have to be re-derived every time an anchor moves and
 * look identical until one of them is wrong. Presets are nodes too: "rect" is
 * four corners, "ellipse" is four smooth nodes and KAPPA. There is no
 * parametric path beside the vector one, so there is one renderer and one
 * editor and no conversion between them.
 */
class FigureShapeGeometry {

    /** The circle constant for cubic beziers: a quarter arc of radius 1 is
     *  drawn by handles this long. Mirrors `shapegeom.KAPPA`. */
    static get KAPPA() { return 0.5522847498307936; }

    /** Mirrors `schema.MAX_SHAPE_NODES`. */
    static get MAX_NODES() { return 500; }

    /** Mirrors `schema.SHAPE_COORD_SLACK`. */
    static get COORD_SLACK() { return 4; }

    /** Mirrors `shapegeom.MAX_FLATTEN_DEPTH`. */
    static get MAX_FLATTEN_DEPTH() { return 16; }

    /** Mirrors `schema.SHAPE_PRESETS`. The node tables that go with these ids
     *  live in `figureShapeDefs.js`; this is only the vocabulary the normaliser
     *  accepts, and it is here so the normaliser stays pure. */
    static get PRESET_IDS() {
        return ["rect", "rounded_rect", "ellipse", "capsule",
                "triangle", "right_triangle", "pentagon", "hexagon", "octagon",
                "diamond", "trapezoid", "parallelogram",
                "star5", "star6", "burst",
                "bar", "pill",
                "custom"];
    }

    // -- normalisation, mirrored from server/schema.py ---------------------

    /**
     * A shape payload coerced into the stored shape. Never throws.
     *
     * The Python side cannot throw either, and for a sharper reason: its
     * document loop reads a raised ValueError as "drop this annotation". Both
     * fall back to the unit rectangle, which is visible on the page and can be
     * grabbed and fixed, rather than to an empty path that draws nothing.
     */
    static normalize(shape) {
        const raw = (shape && typeof shape === "object") ? shape : {};
        let preset = FigureShapeGeometry.cleanText(raw.preset, 32);
        if (!FigureShapeGeometry.PRESET_IDS.includes(preset)) preset = "custom";
        const closed = raw.closed === undefined ? true : Boolean(raw.closed);

        const nodes = [];
        const entries = Array.isArray(raw.nodes) ? raw.nodes.slice(0, FigureShapeGeometry.MAX_NODES) : [];
        for (const entry of entries) {
            if (!entry || typeof entry !== "object") continue;
            nodes.push({
                x: FigureShapeGeometry.coord(entry.x),
                y: FigureShapeGeometry.coord(entry.y),
                // Anything that is not the word "smooth" is a corner: a node
                // type is a promise about the handles either side of it, and
                // the weaker promise is the safe one to guess.
                type: FigureShapeGeometry.cleanText(entry.type, 8) === "smooth" ? "smooth" : "corner",
                in: FigureShapeGeometry.handle(entry.in),
                out: FigureShapeGeometry.handle(entry.out),
            });
        }
        if (nodes.length < (closed ? 3 : 2)) return FigureShapeGeometry.unitShape(preset);
        return { preset, closed, nodes };
    }

    /** One coordinate in box space, clamped well OUTSIDE [0, 1] -- handles sit
     *  past their anchor as a matter of course and a node dragged outside the
     *  box is normal until the box is renormalised around it. This is a guard
     *  against a runaway client, not a geometry rule. */
    static coord(value) {
        const slack = FigureShapeGeometry.COORD_SLACK;
        return FigureShapeGeometry.clamp(FigureShapeGeometry.asFloat(value, 0), -slack, 1 + slack);
    }

    /** A control point, or null when the segment beside it is straight. An
     *  unreadable coordinate is absent rather than zero: a handle at the origin
     *  is a curve yanked to the corner of the box, where null renders as the
     *  straight segment the node already implies. */
    static handle(raw) {
        if (!raw || typeof raw !== "object") return null;
        if (FigureShapeGeometry.asFloat(raw.x, null) === null) return null;
        if (FigureShapeGeometry.asFloat(raw.y, null) === null) return null;
        return { x: FigureShapeGeometry.coord(raw.x), y: FigureShapeGeometry.coord(raw.y) };
    }

    /** What a malformed shape becomes: its own box, four corners. */
    static unitShape(preset) {
        return {
            preset: FigureShapeGeometry.PRESET_IDS.includes(preset) ? preset : "rect",
            closed: true,
            nodes: [[0, 0], [1, 0], [1, 1], [0, 1]].map(([x, y]) =>
                ({ x, y, type: "corner", in: null, out: null })),
        };
    }

    static asFloat(value, fallback) {
        if (typeof value !== "number" || !Number.isFinite(value)) return fallback;
        return value;
    }

    static clamp(value, low, high) { return Math.max(low, Math.min(high, value)); }

    /** `schema.clean_text` in one line: control characters out, trimmed, capped. */
    static cleanText(value, limit) {
        if (value === null || value === undefined) return "";
        const text = typeof value === "string" ? value : String(value);
        // eslint-disable-next-line no-control-regex
        return text.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, "").trim().slice(0, limit);
    }

    // -- the path ----------------------------------------------------------

    /** Every (start, end) pair the path draws, closing edge included. */
    static edges(nodes, closed) {
        const out = [];
        if (!nodes || nodes.length < 2) return out;
        const count = closed ? nodes.length : nodes.length - 1;
        for (let index = 0; index < count; index += 1) {
            out.push([nodes[index], nodes[(index + 1) % nodes.length]]);
        }
        return out;
    }

    /** The four cubic control points of one edge, handles resolved.
     *
     *  A missing handle degenerates to its own anchor, which is exactly the
     *  cubic that draws the straight half of a half-curved segment -- one
     *  branch fewer than treating it as its own case, and the same curve. */
    static controls(start, end) {
        return [
            { x: start.x, y: start.y },
            start.out ? { x: start.out.x, y: start.out.y } : { x: start.x, y: start.y },
            end.in ? { x: end.in.x, y: end.in.y } : { x: end.x, y: end.y },
            { x: end.x, y: end.y },
        ];
    }

    static isStraight(start, end) { return !start.out && !end.in; }

    /** The path in absolute page millimetres -- what `compose` stores on the
     *  instruction and both exporters walk. Mirrors `shapegeom.segments_mm`. */
    static segmentsMm(shape, geometry) {
        const nodes = shape.nodes || [];
        if (nodes.length < 2) return [];
        const place = (point) => [geometry.x_mm + point.x * geometry.w_mm,
                                  geometry.y_mm + point.y * geometry.h_mm];
        const out = [["move", ...place(nodes[0])]];
        for (const [start, end] of FigureShapeGeometry.edges(nodes, shape.closed)) {
            if (FigureShapeGeometry.isStraight(start, end)) {
                out.push(["line", ...place(end)]);
                continue;
            }
            const [, c1, c2] = FigureShapeGeometry.controls(start, end);
            out.push(["curve", ...place(c1), ...place(c2), ...place(end)]);
        }
        if (shape.closed) out.push(["close"]);
        return out;
    }

    /**
     * The SVG `d` for a node list, in the shape's own 0-1 box space.
     *
     * Normalised rather than mm or pixels, because the canvas draws it inside
     * an svg with `viewBox="0 0 1 1"` and `preserveAspectRatio="none"`. That is
     * the whole reason resizing a shape is free: the browser scales the path,
     * `vector-effect="non-scaling-stroke"` keeps the outline the width the user
     * asked for, and nothing has to be re-emitted on a drag. The same string
     * draws the picker's icons at 24px.
     */
    static pathD(nodes, closed) {
        if (!nodes || nodes.length < 2) return "";
        const n = (value) => Math.round(value * 1e5) / 1e5;
        const parts = [`M ${n(nodes[0].x)} ${n(nodes[0].y)}`];
        for (const [start, end] of FigureShapeGeometry.edges(nodes, closed)) {
            if (FigureShapeGeometry.isStraight(start, end)) {
                parts.push(`L ${n(end.x)} ${n(end.y)}`);
                continue;
            }
            const [, c1, c2] = FigureShapeGeometry.controls(start, end);
            parts.push(`C ${n(c1.x)} ${n(c1.y)} ${n(c2.x)} ${n(c2.y)} ${n(end.x)} ${n(end.y)}`);
        }
        if (closed) parts.push("Z");
        return parts.join(" ");
    }

    /** The path as a polyline within `tolerance` of the curve. Mirrors
     *  `shapegeom.flatten`; the canvas uses it for hit maths, the raster
     *  exporter because Pillow has no curve primitive. */
    static flatten(segments, tolerance) {
        const points = [];
        let current = [0, 0];
        let origin = [0, 0];
        for (const segment of segments) {
            if (segment[0] === "move") {
                current = origin = [segment[1], segment[2]];
                points.push(current);
            } else if (segment[0] === "line") {
                current = [segment[1], segment[2]];
                points.push(current);
            } else if (segment[0] === "curve") {
                const end = [segment[5], segment[6]];
                FigureShapeGeometry.flattenCubic(points, current, [segment[1], segment[2]],
                                                 [segment[3], segment[4]], end, tolerance, 0);
                current = end;
            } else if (segment[0] === "close") {
                const last = points[points.length - 1];
                if (last && (last[0] !== origin[0] || last[1] !== origin[1])) points.push(origin);
                current = origin;
            }
        }
        return points;
    }

    static flattenCubic(out, p0, p1, p2, p3, tolerance, depth) {
        if (depth >= FigureShapeGeometry.MAX_FLATTEN_DEPTH
            || FigureShapeGeometry.flatEnough(p0, p1, p2, p3, tolerance)) {
            out.push(p3);
            return;
        }
        const mid = (a, b) => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
        const p01 = mid(p0, p1), p12 = mid(p1, p2), p23 = mid(p2, p3);
        const p012 = mid(p01, p12), p123 = mid(p12, p23);
        const middle = mid(p012, p123);
        FigureShapeGeometry.flattenCubic(out, p0, p01, p012, middle, tolerance, depth + 1);
        FigureShapeGeometry.flattenCubic(out, middle, p123, p23, p3, tolerance, depth + 1);
    }

    /** Whether the chord stands in for the curve. The usual bound on how far a
     *  cubic can stray from the line between its endpoints, measured from the
     *  control points -- conservative, and with no square roots, which matters
     *  on every subdivision of every segment of every shape on the page. */
    static flatEnough(p0, p1, p2, p3, tolerance) {
        const ux = (3 * p1[0] - 2 * p0[0] - p3[0]) ** 2;
        const uy = (3 * p1[1] - 2 * p0[1] - p3[1]) ** 2;
        const vx = (3 * p2[0] - 2 * p3[0] - p0[0]) ** 2;
        const vy = (3 * p2[1] - 2 * p3[1] - p0[1]) ** 2;
        return Math.max(ux, vx) + Math.max(uy, vy) <= 16 * tolerance * tolerance;
    }

    /**
     * The tight box the ink occupies, in node space: {x, y, w, h}.
     *
     * Exact rather than sampled, and this is the load-bearing one. `w_mm`/`h_mm`
     * are what all three renderers rotate ABOUT, so a box that is off by a hair
     * does not merely crop a rotated shape -- it moves it.
     */
    static inkBounds(nodes, closed) {
        if (!nodes || !nodes.length) return { x: 0, y: 0, w: 0, h: 0 };
        const xs = [nodes[0].x];
        const ys = [nodes[0].y];
        for (const [start, end] of FigureShapeGeometry.edges(nodes, closed)) {
            xs.push(end.x);
            ys.push(end.y);
            if (FigureShapeGeometry.isStraight(start, end)) continue;
            const [p0, c1, c2, p3] = FigureShapeGeometry.controls(start, end);
            xs.push(...FigureShapeGeometry.cubicExtrema(p0.x, c1.x, c2.x, p3.x));
            ys.push(...FigureShapeGeometry.cubicExtrema(p0.y, c1.y, c2.y, p3.y));
        }
        const x = Math.min(...xs), y = Math.min(...ys);
        return { x, y, w: Math.max(...xs) - x, h: Math.max(...ys) - y };
    }

    /** The curve's value at each interior turning point, on one axis. */
    static cubicExtrema(p0, p1, p2, p3) {
        const a = -p0 + 3 * p1 - 3 * p2 + p3;
        const b = 2 * (p0 - 2 * p1 + p2);
        const c = p1 - p0;
        const out = [];
        for (const t of FigureShapeGeometry.quadraticRoots(a, b, c)) {
            if (!(t > 0 && t < 1)) continue;
            const s = 1 - t;
            out.push(s * s * s * p0 + 3 * s * s * t * p1 + 3 * s * t * t * p2 + t * t * t * p3);
        }
        return out;
    }

    static quadraticRoots(a, b, c) {
        if (Math.abs(a) < 1e-12) return Math.abs(b) < 1e-12 ? [] : [-c / b];
        const discriminant = b * b - 4 * a * c;
        if (discriminant < 0) return [];
        const root = Math.sqrt(discriminant);
        return [(-b + root) / (2 * a), (-b - root) / (2 * a)];
    }

    // -- editing ------------------------------------------------------------

    /**
     * The point at parameter `t` along the edge between two nodes.
     *
     * A straight edge is interpolated linearly rather than run through the
     * degenerate cubic its coincident control points describe. The two differ:
     * that cubic eases in and out, so t = 0.25 lands at 0.156 along the line.
     * `nearestOnSegment` projects onto a straight edge and reports the LINEAR
     * parameter, and the point editor feeds that straight into `splitSegment`
     * -- so if these disagreed, clicking a segment would insert the node
     * visibly away from the pointer, and only on straight ones.
     */
    static pointAt(start, end, t) {
        if (FigureShapeGeometry.isStraight(start, end)) {
            return { x: start.x + (end.x - start.x) * t, y: start.y + (end.y - start.y) * t };
        }
        const [p0, p1, p2, p3] = FigureShapeGeometry.controls(start, end);
        const s = 1 - t;
        return {
            x: s * s * s * p0.x + 3 * s * s * t * p1.x + 3 * s * t * t * p2.x + t * t * t * p3.x,
            y: s * s * s * p0.y + 3 * s * s * t * p1.y + 3 * s * t * t * p2.y + t * t * t * p3.y,
        };
    }

    /**
     * Cut one edge in two at `t`, returning the node to insert and the handle
     * changes either side of it.
     *
     * De Casteljau rather than "drop a point on the chord": the two halves of a
     * split cubic are exact cubics, so the curve through the new node is the
     * curve that was already there. Inserting a point on a curved segment
     * without this visibly changes the shape at the moment the user asks to
     * edit it, which is the one moment they are watching it.
     */
    static splitSegment(start, end, t) {
        if (FigureShapeGeometry.isStraight(start, end)) {
            const point = FigureShapeGeometry.pointAt(start, end, t);
            return { node: { x: point.x, y: point.y, type: "corner", in: null, out: null },
                     startOut: null, endIn: null };
        }
        const [p0, p1, p2, p3] = FigureShapeGeometry.controls(start, end);
        const lerp = (a, b) => ({ x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t });
        const p01 = lerp(p0, p1), p12 = lerp(p1, p2), p23 = lerp(p2, p3);
        const p012 = lerp(p01, p12), p123 = lerp(p12, p23);
        const middle = lerp(p012, p123);
        return {
            node: { x: middle.x, y: middle.y, type: "smooth", in: p012, out: p123 },
            startOut: p01,
            endIn: p23,
        };
    }

    /**
     * Where on an edge a point lands: {t, point, distance}.
     *
     * Two passes of sampling rather than a closed form. Projecting onto a cubic
     * means solving a quintic, and the answer is only ever used to decide which
     * segment a click hit and where to put a node on it -- a target the user is
     * aiming at with a ~9px handle. Straight edges, which are most of them, are
     * projected exactly.
     */
    static nearestOnSegment(start, end, point) {
        if (FigureShapeGeometry.isStraight(start, end)) {
            const dx = end.x - start.x, dy = end.y - start.y;
            const span = dx * dx + dy * dy;
            const t = span === 0 ? 0
                : FigureShapeGeometry.clamp(((point.x - start.x) * dx + (point.y - start.y) * dy) / span, 0, 1);
            const at = { x: start.x + dx * t, y: start.y + dy * t };
            return { t, point: at, distance: Math.hypot(point.x - at.x, point.y - at.y) };
        }
        let best = { t: 0, point: null, distance: Infinity };
        const scan = (from, to, steps) => {
            for (let index = 0; index <= steps; index += 1) {
                const t = from + (to - from) * (index / steps);
                const at = FigureShapeGeometry.pointAt(start, end, t);
                const distance = Math.hypot(point.x - at.x, point.y - at.y);
                if (distance < best.distance) best = { t, point: at, distance };
            }
        };
        scan(0, 1, 32);
        scan(Math.max(0, best.t - 1 / 32), Math.min(1, best.t + 1 / 32), 16);
        return best;
    }

    /**
     * Smooth nodes through a list of points -- the curve tool and the tail of
     * the freehand one.
     *
     * Catmull-Rom converted to bezier: a node's tangent is the line between its
     * two neighbours, and a handle a sixth of that away reproduces the spline
     * exactly. Chosen over fitting a curve to the points because the result is
     * node-based from the first frame, so the shape the user sees while drawing
     * is the shape Edit Points opens -- one representation, no conversion.
     */
    static catmullRom(points, closed) {
        const count = points.length;
        if (count < 2) return [];
        return points.map((point, index) => {
            const previous = index === 0 ? (closed ? points[count - 1] : points[0]) : points[index - 1];
            const next = index === count - 1 ? (closed ? points[0] : points[count - 1]) : points[index + 1];
            const tx = (next.x - previous.x) / 6;
            const ty = (next.y - previous.y) / 6;
            const first = !closed && index === 0;
            const last = !closed && index === count - 1;
            return {
                x: point.x, y: point.y, type: "smooth",
                in: first ? null : { x: point.x - tx, y: point.y - ty },
                out: last ? null : { x: point.x + tx, y: point.y + ty },
            };
        });
    }

    /**
     * Ramer-Douglas-Peucker: the points that carry the shape, and no others.
     *
     * A freehand stroke arrives as hundreds of pointer samples, and every one
     * of them would be a node the user has to drag past to reach the one they
     * meant. Keeping the points whose removal would move the line furthest is
     * what makes the result editable rather than merely accurate.
     */
    static rdp(points, epsilon) {
        if (points.length < 3) return points.slice();
        const first = points[0];
        const last = points[points.length - 1];
        let worst = 0;
        let index = 0;
        for (let i = 1; i < points.length - 1; i += 1) {
            const distance = FigureShapeGeometry.pointToSegment(points[i], first, last);
            if (distance > worst) { worst = distance; index = i; }
        }
        if (worst <= epsilon) return [first, last];
        return FigureShapeGeometry.rdp(points.slice(0, index + 1), epsilon)
            .slice(0, -1)
            .concat(FigureShapeGeometry.rdp(points.slice(index), epsilon));
    }

    static pointToSegment(point, a, b) {
        const dx = b.x - a.x, dy = b.y - a.y;
        const span = dx * dx + dy * dy;
        const t = span === 0 ? 0
            : FigureShapeGeometry.clamp(((point.x - a.x) * dx + (point.y - a.y) * dy) / span, 0, 1);
        return Math.hypot(point.x - (a.x + dx * t), point.y - (a.y + dy * t));
    }

    // -- the box --------------------------------------------------------------

    /** Nodes denormalised into local millimetres -- the frame the point editor
     *  works in, where the box's top-left is the origin and rotation does not
     *  exist yet. */
    static localNodesMm(nodes, geometry) {
        const place = (point) => (point
            ? { x: point.x * geometry.w_mm, y: point.y * geometry.h_mm }
            : null);
        return nodes.map((node) => ({
            x: node.x * geometry.w_mm, y: node.y * geometry.h_mm,
            type: node.type, in: place(node.in), out: place(node.out),
        }));
    }

    /**
     * Put the box back around the ink after an edit: {geometry, nodes}.
     *
     * `nodes` arrive in LOCAL millimetres (see `localNodesMm`) and come back
     * normalised, with a geometry whose box is the ink's tight bounds. Without
     * this, dragging a node outside the box leaves coordinates like 3.7 that
     * grow every edit, and the box -- which is what the renderers rotate about
     * and what the resize handles grab -- stops describing the shape at all.
     *
     * The rotation step is the part that is easy to get wrong. Moving the box
     * moves its centre, and on a ROTATED shape the world offset is that centre
     * delta TURNED by the angle: the local frame is tilted, so a shift of 2mm
     * "right" in it is not 2mm right on the page. Getting this wrong makes a
     * rotated shape jump the moment a node is dragged, and only a rotated one,
     * which is why the test that pins it uses 30 degrees.
     */
    static renormalize(nodes, closed, geometry) {
        const bounds = FigureShapeGeometry.inkBounds(nodes, closed);
        const centre = { x: bounds.x + bounds.w / 2, y: bounds.y + bounds.h / 2 };
        // A shape can legitimately be flat -- a horizontal open path has zero
        // height. The floor keeps it grabbable and keeps the division below
        // finite; the ink stays centred in the box either way.
        const w = Math.max(bounds.w, 0.1);
        const h = Math.max(bounds.h, 0.1);
        const originX = centre.x - w / 2;
        const originY = centre.y - h / 2;

        const shift = FigureShapeGeometry.turn(centre.x - geometry.w_mm / 2,
                                               centre.y - geometry.h_mm / 2,
                                               geometry.rotation || 0);
        const place = (point) => (point
            ? { x: (point.x - originX) / w, y: (point.y - originY) / h }
            : null);
        return {
            geometry: {
                ...geometry,
                x_mm: geometry.x_mm + geometry.w_mm / 2 + shift.x - w / 2,
                y_mm: geometry.y_mm + geometry.h_mm / 2 + shift.y - h / 2,
                w_mm: w, h_mm: h,
            },
            nodes: nodes.map((node) => ({
                x: (node.x - originX) / w, y: (node.y - originY) / h,
                type: node.type, in: place(node.in), out: place(node.out),
            })),
        };
    }

    /**
     * A drag delta snapped to the nearest 45 degrees -- what Shift means.
     *
     * Projected onto the chosen axis rather than merely zeroing the smaller
     * component: zeroing makes a 40mm diagonal drag become a 40mm horizontal
     * one, so the point jumps further than the pointer went. The projection is
     * what the pointer actually travelled ALONG that axis, which is the
     * distance the user can see themselves making.
     */
    static constrainDelta(dx, dy) {
        if (!dx && !dy) return { x: 0, y: 0 };
        const step = Math.PI / 4;
        const angle = Math.round(Math.atan2(dy, dx) / step) * step;
        const cos = Math.cos(angle);
        const sin = Math.sin(angle);
        const along = dx * cos + dy * sin;
        return { x: cos * along, y: sin * along };
    }

    /** A vector turned clockwise by `degrees`, in page coordinates (y down).
     *  The same four lines as `FigureCanvas.turn`, kept here so this file has
     *  no dependencies and the probe can load it alone. */
    static turn(x, y, degrees) {
        const radians = degrees * Math.PI / 180;
        const cos = Math.cos(radians);
        const sin = Math.sin(radians);
        return { x: x * cos - y * sin, y: x * sin + y * cos };
    }
}
