/**
 * telemetry.js - the tab's half of Plexora's optional, anonymous telemetry.
 *
 * A PRODUCER ONLY. The tab never talks to the telemetry service: it folds what
 * it counts into one small aggregate and posts that to its own Plexora server
 * (`POST /telemetry/ingest`) every few minutes, when it is hidden, and when it
 * goes away. The server validates every row against the same allowlist the
 * service uses and adds it to its own hourly window, so there is one install
 * identity per machine and nothing in the page reaches the network.
 *
 * WHAT IT HOLDS is counts and nine-bin timing histograms keyed by a few
 * closed-vocabulary dimensions -- never a URL, a project, a channel, a marker,
 * a gene, a message or a stack. Anything that is not on the allowlist is
 * dropped here, and would be refused by the server if it were not.
 *
 * OFF IS FREE. Until `/telemetry/status` answers, calls accumulate in memory
 * (so the first tiles of a page are timed too); if it says off, the aggregate
 * is thrown away and every later call returns at its first line.
 *
 *     PlexoraTelemetry.count("tool.summary", "open", 1, { tool: "gating" });
 *     PlexoraTelemetry.observe("render.summary", "tile_ms", 42, dims);
 *     PlexoraTelemetry.feature("gate.commit", "gating");
 */
window.PlexoraTelemetry = (function () {
    "use strict";

    const SCHEMA = 1;
    //: Millisecond histogram edges, the same nine bins as plexora/telemetry/schema.py.
    const EDGES = [16, 50, 100, 250, 500, 1000, 2500, 5000];
    const BAND10 = ["0", "1", "10", "100", "1k", "10k", "100k", "1M", "10M", "100M", "1G", "10G+"];
    const POW2_EDGES = [1, 2, 4, 8, 16, 32, 64, 128, 256];
    const POW2 = ["0", "1", "2-3", "4-7", "8-15", "16-31", "32-63", "64-127", "128-255", "256+"];
    const FIRST_PARTY = new Set(["core", "cell_explorer", "figure_builder", "gating", "qc", "roi",
                                 "transcripts", "visium_hd", "rotate"]);
    const FEATURES = new Set([
        "gate.commit", "gate.brush", "autogate.run", "gates.export", "roi.create", "roi.edit",
        "roi.delete", "roi.export", "roi.import", "roi.save", "figure.keep", "figure.export",
        "column.select", "gene.add", "gene_group.add", "hd.gene.add", "channel.toggle",
        "layer.add", "rotate", "flip",
    ]);
    //: Rows and bytes one post may carry; the server refuses more.
    const MAX_ROWS = 1500;
    const MAX_BODY = 60 * 1024;
    //: Posts kept after failing, before the aggregate is let go.
    const MAX_FAILURES = 3;

    const state = {
        //: null until the server has answered, then true or false.
        enabled: null,
        mode: "off",
        interval: 300,
        labels: {},
        failures: 0,
        timer: null,
        status: null,
    };
    const rows = new Map();
    const listeners = [];
    const viewerId = randomHex(16);

    function randomHex(bytes) {
        const buffer = new Uint8Array(bytes);
        try {
            window.crypto.getRandomValues(buffer);
        } catch (error) {
            for (let i = 0; i < bytes; i += 1) buffer[i] = Math.floor(Math.random() * 256);
        }
        return Array.from(buffer, (b) => b.toString(16).padStart(2, "0")).join("");
    }

    function fnv8(text) {
        let hash = 0x811c9dc5;
        const value = String(text);
        for (let i = 0; i < value.length; i += 1) {
            hash ^= value.charCodeAt(i);
            hash = Math.imul(hash, 0x01000193) >>> 0;
        }
        return hash.toString(16).padStart(8, "0");
    }

    function msBin(ms) {
        let index = 0;
        while (index < EDGES.length && ms > EDGES[index]) index += 1;
        return index;
    }

    function band10(n) {
        const value = Math.floor(Number(n));
        if (!(value > 0)) return "0";
        return BAND10[Math.min(String(value).length, BAND10.length - 1)];
    }

    function bandPow2(n) {
        const value = Math.floor(Number(n));
        if (!(value > 0)) return "0";
        let index = 0;
        while (index < POW2_EDGES.length && POW2_EDGES[index] <= value) index += 1;
        return POW2[index];
    }

    /** A plugin or tool id as telemetry may say it: ours verbatim, anybody
     *  else's as `ext:<8 hex>` -- the server's own label when it has sent one. */
    function ownerLabel(name) {
        const id = String(name || "");
        if (FIRST_PARTY.has(id)) return id;
        return state.labels[id] || "ext:" + fnv8(id);
    }

    function keyOf(event, key, dims) {
        const names = Object.keys(dims).sort();
        let text = event + "|" + key;
        for (const name of names) text += "|" + name + "=" + dims[name];
        return text;
    }

    function slot(event, key, dims, hist) {
        const id = keyOf(event, key, dims);
        let row = rows.get(id);
        if (!row) {
            if (rows.size >= MAX_ROWS) return null;
            row = { e: event, k: key, d: Object.assign({}, dims), n: 0 };
            if (hist) {
                row.h = [0, 0, 0, 0, 0, 0, 0, 0, 0];
                row.s = 0;
                row.mx = 0;
            }
            rows.set(id, row);
        }
        return row;
    }

    function count(event, key, n, dims) {
        if (state.enabled === false) return;
        const row = slot(event, key, dims || {}, false);
        if (row) row.n += n === undefined ? 1 : n;
    }

    function observe(event, key, ms, dims) {
        if (state.enabled === false) return;
        const value = Number(ms);
        if (!(value >= 0) || !isFinite(value)) return;
        const row = slot(event, key, dims || {}, true);
        if (!row) return;
        row.n += 1;
        row.h[msBin(value)] += 1;
        row.s += value;
        if (value > row.mx) row.mx = value;
    }

    function feature(name, plugin) {
        if (state.enabled === false || !FEATURES.has(name)) return;
        count("feature.summary", "n", 1, { feature: name, plugin: ownerLabel(plugin || "core") });
    }

    /** What a plugin gets as `ctx.telemetry`: features only, under its own name. */
    function forPlugin(id) {
        return Object.freeze({ feature: (name) => feature(name, id) });
    }

    function tool(key, id, extra) {
        count("tool.summary", key, 1, Object.assign({ tool: ownerLabel(id) }, extra || {}));
    }

    function toolLoad(id, ms) {
        observe("tool.summary", "load_ms", ms, { tool: ownerLabel(id) });
    }

    /** Called just before a post, so a producer can fold what it keeps in
     *  plain counters (per-frame paths) into rows. */
    function beforePost(fn) {
        if (typeof fn === "function") listeners.push(fn);
    }

    function take() {
        for (const fn of listeners) {
            try {
                fn();
            } catch (error) {
                // A producer that cannot fold loses its own counts, nobody else's.
            }
        }
        const taken = [];
        let bytes = 64;
        for (const [id, row] of rows) {
            const size = JSON.stringify(row).length + 1;
            if (bytes + size > MAX_BODY) break;
            bytes += size;
            taken.push(row);
            rows.delete(id);
        }
        for (const row of taken) {
            if (row.s !== undefined) {
                row.s = Math.round(row.s * 10) / 10;
                row.mx = Math.round(row.mx * 10) / 10;
            }
        }
        return taken;
    }

    function restore(taken) {
        for (const row of taken) {
            const existing = slot(row.e, row.k, row.d, row.h !== undefined);
            if (!existing) continue;
            existing.n += row.n;
            if (row.h) {
                for (let i = 0; i < 9; i += 1) existing.h[i] += row.h[i];
                existing.s += row.s;
                existing.mx = Math.max(existing.mx, row.mx);
            }
        }
    }

    function url() {
        return typeof plexoraUrl === "function" ? plexoraUrl("telemetry/ingest") : "/telemetry/ingest";
    }

    /**
     * Send the aggregate to this tab's server. `how` is "fetch" (the
     * periodic post), "keepalive" (the tab is being hidden) or "beacon" (it
     * is going away, and only sendBeacon is certain to leave).
     */
    function post(how) {
        if (state.enabled !== true) return Promise.resolve(false);
        const taken = take();
        if (!taken.length) return Promise.resolve(false);
        const body = JSON.stringify({ schema: SCHEMA, viewer_id: viewerId, rows: taken });
        if (how === "beacon" && navigator.sendBeacon) {
            try {
                if (navigator.sendBeacon(url(), new Blob([body], { type: "application/json" }))) {
                    return Promise.resolve(true);
                }
            } catch (error) {
                // Fall through to a keepalive fetch.
            }
        }
        return fetch(url(), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body,
            keepalive: how !== "fetch",
        }).then((response) => {
            if (response.ok || response.status === 400 || response.status === 413) {
                // Sent -- or refused for its shape, which a retry cannot fix.
                state.failures = 0;
                return response.ok;
            }
            throw new Error(String(response.status));
        }).catch(() => {
            state.failures += 1;
            if (state.failures <= MAX_FAILURES) restore(taken);
            return false;
        });
    }

    function schedule() {
        if (state.timer) clearInterval(state.timer);
        state.timer = setInterval(() => {
            if (document.visibilityState === "visible") post("fetch");
        }, Math.max(60, state.interval) * 1000);
    }

    function statusUrl() {
        const path = "telemetry/status?brief=1";
        return typeof plexoraUrl === "function" ? plexoraUrl(path) : "/" + path;
    }

    function noticeSeen() {
        const target = typeof plexoraUrl === "function"
            ? plexoraUrl("telemetry/notice_seen") : "/telemetry/notice_seen";
        fetch(target, { method: "POST" }).catch(() => {});
    }

    /** The one-time notice, for launches with no terminal to print it in. */
    function showNotice() {
        const toast = window.PlexoraToast;
        if (!toast || typeof toast.show !== "function") return false;
        toast.show({
            title: "Plexora sends anonymous usage counts",
            note: "Which features are used, how long tiles take to draw and which kinds of "
                + "errors occur -- never file, project, marker or gene names, cell data, "
                + "usernames or machine names. Change this in Settings.",
            timeout: 0,
            actions: [
                {
                    label: "Settings",
                    onSelect: () => {
                        noticeSeen();
                        const settings = typeof plexoraUrl === "function"
                            ? plexoraUrl("settings") : "/settings";
                        window.location.href = settings + "#telemetry";
                    },
                },
                { label: "OK", primary: true, onSelect: () => noticeSeen() },
            ],
            onDismiss: (why) => {
                if (why === "user") noticeSeen();
            },
        });
        return true;
    }

    function apply(status) {
        state.status = status || {};
        state.mode = state.status.mode || "off";
        state.enabled = state.mode !== "off" && state.status.enabled !== false;
        state.interval = Number(state.status.ingest_interval_s) || 300;
        state.labels = state.status.plugin_labels || {};
        if (!state.enabled) {
            rows.clear();
            if (state.timer) clearInterval(state.timer);
            state.timer = null;
        } else {
            schedule();
        }
        if (state.status.notice_pending) {
            // toast.js is deferred; it is certainly there by the load event.
            if (!showNotice()) window.addEventListener("load", showNotice, { once: true });
        }
    }

    function boot() {
        if (typeof fetch !== "function") return;
        fetch(statusUrl(), { cache: "no-store" })
            .then((response) => (response.ok ? response.json() : { mode: "off" }))
            .then(apply)
            .catch(() => apply({ mode: "off" }));
        document.addEventListener("visibilitychange", () => {
            if (document.visibilityState === "hidden") post("keepalive");
        });
        window.addEventListener("pagehide", () => post("beacon"));
    }

    boot();

    return {
        count, observe, feature, forPlugin, tool, toolLoad, beforePost, post,
        ownerLabel, msBin, band10, bandPow2, fnv8,
        get enabled() { return state.enabled; },
        get mode() { return state.mode; },
        get viewerId() { return viewerId; },
        /** Tests only. */
        _rows: rows,
        _apply: apply,
    };
})();
