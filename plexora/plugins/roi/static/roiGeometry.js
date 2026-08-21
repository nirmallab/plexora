/**
 * roiGeometry.js - polygon arithmetic, and nothing else.
 *
 * Pure functions over plain arrays: no DOM, no viewer, no state. That is what
 * lets the hard parts -- simplification, hit-testing, self-intersection -- be
 * tested directly (tests/js/roi_geometry_probe.mjs) instead of only through a
 * browser nobody can run in CI.
 *
 * Every coordinate here is a FULL-RESOLUTION IMAGE PIXEL. Screen pixels never
 * reach this file; roiTools converts at the edge. Tolerances are therefore
 * passed in already divided by the current zoom, which is why several functions
 * take an epsilon rather than assuming one -- "within 6 pixels of the cursor"
 * is a different number of image pixels at every zoom level.
 */
const RoiGeometry = (function () {

    /** Drop points that repeat their predecessor within `epsilon`.
     *
     * A pointer stream is full of these: a stationary mouse still fires move
     * events, and two identical positions in a row make a zero-length segment
     * that every downstream algorithm has to special-case. */
    function dedupe(points, epsilon = 0) {
        const out = [];
        for (const point of points) {
            const last = out[out.length - 1];
            if (last && Math.abs(point[0] - last[0]) <= epsilon
                && Math.abs(point[1] - last[1]) <= epsilon) continue;
            out.push([point[0], point[1]]);
        }
        return out;
    }

    /** How many genuinely different positions a ring has, ignoring a closing
     *  duplicate. Three is the minimum that encloses anything. */
    function distinctCount(points) {
        const seen = new Set();
        for (const [x, y] of points) seen.add(`${x},${y}`);
        return seen.size;
    }

    function closeRing(points) {
        if (!points.length) return points;
        const first = points[0];
        const last = points[points.length - 1];
        if (first[0] === last[0] && first[1] === last[1]) return points;
        return [...points, [first[0], first[1]]];
    }

    /** A ring without its closing duplicate -- the form the editor works in,
     *  where every position is a vertex the user can drag. */
    function openRing(points) {
        if (points.length > 1) {
            const first = points[0];
            const last = points[points.length - 1];
            if (first[0] === last[0] && first[1] === last[1]) return points.slice(0, -1);
        }
        return points;
    }

    /** Twice the signed area (the shoelace sum). Sign gives winding; magnitude
     *  is what the tiny-shape guard measures. */
    function signedArea(points) {
        const ring = openRing(points);
        let sum = 0;
        for (let i = 0; i < ring.length; i++) {
            const [x1, y1] = ring[i];
            const [x2, y2] = ring[(i + 1) % ring.length];
            sum += x1 * y2 - x2 * y1;
        }
        return sum / 2;
    }

    function area(points) {
        return Math.abs(signedArea(points));
    }

    // -- geometry objects ------------------------------------------------

    function polygonFrom(points) {
        return { type: "Polygon", coordinates: [closeRing(points)] };
    }

    /** Every ring of a Polygon or MultiPolygon, outer and holes alike. */
    function rings(geometry) {
        if (!geometry || !geometry.coordinates) return [];
        if (geometry.type === "MultiPolygon") {
            return geometry.coordinates.flat();
        }
        return geometry.coordinates;
    }

    /** The polygons of a geometry, each as its own list of rings. */
    function polygons(geometry) {
        if (!geometry || !geometry.coordinates) return [];
        return geometry.type === "MultiPolygon" ? geometry.coordinates : [geometry.coordinates];
    }

    function vertexCount(geometry) {
        return rings(geometry).reduce((total, ring) => total + ring.length, 0);
    }

    /**
     * Whether the editor can move this shape's individual vertices.
     *
     * Only a plain single-ring polygon. Holes and MultiPolygons are preserved
     * exactly as imported and can be selected, renamed, recategorized, moved as
     * a whole and deleted -- they just cannot have their vertices dragged,
     * because "drag a vertex" is ambiguous once there is more than one ring and
     * guessing wrong silently rewrites somebody's imported contour.
     */
    function isVertexEditable(geometry) {
        return Boolean(geometry) && geometry.type === "Polygon"
            && Array.isArray(geometry.coordinates) && geometry.coordinates.length === 1;
    }

    function bounds(geometry) {
        let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
        for (const ring of rings(geometry)) {
            for (const [x, y] of ring) {
                if (x < minX) minX = x;
                if (y < minY) minY = y;
                if (x > maxX) maxX = x;
                if (y > maxY) maxY = y;
            }
        }
        if (minX === Infinity) return null;
        return { minX, minY, maxX, maxY };
    }

    function translate(geometry, dx, dy) {
        const move = (ring) => ring.map(([x, y]) => [x + dx, y + dy]);
        if (geometry.type === "MultiPolygon") {
            return {
                type: "MultiPolygon",
                coordinates: geometry.coordinates.map((polygon) => polygon.map(move)),
            };
        }
        return { type: "Polygon", coordinates: geometry.coordinates.map(move) };
    }

    // -- hit testing -----------------------------------------------------

    /**
     * Even-odd ray casting, which handles holes for free: a point inside an
     * interior ring crosses the outer boundary once and the hole once, an even
     * number, and so reads as outside -- which it is.
     */
    function containsPoint(geometry, x, y) {
        for (const polygon of polygons(geometry)) {
            let inside = false;
            for (const ring of polygon) {
                const points = openRing(ring);
                for (let i = 0, j = points.length - 1; i < points.length; j = i++) {
                    const [xi, yi] = points[i];
                    const [xj, yj] = points[j];
                    if ((yi > y) !== (yj > y)
                        && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) {
                        inside = !inside;
                    }
                }
            }
            if (inside) return true;
        }
        return false;
    }

    function distanceToSegment(px, py, ax, ay, bx, by) {
        const dx = bx - ax;
        const dy = by - ay;
        const lengthSquared = dx * dx + dy * dy;
        let t = lengthSquared === 0 ? 0 : ((px - ax) * dx + (py - ay) * dy) / lengthSquared;
        t = Math.max(0, Math.min(1, t));
        const cx = ax + t * dx;
        const cy = ay + t * dy;
        return Math.hypot(px - cx, py - cy);
    }

    /** Shortest distance from a point to any edge -- what makes a thin sliver
     *  or a shape the cursor is just outside of still clickable. */
    function distanceToBoundary(geometry, x, y) {
        let best = Infinity;
        for (const ring of rings(geometry)) {
            const points = closeRing(ring);
            for (let i = 0; i + 1 < points.length; i++) {
                const distance = distanceToSegment(
                    x, y, points[i][0], points[i][1], points[i + 1][0], points[i + 1][1]);
                if (distance < best) best = distance;
            }
        }
        return best;
    }

    /** The vertex nearest (x, y) within `tolerance`, or null.
     *
     * Reported as {ring, index} so a caller can put it back where it came from;
     * only single-ring polygons are offered for editing (see isVertexEditable),
     * but the index is ring-qualified anyway so that stays true if that changes. */
    function nearestVertex(geometry, x, y, tolerance) {
        let best = null;
        const list = rings(geometry);
        for (let r = 0; r < list.length; r++) {
            const points = openRing(list[r]);
            for (let i = 0; i < points.length; i++) {
                const distance = Math.hypot(points[i][0] - x, points[i][1] - y);
                if (distance <= tolerance && (!best || distance < best.distance)) {
                    best = { ring: r, index: i, distance };
                }
            }
        }
        return best;
    }

    // -- simplification --------------------------------------------------

    /**
     * Ramer-Douglas-Peucker.
     *
     * Freehand drawing produces one point per pointer event -- thousands for a
     * single region, most of them describing sub-pixel wobble in a straight
     * line. Stored raw they make every redraw, every hit test and every save
     * proportional to how slowly the user moved the mouse.
     *
     * Run ONCE, when the stroke is completed, and never again. Simplification
     * is lossy, so re-running it on each subsequent edit would walk the shape
     * away from what was drawn a little at a time -- a shape nobody touched
     * would keep changing.
     *
     * Iterative rather than recursive: a 100,000-point trace recurses deeply
     * enough to blow the stack, and losing the whole shape to a RangeError at
     * the moment of completion is the worst possible time for it.
     */
    function simplify(points, epsilon) {
        if (points.length <= 2 || !(epsilon > 0)) return points.slice();

        const keep = new Uint8Array(points.length);
        keep[0] = 1;
        keep[points.length - 1] = 1;

        const stack = [[0, points.length - 1]];
        while (stack.length) {
            const [first, last] = stack.pop();
            if (last <= first + 1) continue;

            let farthest = -1;
            let farthestDistance = 0;
            for (let i = first + 1; i < last; i++) {
                const distance = distanceToSegment(
                    points[i][0], points[i][1],
                    points[first][0], points[first][1],
                    points[last][0], points[last][1]);
                if (distance > farthestDistance) {
                    farthestDistance = distance;
                    farthest = i;
                }
            }

            if (farthestDistance > epsilon && farthest > 0) {
                keep[farthest] = 1;
                stack.push([first, farthest], [farthest, last]);
            }
        }

        const out = [];
        for (let i = 0; i < points.length; i++) if (keep[i]) out.push(points[i]);
        return out;
    }

    // -- validity --------------------------------------------------------

    /** Above this, the O(n^2) pair check below is skipped rather than run.
     *
     * The check is advisory -- it decides how a shape is outlined, not whether
     * it is stored -- so declining to answer for a very large geometry is a
     * fair trade against freezing the tab for several seconds mid-draw.
     * Freehand strokes are simplified before this runs, so in practice the
     * shapes that reach it are well under the limit; imported million-vertex
     * contours are the case this protects against. */
    const MAX_INTERSECTION_CHECK = 3000;

    /**
     * Does this ring cross itself?
     *
     * Reported, never repaired. `make_valid` and its equivalents turn a bow-tie
     * into two triangles, which is a different annotation from the one the user
     * drew -- and doing that invisibly, to a shape somebody is going to publish
     * a measurement from, is not a repair. The renderer draws the outline
     * differently and the user decides.
     */
    function selfIntersects(ring) {
        const points = openRing(ring);
        const n = points.length;
        if (n < 4 || n > MAX_INTERSECTION_CHECK) return false;

        for (let i = 0; i < n; i++) {
            const a1 = points[i];
            const a2 = points[(i + 1) % n];
            for (let j = i + 1; j < n; j++) {
                // Segments sharing an endpoint always "touch"; only genuinely
                // disjoint pairs count as a crossing.
                if (j === i || (j + 1) % n === i || j === (i + 1) % n) continue;
                const b1 = points[j];
                const b2 = points[(j + 1) % n];
                if (segmentsCross(a1, a2, b1, b2)) return true;
            }
        }
        return false;
    }

    function orientation(p, q, r) {
        const value = (q[1] - p[1]) * (r[0] - q[0]) - (q[0] - p[0]) * (r[1] - q[1]);
        if (value === 0) return 0;
        return value > 0 ? 1 : 2;
    }

    function onSegment(p, q, r) {
        return q[0] <= Math.max(p[0], r[0]) && q[0] >= Math.min(p[0], r[0])
            && q[1] <= Math.max(p[1], r[1]) && q[1] >= Math.min(p[1], r[1]);
    }

    function segmentsCross(p1, q1, p2, q2) {
        const o1 = orientation(p1, q1, p2);
        const o2 = orientation(p1, q1, q2);
        const o3 = orientation(p2, q2, p1);
        const o4 = orientation(p2, q2, q1);
        if (o1 !== o2 && o3 !== o4) return true;
        if (o1 === 0 && onSegment(p1, p2, q1)) return true;
        if (o2 === 0 && onSegment(p1, q2, q1)) return true;
        if (o3 === 0 && onSegment(p2, p1, q2)) return true;
        if (o4 === 0 && onSegment(p2, q1, q2)) return true;
        return false;
    }

    /** Keep a point inside the image. Drawing tools clamp as the pointer moves,
     *  so a shape cannot be given coordinates that name no pixel; imported
     *  geometry is never clamped, because a region that genuinely continues off
     *  the edge of the slide is a real thing to have recorded. */
    function clamp(point, width, height) {
        const x = width ? Math.min(Math.max(point[0], 0), width) : point[0];
        const y = height ? Math.min(Math.max(point[1], 0), height) : point[1];
        return [x, y];
    }

    return Object.freeze({
        dedupe, distinctCount, closeRing, openRing, signedArea, area,
        polygonFrom, rings, polygons, vertexCount, isVertexEditable, bounds, translate,
        containsPoint, distanceToSegment, distanceToBoundary, nearestVertex,
        simplify, selfIntersects, segmentsCross, clamp,
        MAX_INTERSECTION_CHECK,
    });
})();
