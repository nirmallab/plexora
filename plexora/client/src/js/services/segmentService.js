/**
 * segmentService.js -- magic select's client: click (or drag) -> an outline.
 *
 * The ROI panel's magic tool and the QC panel's magic mode both call this,
 * and both stay small because everything they share is here:
 *
 *   **Setting itself up, once, by itself.** The model is not in the install.
 *   The first time magic select is armed and the server says the model is
 *   missing, the download starts at once and ONE small modal shows its
 *   progress -- "Setting up magic select", a bar, "a one-time download" --
 *   then "Magic select is ready" and closes itself ("Continue in background"
 *   sends it away; the download carries on). No button to start it, no model
 *   name: people read "magic select" and nothing else. A click made while it downloads is remembered and run
 *   when it is ready, if the view has not moved.
 *
 *   **Reusing the expensive half.** The server keeps the encoded picture of a
 *   view (the slow step) and hands back a `token`. A second click on the same
 *   view -- grow, carve, refine -- sends the token AND the view and channels
 *   the first click used, so the server answers with one cheap decoder call.
 *
 *   **Deciding what a click means** (`plan`): a new region, growing or
 *   carving the one being made, refining a selected region, or a refusal (a
 *   locked region, no category to put it in).
 *
 * `?segment=stub` in the page URL answers every request with a 24-sided
 * polygon after 300 ms and never downloads anything -- for working on the UI,
 * probes and screenshots without the model.
 *
 * Core: it calls only core routes (`/segment/v1/...`).
 */
(function (global) {
    "use strict";

    //: The key that arms magic select, in the ROI panel and the QC panel.
    const KEY = "e";
    const POLL_MS = 1000;
    //: How far, as a multiple of the region's own size, a plain click may land
    //: from the region being made and still grow it rather than start another.
    const NEAR = 1.0;

    let cached = null;         // {token, prevKey, sample, view, channels, crop, snapshot}
    let polling = null;        // interval id while a download runs
    let modalDismissed = false; // the user sent the setup modal away; keep it away
    let readyWaiters = [];     // callbacks to run once ready
    let lastStatus = null;
    let warnedUnavailable = false;

    function url(path) {
        return (typeof global.plexoraUrl === "function")
            ? global.plexoraUrl(path) : "/" + String(path).replace(/^\/+/, "");
    }

    function stubbed() {
        try {
            return new URLSearchParams(global.location ? global.location.search : "")
                .get("segment") === "stub";
        } catch (error) {
            return false;
        }
    }

    async function request(method, path, body) {
        const options = { method, headers: { "Accept": "application/json" } };
        if (body !== undefined) {
            options.headers["Content-Type"] = "application/json";
            options.body = JSON.stringify(body);
        }
        let response;
        try {
            response = await fetch(url(path), options);
        } catch (error) {
            return { ok: false, status: 0, data: { error: { code: "network",
                message: "The server did not answer." } } };
        }
        let data = {};
        try {
            data = await response.json();
        } catch (error) {
            data = {};
        }
        return { ok: response.ok && data.ok !== false, status: response.status, data };
    }

    // -- status and the one-time setup ---------------------------------------

    async function status() {
        if (stubbed()) return { state: "ready", ready: true };
        const answer = await request("GET", "segment/v1/status");
        lastStatus = answer.data && answer.data.state ? answer.data : null;
        return lastStatus || { state: "error", hint: "The server did not answer." };
    }

    // -- the setup modal ---------------------------------------------------------
    //
    // One small modal while the model downloads: a title, one sentence ("this
    // happens once"), a progress bar with the megabytes, and a button that
    // lets the download carry on in the background. It turns into "Magic
    // select is ready" and closes itself. A modal rather than a corner notice
    // because the user just asked for something that cannot happen yet, and
    // the wait has to be seen before they click again.

    let modal = null;          // {dialog, title, note, bar, meta, button, dismissed}

    function percent(download) {
        if (!download || !(download.total > 0)) return 0;
        return Math.max(0, Math.min(100, Math.floor(100 * download.done / download.total)));
    }

    function megabytes(bytes) {
        return `${(Number(bytes || 0) / 1e6).toFixed(0)} MB`;
    }

    function buildModal() {
        const dialog = document.createElement("dialog");
        dialog.className = "plx-dialog plx-confirm plx-segment-setup";
        const head = document.createElement("div");
        head.className = "plx-segment-setup-head";
        const icon = document.createElement("span");
        icon.className = "fas fa-wand-magic-sparkles";
        icon.setAttribute("aria-hidden", "true");
        const title = document.createElement("h2");
        title.className = "plx-dialog-title";
        head.append(icon, title);
        const note = document.createElement("p");
        note.className = "plx-confirm-body";
        const bar = document.createElement("progress");
        bar.className = "plx-segment-setup-progress";
        bar.max = 100;
        const meta = document.createElement("div");
        meta.className = "plx-segment-setup-meta";
        const left = document.createElement("span");
        const right = document.createElement("span");
        meta.append(left, right);
        const actions = document.createElement("div");
        actions.className = "plx-dialog-actions";
        const button = document.createElement("button");
        button.type = "button";
        button.className = "plx-button";
        actions.appendChild(button);
        dialog.append(head, note, bar, meta, actions);
        const entry = { dialog, title, note, bar, meta: [left, right], button, dismissed: false };
        button.addEventListener("click", () => {
            entry.dismissed = true;
            // Kept away until magic select is asked for again (ensureReady).
            if (entry.dialog.dataset.state === "downloading") modalDismissed = true;
            dialog.close();
        });
        // Esc is "carry on in the background", never "cancel the download".
        dialog.addEventListener("cancel", () => {
            entry.dismissed = true;
            if (entry.dialog.dataset.state === "downloading") modalDismissed = true;
        });
        dialog.addEventListener("close", () => {
            dialog.remove();
            if (modal === entry) modal = null;
        });
        return entry;
    }

    function openModal() {
        if (modal) return modal;
        if (typeof document === "undefined" || !document.body) return null;
        modal = buildModal();
        document.body.appendChild(modal.dialog);
        if (typeof modal.dialog.showModal === "function") modal.dialog.showModal();
        else modal.dialog.setAttribute("open", "");
        return modal;
    }

    function closeModal() {
        if (!modal) return;
        const entry = modal;
        modal = null;
        if (typeof entry.dialog.close === "function" && entry.dialog.open) entry.dialog.close();
        else entry.dialog.remove();
    }

    /** The progress, in the one modal (opened unless the user sent it away). */
    function showProgress(state) {
        if (modalDismissed) return;
        const entry = openModal();
        if (!entry) return;
        const download = (state && state.download) || {};
        const value = percent(download);
        entry.dialog.dataset.state = "downloading";
        entry.title.textContent = "Setting up magic select";
        entry.note.textContent = "Downloading the files magic select needs. This happens only once.";
        entry.bar.hidden = false;
        entry.bar.value = value;
        entry.meta[0].textContent = `${value} %`;
        entry.meta[1].textContent = download.total
            ? `${megabytes(download.done)} of ${megabytes(download.total)}` : "";
        entry.button.textContent = "Continue in background";
    }

    function stopPolling() {
        if (polling) global.clearInterval(polling);
        polling = null;
    }

    function settle(state) {
        stopPolling();
        const waiters = readyWaiters;
        readyWaiters = [];
        const shown = modal;
        modalDismissed = false;
        if (state.state === "ready") {
            const replayed = waiters.map((run) => {
                try {
                    return run() !== false;
                } catch (error) {
                    return false;
                }
            });
            const ranOne = replayed.some(Boolean);
            if (!shown) return;
            shown.dialog.dataset.state = "ready";
            shown.title.textContent = "Magic select is ready";
            shown.note.textContent = waiters.length && !ranOne
                ? "Click the artifact again." : "Click an artifact to outline it.";
            shown.bar.value = 100;
            shown.meta[0].textContent = "100 %";
            shown.meta[1].textContent = "";
            shown.button.textContent = "Done";
            global.setTimeout(() => { if (modal === shown) closeModal(); }, 1400);
            return;
        }
        if (state.state === "error") {
            const entry = shown || openModal();
            if (!entry) return;
            entry.dialog.dataset.state = "error";
            entry.title.textContent = "Magic select could not be set up";
            // The server's reason (a URL, an HTTP status) is for whoever reads
            // the log, not for the person at the viewer.
            if (state.hint && global.console) global.console.warn("Magic select setup:", state.hint);
            entry.note.textContent = "The files it needs could not be downloaded. "
                + "Check the connection, then press E (or the wand) to try again.";
            entry.bar.hidden = true;
            entry.meta[0].textContent = "";
            entry.meta[1].textContent = "";
            entry.button.textContent = "Close";
            return;
        }
        closeModal();   // cancelled, or switched off meanwhile
    }

    function poll() {
        if (polling) return;
        polling = global.setInterval(async () => {
            const state = await status();
            if (state.state === "downloading") {
                showProgress(state);
                return;
            }
            settle(state);
        }, POLL_MS);
    }

    function unavailable(state) {
        if (warnedUnavailable) return;
        warnedUnavailable = true;
        const confirm = global.PlexoraConfirm;
        if (!confirm || typeof confirm.tell !== "function") return;
        confirm.tell({
            title: "Magic select is not available on this server",
            body: state && state.hint ? [String(state.hint)] : undefined,
        });
    }

    /** Start the setup if it is needed. Resolves true when ready right now. */
    async function install(state) {
        const current = state || await status();
        if (current.state === "ready") return true;
        if (current.state === "not_installed_runtime" || current.state === "disabled") {
            unavailable(current);
            return false;
        }
        if (current.state === "weights_missing" || current.state === "error") {
            const answer = await request("POST", "segment/v1/install", {});
            const after = answer.data && answer.data.state ? answer.data : current;
            if (after.state === "ready") return true;
            if (!answer.ok && after.state !== "downloading") {
                if (answer.data?.error?.code === "not_installed_runtime") unavailable(after);
                else settle({ state: "error" });
                return false;
            }
            showProgress(after);
            poll();
            return false;
        }
        if (current.state === "downloading") {
            showProgress(current);
            poll();
        }
        return false;
    }

    /**
     * Called whenever magic select is armed, and before a click while not
     * ready. Never blocks: starts the setup (once) and says whether a click
     * can be answered now. `onReady` (optional) runs when the setup finishes;
     * return false from it to say the remembered click could not be replayed.
     */
    async function ensureReady(onReady) {
        if (stubbed()) return true;
        const current = await status();
        if (current.state === "ready") return true;
        if (typeof onReady === "function") readyWaiters = [onReady];
        // Asking again (E pressed while it downloads) brings the modal back.
        modalDismissed = false;
        await install(current);
        return false;
    }

    // -- the request -----------------------------------------------------------

    /** The view and channels to send, from a PlexoraViewSnapshot. */
    function describeView(snapshot) {
        const v = (snapshot && snapshot.viewport) || null;
        const view = v ? { x: v.x, y: v.y, width: v.width ?? v.w, height: v.height ?? v.h }
            : null;
        const channels = ((snapshot && snapshot.channels) || []).map((c) => {
            const entry = { name: c.name };
            if (c.color) entry.color = c.color;
            if (Array.isArray(c.range)) entry.range = c.range;
            return entry;
        });
        return { view, channels };
    }

    function insideCrop(crop, points, box) {
        if (!crop) return false;
        const right = crop.x + crop.width;
        const bottom = crop.y + crop.height;
        const ok = (points || []).every((p) => p.x >= crop.x && p.x <= right
            && p.y >= crop.y && p.y <= bottom);
        if (!ok) return false;
        if (!box) return true;
        return box.x >= crop.x && box.y >= crop.y && box.x + box.width <= right
            && box.y + box.height <= bottom;
    }

    function stubAnswer(points, box) {
        const centre = box ? { x: box.x + box.width / 2, y: box.y + box.height / 2 }
            : (points.find((p) => p.label === 1) || points[0]);
        const rx = box ? box.width / 2 : 40;
        const ry = box ? box.height / 2 : 40;
        const ring = [];
        for (let i = 0; i < 24; i += 1) {
            const a = (i / 24) * Math.PI * 2;
            ring.push([Math.round((centre.x + rx * Math.cos(a)) * 100) / 100,
                       Math.round((centre.y + ry * Math.sin(a)) * 100) / 100]);
        }
        ring.push(ring[0].slice());
        return new Promise((resolve) => global.setTimeout(() => resolve({
            ok: true, geometry: { type: "Polygon", coordinates: [ring] },
            bbox: { x: centre.x - rx, y: centre.y - ry, width: 2 * rx, height: 2 * ry },
            flags: { empty: false, touches_edge: false, too_large: false },
            token: "stub", prev_key: "stub", iou: 0.9,
            provenance: { method: "sam" },
        }), 300));
    }

    /**
     * One outline. `points` are `{x, y, label}` in image pixels (label 1
     * include, 0 exclude), `box` optional `{x, y, width, height}`. Resolves to
     * the server's answer, or `{ok: false, kind}` where kind is "setup" (the
     * model is being set up; the click was remembered if `retry` was given),
     * "busy", "unavailable" or "error" (with `message`).
     */
    async function point({ datasource, points, box, snapshot, simplifyPx, usePrevious,
                           maskGeometry, retry } = {}) {
        const prompts = (points || []).map((p) => ({ x: Number(p.x), y: Number(p.y),
                                                     label: p.label ? 1 : 0 }));
        if (stubbed()) return stubAnswer(prompts, box || null);
        const snap = snapshot || global.PlexoraViewSnapshot?.capture({ sample: datasource });
        let { view, channels } = describeView(snap);
        let token;
        const reuse = cached && cached.sample === datasource
            && global.PlexoraViewSnapshot?.sameView(cached.snapshot, snap)
            && insideCrop(cached.crop, prompts, box);
        if (reuse) {
            token = cached.token;
            view = cached.view;
            channels = cached.channels;
        }
        if (!view) return { ok: false, kind: "error", message: "The viewer is not open yet." };
        const options = {};
        if (Number.isFinite(simplifyPx)) options.simplify_px = simplifyPx;
        if (usePrevious && reuse && cached.prevKey) {
            options.use_prev_mask = true;
            options.prev_key = cached.prevKey;
        } else if (maskGeometry) {
            // Refining an existing region: its outline is where the model starts.
            options.mask_geometry = maskGeometry;
        }
        const answer = await request("POST", "segment/v1/segment", {
            datasource, view, channels, points: prompts, box: box || undefined, token,
            options,
        });
        if (answer.status === 409 && answer.data?.error?.code === "not_ready") {
            // Only this click is remembered: an older caller's is dropped.
            readyWaiters = typeof retry === "function" ? [retry] : [];
            await install(answer.data.error);
            return { ok: false, kind: "setup" };
        }
        if (answer.status === 429) return { ok: false, kind: "busy" };
        if (!answer.ok) {
            return { ok: false, kind: "error",
                     message: answer.data?.error?.message || "Magic select failed." };
        }
        cached = { token: answer.data.token, prevKey: answer.data.prev_key, sample: datasource,
                   view, channels, crop: answer.data.crop, snapshot: snap };
        return answer.data;
    }

    function forget() {
        cached = null;
    }

    // -- what a click means ------------------------------------------------------

    function grow(bbox, factor) {
        if (!bbox) return null;
        const w = bbox.width ?? bbox.w ?? 0;
        const h = bbox.height ?? bbox.h ?? 0;
        const pad = Math.max(w, h) * factor;
        return { x: bbox.x - pad, y: bbox.y - pad, width: w + 2 * pad, height: h + 2 * pad };
    }

    function inside(box, p) {
        return Boolean(box) && p.x >= box.x && p.x <= box.x + (box.width ?? box.w)
            && p.y >= box.y && p.y <= box.y + (box.height ?? box.h);
    }

    /**
     * What a click at `point` (image pixels) means.
     *   session  -- the outline being made right now ({roiId, bbox}), or null
     *   selected -- the selected region ({id, bbox, locked}), or null
     *   shift    -- Shift held: carve (exclude) instead of grow
     *   canCreate -- a category exists to put a new region in
     * Returns {kind: "new" | "grow" | "carve" | "refine" | "locked" |
     * "needCategory" | "needSelection", roiId?}.
     */
    function plan({ point: p, shift = false, selected = null, session = null,
                    canCreate = true } = {}) {
        if (session && session.roiId) {
            const near = inside(grow(session.bbox, NEAR), p);
            if (shift) return near ? { kind: "carve", roiId: session.roiId }
                : { kind: "needSelection" };
            if (near) return { kind: "grow", roiId: session.roiId };
        }
        if (selected && selected.id && inside(grow(selected.bbox, 0.02), p)) {
            if (selected.locked) return { kind: "locked", roiId: selected.id };
            return { kind: shift ? "carve" : "refine", roiId: selected.id };
        }
        if (shift) return { kind: "needSelection" };
        if (!canCreate) return { kind: "needCategory" };
        return { kind: "new" };
    }

    /** The prompts of one outline being made (points and an optional box). */
    /** The server takes at most this many points per request. */
    const MAX_POINTS = 32;
    /** A scribble becomes at most this many points: more adds nothing the
     *  model uses, and leaves room for later clicks in the same outline. */
    const STROKE_POINTS = 8;

    /**
     * A scribble (image-pixel [x, y] pairs, in drawing order) as the points
     * that prompt the model: spaced evenly along the line by length, never
     * closer than `spacing` (a short dab is one point), at most `max`.
     */
    function strokePoints(stroke, { spacing = 1, max = STROKE_POINTS } = {}) {
        const line = (stroke || []).filter((p) => p && Number.isFinite(p[0]) && Number.isFinite(p[1]));
        if (!line.length) return [];
        const along = [0];
        for (let i = 1; i < line.length; i++) {
            along.push(along[i - 1] + Math.hypot(line[i][0] - line[i - 1][0],
                                                 line[i][1] - line[i - 1][1]));
        }
        const length = along[along.length - 1];
        const count = Math.max(1, Math.min(max, Math.floor(length / Math.max(spacing, 1e-9)) + 1));
        if (count === 1) {
            const [x, y] = line[Math.floor(line.length / 2)];
            return [{ x, y }];
        }
        const out = [];
        let j = 0;
        for (let k = 0; k < count; k++) {
            const target = (length * k) / (count - 1);
            while (j < line.length - 2 && along[j + 1] < target) j++;
            const span = along[j + 1] - along[j] || 1;
            const t = Math.min(1, Math.max(0, (target - along[j]) / span));
            out.push({ x: line[j][0] + (line[j + 1][0] - line[j][0]) * t,
                       y: line[j][1] + (line[j + 1][1] - line[j][1]) * t });
        }
        return out;
    }

    class Session {
        constructor({ roiId = null, bbox = null, box = null, seed = null } = {}) {
            this.roiId = roiId;
            this.bbox = bbox;
            this.box = box;
            //: The existing region's outline this session refines, if any.
            this.seed = seed;
            this.points = [];
            //: How many points each prompt added, so an undo takes back a
            //: whole scribble, not its last point.
            this.steps = [];
        }

        add(p, label) {
            return this.addMany([p], label);
        }

        /** One prompt of several points (a scribble). Past MAX_POINTS the
         *  oldest prompts go: the latest strokes are the ones that say what
         *  is still wrong. */
        addMany(points, label) {
            const fresh = (points || []).map((p) => ({ x: p.x, y: p.y, label: label ? 1 : 0 }));
            if (!fresh.length) return this;
            this.points.push(...fresh.slice(-MAX_POINTS));
            this.steps.push(Math.min(fresh.length, MAX_POINTS));
            while (this.points.length > MAX_POINTS && this.steps.length > 1) {
                this.points.splice(0, this.steps.shift());
            }
            return this;
        }

        /** Take back the last prompt; returns its points, or null. */
        undo() {
            const n = this.steps.pop();
            if (!n) return null;
            return this.points.splice(this.points.length - n, n);
        }

        /** Whether the next request may start from the previous mask. */
        get refining() {
            return this.points.length > 1 || Boolean(this.box && this.points.length > 0);
        }

        absorb(result) {
            if (result && result.bbox) this.bbox = result.bbox;
            return this;
        }
    }

    const api = { KEY, status, ensureReady, install, point, describeView, plan, Session,
                  strokePoints, MAX_POINTS, forget, stubbed };
    global.PlexoraSegment = api;
    if (typeof globalThis !== "undefined" && globalThis !== global) {
        globalThis.PlexoraSegment = api;
    }
})(typeof window !== "undefined" ? window : globalThis);
