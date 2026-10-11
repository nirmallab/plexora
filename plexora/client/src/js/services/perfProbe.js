/**
 * perfProbe.js - where the 3-D viewer's time goes, by surface and revision.
 *
 * The plane, the section panes and the 3-D view each say when they started
 * on a navigation revision, when they were ready, when they committed and when
 * they first painted it (`mark("xz", "committed", rev)`); a few counters say
 * how often the expensive things happened (shader compiles, decodes, level
 * fetches). `window.__plexora.perf` exposes them to the benchmark
 * (tools/bench3d) and to anyone in the console:
 *
 *   __plexora.perf.timeline(rev)   every surface's marks for one revision
 *   __plexora.perf.counters()      the counters since the page opened
 *   __plexora.perf.memory()        what each cache holds, in bytes
 *
 * Cheap enough to leave on: a mark is an array push into a bounded ring and a
 * counter is an object increment. The marks are mirrored into the browser's
 * performance timeline (`plx:<surface>:<name>:<rev>`) only when a profiler or
 * the benchmark asks (`?perf=1`, or `PlexoraViewPerf.mirror = true`), so a long
 * session does not grow the user-timing buffer. A planar image makes no marks
 * at all.
 */
(function (global) {
    "use strict";

    const LIMIT = 4000;
    const now = () => (global.performance?.now ? global.performance.now() : Date.now());

    const marks = [];
    const counters = Object.create(null);
    const providers = new Map();

    let mirror = false;
    try {
        mirror = /[?&]perf=1\b/.test(global.location?.search || "") || Boolean(global.__bench);
    } catch (error) {
        mirror = false;
    }

    const PlexoraViewPerf = {
        //: Whether marks also go to the browser's performance timeline.
        get mirror() { return mirror; },
        set mirror(value) { mirror = Boolean(value); },

        /** Record that `surface` reached `name` for revision `rev`. */
        mark(surface, name, rev = null, extra = null) {
            const at = now();
            marks.push({ at, surface, name, rev, extra });
            if (marks.length > LIMIT) marks.splice(0, marks.length - LIMIT);
            if (mirror) {
                try {
                    global.performance?.mark?.(`plx:${surface}:${name}:${rev ?? ""}`);
                } catch (error) {
                    // A mark name the browser refuses: the ring still has it.
                }
            }
            return at;
        },

        count(name, n = 1) {
            counters[name] = (counters[name] || 0) + n;
        },

        marks() {
            return marks.slice();
        },

        /** One revision's marks, by surface: {main: {requested: t, ...}, ...}. */
        timeline(rev) {
            const out = {};
            for (const entry of marks) {
                if (entry.rev !== rev) continue;
                (out[entry.surface] ||= {})[entry.name] ??= entry.at;
            }
            return out;
        },

        counters() {
            return { ...counters };
        },

        /** A cache reports its size here: `provide("volumeTextures", () => bytes)`. */
        provide(name, fn) {
            providers.set(name, fn);
        },

        memory() {
            const out = {};
            for (const [name, fn] of providers) {
                try {
                    out[name] = fn();
                } catch (error) {
                    out[name] = null;
                }
            }
            if (global.performance?.memory) out.heap = global.performance.memory.usedJSHeapSize;
            return out;
        },

        reset() {
            marks.length = 0;
            for (const key of Object.keys(counters)) delete counters[key];
        },
    };

    global.PlexoraViewPerf = PlexoraViewPerf;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = PlexoraViewPerf;
    }
})(typeof window !== "undefined" ? window : globalThis);
