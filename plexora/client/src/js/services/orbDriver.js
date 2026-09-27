/**
 * orbDriver.js -- the agent panel's "thinking" orb, drawn by the vendored
 * thinking-orbs engine (client/external/thinking-orbs-0.3.2/orbs.js, MIT).
 *
 * The engine is pure geometry plus a 2D-canvas painter: `resolvePreset(state,
 * size)` picks a mode and its tuning, `MODE_FRAMES[mode](size, t, opts)`
 * returns one frame's dots and lines, and `paintFrame` draws it. This file is
 * the ~60 lines the package's React component would otherwise be: a canvas at
 * the device pixel ratio (capped at 2), a rAF loop that stops while paused or
 * while the tab is hidden, and -- under `prefers-reduced-motion` -- one still
 * frame, redrawn only when the state changes.
 *
 * The engine is an ES module and this is a classic script, so it is fetched
 * with `import()` the first time an orb is mounted (26 kB, only when an agent
 * session starts). `configure({load})` swaps the loader: the probe runs this
 * file under `vm`, where a module cannot be imported.
 *
 * The chunk's exports are minified aliases (M, S, p, r); the long names are
 * accepted too, so a stub -- or a later version that exports them -- works.
 *
 *   window.PlexoraOrb.mount(canvas, {state, size, tint, dark})
 *       -> {setState, pause, resume, destroy}
 *   window.PlexoraOrb.configure({load})
 *   window.PlexoraOrb.isReady()
 *
 * A canvas whose engine could not load gets `data-orb="static"`, which
 * agentPanel.css paints as a plain dot: the panel never depends on the orb.
 */
window.PlexoraOrb = (function () {
    "use strict";

    const MODULE_PATH = "client/external/thinking-orbs-0.3.2/orbs.js";
    //: The sizes the engine has presets for; any other is snapped to the
    //: nearest (resolvePreset has nothing for, say, 24).
    const PRESET_SIZES = [20, 32, 64];
    //: The frame a reduced-motion orb stands still on (the package's own).
    const STILL_T = 0.6;

    let load = () => {
        const path = (typeof plexoraUrl === "function") ? plexoraUrl(MODULE_PATH) : "/" + MODULE_PATH;
        return import(path);
    };
    let engine = null;
    let pending = null;

    function adopt(module) {
        const m = module || {};
        const found = {
            MODE_FRAMES: m.MODE_FRAMES || m.M,
            STATE_TO_MODE: m.STATE_TO_MODE || m.S,
            paintFrame: m.paintFrame || m.p,
            resolvePreset: m.resolvePreset || m.r,
        };
        if (!found.MODE_FRAMES || !found.paintFrame || !found.resolvePreset) {
            throw new Error("the orb engine does not export what the driver draws with");
        }
        return found;
    }

    function ensure() {
        if (engine) return Promise.resolve(engine);
        if (!pending) {
            pending = Promise.resolve().then(() => load()).then((module) => {
                engine = adopt(module);
                return engine;
            }).catch((error) => {
                pending = null;
                throw error;
            });
        }
        return pending;
    }

    function reducedMotion() {
        try {
            return Boolean(window.matchMedia
                && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
        } catch (error) {
            return false;
        }
    }

    function hidden() {
        return typeof document !== "undefined" && document.visibilityState === "hidden";
    }

    function snap(size) {
        const wanted = Number(size) || 32;
        return PRESET_SIZES.reduce((best, s) => (Math.abs(s - wanted) < Math.abs(best - wanted) ? s : best));
    }

    /** "#rrggbb", "#rgb" or "rgb(r, g, b)" -> {r, g, b}; anything else -> undefined. */
    function parseTint(value) {
        if (!value) return undefined;
        if (typeof value === "object") return value;
        const text = String(value).trim();
        const hex = text.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
        if (hex) {
            let digits = hex[1];
            if (digits.length === 3) digits = digits.replace(/./g, (c) => c + c);
            const n = parseInt(digits, 16);
            return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
        }
        const fn = text.match(/^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)/i);
        if (fn) return { r: Number(fn[1]), g: Number(fn[2]), b: Number(fn[3]) };
        return undefined;
    }

    function mount(canvas, options = {}) {
        const size = snap(options.size);
        const dark = options.dark !== false;
        const tint = parseTint(options.tint);
        const dpr = Math.min(2, Number(window.devicePixelRatio) || 1);
        let state = options.state || "breathing";
        let paused = false;
        let destroyed = false;
        let raf = 0;
        let running = false;
        let ctx = null;
        let resolved = null;
        //: The state the still frame shows, under reduced motion: drawn once
        //: per state, not once per call.
        let stillFor = null;

        canvas.width = Math.round(size * dpr);
        canvas.height = Math.round(size * dpr);
        if (canvas.style) {
            canvas.style.width = `${size}px`;
            canvas.style.height = `${size}px`;
        }
        canvas.setAttribute?.("data-orb", "loading");
        canvas.setAttribute?.("aria-hidden", "true");

        function resolve() {
            const known = engine.STATE_TO_MODE ? engine.STATE_TO_MODE[state] : true;
            resolved = engine.resolvePreset(known ? state : "breathing", size);
        }

        function draw(t) {
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.clearRect(0, 0, size, size);
            engine.paintFrame(ctx, engine.MODE_FRAMES[resolved.mode](size, t, resolved.opts), dark, tint);
        }

        const now = () => (window.performance && window.performance.now ? window.performance.now() : Date.now());
        const tick = () => {
            if (!running) return;
            draw(now() / 1000 * resolved.speed);
            raf = window.requestAnimationFrame(tick);
        };

        function stop() {
            running = false;
            if (raf && window.cancelAnimationFrame) window.cancelAnimationFrame(raf);
            raf = 0;
        }

        /** Draw what the state calls for now: a still, a loop, or nothing new. */
        function refresh() {
            if (destroyed || !ctx) return;
            if (reducedMotion()) {
                stop();
                if (stillFor !== state) {
                    stillFor = state;
                    draw(STILL_T);
                }
                return;
            }
            if (paused || hidden()) {
                stop();
                return;
            }
            if (running) return;
            running = true;
            draw(now() / 1000 * resolved.speed);
            raf = window.requestAnimationFrame(tick);
        }

        const onVisibility = () => refresh();
        if (typeof document !== "undefined" && document.addEventListener) {
            document.addEventListener("visibilitychange", onVisibility);
        }

        ensure().then(() => {
            if (destroyed) return;
            ctx = canvas.getContext ? canvas.getContext("2d") : null;
            if (!ctx) throw new Error("no 2D canvas");
            resolve();
            canvas.setAttribute?.("data-orb", "live");
            refresh();
        }).catch((error) => {
            if (destroyed) return;
            canvas.setAttribute?.("data-orb", "static");
            console.warn("Plexora: the agent orb could not be drawn;", error && error.message ? error.message : error);
        });

        return {
            setState(next) {
                if (!next || next === state) return;
                state = String(next);
                if (!ctx) return;
                resolve();
                // A running loop picks the new mode up on its next frame; a
                // still one is redrawn now.
                if (reducedMotion()) refresh();
            },
            pause() {
                paused = true;
                stop();
            },
            resume() {
                paused = false;
                refresh();
            },
            destroy() {
                destroyed = true;
                stop();
                if (typeof document !== "undefined" && document.removeEventListener) {
                    document.removeEventListener("visibilitychange", onVisibility);
                }
            },
            /** The state it is drawing (the probe's question). */
            state: () => state,
        };
    }

    return {
        mount,
        configure(options = {}) {
            if (typeof options.load === "function") {
                load = options.load;
                engine = null;
                pending = null;
            }
        },
        isReady: () => Boolean(engine),
    };
})();
