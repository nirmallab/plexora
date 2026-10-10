/**
 * tileFetch.js - one scheduler for every channel and section tile.
 *
 * OpenSeadragon fetched each tile with an XHR, turned the bytes into an image
 * (createImageBitmap, then a 2-D canvas) and handed that to the tile-loaded
 * handler, which decoded the SAME bytes again in a worker for the shader --
 * a copy of the buffer posted to it on top. Three viewers did this each with
 * their own queue (the plane 10 in flight, each section pane 6), all behind
 * the browser's six connections to one origin, in whatever order requests
 * happened to be made; and a tile of an item taken off the world -- a plane
 * swap overtaken, a channel switched off -- went on downloading.
 *
 * Here a tile source's `downloadTileStart` (OSD's documented hook) hands every
 * job to ONE scheduler instead:
 *
 * - ONE REQUEST PER ADDRESS: the cover and paint items of a channel, a
 *   handover's items and a prefetch's all ask for the same tile, and share one
 *   fetch and one decode;
 * - IN ORDER OF NEED: what is on screen before a handover loading behind it
 *   before a prefetch; the plane before the panes; coarse levels before fine
 *   ones; near the centre before the edge. MAX_IN_FLIGHT at a time;
 * - DECODED ONCE, in a worker, with the response's buffer TRANSFERRED (no
 *   copy); the job is finished as an empty `context2d` -- the only format the
 *   canvas drawer draws -- so OSD's own image decode never runs, and the
 *   colorize pass (tileColorize.js) fills that canvas at draw time as before;
 * - CANCELLED when nobody wants it: an item removed from its world gives up
 *   its jobs, and a request whose every job is gone is aborted.
 *
 * Label (32) and RGB (24) tiles keep OSD's own path: they are not channels.
 * Classic script, `window.PlexoraTileFetch`; loaded before imageViewer.js.
 */
(function (global) {
    "use strict";

    //: Concurrent fetches across every viewer: the browser's own six per
    //: origin, so the order is decided here rather than in its queue.
    const MAX_IN_FLIGHT = 6;
    //: Priority tiers: on screen, a handover behind it, a prefetch.
    const TIER = { visible: 0, handover: 1, standby: 2 };
    const OWNER = { main: 0, pane: 1 };

    function now() {
        return global.performance?.now?.() ?? Date.now();
    }

    /** The decoder for a tile's bytes: WebP is the 8-bit path, PNG the HD one. */
    function kindOf(buffer) {
        const sig = new Uint8Array(buffer, 0, Math.min(4, buffer.byteLength));
        return sig[0] === 0x52 && sig[1] === 0x49 && sig[2] === 0x46 && sig[3] === 0x46
            ? "webp" : "gray16png";
    }

    class TileFetchScheduler {
        constructor({ decoderPool = null, fetchImpl = null, maxInFlight = MAX_IN_FLIGHT,
                      decodeInline = null, makeContext = null, onAttach = null } = {}) {
            this.decoderPool = decoderPool;
            this.onAttach = onAttach;
            this._fetch = fetchImpl || ((...args) => global.fetch(...args));
            this.maxInFlight = maxInFlight;
            this.decodeInline = decodeInline;
            this._makeContext = makeContext;
            //: url -> request {url, jobs: Set, controller, state, key}
            this.requests = new Map();
            this.queue = [];
            this.inFlight = 0;
            this.enabled = true;
            this.stats = { requests: 0, shared: 0, aborted: 0, cancelled: 0, decoded: 0,
                           failed: 0, bytes: 0 };
        }

        /**
         * Route `tileSource`'s downloads through this scheduler. `owner` is
         * "main" or "pane"; `standby` marks a prefetch's items (lowest tier).
         * Called on the options object before OSD copies it.
         */
        attach(tileSource, { owner = "main", standby = false } = {}) {
            const scheduler = this;
            if (this.onAttach) {
                const once = this.onAttach;
                this.onAttach = null;
                once();
            }
            tileSource.plexoraOwner = owner;
            tileSource.plexoraStandby = Boolean(standby);
            tileSource.downloadTileStart = function (job) {
                if (!scheduler.enabled) {
                    return global.OpenSeadragon.TileSource.prototype.downloadTileStart.call(this, job);
                }
                return scheduler.start(job);
            };
            tileSource.downloadTileAbort = function (job) {
                if (job.userData?.plexoraRequest) return scheduler.abort(job);
                return global.OpenSeadragon.TileSource.prototype.downloadTileAbort.call(this, job);
            };
            return tileSource;
        }

        /** Give up the jobs of items taken off `viewer`'s world. */
        watch(viewer) {
            viewer?.world?.addHandler?.("remove-item", (event) => this.cancelItem(event.item));
            return this;
        }

        /** Where a job stands in line: lower is sooner. */
        rank(job) {
            const tile = job.tile || {};
            const item = tile.tiledImage;
            const source = job.source || item?.source || {};
            let tier = TIER.visible;
            const hidden = item && typeof item.getOpacity === "function" && !(item.getOpacity() > 0);
            if (hidden) tier = source.plexoraStandby ? TIER.standby : TIER.handover;
            const owner = OWNER[source.plexoraOwner] ?? 0;
            const level = Number.isFinite(tile.level) ? tile.level : 0;
            const distance = Number.isFinite(tile.squaredDistance) ? tile.squaredDistance : 0;
            return [tier, owner, level, distance];
        }

        start(job) {
            job.userData = job.userData || {};
            let request = this.requests.get(job.src);
            if (request) {
                this.stats.shared += 1;
            } else {
                request = { url: job.src, jobs: new Set(), controller: null, state: "queued",
                            key: null, queuedAt: now() };
                this.requests.set(job.src, request);
                this.queue.push(request);
                this.stats.requests += 1;
            }
            request.jobs.add(job);
            job.userData.plexoraRequest = request;
            this.pump();
        }

        /** OSD gave up on a job (its timeout): the request stops if nobody else wants it. */
        abort(job) {
            const request = job.userData?.plexoraRequest;
            if (!request) return;
            job.userData.plexoraRequest = null;
            request.jobs.delete(job);
            if (!request.jobs.size) this.drop(request);
        }

        drop(request) {
            if (request.state === "done") return;
            request.state = "dropped";
            this.stats.aborted += 1;
            try {
                request.controller?.abort();
            } catch (error) {
                // Already over.
            }
            const at = this.queue.indexOf(request);
            if (at >= 0) this.queue.splice(at, 1);
            if (this.requests.get(request.url) === request) this.requests.delete(request.url);
        }

        /**
         * Every job of `item` -- removed from its world -- ends now: finished
         * as an empty tile, which OSD ignores (the item was reset when it was
         * removed) but which frees its loader's slot at once rather than when
         * the download would have ended.
         */
        cancelItem(item) {
            if (!item) return;
            for (const request of [...this.requests.values()]) {
                for (const job of [...request.jobs]) {
                    if (job.tile?.tiledImage !== item) continue;
                    request.jobs.delete(job);
                    job.userData.plexoraRequest = null;
                    this.stats.cancelled += 1;
                    this.quietly(job);
                }
                if (!request.jobs.size) this.drop(request);
            }
            this.pump();
        }

        /** Finish a job nobody will draw: an empty 1x1 tile. */
        quietly(job) {
            try {
                job.finish(this.context(1, 1), null, "context2d");
            } catch (error) {
                // A job OSD already let go of.
            }
        }

        context(width, height) {
            if (this._makeContext) return this._makeContext(width, height);
            const canvas = global.document.createElement("canvas");
            canvas.width = width;
            canvas.height = height;
            return canvas.getContext("2d");
        }

        pump() {
            while (this.inFlight < this.maxInFlight && this.queue.length) {
                for (const request of this.queue) {
                    let best = null;
                    for (const job of request.jobs) {
                        const rank = this.rank(job);
                        if (!best || compare(rank, best) < 0) best = rank;
                    }
                    request.key = best || [9];
                }
                this.queue.sort((a, b) => compare(a.key, b.key));
                const request = this.queue.shift();
                if (!request.jobs.size) {
                    this.drop(request);
                    continue;
                }
                this.run(request);
            }
        }

        async run(request) {
            this.inFlight += 1;
            request.state = "fetching";
            const controller = typeof AbortController === "function" ? new AbortController() : null;
            request.controller = controller;
            const sample = request.jobs.values().next().value;
            const tier = sample ? this.rank(sample)[0] : TIER.visible;
            let decoded = null;
            let error = null;
            try {
                const response = await this._fetch(request.url, {
                    signal: controller?.signal,
                    credentials: "same-origin",
                    priority: tier === TIER.standby ? "low" : (tier === TIER.visible ? "high" : "auto"),
                });
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const buffer = await response.arrayBuffer();
                if (request.state === "dropped") return;
                if (!buffer.byteLength) throw new Error("empty tile response");
                this.stats.bytes += buffer.byteLength;
                request.state = "decoding";
                decoded = await this.decode(buffer);
                this.stats.decoded += 1;
            } catch (caught) {
                error = caught;
            } finally {
                this.inFlight -= 1;
                if (this.requests.get(request.url) === request) this.requests.delete(request.url);
                const dropped = request.state === "dropped";
                request.state = "done";
                if (!dropped) {
                    for (const job of request.jobs) {
                        job.userData.plexoraRequest = null;
                        if (decoded) this.deliver(job, decoded);
                        else this.fail(job, error);
                    }
                }
                request.jobs.clear();
                this.pump();
            }
        }

        async decode(buffer) {
            const kind = kindOf(buffer);
            const started = now();
            let out = null;
            if (this.decoderPool) {
                try {
                    // An 8-bit tile's bytes are transferred: nobody else holds
                    // them. An HD tile's are copied: a worker without the
                    // PNG decompressor refuses it, and the bytes are then
                    // decoded here.
                    out = await this.decoderPool.decode(buffer, kind, { transfer: kind === "webp" });
                    global.PlexoraPerf?.decoded?.(kind === "webp" ? "webp" : "gray16", "worker", now() - started);
                } catch (error) {
                    if (!error?.unsupported) global.PlexoraPerf?.decodeFallback?.();
                    out = null;
                }
            }
            if (!out) {
                if (!this.decodeInline) throw new Error("no decoder for this tile");
                // A transferred buffer is gone from here: the inline path
                // only runs when the worker never had it (no pool, or it
                // refused the format before taking it).
                out = await this.decodeInline(buffer, kind);
                global.PlexoraPerf?.decoded?.(kind === "webp" ? "webp" : "gray16", "inline", now() - started);
            }
            return { array: out.array, width: out.width, height: out.height,
                     format: out.format || (kind === "webp" ? "u8" : "u16") };
        }

        /** The decoded plane onto the job's tile, and the job finished. */
        deliver(job, decoded) {
            const tile = job.tile;
            if (tile) {
                tile._array = decoded.array;
                tile._format = decoded.format;
            }
            try {
                job.finish(this.context(Math.max(1, decoded.width), Math.max(1, decoded.height)),
                           null, "context2d");
            } catch (error) {
                console.error("tileFetch: could not finish a tile", error);
            }
        }

        fail(job, error) {
            this.stats.failed += 1;
            if (error?.name === "AbortError") {
                // Nobody wants it; OSD must not retry it either.
                job.tries = Infinity;
            }
            try {
                job.fail(`[tileFetch] ${error?.message || error || "failed"}`, null);
            } catch (caught) {
                // A job OSD already let go of.
            }
        }
    }

    function compare(a, b) {
        for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
            const d = (a[i] ?? 0) - (b[i] ?? 0);
            if (d) return d;
        }
        return 0;
    }

    global.PlexoraTileFetch = { TileFetchScheduler, MAX_IN_FLIGHT, TIER, kindOf };
    if (typeof module !== "undefined" && module.exports) {
        module.exports = global.PlexoraTileFetch;
    }
})(typeof window !== "undefined" ? window : globalThis);
