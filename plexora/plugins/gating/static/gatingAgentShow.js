/**
 * gatingAgentShow.js - an agent's look at a marker, played in the tab the way
 * a person would make it.
 *
 * `preview_gate` with a `show` (the server's mirror_script._show) is claimed
 * by gatingAgentBridge.js, which acknowledges it at once and hands it here, so
 * the session goes on while this plays. The gate reaches the agent's value the
 * way a hand gets it there -- too strict or too loose first, then back -- while
 * the view flies to the cells being read, closes in on one, pulls out a little
 * and moves on. One thing at a time, with pauses between: one person, one
 * mouse. Then, until the next look arrives, an occasional small move of the
 * view (a drift to another cell, a breath in or out), further apart as time
 * goes on -- never of the gate: a nudge of it and back read as the marker
 * being gated again.
 *
 * Slow enough to watch the cells change: the server flies the view to where
 * the marker's medium cells are (mirror_script._medium_focus), the cells the
 * candidate gates disagree on, and a drag takes seconds, not a flick. `play`
 * says how long the look's own moves take (`ms`), and the session holds its
 * next look until they are done.
 *
 * Varied, so a run of markers does not look like one gesture repeated: the
 * marker's seed picks its temperament (which way it errs first) and how it
 * moves the view, and every distance, duration and pause is drawn from a
 * generator seeded by the marker and the look. A marker's first look explores;
 * its later looks refine from wherever the slider already is.
 *
 * `plan` is pure (gating_agent_probe.mjs pins it); `play` runs a plan. Every
 * frame of the gate is a preview through the bridge's `paint`, never saved. A
 * newer look takes over from wherever this one has got to. A hand on the gate
 * controls ends the show; a hand on the canvas leaves the view alone for
 * QUIET_MS while the gate goes on. With smooth motion off, the gate and the
 * field are set at once.
 */
(function () {
    //: The beats' durations at the natural pace, in ms, each jittered by
    //: JITTER either way: flying to the field, closing in on a cell, pulling
    //: out a little, hopping to another cell, a slow drift while waiting, a
    //: breath in or out, a pause to look, a hold after a gate move.
    const TIMING = { field: 2900, close: 3100, out: 2400, hop: 3300, drift: 4200,
                     breathe: 3200, think: 1100, hold: 1700 };
    const JITTER = 0.2;
    //: How close is close: the width of image the view shows, image pixels --
    //: a few cells and their neighbours, so a group is seen to change.
    const CLOSE_SPAN = [260, 420];
    //: The whole field is seen at least this wide, so a survey shows the
    //: neighbourhood (the server's field hugs a few cells).
    const FIELD_SPAN = 700;
    //: "Out a bit": that width times this.
    const OUT_FACTOR = [2.0, 3.2];
    //: Never closer than this many screen pixels to an image pixel.
    const MAX_SCALE = 8;
    //: A drag of the gate: `base` plus `per` times the square root of the share
    //: of the slider it crosses, within [min, max] ms.
    const DRAG_MS = { base: 900, per: 3800, min: 900, max: 3400 };
    //: The small moves while the agent reads: how many, and the pause before
    //: each (it grows by IDLE_GROWTH a beat).
    const IDLE_BEATS = 4;
    const IDLE_PAUSE = [3500, 7000];
    const IDLE_GROWTH = 0.35;
    //: After a hand on the canvas, the view is left alone this long.
    const QUIET_MS = 12000;
    //: Which way a marker errs first: too strict then back a little under;
    //: too loose then a little over; strict, loose, then between; or two
    //: careful steps that go a touch past.
    const TEMPERS = ["strict_first", "loose_first", "bracket", "creep"];
    //: How it moves the view: the whole field first; straight in on a cell;
    //: from one cell to another.
    const CAMERAS = ["survey", "dive", "hop"];

    /** mulberry32: a small generator, the same numbers for the same seed. */
    function generator(seed) {
        let a = seed >>> 0;
        return () => {
            a = (a + 0x6D2B79F5) >>> 0;
            let t = a;
            t = Math.imul(t ^ (t >>> 15), t | 1);
            t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
            return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
        };
    }

    const EASES = {
        inOut: (f) => (f < 0.5 ? 4 * f * f * f : 1 - ((-2 * f + 2) ** 3) / 2),
        out: (f) => 1 - (1 - f) ** 3,
        // A touch past the mark and back, as a hand lands.
        land: (f) => 1 + 1.6 * (f - 1) ** 3 + 0.6 * (f - 1) ** 2,
    };

    /**
     * The beats of one look: {beats, idle}, each a list of
     *   {kind: "pause", ms}
     *   {kind: "gate", low, ms, ease}                  the slider's low end
     *   {kind: "glide", x, y, span, ms}                centre and width, image px
     *   {kind: "glide", x, y, keep, ms}                the width now, times `keep`
     *   {kind: "zoom", factor, ms}                     about the centre now
     * from `spec` = {seed, look, target: [low, high], from (the slider's low
     * now), range: [min, max], stops: [candidate gates], cells: [{x, y}],
     * field: {x, y, width, height} | null}. The beats always leave the gate at
     * the target; the idle ones never move it.
     */
    function plan(spec) {
        const look = Math.max(1, Math.floor(Number(spec.look) || 1));
        const seed = Number(spec.seed) >>> 0;
        const random = generator(seed ^ Math.imul(look, 0x9E3779B1));
        const between = (lo, hi) => lo + (hi - lo) * random();
        const jitter = (ms) => Math.round(ms * between(1 - JITTER, 1 + JITTER));
        const [min, max] = spec.range.map(Number);
        const [target, high] = spec.target.map(Number);
        const width = Math.max(max - min, Number.EPSILON);
        const temper = TEMPERS[seed % TEMPERS.length];
        const camera = CAMERAS[Math.floor(seed / TEMPERS.length) % CAMERAS.length];
        const fit = (value) => Math.min(Math.max(value, min), high - width * 0.002);
        // Further from the gate: geometric above it where the values are
        // counts (a step that means something on a skewed axis), linear on a
        // signed one; linear below it either way.
        const up = (r) => fit(min >= 0 && target > 0 && max > target
            ? target * (max / target) ** r : target + (max - target) * r);
        const down = (r) => fit(target - (target - min) * r);
        const stops = (spec.stops || []).map(Number).filter(Number.isFinite);
        const above = stops.filter((v) => v > target).sort((a, b) => a - b);
        const below = stops.filter((v) => v < target).sort((a, b) => b - a);
        // A candidate gate where there is one, the far one as often as not --
        // the aggressive try -- never further than a warp of 0.45.
        const strict = fit(Math.min(up(0.45), above.length && random() < 0.8
            ? above[Math.min(above.length - 1, random() < 0.55 ? 1 : 0)] : up(between(0.15, 0.3))));
        const loose = fit(Math.max(down(0.6), below.length && random() < 0.8
            ? below[Math.min(below.length - 1, random() < 0.55 ? 1 : 0)] : down(between(0.25, 0.45))));

        const beats = [];
        let at = Number.isFinite(Number(spec.from)) ? Number(spec.from) : target;
        const pause = (key) => beats.push({ kind: "pause", ms: jitter(TIMING[key]) });
        const gate = (value, ease) => {
            const low = fit(value);
            const share = Math.abs(low - at) / width;
            const ms = Math.min(DRAG_MS.max, Math.max(DRAG_MS.min,
                jitter(DRAG_MS.base + DRAG_MS.per * Math.sqrt(share))));
            beats.push({ kind: "gate", low, ms, ease: ease || "inOut" });
            at = low;
        };

        const cells = (spec.cells || []).filter((c) => Number.isFinite(Number(c.x))
            && Number.isFinite(Number(c.y)));
        const field = spec.field && Number(spec.field.width) > 0 ? spec.field : null;
        const middle = field ? { x: field.x + field.width / 2, y: field.y + field.height / 2 } : null;
        const cellAt = (i) => (cells.length ? cells[((i % cells.length) + cells.length) % cells.length]
            : middle);
        const c0 = cellAt(look - 1);
        const c1 = cellAt(look);
        const close = between(...CLOSE_SPAN);
        const out = close * between(...OUT_FACTOR);
        const glide = (where, span, key) => {
            if (!where) return;
            beats.push({ kind: "glide", x: Number(where.x), y: Number(where.y), span,
                         ms: jitter(TIMING[key]) });
        };
        const fieldSpan = field ? Math.max(field.width, field.height, FIELD_SPAN) : null;
        const wide = () => glide(middle, fieldSpan, "field");

        if (look === 1) {
            // Two tries either side, then the gate; which first is the marker's.
            // Careful steps start from the loose side when the slider is
            // already on the gate, or there would be nothing to see.
            const creepFrom = Math.abs(target - at) / width < 0.02 ? loose : at;
            const tries = {
                strict_first: [strict, target - (target - loose) * between(0.1, 0.3)],
                loose_first: [loose, target + (strict - target) * between(0.2, 0.45)],
                bracket: [strict, target - (target - loose) * between(0.45, 0.7)],
                creep: [creepFrom + (target - creepFrom) * between(0.35, 0.6),
                        target + (strict - target) * between(0.08, 0.2)],
            }[temper];
            if (camera === "survey") {
                if (field) wide();
                pause("think");
                gate(tries[0]);
                pause("hold");
                glide(c0, close, "close");
                pause("think");
                gate(tries[1]);
                pause("hold");
                glide(c0, out, "out");
                pause("think");
            } else if (camera === "dive") {
                glide(c0, close, "close");
                pause("think");
                gate(tries[0]);
                pause("hold");
                gate(tries[1]);
                pause("think");
                glide(c0, out, "out");
                pause("think");
                glide(c1, close * between(1.2, 1.7), "hop");
                pause("think");
            } else {
                glide(c0, close, "close");
                pause("think");
                gate(tries[0]);
                pause("hold");
                glide(c1, close * between(0.9, 1.2), "hop");
                pause("think");
                gate(tries[1]);
                pause("hold");
                if (field) wide();
                else glide(c1, out, "out");
                pause("think");
            }
            gate(target, random() < 0.5 ? "land" : "out");
        } else {
            // A later look refines: perhaps another cell, then the gate from
            // where it is to where the reads now put it.
            if (random() < 0.7) {
                glide(cellAt(look + 1), between(close, out), "hop");
                pause("think");
            }
            const travel = Math.abs(target - at) / width;
            if (travel < 0.004) {
                const sign = random() < 0.5 ? -1 : 1;
                gate(target + sign * width * between(0.015, 0.04));
                pause("think");
            } else if (random() < 0.4) {
                gate(target + (target - at) * between(0.15, 0.35));
                pause("think");
            }
            gate(target, random() < 0.5 ? "land" : "out");
            if (random() < 0.4) {
                pause("hold");
                glide(cellAt(look + 1), out, "out");
            }
        }
        pause("hold");

        const idle = [];
        for (let i = 0; i < IDLE_BEATS; i += 1) {
            idle.push({ kind: "pause",
                        ms: jitter(between(...IDLE_PAUSE) * (1 + i * IDLE_GROWTH)) });
            // The view only: the gate stays where the look left it.
            const choice = random();
            if (choice < 0.45 && cells.length > 1) {
                const where = cellAt(look + 2 + i);
                idle.push({ kind: "glide", x: Number(where.x), y: Number(where.y),
                            keep: between(0.9, 1.15), ms: jitter(TIMING.drift) });
            } else if (choice < 0.75) {
                idle.push({ kind: "zoom", factor: [0.8, 0.9, 1.12, 1.25][Math.floor(random() * 4)],
                            ms: jitter(TIMING.breathe) });
            }
        }
        if (field) {
            idle.push({ kind: "pause", ms: jitter(IDLE_PAUSE[1]) });
            idle.push({ kind: "glide", x: middle.x, y: middle.y, span: fieldSpan,
                        ms: jitter(TIMING.field) });
        }
        return { beats, idle, temper, camera, ms: lasts(beats) };
    }

    /** How long `beats` take to play, ms: a glide to somewhere off screen
     *  goes out and back in first (`move`), so it is counted at its longest. */
    function lasts(beats) {
        return Math.round(beats.reduce(
            (sum, beat) => sum + beat.ms * (beat.kind === "glide" ? 1.55 : 1), 0));
    }

    // -- the player -----------------------------------------------------------

    let current = null;
    let quietUntil = 0;

    function core() {
        return window.__plexora || {};
    }

    function scene() {
        return window.PlexoraViewerScene || null;
    }

    function imageViewer() {
        const found = core().seaDragonViewer;
        return found && found.viewer ? found : null;
    }

    function smooth() {
        return globalThis.PlexoraChannelFade?.smooth?.() ?? true;
    }

    function now() {
        return globalThis.performance?.now?.() ?? Date.now();
    }

    function alive(run) {
        return current === run && !run.stopped && run.live.gateMarker === run.marker;
    }

    function wait(run, ms) {
        return new Promise((resolve) => {
            const timer = setTimeout(done, Math.max(0, ms));
            function done() {
                clearTimeout(timer);
                run.wake = null;
                resolve();
            }
            run.wake = done;
        });
    }

    function frame() {
        return new Promise((resolve) => {
            const next = typeof requestAnimationFrame === "function" ? requestAnimationFrame
                : (fn) => setTimeout(fn, 16);
            next(resolve);
        });
    }

    /** The slider's low end from where it is to `beat.low`, one paint a frame. */
    async function drag(run, beat) {
        const from = run.at;
        const ease = EASES[beat.ease] || EASES.inOut;
        const started = now();
        for (;;) {
            if (!alive(run)) return;
            const f = Math.min(1, (now() - started) / Math.max(1, beat.ms));
            run.at = from + (beat.low - from) * ease(f);
            run.paint(run.at);
            if (f >= 1) break;
            await frame();
        }
        run.at = beat.low;
    }

    /** What the view shows now: its centre and the narrower of its sides,
     *  image pixels, and the container's narrower side, screen pixels. */
    function viewNow(viewer) {
        const box = scene().currentViewport(viewer);
        const container = viewer.viewer.viewport.getContainerSize();
        if (![box.x, box.y, box.w, box.h].every(Number.isFinite)) return null;
        return { x: box.x + box.w / 2, y: box.y + box.h / 2, span: Math.min(box.w, box.h),
                 screen: Math.min(container.x, container.y) };
    }

    /**
     * A camera beat. A target off screen is reached the way a person gets
     * there -- out far enough to see both, then in -- not by sliding across
     * at full magnification.
     */
    async function move(run, beat) {
        const viewer = imageViewer();
        if (!viewer || !scene()) return;
        const here = viewNow(viewer);
        if (!here) return;
        const toScale = (span) => Math.min(MAX_SCALE, here.screen / Math.max(1, span));
        if (beat.kind === "zoom") {
            await scene().glideTo(viewer, { x: here.x, y: here.y,
                                            scale: toScale(here.span / beat.factor) },
                                  { ms: beat.ms });
            return;
        }
        const span = beat.span || here.span * (beat.keep || 1);
        const distance = Math.hypot(beat.x - here.x, beat.y - here.y);
        if (distance > 0.75 * Math.max(here.span, span)) {
            const apart = Math.max(here.span, span, distance * 1.5);
            const arrived = await scene().glideTo(viewer, {
                x: (beat.x + here.x) / 2, y: (beat.y + here.y) / 2, scale: toScale(apart),
            }, { ms: Math.round(beat.ms * 0.55) });
            if (!arrived || !alive(run) || !run.camera) return;
        }
        await scene().glideTo(viewer, { x: beat.x, y: beat.y, scale: toScale(span) },
                              { ms: beat.ms });
    }

    async function perform(run, beats) {
        for (const beat of beats) {
            if (!alive(run)) return;
            if (beat.kind === "pause") await wait(run, beat.ms);
            else if (beat.kind === "gate") await drag(run, beat);
            else if (run.camera && now() >= quietUntil) await move(run, beat);
        }
    }

    // A hand on the canvas: the view is the user's for a while, the gate goes
    // on. A hand on the gate: the show is over, and what they set stands.
    // Listened for once, for every show (a show that ended must not take a
    // newer one's listeners with it), and only acted on while one plays.
    function onCanvas() {
        if (!current) return;
        quietUntil = now() + QUIET_MS;
        current.camera = false;
        const viewer = imageViewer();
        if (viewer) scene()?.stopGlide?.(viewer);
    }

    function onPanel(event) {
        const panel = document.getElementById("gate_marker_section");
        if (!current || !panel || !panel.contains(event.target)) return;
        if (event.target.closest?.("#gating_agent_pill")) return;
        stop();
    }

    function listen() {
        const viewer = imageViewer();
        const osd = viewer && viewer.viewer;
        if (osd && typeof osd.addHandler === "function" && !osd.__plexoraGatingShow) {
            osd.__plexoraGatingShow = true;
            for (const name of ["canvas-press", "canvas-scroll", "canvas-key"]) {
                osd.addHandler(name, onCanvas);
            }
        }
    }

    document.addEventListener?.("pointerdown", onPanel, true);
    document.addEventListener?.("keydown", onPanel, true);

    /**
     * Play a look for `marker` on `live` (the gating controller): `paint(low)`
     * previews the gate's low end, `from` is where it is now, `range` the
     * slider's ends, `target` [low, high] the agent's gate, `show` the
     * server's. Returns at once, with how long the look's moves take (`ms`,
     * before the idle ones); the show runs on its own.
     */
    function play({ live, marker, paint, from, range, target, show }) {
        stop();
        const spec = { seed: show.seed, look: show.look, target, from, range,
                       stops: show.stops, cells: show.cells, field: show.field };
        const run = { live, marker, paint, at: Number.isFinite(from) ? from : target[0],
                      camera: now() >= quietUntil, stopped: false, wake: null };
        current = run;
        if (!smooth()) {
            paint(target[0]);
            const viewer = imageViewer();
            if (viewer && scene() && show.field && run.camera) {
                try {
                    scene().fitRegion(viewer, show.field);
                } catch (error) { /* a view that cannot be framed keeps its own */ }
            }
            current = null;
            return { played: false, temper: null };
        }
        const choreography = plan(spec);
        listen();
        (async () => {
            try {
                await perform(run, choreography.beats);
                if (alive(run)) run.paint(target[0]);
                await perform(run, choreography.idle);
            } catch (error) {
                console.warn("gating: the agent's look stopped", error);
            } finally {
                if (current === run) current = null;
            }
        })();
        return { played: true, temper: choreography.temper, camera: choreography.camera,
                 ms: choreography.ms };
    }

    /** End the show where it is (a newer look, a hand on the gate, the
     *  viewer given back). The gate stays wherever it was last painted. */
    function stop() {
        const run = current;
        if (!run) return;
        run.stopped = true;
        current = null;
        if (run.wake) run.wake();
    }

    /** Put the look's gate back on the slider where it had got to: re-reading
     *  the saved gates put the stored one in its place, and between two moves
     *  nothing else would paint it again. Nothing when no look is playing. */
    function repaint() {
        const run = current;
        if (run && alive(run)) run.paint(run.at);
    }

    window.PlexoraGatingShow = { play, stop, plan, repaint, playing: () => Boolean(current) };
})();
