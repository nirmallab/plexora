/**
 * io/block-cache.ts
 *
 * Byte-budget LRU block cache with in-flight request dedupe and sequential
 * read-ahead. Environment-free: the actual byte source is injected, so this
 * runs (and is tested) anywhere. Used by the broker as the cache shared by
 * every decode worker.
 */
export class BlockStore {
    opts;
    /** LRU via Map insertion order: oldest entry first. */
    blocks = new Map();
    inflight = new Map();
    sizes = new Map();
    lastBlock = new Map();
    totalBytes = 0;
    constructor(opts) {
        this.opts = opts;
    }
    /** Register (or update) the byte length of a file. */
    setFileSize(fileKey, size) {
        this.sizes.set(fileKey, size);
    }
    /** Bytes currently held by cached blocks (excludes in-flight fetches). */
    get cachedBytes() {
        return this.totalBytes;
    }
    /**
     * Get one block, from cache or by (deduped) fetch. Sequential access
     * triggers read-ahead of the following blocks.
     */
    async getBlock(fileKey, blockIndex) {
        const key = `${fileKey}:${blockIndex}`;
        const hit = this.blocks.get(key);
        if (hit) {
            // Touch for LRU recency.
            this.blocks.delete(key);
            this.blocks.set(key, hit);
            this.scheduleReadAhead(fileKey, blockIndex);
            return hit;
        }
        const block = await this.fetchBlock(fileKey, blockIndex, key);
        this.scheduleReadAhead(fileKey, blockIndex);
        return block;
    }
    /** Drop all state for a file. In-flight fetches settle harmlessly. */
    releaseFile(fileKey) {
        this.sizes.delete(fileKey);
        this.lastBlock.delete(fileKey);
        const prefix = `${fileKey}:`;
        for (const key of [...this.blocks.keys()]) {
            if (key.startsWith(prefix)) {
                this.totalBytes -= this.blocks.get(key).byteLength;
                this.blocks.delete(key);
            }
        }
    }
    fetchBlock(fileKey, blockIndex, key) {
        const pending = this.inflight.get(key);
        if (pending)
            return pending;
        const { blockSize, fetchRange } = this.opts;
        const size = this.sizes.get(fileKey);
        const start = blockIndex * blockSize;
        const end = size !== undefined ? Math.min(start + blockSize, size) : start + blockSize;
        if (end <= start)
            return Promise.resolve(new Uint8Array(0));
        const p = fetchRange(fileKey, start, end).then((block) => {
            this.inflight.delete(key);
            this.insert(key, block);
            return block;
        }, (err) => {
            this.inflight.delete(key);
            throw err;
        });
        this.inflight.set(key, p);
        return p;
    }
    insert(key, block) {
        const existing = this.blocks.get(key);
        if (existing) {
            this.totalBytes -= existing.byteLength;
            this.blocks.delete(key);
        }
        this.blocks.set(key, block);
        this.totalBytes += block.byteLength;
        // Evict oldest entries until within budget (never the one just added).
        for (const oldest of this.blocks.keys()) {
            if (this.totalBytes <= this.opts.maxBytes || oldest === key)
                break;
            this.totalBytes -= this.blocks.get(oldest).byteLength;
            this.blocks.delete(oldest);
        }
    }
    scheduleReadAhead(fileKey, blockIndex) {
        const last = this.lastBlock.get(fileKey);
        this.lastBlock.set(fileKey, blockIndex);
        if (this.opts.readAhead <= 0 || last === undefined || blockIndex !== last + 1)
            return;
        const size = this.sizes.get(fileKey);
        const maxBlock = size !== undefined
            ? Math.ceil(size / this.opts.blockSize) - 1
            : Number.MAX_SAFE_INTEGER;
        for (let i = 1; i <= this.opts.readAhead; i++) {
            const next = blockIndex + i;
            if (next > maxBlock)
                break;
            const key = `${fileKey}:${next}`;
            if (!this.blocks.has(key) && !this.inflight.has(key)) {
                this.fetchBlock(fileKey, next, key).catch(() => {
                    // Prefetch failures are silent; the demand read will retry and report.
                });
            }
        }
    }
}
//# sourceMappingURL=block-cache.js.map