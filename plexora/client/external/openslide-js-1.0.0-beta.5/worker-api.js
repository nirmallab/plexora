/**
 * worker-api.ts
 *
 * Low-level bridge between JavaScript and the OpenSlide WASM module.
 * Runs inside a Web Worker. Handles cwrap binding, memory management,
 * virtual filesystem mounting, and pixel format conversion.
 */
import { convertArgbToRgba } from './pixel-convert.js';
import { mountUrlViaBroker, mountFilesViaBroker, mountDirViaBroker } from './io/fs-mount.js';
export class WorkerApi {
    mod;
    fn;
    /** Shared I/O channel; mounts route through the broker when present. */
    io;
    /** Broker file keys per mount, released on unmount. */
    brokerMounts = new Map();
    constructor(mod, io = null) {
        this.mod = mod;
        this.io = io;
        const cw = (name, ret, args) => mod.cwrap(name, ret, args, { async: true });
        this.fn = {
            open: cw('os_open', 'number', ['string']),
            close: cw('os_close', null, ['number']),
            getLevelCount: cw('os_get_level_count', 'number', ['number']),
            getLevelDimensions: cw('os_get_level_dimensions', 'number', ['number', 'number']),
            getLevelDownsample: cw('os_get_level_downsample', 'number', ['number', 'number']),
            getBestLevelForDownsample: cw('os_get_best_level_for_downsample', 'number', ['number', 'number']),
            readRegion: cw('os_read_region', 'number', ['number', 'bigint', 'bigint', 'number', 'bigint', 'bigint']),
            freeResult: cw('os_free_result', null, ['number']),
            getPropertyNames: cw('os_get_property_names', 'number', ['number']),
            getPropertyValue: cw('os_get_property_value', 'number', ['number', 'string']),
            getAssociatedImageNames: cw('os_get_associated_image_names', 'number', ['number']),
            getAssociatedImageDimensions: cw('os_get_associated_image_dimensions', 'number', ['number', 'string']),
            readAssociatedImage: cw('os_read_associated_image', 'number', ['number', 'string']),
            getError: cw('os_get_error', 'number', ['number']),
            getVersion: cw('os_get_version', 'number', []),
            detectVendor: cw('os_detect_vendor', 'number', ['string']),
            getIccProfileSize: cw('os_get_icc_profile_size', 'bigint', ['number']),
            readIccProfile: cw('os_read_icc_profile', 'number', ['number']),
        };
    }
    /** Check for an OpenSlide error after a call and throw if present. */
    async checkError(handle) {
        const errPtr = await this.fn.getError(handle);
        if (errPtr) {
            const msg = this.mod.UTF8ToString(errPtr);
            throw new Error(msg);
        }
    }
    /** Read a NULL-terminated array of C strings from a pointer. */
    readStringArray(ptr) {
        const results = [];
        const ptrSize = 4; // wasm32
        let offset = ptr;
        while (true) {
            const strPtr = this.mod.HEAP32[offset >> 2];
            if (!strPtr)
                break;
            results.push(this.mod.UTF8ToString(strPtr));
            offset += ptrSize;
        }
        return results;
    }
    /**
     * Copy `pixels` ARGB words from the WASM heap at `ptr` into a fresh
     * (transferable) buffer, converting to straight RGBA in the same pass.
     * The heap view is taken fresh per call — memory growth invalidates
     * earlier views — and the caller must not await between readRegion
     * returning and this copy.
     */
    copyHeapArgbAsRgba(ptr, pixels) {
        const heap = this.mod.HEAPU8;
        const src = new Uint32Array(heap.buffer, heap.byteOffset + ptr, pixels);
        const out = new ArrayBuffer(pixels * 4);
        convertArgbToRgba(src, new Uint32Array(out));
        return out;
    }
    // --- Public API ---
    async open(path) {
        const handle = await this.fn.open(path);
        if (!handle) {
            // A NULL handle carries no OpenSlide error object, so probe for the
            // reason: whether a vendor was even detected (detection vs. companion-read
            // failure) and which mount backend served the bytes. Keeps a silent open
            // failure from ever being an opaque string again.
            const vendor = await this.detectVendor(path).catch(() => null);
            const detail = vendor
                ? `format detected as "${vendor}" but its files could not be read`
                : 'no supported slide format was detected';
            const backend = this.io ? 'broker' : 'workerfs';
            throw new Error(`Failed to open slide: ${path} (${detail}; io=${backend})`);
        }
        await this.checkError(handle);
        return handle;
    }
    async close(handle) {
        await this.fn.close(handle);
    }
    async getSlideInfo(handle) {
        const levelCount = await this.fn.getLevelCount(handle);
        await this.checkError(handle);
        const levelDimensions = [];
        const levelDownsamples = [];
        for (let i = 0; i < levelCount; i++) {
            const dimPtr = await this.fn.getLevelDimensions(handle, i);
            const w = Number(this.mod.HEAP64[dimPtr >> 3]);
            const h = Number(this.mod.HEAP64[(dimPtr >> 3) + 1]);
            await this.fn.freeResult(dimPtr);
            levelDimensions.push({ width: w, height: h });
            const ds = await this.fn.getLevelDownsample(handle, i);
            levelDownsamples.push(ds);
        }
        // Properties
        const namesPtr = await this.fn.getPropertyNames(handle);
        const propNames = this.readStringArray(namesPtr);
        const properties = new Map();
        for (const name of propNames) {
            const valPtr = await this.fn.getPropertyValue(handle, name);
            if (valPtr) {
                properties.set(name, this.mod.UTF8ToString(valPtr));
            }
        }
        // Associated images
        const assocPtr = await this.fn.getAssociatedImageNames(handle);
        const associatedImageNames = this.readStringArray(assocPtr);
        return { levelCount, levelDimensions, levelDownsamples, properties, associatedImageNames };
    }
    /**
     * Read a region and return the raw RGBA bytes as a transferable ArrayBuffer.
     */
    async readRegion(handle, x, y, level, w, h) {
        const ptr = await this.fn.readRegion(handle, BigInt(x), BigInt(y), level, BigInt(w), BigInt(h));
        await this.checkError(handle);
        if (!ptr)
            throw new Error('readRegion returned null');
        const rgba = this.copyHeapArgbAsRgba(ptr, w * h);
        await this.fn.freeResult(ptr);
        return rgba;
    }
    async readAssociatedImage(handle, name) {
        const dimPtr = await this.fn.getAssociatedImageDimensions(handle, name);
        const w = Number(this.mod.HEAP64[dimPtr >> 3]);
        const h = Number(this.mod.HEAP64[(dimPtr >> 3) + 1]);
        await this.fn.freeResult(dimPtr);
        const ptr = await this.fn.readAssociatedImage(handle, name);
        await this.checkError(handle);
        if (!ptr)
            throw new Error(`readAssociatedImage returned null for '${name}'`);
        const rgba = this.copyHeapArgbAsRgba(ptr, w * h);
        await this.fn.freeResult(ptr);
        return { buffer: rgba, width: w, height: h };
    }
    async getAssociatedImageDimensions(handle, name) {
        const dimPtr = await this.fn.getAssociatedImageDimensions(handle, name);
        const w = Number(this.mod.HEAP64[dimPtr >> 3]);
        const h = Number(this.mod.HEAP64[(dimPtr >> 3) + 1]);
        await this.fn.freeResult(dimPtr);
        return { width: w, height: h };
    }
    async detectVendor(path) {
        const ptr = await this.fn.detectVendor(path);
        return ptr ? this.mod.UTF8ToString(ptr) : null;
    }
    async getVersion() {
        const ptr = await this.fn.getVersion();
        return ptr ? this.mod.UTF8ToString(ptr) : 'unknown';
    }
    async getIccProfile(handle) {
        const size = await this.fn.getIccProfileSize(handle);
        if (size <= 0n)
            return null;
        const ptr = await this.fn.readIccProfile(handle);
        if (!ptr)
            return null;
        const numSize = Number(size);
        const data = new Uint8Array(numSize);
        data.set(this.mod.HEAPU8.subarray(ptr, ptr + numSize));
        await this.fn.freeResult(ptr);
        return data.buffer;
    }
    // --- Filesystem helpers ---
    mountFiles(files, mountId) {
        if (this.io) {
            const { path, fileKeys } = mountFilesViaBroker(this.mod, files, mountId, this.io);
            this.brokerMounts.set(mountId, fileKeys);
            return path;
        }
        const dir = `/mnt/${mountId}`;
        this.mod.FS.mkdir(dir);
        this.mod.FS.mount(this.mod.WORKERFS, { files }, dir);
        return `${dir}/${files[0].name}`;
    }
    /**
     * Mount files with directory structure (for multi-file formats like MRXS).
     *
     * WORKERFS mounts are flat — each mount point gets a set of files with no
     * subdirectories. So we group files by parent directory and create a separate
     * WORKERFS mount for each group. The root dir must be created with mkdir
     * first, then subdirectories get their own WORKERFS mounts.
     *
     * Layout for MRXS "image_1.mrxs" + "image_1/Slidedat.ini" + "image_1/Data0001.dat":
     *   /mnt/<id>/root/       ← WORKERFS mount with [image_1.mrxs]
     *   /mnt/<id>/root/image_1/ ← WORKERFS mount with [Slidedat.ini, Data0001.dat, ...]
     */
    mountDir(entries, indexFile, mountId) {
        if (this.io) {
            const { path, fileKeys } = mountDirViaBroker(this.mod, entries, indexFile, mountId, this.io);
            this.brokerMounts.set(mountId, fileKeys);
            return path;
        }
        const base = `/mnt/${mountId}`;
        this.mod.FS.mkdir(base);
        const root = `${base}/root`;
        this.mod.FS.mkdir(root);
        // Group files by their immediate parent directory
        const dirs = new Map();
        for (const entry of entries) {
            const lastSlash = entry.path.lastIndexOf('/');
            const dir = lastSlash > 0 ? entry.path.slice(0, lastSlash) : '';
            if (!dirs.has(dir))
                dirs.set(dir, []);
            dirs.get(dir).push(entry.file);
        }
        // First: create all subdirectory paths (before any WORKERFS mounts)
        for (const dir of dirs.keys()) {
            if (!dir)
                continue;
            const parts = dir.split('/');
            let current = root;
            for (const part of parts) {
                current += '/' + part;
                try {
                    this.mod.FS.mkdir(current);
                }
                catch { /* exists */ }
            }
        }
        // Mount root-level files at /mnt/<id>/root/ only if there are root files
        // BUT: we can only WORKERFS-mount on an empty directory, and we just created
        // subdirs under root. So root-level files must be written via MEMFS instead.
        const rootFiles = dirs.get('');
        if (rootFiles) {
            for (const file of rootFiles) {
                // Write file content to MEMFS — we need to read the File synchronously,
                // but we're in sync context. Use a WORKERFS trick: mount in a temp dir.
                const tmpDir = `${base}/tmp_${Math.random().toString(36).slice(2, 6)}`;
                this.mod.FS.mkdir(tmpDir);
                this.mod.FS.mount(this.mod.WORKERFS, { files: [file] }, tmpDir);
                // Read from temp and write to target
                const data = this.mod.FS.readFile(`${tmpDir}/${file.name}`);
                this.mod.FS.writeFile(`${root}/${file.name}`, data);
                this.mod.FS.unmount(tmpDir);
                this.mod.FS.rmdir(tmpDir);
            }
        }
        // Mount each subdirectory group via WORKERFS
        for (const [dir, files] of dirs) {
            if (!dir)
                continue; // root files already handled
            const mountPoint = `${root}/${dir}`;
            this.mod.FS.mount(this.mod.WORKERFS, { files }, mountPoint);
        }
        return `${root}/${indexFile}`;
    }
    async mountUrl(url, mountId) {
        if (this.io) {
            const { path, fileKeys } = await mountUrlViaBroker(this.mod, url, mountId, this.io);
            this.brokerMounts.set(mountId, fileKeys);
            return path;
        }
        const dir = `/mnt/${mountId}`;
        this.mod.FS.mkdir(dir);
        this.mod.FS.mount(this.mod.MEMFS, {}, dir);
        this.mod.FS.createLazyFile(dir, 'remote', url, true, false);
        return `${dir}/remote`;
    }
    unmount(mountId) {
        const dir = `/mnt/${mountId}`;
        try {
            this.mod.FS.unmount(dir);
            this.mod.FS.rmdir(dir);
        }
        catch {
            // May already be unmounted
        }
        const fileKeys = this.brokerMounts.get(mountId);
        if (fileKeys && this.io) {
            this.brokerMounts.delete(mountId);
            this.io.release(fileKeys);
        }
    }
}
//# sourceMappingURL=worker-api.js.map