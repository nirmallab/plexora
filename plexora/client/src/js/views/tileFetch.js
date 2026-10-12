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
 * - BATCHED ON A SLOW LINK: each server's link is measured -- a tile's wait
 *   for its first byte, less the server's own time (`Server-Timing`) -- and
 *   where even its least recent wait is over BATCH_ENTER_MS (a data node through a tunnel, a
 *   server across the internet), queued tiles go BATCH_MAX at a time in one
 *   request (`/generated/batch`, a node's `/node/v1/batch`;
 *   server/tile_batch.py): six connections then carry six batches rather
 *   than six tiles. HTTP/2 would multiplex them, but browsers speak it only
 *   over TLS. On localhost the wait is nothing and nothing is batched.
 *
 * Label (32) and RGB (24) tiles keep OSD's own path: they are not channels.
 * Classic script, `window.PlexoraTileFetch`; loaded before imageViewer.js.
 */
(function (global) {
    "use strict";

    //: Concurrent fetches across every viewer: the browser's own six per
    //: origin, so the order is decided here rather than in its queue.
    const MAX_IN_FLIGHT = 6;
    //: Of those, at most this many for prefetched planes: a request on the
    //: wire cannot be overtaken, and the server answers in the order asked --
    //: a prefetch must never be what a tile on screen waits behind.
    const MAX_STANDBY_IN_FLIGHT = 1;
    //: A link whose wait for a tile's first byte -- the server's time taken
    //: out -- is over BATCH_ENTER_MS for every one of its last BATCH_WINDOW
    //: tiles is batched, and stops being when one comes under BATCH_LEAVE_MS;
    //: BATCH_SAMPLES tiles are timed before either. The least wait, not the
    //: average: a link's latency is a floor that a busy page or a queue in
    //: the server only adds to, and localhost always has tiles near zero.
    const BATCH_ENTER_MS = 60;
    const BATCH_LEAVE_MS = 25;
    const BATCH_SAMPLES = 6;
    const BATCH_WINDOW = 12;
    //: Tiles in one batch.
    const BATCH_MAX = 16;
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
            this.standbyInFlight = 0;
            this.maxStandbyInFlight = MAX_STANDBY_IN_FLIGHT;
            this.enabled = true;
            //: batch endpoint -> {url, wait (ms, averaged), samples, batching,
            //: unbatchable}: one per server the tiles come from.
            this.links = new Map();
            this.batching = true;
            this.stats = { requests: 0, shared: 0, aborted: 0, cancelled: 0, decoded: 0,
                           failed: 0, bytes: 0, batches: 0, batchedTiles: 0 };
        }

        /**
         * The server a tile comes from, as `{link, tilePath}`: where its batch
         * route is, and the tile's path from there. Null for an address with
         * neither a `/generated/` nor a `/node/v1/` route in it.
         */
        linkOf(url) {
            let parsed;
            try {
                parsed = new URL(url, global.location?.href || "http://localhost/");
            } catch (error) {
                return null;
            }
            const path = parsed.pathname;
            let at = path.indexOf("/node/v1/");
            let batchPath;
            if (at >= 0) {
                // A node's batch is behind its token, like any of its routes.
                const token = parsed.searchParams.get("t");
                batchPath = `${path.slice(0, at)}/node/v1/batch${token ? `?t=${encodeURIComponent(token)}` : ""}`;
            } else {
                at = path.indexOf("/generated/");
                if (at < 0) return null;
                batchPath = `${path.slice(0, at)}/generated/batch`;
            }
            const key = parsed.origin + batchPath;
            let link = this.links.get(key);
            if (!link) {
                link = { url: key, wait: null, recent: [], samples: 0, batching: false,
                         unbatchable: false };
                this.links.set(key, link);
            }
            return { link, tilePath: path.slice(at) + parsed.search };
        }

        /**
         * A response's headers arrived: what it says about its link. The
         * browser's own timing of the request when it has it (request sent
         * to first byte, which no busy main thread delays), else the time
         * from the call to the headers; the server's `Server-Timing` total
         * taken out of either.
         */
        observe(link, sentAt, headersAt, response, url = null) {
            if (!link) return;
            const timing = response?.headers?.get?.("Server-Timing") || "";
            const server = Number((/total;dur=([\d.]+)/.exec(timing) || [])[1]) || 0;
            let elapsed = headersAt - sentAt;
            const entries = url && global.performance?.getEntriesByName?.(url);
            const entry = entries && entries[entries.length - 1];
            if (entry && entry.requestStart > 0 && entry.responseStart >= entry.requestStart
                    && entry.startTime >= sentAt - 50) {
                elapsed = entry.responseStart - entry.requestStart;
            }
            const wait = Math.max(0, elapsed - server);
            link.recent.push(wait);
            if (link.recent.length > BATCH_WINDOW) link.recent.shift();
            link.wait = Math.min(...link.recent);
            link.samples += 1;
            if (link.unbatchable || link.samples < BATCH_SAMPLES) return;
            if (!link.batching && link.wait > BATCH_ENTER_MS) link.batching = true;
            else if (link.batching && link.wait < BATCH_LEAVE_MS) link.batching = false;
        }

        /** Nothing fetching, decoding or waiting: what a frame capture
         *  waits for, beside each item's own fully-loaded flag. */
        idle() {
            return this.inFlight === 0 && this.queue.length === 0 && this.requests.size === 0;
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
                const where = this.linkOf(job.src);
                request = { url: job.src, jobs: new Set(), controller: null, state: "queued",
                            key: null, queuedAt: now(), link: where?.link || null,
                            tilePath: where?.tilePath || null };
                this.requests.set(job.src, request);
                this.queue.push(request);
                this.stats.requests += 1;
            }
            request.jobs.add(job);
            job.userData.plexoraRequest = request;
            if (request.link?.batching && this.batching) {
                // OSD starts a frame's tiles one call at a time: on a batched
                // link, line them all up before any batch is cut.
                if (!this._pumpSoon) {
                    this._pumpSoon = true;
                    Promise.resolve().then(() => {
                        this._pumpSoon = false;
                        this.pump();
                    });
                }
                return;
            }
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
                const request = this.queue[0];
                if (request.jobs.size && request.key[0] === TIER.standby
                        && this.standbyInFlight >= this.maxStandbyInFlight) {
                    return;   // only prefetches left, and their share is in use
                }
                this.queue.shift();
                if (!request.jobs.size) {
                    this.drop(request);
                    continue;
                }
                if (this.batching && request.link?.batching && !request.link.unbatchable) {
                    // The next queued tiles from the same server, in the same
                    // tier, ride along (the queue is in order of need).
                    const batch = [request];
                    for (let i = 0; i < this.queue.length && batch.length < BATCH_MAX;) {
                        const other = this.queue[i];
                        if (other.link === request.link && other.jobs.size
                                && other.key[0] === request.key[0]) {
                            this.queue.splice(i, 1);
                            batch.push(other);
                        } else {
                            i += 1;
                        }
                    }
                    this.runBatch(request.link, batch);
                    continue;
                }
                this.run(request);
            }
        }

        /** Several tiles from one server in one request (server/tile_batch.py). */
        async runBatch(link, requests) {
            this.inFlight += 1;
            const tier = requests[0].key ? requests[0].key[0] : TIER.visible;
            const standby = tier === TIER.standby;
            if (standby) this.standbyInFlight += 1;
            for (const request of requests) request.state = "fetching";
            this.stats.batches += 1;
            this.stats.batchedTiles += requests.length;
            let parts = null;
            let error = null;
            let requeued = false;
            try {
                const sentAt = now();
                const response = await this._fetch(link.url, {
                    method: "POST",
                    credentials: "same-origin",
                    // text/plain: a "simple" request, so a node across origins
                    // answers it without a preflight round trip first.
                    headers: { "Content-Type": "text/plain" },
                    body: JSON.stringify({ tiles: requests.map((request) => request.tilePath) }),
                    priority: standby ? "low" : (tier === TIER.visible ? "high" : "auto"),
                });
                this.observe(link, sentAt, now(), response, link.url);
                if (response.status === 404 || response.status === 405) {
                    // A server without the route (an older node): never again,
                    // and these tiles go back in line one at a time.
                    link.unbatchable = true;
                    link.batching = false;
                    for (const request of requests) {
                        request.state = "queued";
                        this.queue.push(request);
                    }
                    requeued = true;
                }
                if (requeued) return;
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const buffer = await response.arrayBuffer();
                this.stats.bytes += buffer.byteLength;
                parts = splitBatch(buffer);
            } catch (caught) {
                error = caught;
            } finally {
                this.inFlight -= 1;
                if (standby) this.standbyInFlight -= 1;
                if (requeued) this.pump();
            }
            if (parts || error) {
                await Promise.all(requests.map(async (request, i) => {
                    const part = parts?.[i];
                    let decoded = null;
                    let failure = error;
                    if (!failure && request.state !== "dropped") {
                        if (part && part.status === 200 && part.bytes.byteLength) {
                            try {
                                request.state = "decoding";
                                decoded = await this.decode(part.bytes);
                                decoded.coarse = Boolean(part.steps);
                                this.stats.decoded += 1;
                            } catch (caught) {
                                failure = caught;
                            }
                        } else {
                            failure = new Error(`HTTP ${part ? part.status : "missing"}`);
                        }
                    }
                    this.settle(request, decoded, failure);
                }));
            }
            this.pump();
        }

        /** A request is over: its jobs get the tile, or its failure. */
        settle(request, decoded, error) {
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
        }

        async run(request) {
            this.inFlight += 1;
            request.state = "fetching";
            const controller = typeof AbortController === "function" ? new AbortController() : null;
            request.controller = controller;
            const tier = request.key ? request.key[0] : TIER.visible;
            const standby = tier === TIER.standby;
            if (standby) this.standbyInFlight += 1;
            let decoded = null;
            let error = null;
            try {
                const sentAt = now();
                const response = await this._fetch(request.url, {
                    signal: controller?.signal,
                    credentials: "same-origin",
                    priority: tier === TIER.standby ? "low" : (tier === TIER.visible ? "high" : "auto"),
                });
                this.observe(request.link, sentAt, now(), response, request.url);
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const buffer = await response.arrayBuffer();
                if (request.state === "dropped") return;
                if (!buffer.byteLength) throw new Error("empty tile response");
                this.stats.bytes += buffer.byteLength;
                request.state = "decoding";
                decoded = await this.decode(buffer);
                // A coarse stand-in for a section the server has not
                // prepared yet (volume/plane_view.py, `no-store`): marked on
                // the tile, so a frame capture can tell it from the real one.
                decoded.coarse = Boolean(response.headers?.get?.("X-Plexora-Served-Steps"));
                this.stats.decoded += 1;
            } catch (caught) {
                error = caught;
            } finally {
                this.inFlight -= 1;
                if (standby) this.standbyInFlight -= 1;
                this.settle(request, decoded, error);
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
                tile._plexoraCoarse = Boolean(decoded.coarse);
            }
            if (decoded.coarse) this.stats.coarse = (this.stats.coarse || 0) + 1;
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

    /** A batch's body: `[{status, bytes: ArrayBuffer}]` in the order asked. */
    function splitBatch(buffer) {
        const length = new DataView(buffer).getUint32(0, true);
        const header = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, 4, length)));
        let at = 4 + length;
        return header.tiles.map((meta) => {
            // Each its own buffer: an 8-bit tile's is transferred to a decoder.
            const bytes = buffer.slice(at, at + meta.bytes);
            at += meta.bytes;
            return { status: meta.status, bytes, steps: meta.steps || null };
        });
    }

    function compare(a, b) {
        for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
            const d = (a[i] ?? 0) - (b[i] ?? 0);
            if (d) return d;
        }
        return 0;
    }

    global.PlexoraTileFetch = { TileFetchScheduler, MAX_IN_FLIGHT, MAX_STANDBY_IN_FLIGHT, TIER, kindOf,
                                splitBatch, BATCH: { ENTER_MS: BATCH_ENTER_MS, LEAVE_MS: BATCH_LEAVE_MS,
                                                     SAMPLES: BATCH_SAMPLES, MAX: BATCH_MAX,
                                                     WINDOW: BATCH_WINDOW } };
    if (typeof module !== "undefined" && module.exports) {
        module.exports = global.PlexoraTileFetch;
    }
})(typeof window !== "undefined" ? window : globalThis);
