/**
 * The browser node's TIFF reader: one (OME-)TIFF the user picked, read in a
 * worker of this page, answering Plexora-grid tile requests with the bytes the
 * viewer's HD path uploads -- the website's tiles, without Plexora's Python
 * between the file and the screen (services/browserNode.js decides when it
 * may answer; Python answers the rest).
 *
 * From the browser-native investigation's proof (tools/poc_browser_node,
 * P0-P7), where it was held to Python's tiles pixel for pixel; unchanged here
 * but for this header.
 *
 * What the product's tile path expects, and so what this returns:
 *  - a channel tile is `u16`: 2 bytes a pixel, BIG-endian (the HD PNG's sample
 *    order; glInit.js uploads it as RG8UI and frag.glsl rebuilds the value
 *    from r and g). geotiff.js hands back native little-endian, so it is
 *    swapped here, off the main thread.
 *  - a label tile is `u32`: the uint32 id's 4 little-endian bytes per pixel,
 *    which is exactly the RGBA8 layout data_model.read_tile builds for the
 *    node's label PNG (alpha the high byte: 0 for ids < 2^24).
 *  - an edge tile is the clipped size, as `read_tile` slices it.
 *
 * createTiffReader() -> {open, tile, level, region}; workers/tiffWorker.js puts
 * it behind messages.
 *   open({file|url, size, decoder: "native"|"native-blob"|"pako", prefetchOffsets,
 *         record, seed})                         -- `record`: the result's `seed` is
 *                                                 every slice the open read; `seed`:
 *                                                 open from such slices (SeededSource)
 *   tile({channel, level, tx, ty, tw, th, raw})  -- `timing` says where it went
 *   region({channel, level, x0, y0, x1, y1})     -- a window as native values
 *   level({channel, level})                      -- a whole (small) level, raw
 *   max({channel, level, windows})               -- the largest sample over windows
 *   regions({reads: [{channel, level, box}]})     -- rectangles as native values
 */
import { addDecoder, BaseDecoder, GeoTIFF, GeoTIFFImage } from "geotiff";
import pako from "pako"; // CommonJS: Node takes no named import from it
import { lzwDecode } from "./lzw.js";

const now = () => performance.now();

//: Deflate's two TIFF codes (8 Adobe, 32946 the old one).
const DEFLATE = [8, 32946];

/** Decode-time accounting shared by both decoders. */
const decodeClock = { ms: 0, calls: 0 };

/**
 * Bytes through a CompressionStream or DecompressionStream, written straight
 * into it. (Wrapping them in a Blob first, as `new Blob([b]).stream()`, hands
 * every byte to the browser process's blob store and back: a copy each way,
 * and at gigabytes in flight the store refuses -- "Failed to fetch".)
 */
export async function throughStream(bytes, transform) {
    const writer = transform.writable.getWriter();
    writer.write(bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes)).catch(() => {});
    writer.close().catch(() => {});
    return new Response(transform.readable).arrayBuffer();
}

/** zlib inflate in the browser's own code, the bytes written straight in. */
class NativeDeflate extends BaseDecoder {
    async decodeBlock(buffer) {
        const t0 = now();
        const out = await throughStream(buffer, new DecompressionStream("deflate"));
        decodeClock.ms += now() - t0;
        decodeClock.calls += 1;
        return out;
    }
}

/**
 * The same inflate fed through a Blob, as the product's HD tile worker
 * (workers/tileDecoder.js) feeds it: kept to measure what the Blob costs.
 */
class NativeDeflateBlob extends BaseDecoder {
    async decodeBlock(buffer) {
        const t0 = now();
        const stream = new Blob([buffer]).stream().pipeThrough(new DecompressionStream("deflate"));
        const out = await new Response(stream).arrayBuffer();
        decodeClock.ms += now() - t0;
        decodeClock.calls += 1;
        return out;
    }
}

/** geotiff.js's own deflate (pako, JavaScript), timed the same way. */
class PakoDeflate extends BaseDecoder {
    decodeBlock(buffer) {
        const t0 = now();
        const out = pako.inflate(new Uint8Array(buffer)).buffer;
        decodeClock.ms += now() - t0;
        decodeClock.calls += 1;
        return out;
    }
}

/** LZW by node/lzw.js: geotiff.js's own fails on tifffile's tiles. */
class Lzw extends BaseDecoder {
    decodeBlock(buffer) {
        const t0 = now();
        const { tileWidth, tileHeight, bitsPerSample, planarConfiguration, samplesPerPixel = 1 } = this.parameters;
        const bits = Array.isArray(bitsPerSample) || ArrayBuffer.isView(bitsPerSample)
            ? Array.from(bitsPerSample) : [bitsPerSample];
        const pixelBytes = planarConfiguration === 2 ? Math.ceil(bits[0] / 8)
            : bits.reduce((sum, b) => sum + Math.ceil(b / 8), 0) || samplesPerPixel;
        const out = lzwDecode(new Uint8Array(buffer), tileWidth * tileHeight * pixelBytes);
        decodeClock.ms += now() - t0;
        decodeClock.calls += 1;
        return out.byteLength === out.buffer.byteLength ? out.buffer : out.slice().buffer;
    }
}

function useDecoder(kind) {
    addDecoder(DEFLATE, async () => ({ pako: PakoDeflate, "native-blob": NativeDeflateBlob }[kind] || NativeDeflate));
    addDecoder(5, async () => Lzw);
}

/**
 * A File as geotiff.js's source, read with Blob.slice().arrayBuffer() and
 * counted, so every record says how many bytes a tile cost and how long the
 * reads took.
 */
class CountingBlobSource {
    constructor(blob) {
        this.blob = blob;
        this.bytes = 0;
        this.reads = 0;
        this.readMs = 0;
    }

    get fileSize() {
        return this.blob.size;
    }

    async fetch(slices) {
        const t0 = now();
        const out = await Promise.all(slices.map(({ offset, length }) =>
            this.blob.slice(offset, offset + length).arrayBuffer()));
        this.readMs += now() - t0;
        this.reads += slices.length;
        for (const s of slices) this.bytes += s.length;
        return out;
    }
}

/**
 * The same counters over HTTP range requests: a bucket with CORS, or the
 * desktop app's `plexora-file` scheme (desktop/src-tauri/src/localfiles.rs),
 * which says the file's size when the shell grants it (`size`).
 */
class CountingUrlSource {
    constructor(url, size = null) {
        this.url = url;
        this.bytes = 0;
        this.reads = 0;
        this.readMs = 0;
        this.size = Number.isFinite(size) && size > 0 ? size : null;
    }

    get fileSize() {
        return this.size;
    }

    async fetch(slices) {
        const t0 = now();
        const out = await Promise.all(slices.map(async ({ offset, length }) => {
            const response = await fetch(this.url, {
                headers: { Range: `bytes=${offset}-${offset + length - 1}` },
            });
            if (response.status !== 206 && response.status !== 200) {
                throw new Error(`range read answered ${response.status}`);
            }
            const range = response.headers.get("Content-Range");
            if (range && this.size === null) this.size = Number(range.split("/")[1]) || null;
            const body = await response.arrayBuffer();
            return response.status === 200 ? body.slice(offset, offset + length) : body;
        }));
        this.readMs += now() - t0;
        this.reads += slices.length;
        for (const s of slices) this.bytes += s.length;
        return out;
    }
}

/**
 * Another source, answered first from `seed`: the slices a worker of the same
 * pool already read while it opened the file ({offset, length, buffer} each,
 * `length` the range asked for, `buffer` what came back). With `recorded` an
 * array, every slice read through the source is added to it -- the seed the
 * pool's other workers open from (services/browserNode.js): a file whose
 * header and IFDs are scattered over gigabytes costs one chain of reads per
 * pool, not one per worker. Answers are the same bytes, so the same parse.
 */
class SeededSource {
    constructor(inner, seed = null) {
        this.inner = inner;
        this.seed = Array.isArray(seed) ? seed.filter((s) => s && s.buffer) : [];
        this.recorded = null;
    }

    get fileSize() { return this.inner.fileSize; }
    get bytes() { return this.inner.bytes; }
    get reads() { return this.inner.reads; }
    get readMs() { return this.inner.readMs; }

    /** The seeded bytes of one asked slice, or null. */
    fromSeed({ offset, length }) {
        for (const s of this.seed) {
            if (offset >= s.offset && offset + length <= s.offset + s.length) {
                const from = offset - s.offset;
                return s.buffer.slice(from, from + length);
            }
        }
        return null;
    }

    async fetch(slices, signal) {
        const out = slices.map((slice) => (this.seed.length ? this.fromSeed(slice) : null));
        const missing = [];
        out.forEach((buffer, i) => { if (!buffer) missing.push(i); });
        if (missing.length) {
            const got = await this.inner.fetch(missing.map((i) => slices[i]), signal);
            missing.forEach((i, k) => {
                out[i] = got[k];
                if (this.recorded) {
                    this.recorded.push({ offset: slices[i].offset, length: slices[i].length, buffer: got[k].slice(0) });
                }
            });
        }
        return out;
    }
}

/** Channel names and pixel size out of the OME-XML, when there is one. */
function parseOme(xml) {
    if (!xml || !xml.includes("<OME")) return { names: null, physicalSize: null, sizeC: null };
    const pixels = /<Pixels\b[^>]*>/.exec(xml)?.[0] || "";
    const attr = (tag, name) => new RegExp(`\\b${name}="([^"]*)"`).exec(tag)?.[1];
    const names = [];
    const channel = /<Channel\b[^>]*>/g;
    let m;
    while ((m = channel.exec(xml))) names.push(attr(m[0], "Name") ?? null);
    const size = Number(attr(pixels, "PhysicalSizeX"));
    return {
        names: names.length ? names : null,
        physicalSize: Number.isFinite(size) ? { value: size, unit: attr(pixels, "PhysicalSizeXUnit") || "µm" } : null,
        sizeC: Number(attr(pixels, "SizeC")) || null,
    };
}

async function tagValue(ifd, tag) {
    if (!ifd.hasTag(tag)) return undefined;
    return ifd.loadValue(tag);
}

/**
 * The file's main IFDs parsed at `offsets` all at once, or null. A file's
 * IFDs are a chain, each saying where the next is, so walking it is one read
 * after another -- forty for a forty-channel OME-TIFF whose IFDs sit beside
 * each plane's gigabytes. Python knows the offsets (plexora/browser/
 * node_spec.py `ifds`); they are taken only when they ARE the chain: the
 * first is the header's, each IFD's next is the following offset, the last
 * IFD's next is 0. Anything else -- or a parse that fails -- is null, and
 * the chain is walked.
 */
async function mainIfdsAt(tiff, offsets) {
    if (!Array.isArray(offsets) || !offsets.length || offsets.length > 4096) return null;
    if (Number(offsets[0]) !== Number(tiff.firstIFDOffset)) return null;
    const size = tiff.source && tiff.source.fileSize;
    if (offsets.some((offset) => !Number.isSafeInteger(Number(offset)) || Number(offset) <= 0
                     || (size && Number(offset) >= size))) return null;
    let parsed;
    try {
        parsed = await Promise.all(offsets.map((offset) => tiff.parser.parseFileDirectoryAt(Number(offset))));
    } catch (error) {
        return null;
    }
    for (let i = 0; i < parsed.length; i += 1) {
        const next = i + 1 < offsets.length ? Number(offsets[i + 1]) : 0;
        if (Number(parsed[i].nextIFDByteOffset) !== next) return null;
    }
    return parsed;
}

export function createTiffReader() {
    let state = null;

    /**
     * Open the file and work out its layout. Three are handled:
     *  - SUBIFD: one main IFD per channel at full resolution, its reduced levels
     *    as SubIFDs (bioformats / Plexora's own pyramids, every fixture here);
     *  - SERIES: main IFDs that shrink, channels repeated per level (old OME
     *    pyramids written without SubIFDs);
     *  - FLAT: one level, channels as main IFDs of the same size.
     * Only the main IFDs are parsed here; a level's IFD is parsed on first use.
     */
    async function open({ file, url, size = null, source: given, decoder = "native", prefetchOffsets = true,
                          seed = null, record = false, ifds = null }) {
        useDecoder(decoder);
        decodeClock.ms = 0;
        decodeClock.calls = 0;
        const t0 = now();
        // `given` is any {fetch(slices), fileSize} with the same counters (Node tests
        // read through a file handle: fs.openAsBlob truncates sizes past 4 GiB).
        // `seed`: what another worker read opening this file (`record`), answered
        // from memory (SeededSource).
        const source = new SeededSource(given || (file ? new CountingBlobSource(file) : new CountingUrlSource(url, size)),
                                        seed);
        if (record) source.recorded = [];
        const tiff = await GeoTIFF.fromSource(source);
        const tHeader = now();
        // The main IFDs: all at once where the caller knows where they are
        // (`ifds`, checked against the chain they make), else one after another.
        let main = await mainIfdsAt(tiff, ifds);
        const hinted = Boolean(main);
        if (!main) {
            main = [];
            for (let i = 0; ; i += 1) {
                try {
                    main.push(await tiff.requestIFD(i));
                } catch (error) {
                    if (error?.name === "GeoTIFFImageIndexError" || /index/i.test(String(error?.message))) break;
                    throw error;
                }
                if (main.length > 4096) throw new Error("more than 4096 main IFDs");
            }
        }
        const tMain = now();
        const first = new GeoTIFFImage(main[0], tiff.littleEndian, false, source);
        const width = first.getWidth();
        const height = first.getHeight();
        const description = await tagValue(main[0], "ImageDescription");
        const ome = parseOme(typeof description === "string" ? description : "");
        const subifds = await tagValue(main[0], "SubIFDs");

        const imageOf = (ifd) => new GeoTIFFImage(ifd, tiff.littleEndian, false, source);
        // images[channel](level) -> Promise<GeoTIFFImage>, parsed on first use.
        const images = [];
        let layout;
        let levelSizes;
        if (subifds && subifds.length) {
            layout = "subifd";
            const full = [];
            for (const ifd of main) {
                const w = Number(await tagValue(ifd, "ImageWidth"));
                const h = Number(await tagValue(ifd, "ImageLength"));
                if (w === width && h === height) full.push(ifd);
            }
            const channels = ome.sizeC ? full.slice(0, ome.sizeC) : full;
            for (const ifd of channels) {
                const parsed = new Map([[0, Promise.resolve(imageOf(ifd))]]);
                let offsets = null;
                images.push((level) => {
                    if (level < 0 || level > subifds.length) return Promise.resolve(null);
                    if (!parsed.has(level)) {
                        parsed.set(level, (async () => {
                            offsets ||= Array.from(await ifd.loadValue("SubIFDs"), Number);
                            return imageOf(await tiff.parser.parseFileDirectoryAt(offsets[level - 1]));
                        })());
                    }
                    return parsed.get(level);
                });
            }
            // Level sizes from channel 0's SubIFDs: one small IFD parse each,
            // all at once (their offsets are in hand).
            const reduced = await Promise.all(Array.from(subifds, (_, i) => images[0](i + 1)));
            levelSizes = [{ width, height }, ...reduced.map((image) => ({ width: image.getWidth(), height: image.getHeight() }))];
        } else {
            const sizes = [];
            for (const ifd of main) {
                sizes.push([Number(await tagValue(ifd, "ImageWidth")), Number(await tagValue(ifd, "ImageLength"))]);
            }
            const distinct = [...new Set(sizes.map((s) => s.join("x")))];
            if (distinct.length === 1) {
                layout = "flat";
                for (const ifd of main) {
                    const only = Promise.resolve(imageOf(ifd));
                    images.push((level) => Promise.resolve(level === 0 ? only : null));
                }
                levelSizes = [{ width, height }];
            } else {
                layout = "series";
                const perLevel = sizes.filter((s) => s[0] === width && s[1] === height).length;
                const levels = Math.floor(main.length / perLevel);
                for (let c = 0; c < perLevel; c += 1) {
                    images.push((level) => Promise.resolve(
                        level >= 0 && level < levels ? imageOf(main[level * perLevel + c]) : null));
                }
                levelSizes = Array.from({ length: levels }, (_, l) => ({
                    width: sizes[l * perLevel][0], height: sizes[l * perLevel][1],
                }));
            }
        }
        const bits = Number(first.getBitsPerSample());
        const sampleFormat = Number(first.getSampleFormat());
        const compression = Number(main[0].getValue("Compression") || 1);
        const predictor = Number((await tagValue(main[0], "Predictor")) || 1);
        state = {
            tiff, source, images, layout, prefetchOffsets,
            prefetched: new WeakSet(),
            littleEndian: tiff.littleEndian,
        };
        const geometry = {
            layout, width, height,
            numChannels: images.length,
            levels: levelSizes,
            fileTile: { width: first.getTileWidth(), height: first.getTileHeight(), tiled: first.isTiled },
            bitsPerSample: bits,
            sampleFormat,
            compression,
            predictor,
            littleEndian: tiff.littleEndian,
            bigTiff: tiff.bigTiff,
            names: ome.names,
            physicalSize: ome.physicalSize,
            mainIfds: main.length,
            size: source.fileSize,
        };
        const opened = {
            geometry,
            timing: {
                openMs: now() - t0, headerMs: tHeader - t0, mainIfdsMs: tMain - tHeader, hinted,
                reads: source.reads, bytes: source.bytes, readMs: source.readMs,
            },
        };
        if (record) {
            opened.seed = source.recorded;
            source.recorded = null;
        }
        return opened;
    }

    async function image(channel, level) {
        if (!state) throw new Error("open a file first");
        const levels = state.images[channel];
        if (!levels) throw new Error(`no channel ${channel}`);
        const img = await levels(level);
        if (!img) throw new Error(`no level ${level}`);
        if (state.prefetchOffsets && !state.prefetched.has(img)) {
            // One read for the whole offset and byte-count arrays instead of two
            // tiny reads per tile (geotiff.js loads deferred arrays per index).
            state.prefetched.add(img);
            const tags = img.isTiled ? ["TileOffsets", "TileByteCounts"] : ["StripOffsets", "StripByteCounts"];
            await Promise.all(tags.map((tag) => img.fileDirectory.loadValue(tag)));
        }
        return img;
    }

    /** Native little-endian uint16 -> the big-endian bytes frag.glsl reads. */
    function toBigEndian16(values) {
        const out = new Uint16Array(values.length);
        for (let i = 0; i < values.length; i += 1) {
            const v = values[i];
            out[i] = ((v & 0xff) << 8) | (v >>> 8);
        }
        return new Uint8Array(out.buffer);
    }

    async function tile({ channel, level, tx, ty, tw = 1024, th = 1024, raw = false }) {
        const t0 = now();
        const reads0 = state.source.reads;
        const bytes0 = state.source.bytes;
        const readMs0 = state.source.readMs;
        const decode0 = decodeClock.ms;
        const img = await image(channel, level);
        const W = img.getWidth();
        const H = img.getHeight();
        const x0 = tx * tw;
        const y0 = ty * th;
        const x1 = Math.min(W, x0 + tw);
        const y1 = Math.min(H, y0 + th);
        if (x0 >= W || y0 >= H) {
            return { width: 0, height: 0, array: new Uint8Array(0), format: "empty", timing: { totalMs: now() - t0 } };
        }
        const tRead = now();
        const [values] = await img.readRasters({ window: [x0, y0, x1, y1], samples: [0] });
        const tDecoded = now();
        let array;
        let format;
        const bits = img.getBitsPerSample();
        if (raw) {
            array = new Uint8Array(values.buffer, values.byteOffset, values.byteLength);
            format = `raw${bits}`;
        } else if (bits === 16) {
            array = toBigEndian16(values);
            format = "u16";
        } else if (bits === 32) {
            // A label tile: the id's little-endian bytes are already RGBA8.
            array = new Uint8Array(values.buffer, values.byteOffset, values.byteLength);
            if (!state.littleEndian) throw new Error("big-endian label TIFFs are not handled");
            format = "u32";
        } else if (bits === 8) {
            array = new Uint8Array(values.buffer, values.byteOffset, values.byteLength);
            format = "u8";
        } else {
            throw new Error(`${bits}-bit samples are not handled`);
        }
        const tEnd = now();
        return {
            width: x1 - x0, height: y1 - y0, array, format,
            timing: {
                totalMs: tEnd - t0,
                rasterMs: tDecoded - tRead,
                readMs: state.source.readMs - readMs0,
                decodeMs: decodeClock.ms - decode0,
                swapMs: tEnd - tDecoded,
                reads: state.source.reads - reads0,
                bytes: state.source.bytes - bytes0,
            },
        };
    }

    /** The typed array kind of an image's samples. */
    function arrayKind(img) {
        const bits = Number(img.getBitsPerSample());
        const format = Number(img.getSampleFormat?.() ?? 1);
        if (format === 3) return bits === 64 ? Float64Array : Float32Array;
        if (format === 2) return { 8: Int8Array, 16: Int16Array, 32: Int32Array }[bits];
        return { 8: Uint8Array, 16: Uint16Array, 32: Uint32Array }[bits];
    }

    /** A big-endian file's samples in this machine's (little-endian) order. */
    function fromBigEndian(buffer, Kind) {
        const n = Kind.BYTES_PER_ELEMENT;
        if (n === 1) return buffer;
        const view = new DataView(buffer);
        const out = new Kind(buffer.byteLength / n);
        const get = Kind === Float32Array ? "getFloat32" : Kind === Float64Array ? "getFloat64"
            : `get${Kind.name.replace("Array", "")}`;
        for (let k = 0; k < out.length; k += 1) out[k] = view[get](k * n, false);
        return out.buffer;
    }

    /**
     * Rows of an uncompressed stripped image straight from the file: a row is
     * at a fixed offset, so a band costs one read per strip it crosses.
     * geotiff.js would read each whole strip, and a flat image written without
     * compression is often one strip per plane, gigabytes long.
     */
    async function uncompressedRows(img, x0, y0, x1, y1) {
        const Kind = arrayKind(img);
        const W = img.getWidth();
        const rowBytes = W * Kind.BYTES_PER_ELEMENT;
        const rps = Number(img.fileDirectory.getValue("RowsPerStrip") || img.getHeight());
        const offsets = await img.fileDirectory.loadValue("StripOffsets");
        const slices = [];
        for (let s = Math.floor(y0 / rps); s * rps < y1; s += 1) {
            const r0 = Math.max(y0, s * rps);
            const r1 = Math.min(y1, (s + 1) * rps);
            slices.push({ offset: Number(offsets[s]) + (r0 - s * rps) * rowBytes, length: (r1 - r0) * rowBytes, r0, r1 });
        }
        const buffers = await state.source.fetch(slices.map(({ offset, length }) => ({ offset, length })));
        const w = x1 - x0;
        const out = new Kind(w * (y1 - y0));
        slices.forEach((s, i) => {
            const rows = new Kind(state.littleEndian ? buffers[i] : fromBigEndian(buffers[i], Kind));
            if (x0 === 0 && w === W) { out.set(rows, (s.r0 - y0) * w); return; }
            for (let r = s.r0; r < s.r1; r += 1) {
                const from = (r - s.r0) * W + x0;
                out.set(rows.subarray(from, from + w), (r - y0) * w);
            }
        });
        return out;
    }

    /**
     * Rows of a deflate-compressed stripped image: the strips a band crosses
     * are read in one go (they sit back to back in every writer's output) and
     * inflated by the browser, instead of geotiff.js's one read and one
     * decoder call per strip -- thousands for a band of a wide image.
     */
    async function deflatedRows(img, x0, y0, x1, y1) {
        const Kind = arrayKind(img);
        const W = img.getWidth();
        const H = img.getHeight();
        const rps = Number(img.fileDirectory.getValue("RowsPerStrip") || H);
        const offsets = await img.fileDirectory.loadValue("StripOffsets");
        const counts = await img.fileDirectory.loadValue("StripByteCounts");
        const s0 = Math.floor(y0 / rps);
        const s1 = Math.ceil(y1 / rps);
        let lo = Infinity, hi = 0, sum = 0;
        for (let s = s0; s < s1; s += 1) {
            lo = Math.min(lo, Number(offsets[s]));
            hi = Math.max(hi, Number(offsets[s]) + Number(counts[s]));
            sum += Number(counts[s]);
        }
        // One read when the strips are (nearly) contiguous; else one per strip.
        let pieces;
        if (hi - lo <= 1.5 * sum) {
            const [all] = await state.source.fetch([{ offset: lo, length: hi - lo }]);
            pieces = [];
            for (let s = s0; s < s1; s += 1) pieces.push(new Uint8Array(all, Number(offsets[s]) - lo, Number(counts[s])));
        } else {
            const slices = [];
            for (let s = s0; s < s1; s += 1) slices.push({ offset: Number(offsets[s]), length: Number(counts[s]) });
            pieces = (await state.source.fetch(slices)).map((b) => new Uint8Array(b));
        }
        const t0 = now();
        const inflated = await Promise.all(pieces.map((bytes) => throughStream(bytes, new DecompressionStream("deflate"))));
        decodeClock.ms += now() - t0;
        decodeClock.calls += pieces.length;
        const w = x1 - x0;
        const out = new Kind(w * (y1 - y0));
        inflated.forEach((buffer, i) => {
            const s = s0 + i;
            const rows = new Kind(state.littleEndian ? buffer : fromBigEndian(buffer, Kind));
            const r0 = Math.max(y0, s * rps);
            const r1 = Math.min(y1, (s + 1) * rps, H);
            for (let r = r0; r < r1; r += 1) {
                const from = (r - s * rps) * W + x0;
                out.set(rows.subarray(from, from + w), (r - y0) * w);
            }
        });
        return out;
    }

    /**
     * A window of one channel as native values, whatever the layout (tiles or
     * strips, any codec geotiff.js knows; uncompressed and plain-deflate strips
     * read directly).
     */
    async function region({ channel, level: index = 0, x0, y0, x1, y1 }) {
        const t0 = now();
        const decode0 = decodeClock.ms;
        const readMs0 = state.source.readMs;
        const bytes0 = state.source.bytes;
        const img = await image(channel, index);
        const compression = Number(img.fileDirectory.getValue("Compression") || 1);
        const predictor = Number(img.fileDirectory.getValue("Predictor") || 1);
        const single = Number(img.getSamplesPerPixel()) === 1;
        let values;
        if (!img.isTiled && compression === 1 && single) {
            values = await uncompressedRows(img, x0, y0, x1, y1);
        } else if (!img.isTiled && DEFLATE.includes(compression) && predictor === 1 && single) {
            values = await deflatedRows(img, x0, y0, x1, y1);
        } else {
            [values] = await img.readRasters({ window: [x0, y0, x1, y1], samples: [0] });
        }
        return {
            values, width: x1 - x0, height: y1 - y0,
            timing: { totalMs: now() - t0, decodeMs: decodeClock.ms - decode0,
                      readMs: state.source.readMs - readMs0, bytes: state.source.bytes - bytes0 },
        };
    }

    /** A whole level of one channel as native values (for statistics). */
    async function level({ channel, level: index }) {
        const t0 = now();
        const img = await image(channel, index);
        const [values] = await img.readRasters({ samples: [0] });
        return {
            width: img.getWidth(), height: img.getHeight(),
            array: new Uint8Array(values.buffer, values.byteOffset, values.byteLength),
            bits: img.getBitsPerSample(),
            timing: { totalMs: now() - t0 },
        };
    }

    //: Each tiled image's reading plan (`tilePlan`), once.
    const plans = new WeakMap();

    /**
     * How a tiled plane is read fast (`tiledMax`, `tiledRegion`), or null for
     * one `region` reads instead: another codec, a floating point predictor,
     * several samples a pixel. Its tiles' offsets and byte counts, read once.
     */
    async function tilePlan(img) {
        if (plans.has(img)) return plans.get(img);
        let plan = null;
        const compression = Number(img.fileDirectory.getValue("Compression") || 1);
        const predictor = Number(img.fileDirectory.getValue("Predictor") || 1);
        const Kind = img.isTiled && Number(img.getSamplesPerPixel()) === 1 ? arrayKind(img) : null;
        if (Kind && [1, ...DEFLATE].includes(compression) && [1, 2].includes(predictor)
            && !(predictor === 2 && (Kind === Float32Array || Kind === Float64Array))) {
            const W = img.getWidth();
            const H = img.getHeight();
            const tw = img.getTileWidth();
            const th = img.getTileHeight();
            plan = {
                compression, predictor, Kind, W, H, tw, th,
                across: Math.ceil(W / tw), down: Math.ceil(H / th),
                tileBytes: tw * th * Kind.BYTES_PER_ELEMENT,
                offsets: await img.fileDirectory.loadValue("TileOffsets"),
                counts: await img.fileDirectory.loadValue("TileByteCounts"),
            };
        }
        plans.set(img, plan);
        return plan;
    }

    /** A window's tiles as reads of tiles back to back: [{offset, length, tiles: [index]}]. */
    function readsOf(plan, [x0, y0, x1, y1]) {
        const reads = [];
        for (let ty = Math.floor(y0 / plan.th); ty * plan.th < y1 && ty < plan.down; ty += 1) {
            let run = null;
            for (let tx = Math.floor(x0 / plan.tw); tx * plan.tw < x1 && tx < plan.across; tx += 1) {
                const i = ty * plan.across + tx;
                const at = Number(plan.offsets[i]);
                const n = Number(plan.counts[i]);
                if (run && at === run.offset + run.length) {
                    run.length += n;
                    run.tiles.push(i);
                } else {
                    run = { offset: at, length: n, tiles: [i] };
                    reads.push(run);
                }
            }
        }
        return reads;
    }

    /** A window's tiles' bytes, read (`decodeTiles` decodes them). */
    function fetchTiles(plan, window) {
        const reads = readsOf(plan, window);
        const real = reads.filter((read) => read.length > 0);
        return state.source.fetch(real.map(({ offset, length }) => ({ offset, length })))
            .then((buffers) => ({ reads, real, buffers }));
    }

    /** `fn(index, samples)` for every tile fetched, in this machine's byte
     *  order, the predictor not yet undone; `samples` null for a tile with no
     *  bytes (tifffile's zeros). */
    async function decodeTiles(plan, fetched, fn) {
        for (const read of fetched.reads) if (read.length === 0) for (const i of read.tiles) fn(i, null);
        for (let r = 0; r < fetched.real.length; r += 1) {
            let from = 0;
            for (const i of fetched.real[r].tiles) {
                const n = Number(plan.counts[i]);
                let bytes = fetched.buffers[r].slice(from, from + n);
                from += n;
                if (!n) {
                    fn(i, null);
                    continue;
                }
                if (plan.compression !== 1) {
                    const t1 = now();
                    bytes = await throughStream(bytes, new DecompressionStream("deflate"));
                    decodeClock.ms += now() - t1;
                    decodeClock.calls += 1;
                }
                if (!state.littleEndian) bytes = fromBigEndian(bytes, plan.Kind);
                fn(i, new plan.Kind(bytes, 0, Math.min(plan.tileBytes, bytes.byteLength) / plan.Kind.BYTES_PER_ELEMENT));
            }
        }
    }

    /** The part of tile `i` inside both the image and `window`, in the tile's
     *  own pixels, its rows' predictor undone in place (along each row from
     *  its first sample, in the sample type: the byte order settled first,
     *  which geotiff.js does not for a big-endian file). */
    function settle(plan, i, samples, [x0, y0, x1, y1]) {
        const ox = (i % plan.across) * plan.tw;
        const oy = Math.floor(i / plan.across) * plan.th;
        const left = Math.max(0, x0 - ox);
        const right = Math.min(plan.tw, plan.W - ox, x1 - ox);
        const top = Math.max(0, y0 - oy);
        const bottom = Math.min(plan.th, plan.H - oy, y1 - oy, Math.floor(samples.length / plan.tw));
        if (plan.predictor === 2) {
            for (let y = top; y < bottom; y += 1) {
                const o = y * plan.tw;
                for (let x = o + 1, end = o + right; x < end; x += 1) samples[x] += samples[x - 1];
            }
        }
        return { ox, oy, left, right, top, bottom };
    }

    /**
     * `max` of a tiled plane at the speed the browser reads and inflates
     * (a 66 GB slide's channel: 3 s, as the desktop's own read, not the 7-9 s
     * of `region` per window, which assembles each window through geotiff.js
     * only for its largest value to be taken): each window's tiles in a few
     * large reads (a row of tiles sits back to back in the file), the next
     * window's read while this one is decoded, the browser's zlib, and the
     * maximum taken over each tile in place -- over the image only, an edge
     * tile's padding not being the image's, and an empty tile counting as 0,
     * as Python reads them. null for a file `tilePlan` leaves to `region`.
     */
    async function tiledMax(img, windows) {
        const plan = await tilePlan(img);
        if (!plan) return null;
        let top = -Infinity;
        let next = windows.length ? fetchTiles(plan, windows[0]) : null;
        for (let w = 0; w < windows.length; w += 1) {
            const fetched = await next;
            next = w + 1 < windows.length ? fetchTiles(plan, windows[w + 1]) : null;
            await decodeTiles(plan, fetched, (i, samples) => {
                if (!samples) {
                    top = Math.max(top, 0);
                    return;
                }
                const part = settle(plan, i, samples, windows[w]);
                for (let y = part.top; y < part.bottom; y += 1) {
                    const o = y * plan.tw;
                    for (let x = o + part.left, end = o + part.right; x < end; x += 1) if (samples[x] > top) top = samples[x];
                }
            });
        }
        return top;
    }

    /**
     * A window of a tiled plane as `region` answers it (native values, rows
     * of x1 - x0), read as `tiledMax` reads: its tiles in large reads,
     * inflated by the browser, each copied in with its predictor undone. The
     * window is inside the plane (Python clips it). null for a file
     * `tilePlan` leaves to `region`.
     */
    async function tiledRegion(img, x0, y0, x1, y1) {
        const plan = await tilePlan(img);
        if (!plan) return null;
        const w = x1 - x0;
        const out = new plan.Kind(w * (y1 - y0));
        const window = [x0, y0, x1, y1];
        await decodeTiles(plan, await fetchTiles(plan, window), (i, samples) => {
            if (!samples) return;
            const part = settle(plan, i, samples, window);
            for (let y = part.top; y < part.bottom; y += 1) {
                const o = y * plan.tw;
                out.set(samples.subarray(o + part.left, o + part.right),
                        (part.oy + y - y0) * w + (part.ox + part.left - x0));
            }
        });
        return out;
    }

    /**
     * Python's rectangles of a picked image, read here (data_model's cell
     * crops: services/browserNode.js `regionsOf`). `reads`: [{channel, level,
     * box: [x0, y0, x1, y1]}], each inside its level. {regions: [{values,
     * width, height} | {refused}]}: refused where geotiff.js would read other
     * values than Python (a big-endian file with a predictor, outside the
     * tiled fast path), which Python then reads itself.
     */
    async function regions({ reads }) {
        const found = [];
        for (const { channel, level: index, box } of reads) {
            const [x0, y0, x1, y1] = box.map(Number);
            const img = await image(channel, index);
            let values = await tiledRegion(img, x0, y0, x1, y1);
            if (values === null) {
                if (!state.littleEndian && Number(img.fileDirectory.getValue("Predictor") || 1) !== 1) {
                    found.push({ refused: "a big-endian file with a predictor" });
                    continue;
                }
                ({ values } = await region({ channel, level: index, x0, y0, x1, y1 }));
            }
            found.push({ values, width: x1 - x0, height: y1 - y0 });
        }
        return { regions: found };
    }

    /**
     * The largest sample of one channel's level over `windows` ([x0, y0, x1,
     * y1] each, read one at a time): what `data_model.quantization_window_of`
     * reads every full-resolution pixel for, a part of the plane per worker.
     */
    async function max({ channel, level: index = 0, windows }) {
        const t0 = now();
        const fast = await tiledMax(await image(channel, index), windows);
        if (fast !== null) return { max: fast, windows: windows.length, timing: { totalMs: now() - t0, tiled: true } };
        let top = -Infinity;
        for (const window of windows) {
            const { values } = await region({ channel, level: index, x0: window[0], y0: window[1],
                                              x1: window[2], y1: window[3] });
            for (let i = 0; i < values.length; i += 1) if (values[i] > top) top = values[i];
        }
        return { max: top, windows: windows.length, timing: { totalMs: now() - t0 } };
    }

    return { open, tile, level, region, max, regions };
}
