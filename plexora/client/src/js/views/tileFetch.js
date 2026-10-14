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
 *   before a prefetch -- and COVERAGE BEFORE SHARPNESS: once a plane covers
 *   the view at some level its finer tiles only sharpen it, and the next
 *   plane's tiles that would cover the view go first (a step shows the next
 *   plane coarse before the last one finishes sharpening); the plane before
 *   the panes; coarse levels before fine ones; near the centre before the
 *   edge. MAX_IN_FLIGHT at a time. While the hand moves (`setScrubbing`)
 *   what only sharpens waits in line -- a plane coming in loads only its
 *   coarsest level, the plane on screen stays at the level that covers it,
 *   a prefetched plane loads its two coarsest levels -- and goes when the
 *   hand stops;
 * - DECODED ONCE, in a worker, with the response's buffer TRANSFERRED (no
 *   copy); the job is finished as an empty `context2d` -- the only format the
 *   canvas drawer draws -- so OSD's own image decode never runs, and the
 *   colorize pass (tileColorize.js) fills that canvas at draw time as before;
 * - CANCELLED when nobody wants it: an item removed from its world gives up
 *   its jobs, and a request whose every job is gone is aborted.
 * - A FAILURE SAYS WHICH: OSD asks again for a failed tile up to its viewer's
 *   `tileRetryMax` times, and marks it `exists` false in between -- what it
 *   also calls a tile past the image's edge. `fail` tags the tile
 *   (`_plexoraFailed`: "retrying", "final" once the retries are spent,
 *   "aborted"), so channelFade.covers waits for a retry and counts only a
 *   spent one as a hole; `retryFailed(items)` asks for the holes again.
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
    //: a prefetch must never be what a tile on screen waits behind. One more
    //: (MAX_STANDBY_IDLE) only while nothing else is in flight or waiting.
    const MAX_STANDBY_IN_FLIGHT = 1;
    const MAX_STANDBY_IDLE = 2;
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
    //: Priority tiers: what the plane on screen needs to cover the view,
    //: what the plane coming in behind it (a handover) needs to, the plane on
    //: screen sharpening, the one coming in sharpening, a prefetch.
    const TIER = { visible: 0, handover: 1, sharpen: 2, handoverSharpen: 3, standby: 4 };
    const OWNER = { main: 0, pane: 1 };
    //: While the hand moves (`setScrubbing`), how far a plane already
    //: covering the view (the sharpen tiers) may sharpen: this many levels
    //: past the level that covers it -- none. The hand is gone before a
    //: finer level lands, and the wire is for the next plane's coarse tiles;
    //: the rest goes when the hand stops. (At a stack's home view only two
    //: levels are in play, the one-tile level and full resolution, so one
    //: level past the cover was everything: a drag fetched every plane it
    //: passed at full resolution, four times the tiles that cover it.) OSD
    //: numbers a pyramid's levels from the coarsest (the source's minLevel,
    //: usually 0) up to full resolution (maxLevel): a HIGHER level is FINER,
    //: as `rank`'s coarse-first (ascending level) says.
    const SCRUB_SHARPEN_SPAN = 0;
    //: ...and how much of a prefetched plane (the standby tier) loads: its
    //: coarsest visited level and this many finer, its two coarsest. One
    //: tile a channel buys the preview; the rest fills in at rest.
    const SCRUB_STANDBY_SPAN = 1;
    //: ...and how much of a plane coming in that does not cover the view yet
    //: (the visible and handover tiers): its coarsest visited level -- the
    //: one-tile level OSD's walk down stops at -- and this many finer. That
    //: level covers the view by itself, so it is all a preview needs; the
    //: finer ones OSD asks for alongside would only sharpen it, and on the
    //: wire with it they made each preview wait for full-resolution reads
    //: (and the next plane's preview behind them: a request on the wire
    //: cannot be overtaken).
    const SCRUB_COVER_SPAN = 0;
    //: How many times OSD asks again for a failed tile when its viewer does
    //: not say (`tileRetryMax`; the plane and the panes both set 3).
    const RETRY_MAX = 3;

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
            //: The watched viewer's `tileRetryMax` (`watch`), for a job
            //: whose own viewer cannot be reached.
            this.retryMax = RETRY_MAX;
            //: Until when the hand counts as moving (`setScrubbing`; the
            //: performance.now clock), and the timer that pumps once it stops.
            this.scrubUntil = 0;
            this._scrubTimer = null;
            //: `retried`: failures OSD will ask again for.
            this.stats = { requests: 0, shared: 0, aborted: 0, cancelled: 0, decoded: 0,
                           failed: 0, retried: 0, bytes: 0, batches: 0, batchedTiles: 0 };
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

        /** Give up the jobs of items taken off `viewer`'s world; and how many
         *  times its loader asks again for a failed tile (`tileRetryMax`). */
        watch(viewer) {
            viewer?.world?.addHandler?.("remove-item", (event) => this.cancelItem(event.item));
            if (Number.isFinite(viewer?.tileRetryMax)) this.retryMax = viewer.tileRetryMax;
            return this;
        }

        /** How many times OSD asks again for `job`'s tile: its own viewer's
         *  `tileRetryMax` (one scheduler serves the plane and both panes, each
         *  with its own loader), else the watched viewer's. */
        retryMaxOf(job) {
            const own = job?.tile?.tiledImage?.viewer?.tileRetryMax;
            return Number.isFinite(own) ? own : this.retryMax;
        }

        /** Whether the hand is moving (`setScrubbing`): what only sharpens
         *  waits in line (`held`). */
        scrubbing() {
            return now() < this.scrubUntil;
        }

        /**
         * The hand moves until `untilMs` (the performance.now clock; a drag
         * or a held key says so with each request, planeMotion.js). Never
         * shortens what is set. One timer pumps when it runs out, so the
         * tiles held meanwhile go without waiting for another request.
         */
        setScrubbing(untilMs) {
            const until = Number(untilMs);
            if (!Number.isFinite(until) || until <= this.scrubUntil) return this;
            this.scrubUntil = until;
            clearTimeout(this._scrubTimer);
            this._scrubTimer = setTimeout(() => this._scrubEnded(), Math.max(0, Math.ceil(until - now())));
            return this;
        }

        _scrubEnded() {
            this._scrubTimer = null;
            const left = this.scrubUntil - now();
            if (left > 0) {
                // The timer's clock ran ahead of performance.now.
                this._scrubTimer = setTimeout(() => this._scrubEnded(), Math.ceil(left));
                return;
            }
            this.pump();
        }

        /**
         * The hand let go (`plexora:scrub-ended`, planeMotion.js): what was
         * held goes now rather than when `setScrubbing`'s time runs out --
         * which never shortens, so this ends it outright.
         */
        endScrubbing() {
            this.scrubUntil = 0;
            clearTimeout(this._scrubTimer);
            this._scrubTimer = null;
            this.pump();
            return this;
        }

        /** Whether `item` already covers the view at some level
         *  (channelFade.covers): what is left for it only sharpens. Asked
         *  once per item per pump. */
        covers(item) {
            const covers = global.PlexoraChannelFade?.covers;
            if (!item || typeof covers !== "function") return false;
            const cache = this._covered;
            if (cache?.has(item)) return cache.get(item);
            let answer = false;
            try {
                answer = Boolean(covers(item));
            } catch (error) {
                answer = false;
            }
            cache?.set(item, answer);
            return answer;
        }

        /** The finest level at which `item` covers the view
         *  (channelFade.coveringLevel): a number, or null (none does, or
         *  nothing says). Asked once per item per pump. */
        coveringLevel(item) {
            const covering = global.PlexoraChannelFade?.coveringLevel;
            if (!item || typeof covering !== "function") return null;
            const cache = this._covering;
            if (cache?.has(item)) return cache.get(item);
            let answer = null;
            try {
                const level = covering(item);
                answer = Number.isFinite(level) ? level : null;
            } catch (error) {
                answer = null;
            }
            cache?.set(item, answer);
            return answer;
        }

        /**
         * The coarsest level OSD visited for `item` on its updates: the least
         * key of its `coverage`. OSD walks down from the level it shows and
         * stops at `savedCutOffLevel` (the finest level that is one tile), so
         * that is usually this -- one tile a channel. Before any update, that
         * level itself, else the source's coarsest.
         */
        coarsestLevel(item) {
            let least = null;
            for (const key of Object.keys(item?.coverage || {})) {
                const level = Number(key);
                if (Number.isFinite(level) && (least === null || level < least)) least = level;
            }
            if (least !== null) return least;
            if (Number.isFinite(item?.savedCutOffLevel)) return item.savedCutOffLevel;
            const min = Number(item?.source?.minLevel);
            return Number.isFinite(min) ? min : 0;
        }

        /**
         * Whether `job` waits in line while the hand moves: a tile that only
         * sharpens a plane already covering the view more than
         * SCRUB_SHARPEN_SPAN levels past its cover, a prefetched plane's
         * past its two coarsest visited levels (SCRUB_STANDBY_SPAN), or one
         * of a plane that does not cover the view yet finer than its
         * coarsest visited level (SCRUB_COVER_SPAN) -- that level covers it.
         * Never at rest. Asked once per job per pump.
         */
        held(job, rank = null) {
            if (!this.scrubbing()) return false;
            const cache = this._held;
            if (cache?.has(job)) return cache.get(job);
            const [tier, , level] = rank || this.rank(job);
            const item = job.tile?.tiledImage;
            let answer = false;
            if (tier === TIER.sharpen || tier === TIER.handoverSharpen) {
                // Higher is finer (see SCRUB_SHARPEN_SPAN).
                answer = typeof global.PlexoraChannelFade?.coveringLevel === "function"
                    && level > (this.coveringLevel(item) ?? -Infinity) + SCRUB_SHARPEN_SPAN;
            } else if (tier === TIER.standby) {
                answer = level > this.coarsestLevel(item) + SCRUB_STANDBY_SPAN;
            } else {
                answer = level > this.coarsestLevel(item) + SCRUB_COVER_SPAN;
            }
            cache?.set(job, answer);
            return answer;
        }

        /** Where a job stands in line: lower is sooner. */
        rank(job) {
            const tile = job.tile || {};
            const item = tile.tiledImage;
            const source = job.source || item?.source || {};
            let tier = TIER.visible;
            const hidden = item && typeof item.getOpacity === "function" && !(item.getOpacity() > 0);
            if (hidden) tier = source.plexoraStandby ? TIER.standby : TIER.handover;
            if (tier !== TIER.standby && this.covers(item)) {
                tier = tier === TIER.visible ? TIER.sharpen : TIER.handoverSharpen;
            }
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
            this._covered = new Map();
            this._covering = new Map();
            this._held = new Map();
            try {
                this._pump();
            } finally {
                this._covered = null;
                this._covering = null;
                this._held = null;
            }
        }

        _pump() {
            while (this.inFlight < this.maxInFlight && this.queue.length) {
                for (const request of this.queue) {
                    // Keyed by its best job the hand's moving does not hold
                    // back; a request whose every job is held waits in line
                    // under its best held one (`held`).
                    let best = null;
                    let bestHeld = null;
                    for (const job of request.jobs) {
                        const rank = this.rank(job);
                        if (this.held(job, rank)) {
                            if (!bestHeld || compare(rank, bestHeld) < 0) bestHeld = rank;
                        } else if (!best || compare(rank, best) < 0) {
                            best = rank;
                        }
                    }
                    request.held = !best && Boolean(bestHeld);
                    request.key = best || bestHeld || [9];
                }
                this.queue.sort((a, b) => compare(a.key, b.key));
                const at = this.queue.findIndex((queued) => !queued.held);
                if (at < 0) {
                    return;   // only held tiles left: setScrubbing's timer pumps when the hand stops
                }
                const request = this.queue[at];
                // Only prefetches are waiting (the queue is in order of need):
                // a second may go while nothing else is on the wire either.
                const share = this.inFlight === this.standbyInFlight
                    ? Math.max(this.maxStandbyInFlight, this.maxStandbyIdle ?? MAX_STANDBY_IDLE)
                    : this.maxStandbyInFlight;
                if (request.jobs.size && request.key[0] === TIER.standby
                        && this.standbyInFlight >= share) {
                    return;   // only prefetches left, and their share is in use
                }
                this.queue.splice(at, 1);
                if (!request.jobs.size) {
                    this.drop(request);
                    continue;
                }
                if (this.batching && request.link?.batching && !request.link.unbatchable) {
                    // The next queued tiles from the same server, in the same
                    // tier, ride along (the queue is in order of need); a
                    // held one waits for the hand to stop.
                    const batch = [request];
                    for (let i = 0; i < this.queue.length && batch.length < BATCH_MAX;) {
                        const other = this.queue[i];
                        if (other.link === request.link && other.jobs.size && !other.held
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
                delete tile._plexoraFailed;
            }
            if (decoded.coarse) this.stats.coarse = (this.stats.coarse || 0) + 1;
            try {
                job.finish(this.context(Math.max(1, decoded.width), Math.max(1, decoded.height)),
                           null, "context2d");
            } catch (error) {
                console.error("tileFetch: could not finish a tile", error);
            }
        }

        /**
         * A request failed: so does its job, and OSD's loader asks again
         * while `job.tries` (counted by OSD, 1 on the first ask) is under
         * 1 + `tileRetryMax` -- the same test as ImageLoader's. The tile says
         * which (`_plexoraFailed`, read by channelFade.covers).
         */
        fail(job, error) {
            this.stats.failed += 1;
            let failed;
            if (error?.name === "AbortError") {
                // Nobody wants it; OSD must not retry it either.
                job.tries = Infinity;
                failed = "aborted";
            } else if ((Number(job.tries) || 0) >= 1 + this.retryMaxOf(job)) {
                failed = "final";
            } else {
                failed = "retrying";
                this.stats.retried += 1;
            }
            if (job.tile) job.tile._plexoraFailed = failed;
            try {
                job.fail(`[tileFetch] ${error?.message || error || "failed"}`, null);
            } catch (caught) {
                // A job OSD already let go of.
            }
        }

        /**
         * Ask again for every tile of `items` whose retries are spent (tagged
         * "final"). OSD never asks for a tile it marked `exists` false, so
         * each is made an unloaded tile again and its item told to update:
         * the next update finds it wanted, not loaded and not loading, and
         * loads it like any other (TiledImage._updateLevel). Returns how many
         * tiles were asked for again.
         */
        retryFailed(items) {
            let count = 0;
            for (const item of items || []) {
                let mine = 0;
                for (const level of Object.values(item?.tilesMatrix || {})) {
                    for (const column of Object.values(level || {})) {
                        for (const tile of Object.values(column || {})) {
                            if (tile?._plexoraFailed !== "final") continue;
                            delete tile._plexoraFailed;
                            tile.exists = true;
                            tile.loaded = false;
                            tile.loading = false;
                            mine += 1;
                        }
                    }
                }
                if (!mine) continue;
                count += mine;
                item._needsDraw = true;
                item._needsUpdate = true;
                item.viewer?.forceRedraw?.();
            }
            return count;
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

    global.PlexoraTileFetch = { TileFetchScheduler, MAX_IN_FLIGHT, MAX_STANDBY_IN_FLIGHT, TIER, RETRY_MAX, kindOf,
                                SCRUB_SHARPEN_SPAN, SCRUB_STANDBY_SPAN, SCRUB_COVER_SPAN,
                                splitBatch, BATCH: { ENTER_MS: BATCH_ENTER_MS, LEAVE_MS: BATCH_LEAVE_MS,
                                                     SAMPLES: BATCH_SAMPLES, MAX: BATCH_MAX,
                                                     WINDOW: BATCH_WINDOW } };
    if (typeof module !== "undefined" && module.exports) {
        module.exports = global.PlexoraTileFetch;
    }
})(typeof window !== "undefined" ? window : globalThis);
