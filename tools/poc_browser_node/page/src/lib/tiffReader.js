/**
 * The browser node's TIFF reader: one (OME-)TIFF opened from a local File (or
 * a URL) in this worker, answering Plexora-grid tile requests with the bytes
 * the existing shaders upload.
 *
 * What the product's tile path expects, and so what this returns:
 *  - a channel tile is `u16`: 2 bytes a pixel, BIG-endian (the HD PNG's sample
 *    order; glInit.js uploads it as RG8UI and frag.glsl rebuilds the value
 *    from r and g). geotiff.js hands back native little-endian, so it is
 *    swapped here, off the main thread.
 *  - a label tile is `u32`: the uint32 id's 4 little-endian bytes per pixel,
 *    which is exactly the RGBA8 layout data_model.read_tile builds for the
 *    node's label PNG (low three bytes in RGB, alpha 0 for ids < 2^24).
 *  - an edge tile is the clipped size, as `read_tile` slices it.
 *
 * createTiffReader() -> {open, tile, level}; the worker (workers/tiffWorker.js)
 * puts it behind messages, and Node tests (tests/) call it directly over
 * fs.openAsBlob, which is the same file-backed Blob a page gets from a picker.
 *   open({file|url, decoder: "native"|"pako", prefetchOffsets})
 *   tile({channel, level, tx, ty, tw, th, raw})  -- `timing` says where it went
 *   level({channel, level})                      -- a whole (small) level, raw
 */
import { addDecoder, BaseDecoder, GeoTIFF, GeoTIFFImage } from "geotiff";
import { inflate } from "pako";

const now = () => performance.now();

//: Deflate's two TIFF codes (8 Adobe, 32946 the old one).
const DEFLATE = [8, 32946];

/** Decode-time accounting shared by both decoders. */
const decodeClock = { ms: 0, calls: 0 };

/** zlib inflate in the browser's own code: what the product's HD worker uses. */
class NativeDeflate extends BaseDecoder {
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
        const out = inflate(new Uint8Array(buffer)).buffer;
        decodeClock.ms += now() - t0;
        decodeClock.calls += 1;
        return out;
    }
}

function useDecoder(kind) {
    addDecoder(DEFLATE, async () => (kind === "pako" ? PakoDeflate : NativeDeflate));
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

/** The same counters over HTTP range requests (a bucket with CORS). */
class CountingUrlSource {
    constructor(url) {
        this.url = url;
        this.bytes = 0;
        this.reads = 0;
        this.readMs = 0;
        this.size = null;
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
    async function open({ file, url, source: given, decoder = "native", prefetchOffsets = true }) {
        useDecoder(decoder);
        decodeClock.ms = 0;
        decodeClock.calls = 0;
        const t0 = now();
        // `given` is any {fetch(slices), fileSize} with the same counters (Node tests
        // read through a file handle: fs.openAsBlob truncates sizes past 4 GiB).
        const source = given || (file ? new CountingBlobSource(file) : new CountingUrlSource(url));
        const tiff = await GeoTIFF.fromSource(source);
        const tHeader = now();
        const main = [];
        for (let i = 0; ; i += 1) {
            try {
                main.push(await tiff.requestIFD(i));
            } catch (error) {
                if (error?.name === "GeoTIFFImageIndexError" || /index/i.test(String(error?.message))) break;
                throw error;
            }
            if (main.length > 4096) throw new Error("more than 4096 main IFDs");
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
            levelSizes = [{ width, height }];
            // Level sizes from channel 0's SubIFDs: one small IFD parse each.
            for (let level = 1; level <= subifds.length; level += 1) {
                const image = await images[0](level);
                levelSizes.push({ width: image.getWidth(), height: image.getHeight() });
            }
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
        return {
            geometry,
            timing: {
                openMs: now() - t0, headerMs: tHeader - t0, mainIfdsMs: tMain - tHeader,
                reads: source.reads, bytes: source.bytes, readMs: source.readMs,
            },
        };
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

    return { open, tile, level };
}
