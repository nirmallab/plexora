/**
 * N TIFF workers over one File, each with its own geotiff.js view of it.
 *
 * A File crosses to a worker by structured clone without its bytes (it stays
 * a reference to the file on disk), so N workers cost N header parses, not N
 * copies. Requests go to the worker with the fewest in flight.
 */
export class TiffPool {
    constructor(url, size) {
        this.workers = [];
        this.pending = new Map();
        this.nextId = 1;
        for (let i = 0; i < size; i += 1) {
            const worker = new Worker(url);
            worker.inflight = 0;
            worker.onmessage = (event) => {
                const { id, ok, result, error } = event.data;
                const entry = this.pending.get(id);
                if (!entry) return;
                this.pending.delete(id);
                worker.inflight -= 1;
                if (ok) entry.resolve(result);
                else entry.reject(new Error(error));
            };
            this.workers.push(worker);
        }
    }

    call(worker, message) {
        const id = this.nextId++;
        worker.inflight += 1;
        return new Promise((resolve, reject) => {
            this.pending.set(id, { resolve, reject });
            worker.postMessage({ id, ...message });
        });
    }

    /** Open in every worker; the first answer's geometry, every worker's timing. */
    async open(args) {
        const t0 = performance.now();
        const results = await Promise.all(this.workers.map((w) => this.call(w, { op: "open", ...args })));
        return { geometry: results[0].geometry, timings: results.map((r) => r.timing),
                 wallMs: performance.now() - t0 };
    }

    least() {
        let best = this.workers[0];
        for (const w of this.workers) if (w.inflight < best.inflight) best = w;
        return best;
    }

    tile(channel, level, tx, ty, { tw = 1024, th = 1024, raw = false } = {}) {
        return this.call(this.least(), { op: "tile", channel, level, tx, ty, tw, th, raw });
    }

    level(channel, level) {
        return this.call(this.least(), { op: "level", channel, level });
    }

    terminate() {
        for (const w of this.workers) w.terminate();
        this.workers = [];
    }
}
