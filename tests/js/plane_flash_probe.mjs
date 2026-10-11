/**
 * A plane step never shows a frame brighter or darker than both planes.
 *
 * The bug this pins: the plane fade held a copy of OSD's drawer canvas and
 * faded it out over the next plane. That canvas is transparent wherever a
 * channel is dim -- each channel is drawn with alpha = its coverage, and the
 * black (or a slide's off-white) behind it is CSS on the container, not pixels
 * -- so mid-fade the screen showed o*old + (1 - o*alpha_old)*new: the next
 * plane bled through under the old one and the sum overshot both. A step
 * between two mid-grey planes went brighter, then "corrected itself".
 *
 * views/planeMotion.js runs here against canvases that really composite: each
 * fake canvas holds one premultiplied RGBA pixel, and fillRect / drawImage /
 * globalAlpha / resizing behave as a 2-D context's source-over does. The
 * screen is then the browser's own sum: ghost at its CSS opacity over the
 * canvas over the ground. For every pair of planes, every point of the fade
 * must lie between them, and a step taken mid-fade must not jump.
 *
 * Run directly:  node tests/js/plane_flash_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const JS = join(REPO, "plexora/client/src/js");

const failures = [];
let checks = 0;
function check(label, ok, why) {
    checks += 1;
    console.log(`${ok ? "ok" : "FAIL"} - ${label}`);
    if (!ok) failures.push(`${label}${why ? ` -- ${why}` : ""}`);
}

// -- one-pixel canvases that composite ------------------------------------------

const clear = () => ({ r: 0, g: 0, b: 0, a: 0 });
function parse(colour) {
    const m = String(colour).match(/rgba?\(([^)]+)\)/);
    const [r, g, b, a = 1] = m[1].split(",").map((v) => Number(v.trim()));
    return { r: (r / 255) * a, g: (g / 255) * a, b: (b / 255) * a, a };
}
function over(src, dst, alpha = 1) {
    const s = { r: src.r * alpha, g: src.g * alpha, b: src.b * alpha, a: src.a * alpha };
    return { r: s.r + dst.r * (1 - s.a), g: s.g + dst.g * (1 - s.a),
             b: s.b + dst.b * (1 - s.a), a: s.a + dst.a * (1 - s.a) };
}
const lum = (p) => 0.2126 * p.r + 0.7152 * p.g + 0.0722 * p.b;

function canvas(log = null) {
    let width = 0;
    let height = 0;
    const element = {
        tagName: "CANVAS",
        pixel: clear(),
        hidden: false,
        isConnected: true,
        style: {},
        classList: { contains: () => false },
        setAttribute() {},
        get width() { return width; },
        set width(v) { width = v; element.pixel = clear(); },   // resizing clears
        get height() { return height; },
        set height(v) { height = v; element.pixel = clear(); },
        after(node) { element.nextSibling = node; },
        animate(keyframes, options) {
            const animation = { keyframes, options, cancel() { animation.cancelled = true; } };
            element._animation = animation;
            return animation;
        },
        getContext() {
            return {
                globalAlpha: 1,
                filter: "none",
                fillStyle: "rgb(0, 0, 0)",
                fillRect() {
                    log?.push({ canvas: element, op: "fill", alpha: this.globalAlpha });
                    element.pixel = over(parse(this.fillStyle), element.pixel, this.globalAlpha);
                },
                drawImage(image) {
                    log?.push({ canvas: element, op: "draw", image, alpha: this.globalAlpha });
                    element.pixel = over(image.pixel, element.pixel, this.globalAlpha);
                },
                clearRect() {
                    log?.push({ canvas: element, op: "clear" });
                    element.pixel = clear();
                },
            };
        },
    };
    return element;
}

function setup({ ground = "rgb(0, 0, 0)" } = {}) {
    const log = [];
    const box = {
        console,
        performance: { now: () => 0 },
        requestAnimationFrame: () => 0,
        setTimeout: () => 0,
        clearTimeout: () => {},
        addEventListener: () => {},
        getComputedStyle: (element) => ({
            opacity: element._opacity ?? "1",
            backgroundColor: element._background ?? "rgba(0, 0, 0, 0)",
            filter: "none",
        }),
        document: { createElement: () => canvas(log) },
    };
    box.window = box;
    runInContext(readFileSync(join(JS, "views/planeMotion.js"), "utf8"), createContext(box));
    const source = canvas(log);
    source.width = 4;
    source.height = 4;
    const host = { _background: ground, parentElement: null };
    source.parentElement = { _background: "rgba(0, 0, 0, 0)", parentElement: host };
    const viewer = {
        drawer: { canvas: source },
        addHandler() {},
        viewport: { getBounds: () => ({ x: 0, y: 0, width: 1, height: 1 }),
                    getContainerSize: () => ({ x: 4, y: 4 }), getRotation: () => 0,
                    getFlip: () => false },
    };
    const fade = new box.PlexoraPlaneMotion.PlaneFade(viewer, { prefs: { smoothMotion: () => true } });
    return { box, fade, source, log, ground: parse(ground) };
}

/** What the screen shows: the ghost at opacity o over the canvas over the ground. */
function screen({ fade, source, ground }, o) {
    const under = over(source.pixel, ground);
    const ghost = fade.ghost;
    if (!ghost || ghost.hidden || o <= 0) return under;
    return over(ghost.pixel, under, o);
}

/** A channel drawn at coverage `a` in colour `rgb` (0..1): premultiplied. */
function plane(a, rgb = [1, 1, 1]) {
    return { r: rgb[0] * a, g: rgb[1] * a, b: rgb[2] * a, a };
}

const LEVELS = [0, 0.1, 0.25, 0.5, 0.75, 0.9, 1];
const OPACITY = Array.from({ length: 21 }, (_, i) => 1 - i / 20);

// -- every pair of planes, every point of the fade, over black --------------------

{
    let worst = 0;
    let pairs = 0;
    for (const a of LEVELS) {
        for (const b of LEVELS) {
            const env = setup();
            env.source.pixel = plane(a);
            const before = lum(screen(env, 0));
            const held = env.fade.capture();
            env.source.pixel = plane(b);           // the swap: the new plane underneath
            env.fade.release();
            const after = lum(screen(env, 0));
            const lo = Math.min(before, after);
            const hi = Math.max(before, after);
            for (const o of OPACITY) {
                const value = lum(screen(env, o));
                worst = Math.max(worst, value - hi, lo - value);
            }
            if (held) pairs += 1;
        }
    }
    check("every step between two planes stays between them, at every point of the fade",
        worst <= 1e-9, `worst excursion ${worst.toFixed(4)} of full scale`);
    check("and every one of those steps did fade", pairs === LEVELS.length ** 2);
}

// -- the old way, as a control: a translucent copy overshoots ----------------------------

{
    const old = (a, b, o) => {
        const g = { r: 0, g: 0, b: 0, a: 1 };
        const under = over(plane(b), g);
        return lum(over(plane(a), under, o));     // the canvas copied as it is, alpha and all
    };
    const peak = Math.max(...OPACITY.map((o) => old(0.25, 0.75, o)));
    check("control: a translucent copy of the canvas overshoots a 25 % -> 75 % step",
        peak > 0.75 + 0.05, `peak ${peak.toFixed(3)} against 0.75`);
}

// -- the picture is the ground, then the canvas: on both paths ---------------------------

{
    const env = setup();
    env.source.pixel = plane(0.5);
    env.fade.capture();
    const picture = env.fade.picture;
    const onPicture = env.log.filter((e) => e.canvas === picture);
    check("the held picture starts with an opaque fill of the ground",
        onPicture[0]?.op === "fill" && onPicture[0].alpha === 1
        && onPicture[1]?.op === "draw" && onPicture[1].image === env.source);
    check("and the ghost is opaque", Math.abs(env.fade.ghost.pixel.a - 1) < 1e-12);

    // A step mid-fade: 40 % of the way through the first fade.
    env.source.pixel = plane(0.9);
    env.fade.release();
    env.fade.ghost._opacity = "0.6";
    env.fade.animation = env.fade.ghost._animation;
    const shownBefore = lum(screen(env, 0.6));
    env.log.length = 0;
    env.fade.capture();
    const recapture = env.log.filter((e) => e.canvas === env.fade.picture);
    check("a step mid-fade composes the ground again before blending",
        recapture[0]?.op === "fill" && recapture[1]?.op === "draw");
    const shownAfter = lum(screen(env, 1));
    check("and holds exactly what was on screen: no jump back to a plane already left",
        Math.abs(shownBefore - shownAfter) < 1e-9,
        `${shownBefore.toFixed(4)} on screen, ${shownAfter.toFixed(4)} held`);
    // the next plane under it: still between the mix and the new plane
    env.source.pixel = plane(0.1);
    env.fade.release();
    const target = lum(screen(env, 0));
    let worst = 0;
    for (const o of OPACITY) {
        const value = lum(screen(env, o));
        worst = Math.max(worst, value - Math.max(shownAfter, target), Math.min(shownAfter, target) - value);
    }
    check("and the fade from that mix stays between it and the next plane", worst <= 1e-9);
}

// -- a slide's off-white ground, a coloured channel ---------------------------------

{
    let worst = 0;
    for (const a of LEVELS) {
        for (const b of LEVELS) {
            const env = setup({ ground: "rgb(251, 251, 252)" });
            env.source.pixel = plane(a, [0.55, 0.25, 0.6]);
            const before = lum(screen(env, 0));
            env.fade.capture();
            env.source.pixel = plane(b, [0.55, 0.25, 0.6]);
            env.fade.release();
            const after = lum(screen(env, 0));
            for (const o of OPACITY) {
                const value = lum(screen(env, o));
                worst = Math.max(worst, value - Math.max(before, after), Math.min(before, after) - value);
            }
        }
    }
    check("over an off-white ground too", worst <= 1e-9, `worst ${worst.toFixed(4)}`);
}

// -- no ground, no fade ------------------------------------------------------------

{
    const env = setup({ ground: "rgba(0, 0, 0, 0)" });
    env.source.pixel = plane(0.5);
    check("with no ground to compose over, there is no fade (a cut, not a wrong blend)",
        env.fade.capture() === false && !env.fade.ghost);
}

if (failures.length) {
    console.log(`\n${failures.length} of ${checks} checks FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log(`\nall ${checks} checks passed`);
