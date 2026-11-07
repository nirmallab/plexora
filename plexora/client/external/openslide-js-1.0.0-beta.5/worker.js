var __defProp = Object.defineProperty;
var __defNormalProp = (obj, key, value) => key in obj ? __defProp(obj, key, { enumerable: true, configurable: true, writable: true, value }) : obj[key] = value;
var __publicField = (obj, key, value) => __defNormalProp(obj, typeof key !== "symbol" ? key + "" : key, value);

// src/pixel-convert.ts
function convertArgbToRgba(src, dst) {
  for (let i = 0; i < src.length; i++) {
    const v = src[i];
    const a = v >>> 24;
    if (a === 255) {
      dst[i] = v & 4278255360 | v >>> 16 & 255 | (v & 255) << 16;
    } else if (a === 0) {
      dst[i] = 0;
    } else {
      const r = Math.min(255, (v >>> 16 & 255) * 255 / a | 0);
      const g = Math.min(255, (v >>> 8 & 255) * 255 / a | 0);
      const b = Math.min(255, (v & 255) * 255 / a | 0);
      dst[i] = a << 24 | b << 16 | g << 8 | r;
    }
  }
}

// src/io/protocol.ts
var CTRL_STATUS = 0;
var CTRL_SEQ = 1;
var CTRL_BYTES = 2;
var CTRL_ERRNO = 3;
var HEADER_BYTES = 64;
var STATUS_PENDING = 0;
var STATUS_OK = 1;
var STATUS_ERROR = 2;
var EINVAL = 28;
var EIO = 29;
var DEFAULT_IO_CONFIG = {
  blockSize: 1024 * 1024,
  brokerCacheBytes: 256 * 1024 * 1024,
  readAhead: 2,
  maxConcurrentReads: 4,
  readTimeoutMs: 12e4
};

// src/io/sync-client.ts
var SyncIoError = class extends Error {
  constructor(errno, message) {
    super(message);
    this.errno = errno;
    this.name = "SyncIoError";
    Object.setPrototypeOf(this, new.target.prototype);
  }
};
var atomicsWait = (ctrl, index, value, timeoutMs) => Atomics.wait(ctrl, index, value, timeoutMs);
var L1_MAX_BLOCKS = 8;
var SyncIo = class {
  constructor(port, sab, config, wait = atomicsWait) {
    __publicField(this, "port");
    __publicField(this, "config");
    __publicField(this, "wait");
    __publicField(this, "ctrl");
    __publicField(this, "data");
    __publicField(this, "seq", 0);
    __publicField(this, "rpcId", 0);
    __publicField(this, "pendingRpcs", /* @__PURE__ */ new Map());
    /** Per-worker L1 block cache (LRU via Map insertion order). */
    __publicField(this, "l1", /* @__PURE__ */ new Map());
    this.port = port;
    this.config = config;
    this.wait = wait;
    this.ctrl = new Int32Array(sab, 0, HEADER_BYTES / 4);
    this.data = new Uint8Array(sab, HEADER_BYTES);
    port.onmessage = (e) => this.onReply(e.data);
  }
  get blockSize() {
    return this.config.blockSize;
  }
  /** Register a remote URL with the broker; resolves with the file size. */
  registerUrl(fileKey, url) {
    const rpcId = ++this.rpcId;
    return new Promise((resolve, reject) => {
      this.pendingRpcs.set(rpcId, { resolve, reject });
      this.port.postMessage({ type: "register-url", rpcId, fileKey, url });
    });
  }
  /**
   * Hand local Files to the broker. Fire-and-forget: port messages are
   * delivered in order, so the registration always precedes the first read.
   */
  registerFiles(files) {
    this.port.postMessage({ type: "register-files", files });
  }
  /** Drop the broker's references and cache for these files. */
  release(fileKeys) {
    this.port.postMessage({ type: "release", fileKeys });
  }
  /**
   * Synchronously read one block. Must only be called while this thread may
   * block (i.e. from inside an FS read) — no awaits anywhere on this path.
   */
  readBlockSync(fileKey, blockIndex) {
    const key = `${fileKey}:${blockIndex}`;
    const hit = this.l1.get(key);
    if (hit) {
      this.l1.delete(key);
      this.l1.set(key, hit);
      return hit;
    }
    const seq = ++this.seq;
    Atomics.store(this.ctrl, CTRL_STATUS, STATUS_PENDING);
    this.port.postMessage({ type: "read", seq, fileKey, blockIndex });
    const deadline = Date.now() + this.config.readTimeoutMs;
    for (; ; ) {
      const status = Atomics.load(this.ctrl, CTRL_STATUS);
      if (status === STATUS_PENDING) {
        const remaining = deadline - Date.now();
        if (remaining <= 0) {
          throw new SyncIoError(EIO, `broker read timed out after ${this.config.readTimeoutMs}ms`);
        }
        this.wait(this.ctrl, CTRL_STATUS, STATUS_PENDING, remaining);
        continue;
      }
      if (Atomics.load(this.ctrl, CTRL_SEQ) !== seq) {
        Atomics.store(this.ctrl, CTRL_STATUS, STATUS_PENDING);
        continue;
      }
      if (status === STATUS_ERROR) {
        const errno = Atomics.load(this.ctrl, CTRL_ERRNO) || EIO;
        throw new SyncIoError(errno, `broker read failed (errno ${errno})`);
      }
      const bytes = Atomics.load(this.ctrl, CTRL_BYTES);
      const block = this.data.slice(0, bytes);
      this.l1.set(key, block);
      if (this.l1.size > L1_MAX_BLOCKS) {
        this.l1.delete(this.l1.keys().next().value);
      }
      return block;
    }
  }
  /**
   * Synchronous multi-block read into `out` at `outOffset`. Returns the
   * number of bytes written (clamped at EOF).
   */
  readInto(fileKey, fileSize, position, length, out, outOffset) {
    const blockSize = this.config.blockSize;
    const len = Math.min(length, Math.max(0, fileSize - position));
    let written = 0;
    while (written < len) {
      const pos = position + written;
      const blockIndex = Math.floor(pos / blockSize);
      const block = this.readBlockSync(fileKey, blockIndex);
      const start = pos - blockIndex * blockSize;
      const n = Math.min(block.byteLength - start, len - written);
      if (n <= 0) break;
      out.set(block.subarray(start, start + n), outOffset + written);
      written += n;
    }
    return written;
  }
  onReply(msg) {
    if (msg.type !== "rpc") return;
    const pending = this.pendingRpcs.get(msg.rpcId);
    if (!pending) return;
    this.pendingRpcs.delete(msg.rpcId);
    if (msg.ok) {
      pending.resolve(msg.size);
    } else {
      pending.reject(new Error(msg.error));
    }
  }
};

// src/io/fs-mount.ts
function fileKeyForFile(file) {
  return `file:${file.name}:${file.size}:${file.lastModified}`;
}
function fileKeyForUrl(url) {
  return `url:${url}`;
}
function createBrokerFile(mod, parentPath, name, fileKey, size, io) {
  const FS = mod.FS;
  const node = FS.createLazyFile(parentPath, name, `broker://${fileKey}`, true, false);
  node.contents = { length: size };
  node.stream_ops = {
    read(_stream, buffer, offset, length, position) {
      if (position >= size || length <= 0) return 0;
      try {
        return io.readInto(fileKey, size, position, length, buffer, offset);
      } catch (err) {
        throw new FS.ErrnoError(err instanceof SyncIoError ? err.errno : EIO);
      }
    },
    llseek(stream, offset, whence) {
      let position = offset;
      if (whence === 1) {
        position += stream.position;
      } else if (whence === 2) {
        position += size;
      }
      if (position < 0) throw new FS.ErrnoError(EINVAL);
      return position;
    }
  };
}
async function mountUrlViaBroker(mod, url, mountId, io) {
  const fileKey = fileKeyForUrl(url);
  const size = await io.registerUrl(fileKey, url);
  const dir = `/mnt/${mountId}`;
  mod.FS.mkdir(dir);
  mod.FS.mount(mod.MEMFS, {}, dir);
  createBrokerFile(mod, dir, "remote", fileKey, size, io);
  return { path: `${dir}/remote`, fileKeys: [fileKey] };
}
function mountFilesViaBroker(mod, files, mountId, io) {
  const dir = `/mnt/${mountId}`;
  mod.FS.mkdir(dir);
  mod.FS.mount(mod.MEMFS, {}, dir);
  const regs = files.map((file) => ({ fileKey: fileKeyForFile(file), file, size: file.size }));
  io.registerFiles(regs);
  for (const reg of regs) {
    createBrokerFile(mod, dir, reg.file.name, reg.fileKey, reg.size, io);
  }
  return { path: `${dir}/${files[0].name}`, fileKeys: regs.map((r) => r.fileKey) };
}
function mountDirViaBroker(mod, entries, indexFile, mountId, io) {
  const base = `/mnt/${mountId}`;
  mod.FS.mkdir(base);
  mod.FS.mount(mod.MEMFS, {}, base);
  const root = `${base}/root`;
  mod.FS.mkdir(root);
  const made = /* @__PURE__ */ new Set();
  const regs = entries.map((entry) => ({ fileKey: fileKeyForFile(entry.file), file: entry.file, size: entry.file.size }));
  io.registerFiles(regs);
  for (let i = 0; i < entries.length; i++) {
    const entry = entries[i];
    const lastSlash = entry.path.lastIndexOf("/");
    let parent = root;
    if (lastSlash > 0) {
      const parts = entry.path.slice(0, lastSlash).split("/");
      for (const part of parts) {
        parent += "/" + part;
        if (!made.has(parent)) {
          try {
            mod.FS.mkdir(parent);
          } catch {
          }
          made.add(parent);
        }
      }
    }
    const name = entry.path.slice(lastSlash + 1);
    createBrokerFile(mod, parent, name, regs[i].fileKey, regs[i].size, io);
  }
  return { path: `${root}/${indexFile}`, fileKeys: regs.map((r) => r.fileKey) };
}

// src/worker-api.ts
var WorkerApi = class {
  constructor(mod, io = null) {
    __publicField(this, "mod");
    __publicField(this, "fn");
    /** Shared I/O channel; mounts route through the broker when present. */
    __publicField(this, "io");
    /** Broker file keys per mount, released on unmount. */
    __publicField(this, "brokerMounts", /* @__PURE__ */ new Map());
    this.mod = mod;
    this.io = io;
    const cw = (name, ret, args) => mod.cwrap(name, ret, args, { async: true });
    this.fn = {
      open: cw("os_open", "number", ["string"]),
      close: cw("os_close", null, ["number"]),
      getLevelCount: cw("os_get_level_count", "number", ["number"]),
      getLevelDimensions: cw("os_get_level_dimensions", "number", ["number", "number"]),
      getLevelDownsample: cw("os_get_level_downsample", "number", ["number", "number"]),
      getBestLevelForDownsample: cw("os_get_best_level_for_downsample", "number", ["number", "number"]),
      readRegion: cw("os_read_region", "number", ["number", "bigint", "bigint", "number", "bigint", "bigint"]),
      freeResult: cw("os_free_result", null, ["number"]),
      getPropertyNames: cw("os_get_property_names", "number", ["number"]),
      getPropertyValue: cw("os_get_property_value", "number", ["number", "string"]),
      getAssociatedImageNames: cw("os_get_associated_image_names", "number", ["number"]),
      getAssociatedImageDimensions: cw("os_get_associated_image_dimensions", "number", ["number", "string"]),
      readAssociatedImage: cw("os_read_associated_image", "number", ["number", "string"]),
      getError: cw("os_get_error", "number", ["number"]),
      getVersion: cw("os_get_version", "number", []),
      detectVendor: cw("os_detect_vendor", "number", ["string"]),
      getIccProfileSize: cw("os_get_icc_profile_size", "bigint", ["number"]),
      readIccProfile: cw("os_read_icc_profile", "number", ["number"])
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
    const ptrSize = 4;
    let offset = ptr;
    while (true) {
      const strPtr = this.mod.HEAP32[offset >> 2];
      if (!strPtr) break;
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
      const vendor = await this.detectVendor(path).catch(() => null);
      const detail = vendor ? `format detected as "${vendor}" but its files could not be read` : "no supported slide format was detected";
      const backend = this.io ? "broker" : "workerfs";
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
    const namesPtr = await this.fn.getPropertyNames(handle);
    const propNames = this.readStringArray(namesPtr);
    const properties = /* @__PURE__ */ new Map();
    for (const name of propNames) {
      const valPtr = await this.fn.getPropertyValue(handle, name);
      if (valPtr) {
        properties.set(name, this.mod.UTF8ToString(valPtr));
      }
    }
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
    if (!ptr) throw new Error("readRegion returned null");
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
    if (!ptr) throw new Error(`readAssociatedImage returned null for '${name}'`);
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
    return ptr ? this.mod.UTF8ToString(ptr) : "unknown";
  }
  async getIccProfile(handle) {
    const size = await this.fn.getIccProfileSize(handle);
    if (size <= 0n) return null;
    const ptr = await this.fn.readIccProfile(handle);
    if (!ptr) return null;
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
    const dirs = /* @__PURE__ */ new Map();
    for (const entry of entries) {
      const lastSlash = entry.path.lastIndexOf("/");
      const dir = lastSlash > 0 ? entry.path.slice(0, lastSlash) : "";
      if (!dirs.has(dir)) dirs.set(dir, []);
      dirs.get(dir).push(entry.file);
    }
    for (const dir of dirs.keys()) {
      if (!dir) continue;
      const parts = dir.split("/");
      let current = root;
      for (const part of parts) {
        current += "/" + part;
        try {
          this.mod.FS.mkdir(current);
        } catch {
        }
      }
    }
    const rootFiles = dirs.get("");
    if (rootFiles) {
      for (const file of rootFiles) {
        const tmpDir = `${base}/tmp_${Math.random().toString(36).slice(2, 6)}`;
        this.mod.FS.mkdir(tmpDir);
        this.mod.FS.mount(this.mod.WORKERFS, { files: [file] }, tmpDir);
        const data = this.mod.FS.readFile(`${tmpDir}/${file.name}`);
        this.mod.FS.writeFile(`${root}/${file.name}`, data);
        this.mod.FS.unmount(tmpDir);
        this.mod.FS.rmdir(tmpDir);
      }
    }
    for (const [dir, files] of dirs) {
      if (!dir) continue;
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
    this.mod.FS.createLazyFile(dir, "remote", url, true, false);
    return `${dir}/remote`;
  }
  unmount(mountId) {
    const dir = `/mnt/${mountId}`;
    try {
      this.mod.FS.unmount(dir);
      this.mod.FS.rmdir(dir);
    } catch {
    }
    const fileKeys = this.brokerMounts.get(mountId);
    if (fileKeys && this.io) {
      this.brokerMounts.delete(mountId);
      this.io.release(fileKeys);
    }
  }
};

// src/read-queue.ts
var ReadCancelledError = class extends Error {
  constructor() {
    super("cancelled");
    __publicField(this, "code", "cancelled");
    this.name = "ReadCancelledError";
    Object.setPrototypeOf(this, new.target.prototype);
  }
};
var ReadQueue = class {
  constructor(maxConcurrent = 4) {
    __publicField(this, "maxConcurrent");
    __publicField(this, "active", 0);
    __publicField(this, "waiters", []);
    this.maxConcurrent = Math.max(1, maxConcurrent);
  }
  /**
   * Wait for a read slot. Resolves when the caller may start the read;
   * rejects with ReadCancelledError if cancelled while still queued.
   */
  acquire(id) {
    if (this.active < this.maxConcurrent) {
      this.active++;
      return Promise.resolve();
    }
    return new Promise((resolve, reject) => {
      this.waiters.push({ id, resolve, reject });
    });
  }
  /** Release a slot acquired via acquire(). */
  release() {
    const next = this.waiters.shift();
    if (next) {
      next.resolve();
    } else {
      this.active--;
    }
  }
  /**
   * Cancel a read that is still waiting for a slot. Returns true if the
   * entry was found and rejected; false if it already started (or finished).
   */
  cancel(id) {
    const idx = this.waiters.findIndex((w) => w.id === id);
    if (idx === -1) return false;
    const [waiter] = this.waiters.splice(idx, 1);
    waiter.reject(new ReadCancelledError());
    return true;
  }
};

// src/io/block-cache.ts
var BlockStore = class {
  constructor(opts) {
    __publicField(this, "opts");
    /** LRU via Map insertion order: oldest entry first. */
    __publicField(this, "blocks", /* @__PURE__ */ new Map());
    __publicField(this, "inflight", /* @__PURE__ */ new Map());
    __publicField(this, "sizes", /* @__PURE__ */ new Map());
    __publicField(this, "lastBlock", /* @__PURE__ */ new Map());
    __publicField(this, "totalBytes", 0);
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
    if (pending) return pending;
    const { blockSize, fetchRange } = this.opts;
    const size = this.sizes.get(fileKey);
    const start = blockIndex * blockSize;
    const end = size !== void 0 ? Math.min(start + blockSize, size) : start + blockSize;
    if (end <= start) return Promise.resolve(new Uint8Array(0));
    const p = fetchRange(fileKey, start, end).then(
      (block) => {
        this.inflight.delete(key);
        this.insert(key, block);
        return block;
      },
      (err) => {
        this.inflight.delete(key);
        throw err;
      }
    );
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
    for (const oldest of this.blocks.keys()) {
      if (this.totalBytes <= this.opts.maxBytes || oldest === key) break;
      this.totalBytes -= this.blocks.get(oldest).byteLength;
      this.blocks.delete(oldest);
    }
  }
  scheduleReadAhead(fileKey, blockIndex) {
    const last = this.lastBlock.get(fileKey);
    this.lastBlock.set(fileKey, blockIndex);
    if (this.opts.readAhead <= 0 || last === void 0 || blockIndex !== last + 1) return;
    const size = this.sizes.get(fileKey);
    const maxBlock = size !== void 0 ? Math.ceil(size / this.opts.blockSize) - 1 : Number.MAX_SAFE_INTEGER;
    for (let i = 1; i <= this.opts.readAhead; i++) {
      const next = blockIndex + i;
      if (next > maxBlock) break;
      const key = `${fileKey}:${next}`;
      if (!this.blocks.has(key) && !this.inflight.has(key)) {
        this.fetchBlock(fileKey, next, key).catch(() => {
        });
      }
    }
  }
};

// src/io/broker.ts
var IoBroker = class {
  constructor(config) {
    __publicField(this, "config");
    __publicField(this, "store");
    __publicField(this, "files", /* @__PURE__ */ new Map());
    __publicField(this, "statInflight", /* @__PURE__ */ new Map());
    this.config = config;
    this.store = new BlockStore({
      blockSize: config.blockSize,
      maxBytes: config.brokerCacheBytes,
      readAhead: config.readAhead,
      fetchRange: (fileKey, start, end) => this.fetchRange(fileKey, start, end)
    });
  }
  /** Wire one decode worker's channel (its MessagePort + SAB). */
  attachChannel(port, sab) {
    const channel = {
      port,
      ctrl: new Int32Array(sab, 0, HEADER_BYTES / 4),
      data: new Uint8Array(sab, HEADER_BYTES),
      latestSeq: 0
    };
    port.onmessage = (e) => this.onRequest(channel, e.data);
  }
  onRequest(channel, msg) {
    switch (msg.type) {
      case "read":
        channel.latestSeq = msg.seq;
        void this.handleRead(channel, msg.seq, msg.fileKey, msg.blockIndex);
        break;
      case "register-url":
        void this.handleRegisterUrl(channel, msg.rpcId, msg.fileKey, msg.url);
        break;
      case "register-files":
        for (const { fileKey, file, size } of msg.files) {
          const existing = this.files.get(fileKey);
          if (existing) {
            existing.refs++;
          } else {
            this.files.set(fileKey, { kind: "file", file, size, refs: 1 });
            this.store.setFileSize(fileKey, size);
          }
        }
        break;
      case "release":
        for (const fileKey of msg.fileKeys) {
          const entry = this.files.get(fileKey);
          if (!entry) continue;
          if (--entry.refs <= 0) {
            this.files.delete(fileKey);
            this.store.releaseFile(fileKey);
          }
        }
        break;
    }
  }
  async handleRead(channel, seq, fileKey, blockIndex) {
    let block;
    try {
      block = await this.store.getBlock(fileKey, blockIndex);
    } catch (err) {
      if (channel.latestSeq !== seq) return;
      console.error(`openslide-js broker: read failed for ${fileKey} block ${blockIndex}:`, err);
      Atomics.store(channel.ctrl, CTRL_ERRNO, EIO);
      Atomics.store(channel.ctrl, CTRL_SEQ, seq);
      Atomics.store(channel.ctrl, CTRL_STATUS, STATUS_ERROR);
      Atomics.notify(channel.ctrl, CTRL_STATUS);
      return;
    }
    if (channel.latestSeq !== seq) return;
    channel.data.set(block);
    Atomics.store(channel.ctrl, CTRL_BYTES, block.byteLength);
    Atomics.store(channel.ctrl, CTRL_SEQ, seq);
    Atomics.store(channel.ctrl, CTRL_STATUS, STATUS_OK);
    Atomics.notify(channel.ctrl, CTRL_STATUS);
  }
  async handleRegisterUrl(channel, rpcId, fileKey, url) {
    try {
      let entry = this.files.get(fileKey);
      if (!entry) {
        let pending = this.statInflight.get(fileKey);
        if (!pending) {
          pending = this.statUrl(url).finally(() => this.statInflight.delete(fileKey));
          this.statInflight.set(fileKey, pending);
        }
        const statted = await pending;
        entry = this.files.get(fileKey);
        if (!entry) {
          entry = statted;
          this.files.set(fileKey, entry);
          this.store.setFileSize(fileKey, entry.size);
        }
      }
      entry.refs++;
      channel.port.postMessage({ type: "rpc", rpcId, ok: true, size: entry.size });
    } catch (err) {
      channel.port.postMessage({
        type: "rpc",
        rpcId,
        ok: false,
        error: err instanceof Error ? err.message : String(err)
      });
    }
  }
  /**
   * Determine a remote file's size. Servers without range support get the
   * legacy createLazyFile treatment: the whole body is downloaded once and
   * served from memory.
   */
  async statUrl(url) {
    const resp = await fetch(url, { headers: { Range: "bytes=0-0" } });
    if (resp.status === 206) {
      const contentRange = resp.headers.get("Content-Range");
      const match = contentRange && /\/(\d+)\s*$/.exec(contentRange);
      await resp.body?.cancel();
      if (match) {
        return { kind: "url", url, size: parseInt(match[1], 10), refs: 0 };
      }
      throw new Error(`server sent 206 without a Content-Range total for ${url}`);
    }
    if (resp.ok) {
      const body = new Uint8Array(await resp.arrayBuffer());
      return { kind: "url", url, size: body.byteLength, refs: 0, wholeBody: body };
    }
    throw new Error(`HTTP ${resp.status} opening ${url}`);
  }
  async fetchRange(fileKey, start, end) {
    const entry = this.files.get(fileKey);
    if (!entry) throw new Error(`unknown file: ${fileKey}`);
    if (entry.kind === "file") {
      const want = end - start;
      let bytes = new Uint8Array(await entry.file.slice(start, end).arrayBuffer());
      if (bytes.byteLength < want) {
        console.warn(
          `openslide-js broker: short read for ${fileKey} bytes ${start}-${end} (${bytes.byteLength}/${want}); retrying`
        );
        bytes = new Uint8Array(await entry.file.slice(start, end).arrayBuffer());
      }
      return bytes;
    }
    if (entry.wholeBody) {
      return entry.wholeBody.subarray(start, end);
    }
    const resp = await fetch(entry.url, { headers: { Range: `bytes=${start}-${end - 1}` } });
    if (resp.status === 206) {
      return new Uint8Array(await resp.arrayBuffer());
    }
    if (resp.ok) {
      const body = new Uint8Array(await resp.arrayBuffer());
      entry.wholeBody = body;
      return body.subarray(start, end);
    }
    throw new Error(`HTTP ${resp.status} fetching bytes ${start}-${end - 1} of ${entry.url}`);
  }
};

// src/worker.ts
var api = null;
var broker = null;
var readQueue = new ReadQueue(4);
function reply(msg) {
  if (msg.ok && msg.result instanceof ArrayBuffer) {
    self.postMessage(msg, [msg.result]);
  } else {
    self.postMessage(msg);
  }
}
async function handleCommand(cmd) {
  try {
    let result;
    switch (cmd.cmd) {
      case "init-broker": {
        broker = new IoBroker(cmd.ioConfig);
        result = true;
        break;
      }
      case "attach-channel": {
        if (!broker) throw new Error("Worker is not in broker mode");
        broker.attachChannel(cmd.port, cmd.sab);
        result = true;
        break;
      }
      case "init": {
        let createModule;
        if (cmd.wasmUrl) {
          ({ default: createModule } = await import(
            /* webpackIgnore: true */
            cmd.wasmUrl
          ));
        } else {
          const gluePath = true ? "./wasm/openslide.js" : "./wasm/openslide.js";
          ({ default: createModule } = await import(gluePath));
        }
        const mod = await createModule(cmd.wasmBinary ? { wasmBinary: cmd.wasmBinary } : void 0);
        const syncIo = cmd.ioPort && cmd.ioSab && cmd.ioConfig ? new SyncIo(cmd.ioPort, cmd.ioSab, cmd.ioConfig) : null;
        if (cmd.ioConfig) {
          readQueue = new ReadQueue(cmd.ioConfig.maxConcurrentReads);
        }
        api = new WorkerApi(mod, syncIo);
        try {
          mod.FS.mkdir("/mnt");
        } catch {
        }
        result = true;
        break;
      }
      case "mountFile": {
        if (!api) throw new Error("Worker not initialized");
        result = api.mountFiles(cmd.files, cmd.mountId);
        break;
      }
      case "mountDir": {
        if (!api) throw new Error("Worker not initialized");
        result = api.mountDir(cmd.entries, cmd.indexFile, cmd.mountId);
        break;
      }
      case "mountUrl": {
        if (!api) throw new Error("Worker not initialized");
        result = await api.mountUrl(cmd.url, cmd.mountId);
        break;
      }
      case "unmount": {
        if (!api) throw new Error("Worker not initialized");
        api.unmount(cmd.mountId);
        result = true;
        break;
      }
      case "open": {
        if (!api) throw new Error("Worker not initialized");
        result = await api.open(cmd.mountId);
        break;
      }
      case "close": {
        if (!api) throw new Error("Worker not initialized");
        await api.close(cmd.handle);
        result = true;
        break;
      }
      case "getSlideInfo": {
        if (!api) throw new Error("Worker not initialized");
        result = await api.getSlideInfo(cmd.handle);
        break;
      }
      case "readRegion": {
        if (!api) throw new Error("Worker not initialized");
        await readQueue.acquire(cmd.id);
        try {
          result = await api.readRegion(cmd.handle, cmd.x, cmd.y, cmd.level, cmd.w, cmd.h);
        } finally {
          readQueue.release();
        }
        break;
      }
      case "readAssociatedImage": {
        if (!api) throw new Error("Worker not initialized");
        result = await api.readAssociatedImage(cmd.handle, cmd.name);
        break;
      }
      case "getAssociatedImageDimensions": {
        if (!api) throw new Error("Worker not initialized");
        result = await api.getAssociatedImageDimensions(cmd.handle, cmd.name);
        break;
      }
      case "detectVendor": {
        if (!api) throw new Error("Worker not initialized");
        result = await api.detectVendor(cmd.mountId);
        break;
      }
      case "getVersion": {
        if (!api) throw new Error("Worker not initialized");
        result = await api.getVersion();
        break;
      }
      case "getIccProfile": {
        if (!api) throw new Error("Worker not initialized");
        result = await api.getIccProfile(cmd.handle);
        break;
      }
      case "cancel": {
        result = readQueue.cancel(cmd.targetId);
        break;
      }
      default:
        throw new Error(`Unknown command: ${cmd.cmd}`);
    }
    reply({ id: cmd.id, ok: true, result });
  } catch (err) {
    if (err instanceof ReadCancelledError) {
      reply({ id: cmd.id, ok: false, error: err.message, code: err.code });
      return;
    }
    let errMsg;
    if (err instanceof Error) {
      errMsg = err.message || err.toString();
    } else if (typeof err === "string") {
      errMsg = err;
    } else if (err && typeof err === "object") {
      const parts = [];
      if ("errno" in err) parts.push(`errno=${err.errno}`);
      if ("code" in err) parts.push(`code=${err.code}`);
      if ("message" in err) parts.push(`message=${err.message}`);
      errMsg = parts.length > 0 ? parts.join(", ") : JSON.stringify(err);
    } else {
      errMsg = String(err);
    }
    reply({ id: cmd.id, ok: false, error: errMsg });
  }
}
self.onmessage = (e) => {
  handleCommand(e.data);
};
