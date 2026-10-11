/**
 * volumeCamera.js - which way the 3-D view looks at a stack, and how it turns.
 *
 * THE ORIENTATION IS ONE ROTATION, `q` (a unit quaternion), taking the image's
 * own axes to the screen's. Image axes are the stack's: x along a row, y down
 * the rows, z deeper into the stack -- the axes of the plane viewer and the
 * section panes. Screen axes are x right, y up, z toward the viewer. Nothing
 * here touches voxels: the box is drawn in physical proportion by the renderer
 * (its extent comes from the stack's spacing), and this only decides from
 * where it is seen.
 *
 * THE NAMED VIEWS match what the rest of Plexora shows for the same plane:
 *
 *     xy   looking down the stack (z into the screen), x right, y down --
 *          exactly the plane viewer's picture
 *     xz   from the side the rows end on (+y), x right, z down -- the XZ pane
 *     yz   from the right (+x), z right, y down -- the YZ pane beside the plane
 *     iso  a three-quarter view of the XY face, tipped toward the viewer
 *
 * TURNING. A free drag turns the stack about the screen's axes (a trackball:
 * the side under the pointer follows it). With an axis locked -- X, Y or Z,
 * the IMAGE's axis, so the lock means the same thing whichever way the stack
 * is facing -- a drag turns it about that axis only: across the axis's
 * on-screen direction when it lies in the screen, and round the stack's centre
 * when it points at the viewer, so the near side always follows the hand.
 */
(function (global) {
    "use strict";

    const AXES = { x: [1, 0, 0], y: [0, 1, 0], z: [0, 0, 1] };
    //: Radians turned per pixel dragged.
    const ROTATE_PER_PX = 0.008;
    //: Below this on-screen length an axis is "pointing at the viewer" and a
    //: locked drag turns it round the centre instead of across it.
    const FACING_LENGTH = 0.3;

    // -- quaternions [x, y, z, w] --------------------------------------------

    function normalize(q) {
        const length = Math.hypot(q[0], q[1], q[2], q[3]) || 1;
        return [q[0] / length, q[1] / length, q[2] / length, q[3] / length];
    }

    function axisAngle(axis, angle) {
        const length = Math.hypot(axis[0], axis[1], axis[2]);
        if (!length || !angle) return [0, 0, 0, 1];
        const s = Math.sin(angle / 2) / length;
        return [axis[0] * s, axis[1] * s, axis[2] * s, Math.cos(angle / 2)];
    }

    /** a then b, as one rotation: `multiply(b, a)` applies a first. */
    function multiply(a, b) {
        const [ax, ay, az, aw] = a;
        const [bx, by, bz, bw] = b;
        return [
            aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz,
        ];
    }

    function rotate(q, v) {
        const [x, y, z, w] = q;
        // v + 2 w (u x v) + 2 u x (u x v), u the vector part.
        const tx = 2 * (y * v[2] - z * v[1]);
        const ty = 2 * (z * v[0] - x * v[2]);
        const tz = 2 * (x * v[1] - y * v[0]);
        return [v[0] + w * tx + (y * tz - z * ty),
                v[1] + w * ty + (z * tx - x * tz),
                v[2] + w * tz + (x * ty - y * tx)];
    }

    /** The short way from a to b, `t` of the way. */
    function slerp(a, b, t) {
        let cos = a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3];
        let end = b;
        if (cos < 0) {
            cos = -cos;
            end = [-b[0], -b[1], -b[2], -b[3]];
        }
        if (cos > 0.9995) {
            return normalize(a.map((value, i) => value + (end[i] - value) * t));
        }
        const angle = Math.acos(Math.min(1, cos));
        const sin = Math.sin(angle);
        const wa = Math.sin((1 - t) * angle) / sin;
        const wb = Math.sin(t * angle) / sin;
        return a.map((value, i) => value * wa + end[i] * wb);
    }

    /** The angle between two orientations, in radians. */
    function angleBetween(a, b) {
        const cos = Math.abs(a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3]);
        return 2 * Math.acos(Math.min(1, cos));
    }

    /** The rotation as a column-major 4x4 matrix (WebGL's order). */
    function toMat4(q) {
        const [x, y, z, w] = q;
        return new Float32Array([
            1 - 2 * (y * y + z * z), 2 * (x * y + z * w), 2 * (x * z - y * w), 0,
            2 * (x * y - z * w), 1 - 2 * (x * x + z * z), 2 * (y * z + x * w), 0,
            2 * (x * z + y * w), 2 * (y * z - x * w), 1 - 2 * (x * x + y * y), 0,
            0, 0, 0, 1]);
    }

    // -- the named views -------------------------------------------------------

    const S = Math.SQRT1_2;
    const XY = [1, 0, 0, 0];                       // half a turn about x: y down, z away
    const VIEWS = {
        xy: XY,
        xz: [S, 0, 0, S],                          // a quarter turn about x: z down, +y faces us
        yz: [S, 0, S, 0],                          // half a turn about (x+z): z right, +x faces us
        // The XY face turned to show its first column's side (x = 0) and its
        // first row's (y = 0): the corner the earlier turntable opened on.
        iso: normalize(multiply(axisAngle([1, 0, 0], 0.55),
                                multiply(axisAngle([0, 1, 0], 0.6), XY))),
    };
    //: Which view an image axis's button gives: looking along it at the plane
    //: across it, as the section panes show that plane.
    const VIEW_OF_AXIS = { x: "yz", y: "xz", z: "xy" };

    /** The view along `axis` ("x" | "y" | "z"), or from its far side. */
    function viewAlong(axis, { reverse = false } = {}) {
        const base = VIEWS[VIEW_OF_AXIS[axis]] || VIEWS.xy;
        // From behind: half a turn about the screen's vertical.
        return reverse ? normalize(multiply(axisAngle([0, 1, 0], Math.PI), base)) : base;
    }

    // -- turning -----------------------------------------------------------

    /** The image axis `name` on screen: [x right, y up, z toward the viewer]. */
    function axisOnScreen(q, name) {
        return rotate(q, AXES[name]);
    }

    /** q turned by a free drag of (dx, dy) screen pixels: about the screen's
     *  axes, the side under the pointer following it. */
    function turnFree(q, dx, dy, perPx = ROTATE_PER_PX) {
        const length = Math.hypot(dx, dy);
        if (!length) return q;
        return normalize(multiply(axisAngle([dy, dx, 0], length * perPx), q));
    }

    /** q turned by `angle` about its own image axis `name`. */
    function turnAbout(q, name, angle) {
        if (!angle) return q;
        return normalize(multiply(q, axisAngle(AXES[name], angle)));
    }

    /**
     * How a locked drag maps to an angle, fixed when the drag begins so a
     * drag that turns the axis toward the viewer does not change rules
     * halfway. Returns `(dx, dy, pointer, centre) -> angle` in radians.
     */
    function lockedDrag(q, name, perPx = ROTATE_PER_PX) {
        const a = axisOnScreen(q, name);
        const length = Math.hypot(a[0], a[1]);
        if (length >= FACING_LENGTH) {
            // The near side of a turn about a moves along (a.y, a.x) on screen
            // (screen y runs down): dragging that way turns it positively.
            const ux = a[1] / length;
            const uy = a[0] / length;
            return { mode: "across", angle: (dx, dy) => (dx * ux + dy * uy) * perPx };
        }
        // Facing the viewer: the pointer's sweep round the stack's centre.
        const sign = a[2] >= 0 ? 1 : -1;
        return {
            mode: "round",
            angle: (dx, dy, pointer, centre) => {
                if (!pointer || !centre) return 0;
                const before = Math.atan2(pointer.y - dy - centre.y, pointer.x - dx - centre.x);
                const after = Math.atan2(pointer.y - centre.y, pointer.x - centre.x);
                let sweep = after - before;
                if (sweep > Math.PI) sweep -= 2 * Math.PI;
                if (sweep < -Math.PI) sweep += 2 * Math.PI;
                // Screen y runs down, so a sweep the eye calls anticlockwise
                // is negative here, and anticlockwise is +angle about +z.
                return -sweep * sign;
            },
        };
    }

    global.PlexoraVolumeCamera = {
        AXES, VIEWS, VIEW_OF_AXIS, ROTATE_PER_PX, FACING_LENGTH,
        quat: { normalize, axisAngle, multiply, rotate, slerp, angleBetween, toMat4 },
        viewAlong, axisOnScreen, turnFree, turnAbout, lockedDrag,
    };
    if (typeof module !== "undefined" && module.exports) {
        module.exports = global.PlexoraVolumeCamera;
    }
})(typeof window !== "undefined" ? window : globalThis);
