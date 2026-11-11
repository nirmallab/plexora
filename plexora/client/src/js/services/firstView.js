/**
 * The first view of an image, at once: the moment a file is given to the
 * home page, its first channel's smallest pyramid level is read by the page
 * itself and shown inside a frame drawn as the viewer it turns into (the
 * viewer's sidebar with the file's channel names, the preview in the canvas
 * in channel 1's colour, a bar and the open's steps), while the import
 * (Python's inspect and registration, the viewer's page, the project's load)
 * goes on behind it. When the viewer has drawn the same image -- or the
 * person reaches for it, or the import needs a question answered -- the
 * preview fades.
 *
 * Why: everything between a dropped file and its first tile was Python's,
 * one step after another (docs/internal/bench/web/WEBSITE_FIRST_VIEW_2026-11-10.md):
 * 23 s for a 106 GB OME-TIFF from the Downloads folder, over ten minutes for
 * the same file on a file share. What a first view needs is a few hundred KB
 * of that file: its header, the directories down to the smallest level of
 * channel 0, and that level's tile or two.
 *
 * Finding the smallest level in a few reads:
 *   - a pyramid in SubIFDs (OME's own): directory 0 names them all;
 *   - a pyramid in the main chain (every level's planes one after another,
 *     each directory beside its plane, 280 of them across 106 GB): the planes
 *     of a level are written at a fixed stride, so with the OME-XML's plane
 *     count the first directory of the next level is computed from the
 *     first two of this one and read to be sure -- one read a level, not one
 *     a plane; anything that does not check out is walked one by one.
 * Every directory offset found or computed is kept (`offsetsFor`) and handed
 * to the Python worker once the file is mounted (`prefetch`), which reads
 * them side by side before Python's own open walks them.
 *
 * Read from the dropped File itself, never through its file-system handle:
 * Chrome reads a handle's file on the queue the site's own storage uses, and
 * a slow share's reads stalled that storage for seconds at a time.
 *
 * Classic script, `window.PlexoraFirstView`. Loaded by the website's host
 * page (services/shell.js), whose document outlives the app frame's
 * navigation from the home page to the viewer; the frame borrows the host's
 * (frameBoot). The local app's home page loads it on demand and shows a
 * preview Python made from the path (`/import/preview`).
 *
 *   begin({file, name})     -> the preview of a File, started now
 *   beginUrl({url, name})   -> a preview image Python made (the local app)
 *   prefetch(path, file)    -> the file's directories handed to the worker
 *   step(key)               -> a step of the open reached ("viewer": registered)
 *   handOff({leaving, to})  -> kept for the viewer's page, when `leaving` is replaced
 *   resume()                -> taken up on the viewer's page (the local app)
 *   done(reason)            -> the preview let go (drawn, dialog, error, ...)
 *   active()                -> whether one is showing
 */
(function (global) {
    "use strict";

    if (global.PlexoraFirstView) return;
    // A frame of the website's host borrows the host's: the preview must
    // outlive this document.
    try {
        if (global.parent && global.parent !== global && global.parent.PlexoraFirstView) {
            global.PlexoraFirstView = global.parent.PlexoraFirstView;
            return;
        }
    } catch (error) {
        // Not the same origin: a preview of its own.
    }

    const doc = global.document;
    const PIECE = 64 << 10;
    //: The most the smallest level may cost to read for a preview.
    const MAX_TILE_BYTES = 48 << 20;
    const MAX_TILES = 64;
    //: Directories walked one by one at most, when nothing can be computed.
    const MAX_WALK = 4096;

    // -- reading a TIFF's structure ----------------------------------------------

    /** Reads of `file` through 64 KiB pieces, each read once. */
    function reader(file) {
        const pieces = new Map();
        const piece = (index) => {
            let held = pieces.get(index);
            if (!held) {
                const from = index * PIECE;
                held = file.slice(from, Math.min(file.size, from + PIECE)).arrayBuffer()
                    .then((buffer) => new Uint8Array(buffer));
                pieces.set(index, held);
            }
            return held;
        };
        return {
            file,
            reads: () => pieces.size,
            /** `length` bytes at `at` (crossing pieces as it must). */
            async at(at, length) {
                const end = Math.min(file.size, at + length);
                if (end <= at) return new Uint8Array(0);
                if (length > 4 * PIECE) {
                    return new Uint8Array(await file.slice(at, end).arrayBuffer());
                }
                const out = new Uint8Array(end - at);
                const first = Math.floor(at / PIECE);
                const last = Math.floor((end - 1) / PIECE);
                const got = await Promise.all(Array.from({ length: last - first + 1 }, (_, i) => piece(first + i)));
                got.forEach((bytes, i) => {
                    const base = (first + i) * PIECE;
                    const from = Math.max(at, base);
                    const to = Math.min(end, base + bytes.length);
                    if (to > from) out.set(bytes.subarray(from - base, to - base), from - at);
                });
                return out;
            },
        };
    }

    const TYPE_SIZE = { 1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8 };

    async function header(io) {
        const head = await io.at(0, 16);
        const view = new DataView(head.buffer, head.byteOffset, head.byteLength);
        const order = String.fromCharCode(head[0], head[1]);
        if (order !== "II" && order !== "MM") return null;
        const le = order === "II";
        const magic = view.getUint16(2, le);
        if (magic === 42) return { le, big: false, first: view.getUint32(4, le) };
        if (magic === 43) return { le, big: true, first: Number(view.getBigUint64(8, le)) };
        return null;
    }

    /** One directory: its tags (values read when small) and the next one's offset. */
    async function directory(io, tiff, offset) {
        if (!offset || offset >= io.file.size) return null;
        const countBytes = tiff.big ? 8 : 2;
        const entry = tiff.big ? 20 : 12;
        const head = await io.at(offset, countBytes);
        if (head.length < countBytes) return null;
        const hv = new DataView(head.buffer, head.byteOffset, head.byteLength);
        const count = tiff.big ? Number(hv.getBigUint64(0, tiff.le)) : hv.getUint16(0, tiff.le);
        if (!count || count > 4096) return null;
        const body = await io.at(offset + countBytes, count * entry + (tiff.big ? 8 : 4));
        if (body.length < count * entry) return null;
        const v = new DataView(body.buffer, body.byteOffset, body.byteLength);
        const tags = new Map();
        for (let i = 0; i < count; i += 1) {
            const o = i * entry;
            const tag = v.getUint16(o, tiff.le);
            const type = v.getUint16(o + 2, tiff.le);
            const n = tiff.big ? Number(v.getBigUint64(o + 4, tiff.le)) : v.getUint32(o + 4, tiff.le);
            const size = TYPE_SIZE[type] || 1;
            const valueAt = o + (tiff.big ? 12 : 8);
            const inline = n * size <= (tiff.big ? 8 : 4);
            tags.set(tag, { type, n, size, inline, raw: inline ? body.slice(valueAt, valueAt + (tiff.big ? 8 : 4)) : null,
                            at: inline ? null : (tiff.big ? Number(v.getBigUint64(valueAt, tiff.le)) : v.getUint32(valueAt, tiff.le)) });
        }
        const nextAt = count * entry;
        const next = body.length >= nextAt + (tiff.big ? 8 : 4)
            ? (tiff.big ? Number(v.getBigUint64(nextAt, tiff.le)) : v.getUint32(nextAt, tiff.le)) : 0;
        return { offset, tags, next };
    }

    /** A tag's values (numbers; a string for ASCII). */
    async function values(io, tiff, ifd, tag) {
        const t = ifd.tags.get(tag);
        if (!t) return null;
        const bytes = t.inline ? t.raw : await io.at(t.at, t.n * t.size);
        if (t.type === 2) return new TextDecoder().decode(bytes.subarray(0, t.n)).replace(/\0+$/, "");
        const v = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
        const out = new Array(t.n);
        for (let i = 0; i < t.n; i += 1) {
            const o = i * t.size;
            if (t.size === 8) out[i] = Number(v.getBigUint64(o, tiff.le));
            else if (t.size === 4) out[i] = v.getUint32(o, tiff.le);
            else if (t.size === 2) out[i] = v.getUint16(o, tiff.le);
            else out[i] = v.getUint8(o);
        }
        return out;
    }

    async function one(io, tiff, ifd, tag, fallback = null) {
        const got = await values(io, tiff, ifd, tag);
        return got && got.length ? got[0] : fallback;
    }

    /** What a directory's image is, and where its pixels are. */
    async function describe(io, tiff, ifd) {
        const width = await one(io, tiff, ifd, 256);
        const height = await one(io, tiff, ifd, 257);
        if (!width || !height) return null;
        const tiled = ifd.tags.has(322);
        return {
            ifd, width, height,
            bits: await one(io, tiff, ifd, 258, 1),
            samples: await one(io, tiff, ifd, 277, 1),
            compression: await one(io, tiff, ifd, 259, 1),
            predictor: await one(io, tiff, ifd, 317, 1),
            planar: await one(io, tiff, ifd, 284, 1),
            format: await one(io, tiff, ifd, 339, 1),
            tiled,
            tileWidth: tiled ? await one(io, tiff, ifd, 322) : width,
            tileHeight: tiled ? await one(io, tiff, ifd, 323) : (await one(io, tiff, ifd, 278, height)),
            blocks: tiled ? ifd.tags.get(324) : ifd.tags.get(273),
        };
    }

    const sameKind = (a, b) => a && b && a.bits === b.bits && a.samples === b.samples;
    //: A level below `upper`: of its kind, smaller each way, by 8x at most.
    const below = (lower, upper) => sameKind(lower, upper) && lower.width < upper.width
        && lower.height < upper.height && lower.width * 8 >= upper.width;

    /** The planes a level holds, from the OME-XML in directory 0 (C x Z x T), or 0. */
    function planesOf(description) {
        if (typeof description !== "string" || !/<OME[\s>:]|openmicroscopy/.test(description.slice(0, 4096))) return 0;
        const pixels = description.match(/<(?:\w+:)?Pixels\b[^>]*>/);
        if (!pixels) return 0;
        const size = (key) => {
            const m = pixels[0].match(new RegExp(`\\b${key}="(\\d+)"`));
            return m ? Math.max(1, parseInt(m[1], 10)) : 1;
        };
        const fromAttrs = size("SizeC") * size("SizeZ") * size("SizeT");
        // Interleaved RGB is one plane of three samples.
        const samplesPerPixel = description.match(/SamplesPerPixel="(\d+)"/);
        const spp = samplesPerPixel ? parseInt(samplesPerPixel[1], 10) : 1;
        return spp > 1 ? Math.max(1, Math.round(fromAttrs / spp)) : fromAttrs;
    }

    const XML_ENTITIES = { amp: "&", lt: "<", gt: ">", quot: "\"", apos: "'" };
    //: The most channel names kept for the frame's channel list.
    const MAX_NAMES = 64;

    /**
     * The image's channels, from the OME-XML in directory 0: `{count, names}`
     * (count 0 when nothing says; names as many as the XML gives). What the
     * frame's channel list shows before the viewer has its own.
     */
    function channelsOf(description) {
        if (typeof description !== "string" || !/<OME[\s>:]|openmicroscopy/.test(description.slice(0, 4096))) {
            return { count: 0, names: [] };
        }
        const pixels = description.match(/<(?:\w+:)?Pixels\b[^>]*>/);
        const sizeC = pixels && pixels[0].match(/\bSizeC="(\d+)"/);
        const spp = description.match(/SamplesPerPixel="(\d+)"/);
        const rgb = spp && parseInt(spp[1], 10) > 1;
        const names = [];
        for (const tag of description.matchAll(/<(?:\w+:)?Channel\b[^>]*>/g)) {
            if (names.length >= MAX_NAMES) break;
            const named = tag[0].match(/\bName="([^"]*)"/);
            names.push(named ? named[1].replace(/&(amp|lt|gt|quot|apos);/g, (_, e) => XML_ENTITIES[e]).trim() : "");
        }
        const count = rgb ? 1 : (sizeC ? parseInt(sizeC[1], 10) : names.length);
        return { count, names: rgb ? [] : names };
    }

    /**
     * Channel 0's smallest level: `{level: description, offsets: [every
     * directory offset found or computed], reads}`, or null when there is
     * nothing a preview can show.
     */
    async function smallestLevel(io) {
        const tiff = await header(io);
        if (!tiff) return null;
        const offsets = [tiff.first];
        const first = await directory(io, tiff, tiff.first);
        if (!first) return null;
        const top = await describe(io, tiff, first);
        if (!top) return null;
        const description = await values(io, tiff, first, 270);
        const channels = channelsOf(description);
        // OME's pyramid: SubIFDs.
        const subs = await values(io, tiff, first, 330);
        if (Array.isArray(subs) && subs.length) {
            let best = top;
            for (const at of subs) {
                offsets.push(at);
                const d = await directory(io, tiff, at);
                const level = d && await describe(io, tiff, d);
                if (level && sameKind(level, top) && level.width < best.width) best = level;
            }
            // The other planes' directories, packed together after the first
            // in such files: walked a few, for the prefetch.
            let next = first.next;
            for (let i = 0; next && i < 512; i += 1) {
                offsets.push(next);
                const d = await directory(io, tiff, next);
                if (!d) break;
                next = d.next;
            }
            return { tiff, level: best, offsets, layout: "subifds", channels };
        }
        // A pyramid in the main chain, or no pyramid at all.
        const planes = planesOf(description);
        let levelStart = first;
        let levelDesc = top;
        let best = top;
        let layout = "chain";
        if (planes > 1) {
            // Level by level: the first two of a level give its stride, the
            // next level's first directory is computed and read to be sure.
            for (let guard = 0; guard < 16; guard += 1) {
                const secondAt = levelStart.next;
                if (!secondAt) break;
                const second = await directory(io, tiff, secondAt);
                const secondDesc = second && await describe(io, tiff, second);
                if (!secondDesc || secondDesc.width !== levelDesc.width) break;
                const stride = secondAt - levelStart.offset;
                for (let j = 1; j < planes; j += 1) offsets.push(levelStart.offset + j * stride);
                const nextAt = levelStart.offset + planes * stride;
                if (stride <= 0 || nextAt >= io.file.size) { layout = "chain-strided"; break; }
                // This level's last plane where the stride puts it, and the
                // chain ending there: the smallest level, in one read.
                const last = await directory(io, tiff, levelStart.offset + (planes - 1) * stride);
                const lastDesc = last && await describe(io, tiff, last);
                if (lastDesc && lastDesc.width === levelDesc.width && !last.next) { layout = "chain-strided"; break; }
                const candidate = await directory(io, tiff, nextAt);
                const candidateDesc = candidate && await describe(io, tiff, candidate);
                if (!candidateDesc || !below(candidateDesc, levelDesc)) {
                    // Either the last level (the stride ran past its end) or
                    // not strided after all: walked to be sure.
                    const walked = await walkLevel(io, tiff, levelStart, planes, offsets);
                    if (!walked) { layout = "chain-strided"; break; }
                    const after = await describe(io, tiff, walked);
                    if (!after || !below(after, levelDesc)) { layout = "chain-strided"; break; }
                    levelStart = walked;
                    levelDesc = after;
                } else {
                    offsets.push(nextAt);
                    levelStart = candidate;
                    levelDesc = candidateDesc;
                }
                best = levelDesc;
                layout = "chain-strided";
            }
            return { tiff, level: best, offsets, layout, channels };
        }
        // Nothing says how many planes a level has: walked, the smallest
        // directory of the image's own kind kept.
        let next = first.next;
        for (let i = 0; next && i < MAX_WALK; i += 1) {
            offsets.push(next);
            const d = await directory(io, tiff, next);
            if (!d) break;
            const described = await describe(io, tiff, d);
            if (described && sameKind(described, top) && described.width < best.width) best = described;
            next = d.next;
        }
        return { tiff, level: best, offsets, layout, channels };
    }

    /** The directory `planes` after `start`, walked one by one; null at the chain's end. */
    async function walkLevel(io, tiff, start, planes, offsets) {
        let current = start;
        for (let j = 0; j < planes; j += 1) {
            if (!current.next) return null;
            offsets.push(current.next);
            current = await directory(io, tiff, current.next);
            if (!current) return null;
        }
        return current;
    }

    // -- the smallest level's pixels ---------------------------------------------

    /** TIFF LZW (MSB-first codes of 9 to 12 bits, the early width change). */
    function lzw(input, expected) {
        const out = new Uint8Array(expected);
        const prefix = new Int32Array(4096);
        const suffix = new Uint8Array(4096);
        const length = new Int32Array(4096);
        const head = new Uint8Array(4096);
        for (let i = 0; i < 256; i += 1) {
            prefix[i] = -1;
            suffix[i] = i;
            length[i] = 1;
            head[i] = i;
        }
        let next = 258;
        let width = 9;
        let pos = 0;
        let buffer = 0;
        let held = 0;
        let outPos = 0;
        let previous = -1;
        const read = () => {
            while (held < width) {
                if (pos >= input.length) return 257;
                buffer = ((buffer << 8) | input[pos]) & 0xffffff;
                pos += 1;
                held += 8;
            }
            held -= width;
            return (buffer >>> held) & ((1 << width) - 1);
        };
        const emit = (code) => {
            const n = length[code];
            let c = code;
            for (let k = n - 1; k >= 0; k -= 1) {
                if (outPos + k < expected) out[outPos + k] = suffix[c];
                c = prefix[c];
            }
            outPos += n;
        };
        const add = (from, byte) => {
            if (next >= 4096) return;
            prefix[next] = from;
            suffix[next] = byte;
            length[next] = length[from] + 1;
            head[next] = head[from];
            next += 1;
        };
        while (outPos < expected) {
            const code = read();
            if (code === 257) break;
            if (code === 256) {
                next = 258;
                width = 9;
                previous = -1;
                continue;
            }
            if (previous < 0) {
                if (code > 255) break;
                emit(code);
            } else if (code < next) {
                emit(code);
                add(previous, head[code]);
            } else if (code === next) {
                add(previous, head[previous]);
                emit(code);
            } else {
                break;
            }
            previous = code;
            if (next + 1 >= (1 << width) && width < 12) width += 1;
        }
        return out;
    }

    async function inflate(bytes) {
        if (typeof global.DecompressionStream !== "function") throw new Error("no DecompressionStream");
        const stream = new Blob([bytes]).stream().pipeThrough(new global.DecompressionStream("deflate"));
        return new Uint8Array(await new Response(stream).arrayBuffer());
    }

    /** A block's samples, decoded and un-predicted, as bytes. */
    async function decodeBlock(level, bytes, expected, rowSamples, rows) {
        let out;
        if (level.compression === 1) out = bytes;
        else if (level.compression === 5) out = lzw(bytes, expected);
        else if (level.compression === 8 || level.compression === 32946) out = await inflate(bytes);
        else throw new Error(`compression ${level.compression}`);
        if (level.predictor === 2) {
            const step = level.planar === 1 ? level.samples : 1;
            if (level.bits === 8) {
                for (let r = 0; r < rows; r += 1) {
                    const base = r * rowSamples;
                    for (let i = step; i < rowSamples; i += 1) out[base + i] = (out[base + i] + out[base + i - step]) & 0xff;
                }
            } else if (level.bits === 16) {
                const v = new DataView(out.buffer, out.byteOffset, out.byteLength);
                for (let r = 0; r < rows; r += 1) {
                    const base = r * rowSamples;
                    for (let i = step; i < rowSamples; i += 1) {
                        const at = (base + i) * 2;
                        v.setUint16(at, (v.getUint16(at, level.le) + v.getUint16(at - step * 2, level.le)) & 0xffff, level.le);
                    }
                }
            }
        } else if (level.predictor !== 1 && level.predictor) {
            throw new Error(`predictor ${level.predictor}`);
        }
        return out;
    }

    /**
     * The level's first plane as {width, height, samples, data: Uint8Array |
     * Uint16Array}, interleaved when it has several samples.
     */
    async function pixels(io, tiff, level) {
        if (level.bits !== 8 && level.bits !== 16) throw new Error(`${level.bits}-bit samples`);
        if (level.format === 3) throw new Error("float samples");
        const offsets = await values(io, tiff, level.ifd, level.tiled ? 324 : 273);
        const counts = await values(io, tiff, level.ifd, level.tiled ? 325 : 279);
        if (!offsets || !counts) throw new Error("no pixel table");
        const spp = level.planar === 2 ? 1 : level.samples;
        const across = Math.ceil(level.width / level.tileWidth);
        const down = Math.ceil(level.height / level.tileHeight);
        const blocks = level.tiled ? across * down : down;
        const wanted = Math.min(blocks, offsets.length);
        let bytes = 0;
        for (let i = 0; i < wanted; i += 1) bytes += counts[i] || 0;
        if (wanted > MAX_TILES || bytes > MAX_TILE_BYTES) throw new Error("smallest level too large for a preview");
        const bpp = level.bits / 8;
        const out = new (level.bits === 16 ? Uint16Array : Uint8Array)(level.width * level.height * spp);
        const blockWidth = level.tiled ? level.tileWidth : level.width;
        const raw = await Promise.all(Array.from({ length: wanted }, (_, i) => (counts[i]
            ? io.at(offsets[i], counts[i]) : Promise.resolve(new Uint8Array(0)))));
        const tiffLe = tiff.le;
        for (let i = 0; i < wanted; i += 1) {
            if (!raw[i].length) continue;
            const tx = level.tiled ? i % across : 0;
            const ty = level.tiled ? Math.floor(i / across) : i;
            const rows = level.tiled ? level.tileHeight : Math.min(level.tileHeight, level.height - ty * level.tileHeight);
            const rowSamples = blockWidth * spp;
            const decoded = await decodeBlock({ ...level, le: tiffLe }, raw[i], rows * rowSamples * bpp, rowSamples, rows);
            const dv = new DataView(decoded.buffer, decoded.byteOffset, decoded.byteLength);
            const x0 = tx * blockWidth;
            const y0 = ty * level.tileHeight;
            const w = Math.min(blockWidth, level.width - x0);
            const h = Math.min(rows, level.height - y0);
            for (let y = 0; y < h; y += 1) {
                const src = y * rowSamples;
                const dst = ((y0 + y) * level.width + x0) * spp;
                if (bpp === 1) {
                    out.set(decoded.subarray(src, src + w * spp), dst);
                } else {
                    for (let k = 0; k < w * spp; k += 1) {
                        const at = (src + k) * 2;
                        if (at + 2 <= decoded.length) out[dst + k] = dv.getUint16(at, tiffLe);
                    }
                }
            }
        }
        return { width: level.width, height: level.height, samples: spp, data: out };
    }

    /** The plane as an 8-bit picture, windowed from its own values (0.5-99.8th percentile). */
    function picture(plane) {
        const { width, height, samples, data } = plane;
        const canvas = doc.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        const ctx = canvas.getContext("2d");
        const image = ctx.createImageData(width, height);
        const px = image.data;
        if (samples >= 3 && data instanceof Uint8Array) {
            for (let i = 0, j = 0; i < width * height; i += 1, j += samples) {
                px[i * 4] = data[j];
                px[i * 4 + 1] = data[j + 1];
                px[i * 4 + 2] = data[j + 2];
                px[i * 4 + 3] = 255;
            }
        } else {
            const levels = data instanceof Uint16Array ? 65536 : 256;
            const hist = new Uint32Array(levels);
            let counted = 0;
            for (let i = 0; i < width * height; i += 1) {
                const value = data[i * samples];
                if (value > 0) { hist[value] += 1; counted += 1; }
            }
            const at = (fraction) => {
                const target = counted * fraction;
                let seen = 0;
                for (let v = 0; v < levels; v += 1) {
                    seen += hist[v];
                    if (seen >= target) return v;
                }
                return levels - 1;
            };
            const lo = counted ? at(0.005) : 0;
            const hi = Math.max(lo + 1, counted ? at(0.998) : levels - 1);
            const scale = 255 / (hi - lo);
            for (let i = 0; i < width * height; i += 1) {
                const g = Math.max(0, Math.min(255, Math.round((data[i * samples] - lo) * scale)));
                px[i * 4] = g;
                px[i * 4 + 1] = g;
                px[i * 4 + 2] = g;
                px[i * 4 + 3] = 255;
            }
        }
        ctx.putImageData(image, 0, 0);
        return canvas;
    }

    //: What was found of each file read, by name, size and time (the same
    //: file mounted is another File object): -> {offsets, layout}.
    const found = new Map();
    const keyOf = (file) => `${file.name}|${file.size}|${file.lastModified}`;
    //: Reads still under way, by the same key: a prefetch waits for one.
    const reading = new Map();

    /** A File's first view: {canvas, width, height, ms, reads, layout, channels} (throws when it cannot). */
    async function read(file) {
        const started = performance.now();
        const io = reader(file);
        const located = await smallestLevel(io);
        if (!located) throw new Error("not a TIFF this preview reads");
        found.set(keyOf(file), { offsets: located.offsets, layout: located.layout });
        while (found.size > 8) found.delete(found.keys().next().value);
        const plane = await pixels(io, located.tiff, located.level);
        return { canvas: picture(plane), width: plane.width, height: plane.height,
                 ms: Math.round(performance.now() - started), reads: io.reads(), layout: located.layout,
                 channels: located.channels || { count: 0, names: [] }, full: { width: 0, height: 0 } };
    }

    // -- on screen ---------------------------------------------------------------
    //
    // The first view is drawn as the viewer it turns into: the viewer's
    // sidebar (its header, Layers, the Image card with the file's channel
    // names, channel 1 on) beside the canvas, the preview inside the canvas
    // in channel 1's colour, a bar across the canvas's top and a card of the
    // open's steps in its corner. Once the viewer's page is up the frame
    // moves onto the viewer's canvas, and the real sidebar shows; it leaves
    // when the viewer has drawn the image.
    //
    // The local app's home page and its viewer are two documents, so the
    // frame is handed over (`handOff`, sessionStorage) and taken up again
    // by the viewer's page (`resume`); the website's host page outlives the
    // app frame's navigation and keeps the same one.

    let current = null;
    let watch = 0;
    //: The longest a first view stays up waiting for the viewer, and the
    //: longest a loaded page of the app with no viewer in it is covered.
    const MAX_SHOWN_MS = 180000;
    const NO_VIEWER_MS = 15000;
    //: The open's steps, in order; `step(key)` says one has been reached.
    const STEPS = [
        ["read", "Preview read from the file"],
        ["register", "Registering the image"],
        ["viewer", "Opening the viewer"],
        ["draw", "Drawing at full resolution"],
    ];
    //: Channel 1's colour in the viewer (viewerSidebar.js defaultColors[0]):
    //: the preview is drawn in it, so nothing changes colour at the hand-over.
    const CHANNEL_ONE = [35, 136, 255];
    //: The rows the frame's channel list shows (viewerSidebar's initialChannelSlots).
    const ROWS = 5;
    //: The hand-over between the local app's two documents.
    const HANDOFF_KEY = "plexora.firstView";
    const HANDOFF_MAX_AGE_MS = 60000;
    //: The largest side of the picture handed over (sessionStorage is small).
    const HANDOFF_SIDE = 1024;

    /** The app frame's page: {path, loaded, viewer}, or null. */
    function framePage() {
        const frame = doc.getElementById("plexora-app");
        const win = frame ? frame.contentWindow : global;
        try {
            const d = win.document;
            return { path: win.location.pathname, loaded: d.readyState === "complete" && !d.getElementById("plexora-boot"),
                     viewer: Boolean(d.querySelector(".openseadragon-container")) };
        } catch (error) {
            return null;
        }
    }

    function style() {
        if (doc.getElementById("plexora-first-view-style")) return;
        const css = doc.createElement("style");
        css.id = "plexora-first-view-style";
        css.textContent = `
.plexora-first-view{position:fixed;left:0;right:0;bottom:0;top:44px;z-index:2147483000;pointer-events:none;
  display:grid;grid-template-columns:minmax(320px,380px) minmax(0,1fr);background:#05070a;color:#c9d3e0;
  font:400 13px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif;transition:opacity .28s ease}
.plexora-first-view.is-leaving{opacity:0}
.plexora-first-view.is-in-viewer{grid-template-columns:minmax(0,1fr);background:transparent}
.plexora-first-view.is-in-viewer .pfv-side{display:none}
.pfv-side{box-sizing:border-box;min-width:0;overflow:hidden;display:flex;flex-direction:column;gap:14px;padding:20px 16px 16px;
  background:linear-gradient(180deg,rgba(20,24,31,.98),rgba(10,13,18,.98));border-right:1px solid rgba(255,255,255,.06)}
.pfv-head{padding-bottom:14px;border-bottom:1px solid rgba(255,255,255,.07)}
.pfv-kicker{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#8b96a5}
.pfv-title{font-size:21px;font-weight:700;color:#eef2f7}
.pfv-section{font-size:14px;font-weight:600;color:#dfe5ec}
.pfv-card{display:flex;flex-direction:column;gap:10px;padding:12px;border:1px solid rgba(80,160,230,.35);border-radius:12px;background:#0e1219}
.pfv-card-head{display:flex;align-items:center;gap:8px;font-size:14px;font-weight:600;color:#eef2f7}
.pfv-card-head span{margin-left:auto;font-size:11px;font-weight:500;color:#8b96a5}
.pfv-skel{border-radius:6px;background:linear-gradient(90deg,#161b23 0,#1f2630 50%,#161b23 100%);background-size:480px 100%;
  animation:pfvShimmer 1.4s linear infinite}
.pfv-rows{display:flex;flex-direction:column;gap:6px}
.pfv-row{height:32px;box-sizing:border-box;display:flex;align-items:center;gap:10px;padding:0 10px 0 12px;
  border:1px solid rgba(255,255,255,.05);border-radius:8px;color:#6f7a89;white-space:nowrap;overflow:hidden}
.pfv-row.is-on{color:#eef2f7;border-color:rgba(255,255,255,.08);border-left:3px solid rgb(${CHANNEL_ONE});padding-left:10px}
.pfv-row i{flex:0 0 14px;height:14px;border-radius:3px;background:#2a313b}
.pfv-row.is-on i{background:rgb(${CHANNEL_ONE})}
.pfv-row b{font-weight:400;overflow:hidden;text-overflow:ellipsis}
.pfv-row .pfv-skel{flex:0 1 150px;height:10px}
.pfv-row em{margin-left:auto;font-style:normal;font-size:11px;color:#8fd3ff}
.pfv-note{font-size:11px;color:#8b96a5}
.pfv-stage{position:relative;min-width:0;overflow:hidden;background:#000}
.pfv-pic{position:absolute;inset:24px;display:flex;align-items:center;justify-content:center}
.pfv-pic canvas,.pfv-pic img{max-width:100%;max-height:100%;object-fit:contain;opacity:0;transition:opacity .18s ease}
.plexora-first-view.is-shown .pfv-pic canvas,.plexora-first-view.is-shown .pfv-pic img{opacity:1}
.pfv-bar{position:absolute;left:0;right:0;top:0;height:3px;overflow:hidden;background:rgba(60,140,255,.15)}
.pfv-bar-fill{height:3px;background:#3c8cff;transition:width .3s ease}
.pfv-bar-sweep{position:absolute;top:0;left:0;width:40%;height:3px;
  background:linear-gradient(90deg,transparent,rgba(160,210,255,.8),transparent);animation:pfvSweep 1.3s ease-in-out infinite}
.pfv-pill{position:absolute;right:16px;top:14px;font-size:12px;color:#8fd3ff;background:rgba(12,16,22,.85);
  border:1px solid rgba(60,140,255,.35);border-radius:999px;padding:4px 10px;opacity:0;transition:opacity .18s ease}
.plexora-first-view.is-shown .pfv-pill{opacity:1}
.pfv-steps{position:absolute;left:24px;bottom:24px;width:340px;max-width:calc(100% - 48px);box-sizing:border-box;
  display:flex;flex-direction:column;gap:10px;padding:14px 16px;background:rgba(12,16,22,.92);
  border:1px solid rgba(255,255,255,.09);border-radius:12px;box-shadow:0 8px 28px rgba(0,0,0,.5)}
.plexora-first-view.is-in-viewer .pfv-steps{left:72px;max-width:calc(100% - 96px)}
.pfv-steps-head{display:flex;align-items:center;gap:10px;font-size:14px;font-weight:600;color:#eef2f7;min-width:0}
.pfv-steps-head b{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pfv-steps-head span{margin-left:auto;font-size:12px;font-weight:400;color:#8b96a5;white-space:nowrap}
.pfv-spin{flex:0 0 12px;width:12px;height:12px;border-radius:50%;border:2px solid rgba(60,140,255,.25);
  border-top-color:#3c8cff;animation:pfvSpin .9s linear infinite}
.pfv-meter{height:4px;border-radius:2px;background:#1c2430;overflow:hidden}
.pfv-meter div{height:4px;background:#3c8cff;transition:width .3s ease}
.pfv-step{display:flex;align-items:center;gap:10px;color:#6f7a89}
.pfv-step i{flex:0 0 16px;text-align:center;font-style:normal}
.pfv-step span{margin-left:auto;font-size:12px;color:#8b96a5}
.pfv-step.is-done{color:#c9d3e0}.pfv-step.is-done i{color:#2bd18b}
.pfv-step.is-now{color:#eef2f7}.pfv-step.is-now i{color:#3c8cff}
@keyframes pfvShimmer{0%{background-position:-240px 0}100%{background-position:240px 0}}
@keyframes pfvSweep{0%{transform:translateX(-100%)}100%{transform:translateX(250%)}}
@keyframes pfvSpin{to{transform:rotate(360deg)}}
@media (max-width:900px){.plexora-first-view{grid-template-columns:minmax(0,1fr)}.pfv-side{display:none}}
@media (prefers-reduced-motion:reduce){.pfv-skel,.pfv-bar-sweep,.pfv-spin{animation:none}}`;
        doc.head.appendChild(css);
    }

    function el(tag, className, text) {
        const node = doc.createElement(tag);
        if (className) node.className = className;
        if (text != null) node.textContent = text;
        return node;
    }

    /** "106 GB" from a byte count; "" when not known. */
    function sizeText(bytes) {
        if (!(bytes > 0)) return "";
        const units = ["B", "KB", "MB", "GB", "TB"];
        let value = bytes;
        let unit = 0;
        while (value >= 1000 && unit < units.length - 1) { value /= 1000; unit += 1; }
        return `${value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
    }

    function overlay(name, bytes) {
        style();
        const box = el("div", "plexora-first-view");
        box.setAttribute("aria-hidden", "true");

        const side = el("div", "pfv-side");
        const head = el("div", "pfv-head");
        head.append(el("div", "pfv-kicker", "Plexora"), el("div", "pfv-title", "Viewer"));
        const card = el("div", "pfv-card");
        const cardHead = el("div", "pfv-card-head", "Image");
        const count = el("span");
        cardHead.appendChild(count);
        const rows = el("div", "pfv-rows");
        const note = el("div", "pfv-note", "");
        const skelA = el("div", "pfv-skel");
        skelA.style.height = "30px";
        const skelB = el("div", "pfv-skel");
        skelB.style.height = "56px";
        card.append(cardHead, skelA, skelB, rows, note);
        side.append(head, el("div", "pfv-section", "Layers"), card);

        const stage = el("div", "pfv-stage");
        const bar = el("div", "pfv-bar");
        const barFill = el("div", "pfv-bar-fill");
        bar.append(barFill, el("div", "pfv-bar-sweep"));
        const pic = el("div", "pfv-pic");
        const pill = el("div", "pfv-pill", "Preview · low resolution");
        const steps = el("div", "pfv-steps");
        const stepsHead = el("div", "pfv-steps-head");
        stepsHead.append(el("div", "pfv-spin"), el("b", "", name ? `Opening ${name}` : "Opening…"),
                         el("span", "", sizeText(bytes)));
        const meter = el("div", "pfv-meter");
        const meterFill = el("div");
        meter.appendChild(meterFill);
        steps.append(stepsHead, meter);
        const stepRows = STEPS.map(([, label]) => {
            const row = el("div", "pfv-step");
            row.append(el("i", "", "○"), doc.createTextNode(label), el("span", "", ""));
            steps.appendChild(row);
            return row;
        });
        stage.append(bar, pic, pill, steps);
        box.append(side, stage);
        doc.body.appendChild(box);
        return { box, pic, rows, count, note, barFill, meterFill, stepRows };
    }

    /** The frame's channel list: the file's names, channel 1 on; bars where none is known. */
    function paintChannels(state, channels) {
        const names = (channels && channels.names) || [];
        const total = (channels && channels.count) || 0;
        state.channels = { count: total, names: names.slice(0, MAX_NAMES) };
        state.rows.textContent = "";
        const shown = Math.max(1, Math.min(ROWS, total || ROWS));
        for (let i = 0; i < shown; i += 1) {
            const row = el("div", i === 0 ? "pfv-row is-on" : "pfv-row");
            row.appendChild(el("i"));
            if (names[i]) row.appendChild(el("b", "", names[i]));
            else row.appendChild(el("div", "pfv-skel"));
            if (i === 0) row.appendChild(el("em", "", "Preview"));
            state.rows.appendChild(row);
        }
        state.count.textContent = total > 1 ? `${total} channels` : "";
        state.note.textContent = names.length ? "Channel names from the file · the rest switch on when it opens" : "";
    }

    /** The steps' card and the bar: done ones ticked with their time, the current one marked. */
    function paintSteps(state) {
        let done = 0;
        state.stepRows.forEach((row, i) => {
            const at = state.times[i];
            const now = i === state.step;
            row.className = `pfv-step${at != null ? " is-done" : now ? " is-now" : ""}`;
            row.firstChild.textContent = at != null ? "✓" : now ? "•" : "○";
            row.lastChild.textContent = at != null ? `${(at / 1000).toFixed(1)} s` : "";
            if (at != null) done += 1;
        });
        const pct = `${Math.max(6, Math.round((done / STEPS.length) * 100))}%`;
        state.barFill.style.width = pct;
        state.meterFill.style.width = pct;
    }

    /** Step `key` reached: every step before it done (at now, unless done already). */
    function reach(state, key) {
        const index = STEPS.findIndex(([k]) => k === key);
        if (index < 0 || index < state.step) return;
        const now = Math.round(performance.now() - state.started);
        for (let i = 0; i < index; i += 1) if (state.times[i] == null) state.times[i] = now;
        state.step = index;
        paintSteps(state);
    }

    /** The viewer's canvas in the app's frame, where the preview is drawn to fit. */
    function viewerRect() {
        const frame = doc.getElementById("plexora-app");
        const win = frame ? frame.contentWindow : global;
        try {
            const el = win.document.querySelector(".openseadragon-container");
            if (!el) return null;
            const r = el.getBoundingClientRect();
            const offset = frame ? frame.getBoundingClientRect() : { left: 0, top: 0 };
            return r.width > 50 && r.height > 50 ? { left: r.left + offset.left, top: r.top + offset.top, width: r.width, height: r.height } : null;
        } catch (error) {
            return null;
        }
    }

    /** Whether the viewer in the app's frame has drawn an item fully. */
    function viewerDrawn() {
        const frame = doc.getElementById("plexora-app");
        const win = frame ? frame.contentWindow : global;
        try {
            const viewer = win.__plexora && win.__plexora.seaDragonViewer && win.__plexora.seaDragonViewer.viewer;
            if (!viewer || !viewer.world || !viewer.world.getItemCount()) return false;
            for (let i = 0; i < viewer.world.getItemCount(); i += 1) {
                const item = viewer.world.getItemAt(i);
                if (item.getOpacity() > 0 && item.getFullyLoaded()) return true;
            }
        } catch (error) {
            return false;
        }
        return false;
    }

    /** The person reached for the viewer (wheel, press, key): the preview steps aside. */
    function onReach(handler) {
        const frame = doc.getElementById("plexora-app");
        const win = frame ? frame.contentWindow : global;
        try {
            for (const type of ["wheel", "pointerdown", "keydown"]) win.addEventListener(type, handler, { capture: true, passive: true });
            return () => {
                for (const type of ["wheel", "pointerdown", "keydown"]) win.removeEventListener(type, handler, { capture: true });
            };
        } catch (error) {
            return () => {};
        }
    }

    function follow(state) {
        global.clearInterval(watch);
        let unreach = () => {};
        let reachWin = null;
        watch = global.setInterval(() => {
            if (current !== state) { global.clearInterval(watch); unreach(); return; }
            const rect = viewerRect();
            if (rect) {
                state.box.classList.add("is-in-viewer");
                state.box.style.left = `${rect.left}px`;
                state.box.style.top = `${rect.top}px`;
                state.box.style.right = "auto";
                state.box.style.bottom = "auto";
                state.box.style.width = `${rect.width}px`;
                state.box.style.height = `${rect.height}px`;
                reach(state, "draw");
                const frame = doc.getElementById("plexora-app");
                const win = frame ? frame.contentWindow : global;
                if (win !== reachWin) {
                    unreach();
                    reachWin = win;
                    unreach = onReach(() => done("reached"));
                }
            }
            if (viewerDrawn()) {
                state.times[STEPS.length - 1] = Math.round(performance.now() - state.started);
                paintSteps(state);
                done("drawn");
                return;
            }
            // Never in front of something else for good: a page of the app
            // that is not a viewer (an error, a question), loaded a while,
            // or too long a wait for any viewer -- the page's own progress
            // is what is said then.
            const now = performance.now();
            if (now - state.started > MAX_SHOWN_MS) { done("timeout"); return; }
            const page = framePage();
            if (page && page.path !== state.pagePath) {
                state.pagePath = page.path;
                state.pageSince = now;
            }
            if (page && page.path !== "/" && page.loaded && !page.viewer && now - (state.pageSince || now) > NO_VIEWER_MS) {
                done("no-viewer");
            }
        }, 80);
    }

    /** A grey picture in channel 1's colour (a colour one -- brightfield -- as it is). */
    function tinted(source) {
        const width = source.naturalWidth || source.width;
        const height = source.naturalHeight || source.height;
        if (!width || !height) return source;
        const canvas = doc.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        const ctx = canvas.getContext("2d");
        if (!ctx) return source;
        ctx.drawImage(source, 0, 0);
        let image;
        try {
            image = ctx.getImageData(0, 0, width, height);
        } catch (error) {
            return source;
        }
        const px = image.data;
        // Grey: every sampled pixel's three values equal.
        const stride = Math.max(4, Math.floor(px.length / 4 / 4096) * 4);
        for (let i = 0; i < px.length; i += stride) {
            if (px[i] !== px[i + 1] || px[i] !== px[i + 2]) return source;
        }
        const [r, g, b] = CHANNEL_ONE;
        for (let i = 0; i < px.length; i += 4) {
            const v = px[i];
            px[i] = (v * r) / 255;
            px[i + 1] = (v * g) / 255;
            px[i + 2] = (v * b) / 255;
        }
        ctx.putImageData(image, 0, 0);
        return canvas;
    }

    function show(state, element) {
        const picture = tinted(element);
        if (picture.setAttribute) picture.setAttribute("aria-hidden", "true");
        state.pic.textContent = "";
        state.pic.appendChild(picture);
        state.picture = picture;
        // Two frames: drawn, then faded in.
        global.requestAnimationFrame(() => global.requestAnimationFrame(() => state.box.classList.add("is-shown")));
        state.shownAt = performance.now();
        reach(state, "register");
        global.__plexoraFirstView = { ...(global.__plexoraFirstView || {}), shownMs: Math.round(state.shownAt - state.started) };
    }

    function start(name, { bytes = 0, channels = null } = {}) {
        done("replaced");
        const state = { ...overlay(name, bytes), started: performance.now(), name, bytes, step: 0, times: [] };
        paintChannels(state, channels);
        paintSteps(state);
        current = state;
        global.__plexoraFirstView = { startedAt: Math.round(state.started) };
        follow(state);
        return state;
    }

    /** The preview of a File (the website): started now, shown when read. */
    function begin({ file, name = "" } = {}) {
        if (!file || !doc || !doc.body) return null;
        // A File of this page's, of the same bytes: the one dropped belongs
        // to the app frame's page, which the way to the viewer replaces.
        if (typeof global.File === "function" && !(file instanceof global.File)) {
            file = new global.File([file], file.name, { type: file.type, lastModified: file.lastModified });
        }
        const state = start(name || file.name, { bytes: file.size });
        state.file = file;
        const key = keyOf(file);
        const pending = read(file);
        reading.set(key, pending);
        pending.finally(() => { if (reading.get(key) === pending) reading.delete(key); }).catch(() => null);
        pending.then((preview) => {
            if (current !== state) return;
            global.__plexoraFirstView = { ...(global.__plexoraFirstView || {}), readMs: preview.ms,
                                          reads: preview.reads, layout: preview.layout,
                                          size: [preview.width, preview.height] };
            paintChannels(state, preview.channels);
            show(state, preview.canvas);
        }).catch((error) => {
            if (current !== state) return;
            global.__plexoraFirstView = { ...(global.__plexoraFirstView || {}), failed: String(error && error.message || error) };
            // Nothing to show: the import's own progress is what is on screen.
            done("unreadable");
        });
        return state;
    }

    /** The channels and size Python's preview names in its headers (import_routes.import_preview). */
    function previewHeaders(response) {
        let channels = null;
        try {
            const raw = response.headers.get("X-Plexora-Channels");
            const parsed = raw ? JSON.parse(decodeURIComponent(raw)) : null;
            if (parsed && typeof parsed === "object") {
                channels = { count: Number(parsed.count) || 0,
                             names: Array.isArray(parsed.names) ? parsed.names.map((n) => String(n || "")) : [] };
            }
        } catch (error) {
            channels = null;
        }
        return { channels, bytes: Number(response.headers.get("X-Plexora-Bytes")) || 0 };
    }

    /** A preview image Python made from a path (the local app's home page). */
    function beginUrl({ url, name = "" } = {}) {
        if (!url || !doc || !doc.body) return null;
        const state = start(name);
        global.fetch(url, { cache: "no-store" }).then(async (response) => {
            if (current !== state) return;
            if (response.status !== 200) { done("unreadable"); return; }
            const { channels, bytes } = previewHeaders(response);
            const blob = await response.blob();
            if (current !== state) return;
            if (channels) paintChannels(state, channels);
            if (bytes) {
                state.bytes = bytes;
                state.box.querySelector(".pfv-steps-head span").textContent = sizeText(bytes);
            }
            const img = new Image();
            img.decoding = "async";
            const src = URL.createObjectURL(blob);
            img.onload = () => {
                URL.revokeObjectURL(src);
                if (current !== state) return;
                if (!img.naturalWidth) { done("unreadable"); return; }
                show(state, img);
            };
            img.onerror = () => { URL.revokeObjectURL(src); if (current === state) done("unreadable"); };
            img.src = src;
        }).catch(() => { if (current === state) done("unreadable"); });
        return state;
    }

    /** Step `key` of the open reached (the import's own news: "viewer" when it has registered). */
    function step(key) {
        if (current) reach(current, key);
    }

    /**
     * The page holding the first view is about to be replaced by the viewer's
     * (the local app: a full navigation): what it shows is kept for the
     * viewer's page to take up (`resume`). Nothing when `leaving` is not the
     * document the first view is in -- the website's app frame, whose host
     * keeps it. `to`: the viewer's address, which alone takes it up.
     * -> whether it was kept.
     */
    function handOff({ leaving = doc, to = "" } = {}) {
        const state = current;
        if (!state || leaving !== doc) return false;
        let picture = "";
        try {
            const source = state.picture;
            const width = source && (source.naturalWidth || source.width);
            const height = source && (source.naturalHeight || source.height);
            if (width && height) {
                const scale = Math.min(1, HANDOFF_SIDE / Math.max(width, height));
                const canvas = doc.createElement("canvas");
                canvas.width = Math.max(1, Math.round(width * scale));
                canvas.height = Math.max(1, Math.round(height * scale));
                canvas.getContext("2d").drawImage(source, 0, 0, canvas.width, canvas.height);
                picture = canvas.toDataURL("image/jpeg", 0.9);
            }
        } catch (error) {
            picture = "";
        }
        let target = "";
        try {
            target = to ? new URL(to, global.location.href).pathname : "";
        } catch (error) {
            target = "";
        }
        const record = { at: Date.now(), to: target, name: state.name, bytes: state.bytes, channels: state.channels,
                         elapsed: Math.round(performance.now() - state.started), times: state.times, picture };
        try {
            global.sessionStorage.setItem(HANDOFF_KEY, JSON.stringify(record));
        } catch (error) {
            try {
                global.sessionStorage.setItem(HANDOFF_KEY, JSON.stringify({ ...record, picture: "" }));
            } catch (inner) {
                return false;
            }
        }
        return true;
    }

    /** The viewer's page takes up a first view the home page handed over. -> the state, or null. */
    function resume() {
        let record = null;
        try {
            const raw = global.sessionStorage.getItem(HANDOFF_KEY);
            if (raw) global.sessionStorage.removeItem(HANDOFF_KEY);
            record = raw ? JSON.parse(raw) : null;
        } catch (error) {
            record = null;
        }
        if (!record || !(Date.now() - record.at < HANDOFF_MAX_AGE_MS) || !doc.body) return null;
        if (!doc.getElementById("viewer_sidebar")) return null;
        // Only the viewer it was handed to.
        if (record.to && record.to !== global.location.pathname) return null;
        const state = start(record.name || "", { bytes: record.bytes, channels: record.channels });
        state.started = performance.now() - (Number(record.elapsed) || 0) - Math.max(0, Date.now() - record.at);
        state.times = Array.isArray(record.times) ? record.times.slice(0, STEPS.length) : [];
        state.step = Math.max(0, state.times.length);
        reach(state, "viewer");
        if (record.picture) {
            const img = new Image();
            img.onload = () => { if (current === state && img.naturalWidth) show(state, img); };
            img.src = record.picture;
        }
        return state;
    }

    /** The preview let go: faded out and removed. */
    function done(reason = "") {
        const state = current;
        if (!state) return;
        current = null;
        global.clearInterval(watch);
        global.__plexoraFirstView = { ...(global.__plexoraFirstView || {}), endedBy: reason,
                                      endedMs: Math.round(performance.now() - state.started) };
        state.box.classList.add("is-leaving");
        global.setTimeout(() => state.box.remove(), 320);
    }

    /** Every directory offset the preview found of `file` (for Python's open). */
    function offsetsFor(file) {
        const held = file ? found.get(keyOf(file)) : null;
        return held ? [...new Set(held.offsets)].sort((a, b) => a - b) : null;
    }

    /**
     * The file mounted at `path`: its directories read ahead in the worker,
     * once the preview's read of them is done (waited for `waitMs` at most).
     * -> how many pieces were read. Sent before Python opens the file: the
     * worker answers nothing while Python reads.
     */
    async function prefetch(path, file, { waitMs = 3000 } = {}) {
        if (!file) return 0;
        const pending = reading.get(keyOf(file));
        if (pending) {
            await Promise.race([pending.catch(() => null), new Promise((resolve) => global.setTimeout(resolve, waitMs))]);
        }
        const offsets = offsetsFor(file);
        const python = global.PlexoraPython;
        if (!offsets || !offsets.length || !python || typeof python.prefetch !== "function") return 0;
        const started = performance.now();
        const read = await python.prefetch(path, offsets).then((r) => (r && r.read) || 0).catch(() => 0);
        global.__plexoraFirstView = { ...(global.__plexoraFirstView || {}), prefetched: read,
                                      prefetchMs: Math.round(performance.now() - started) };
        return read;
    }

    /** The smallest level's first plane, raw (tests/js/first_view_probe.mjs). */
    async function plane(file) {
        const io = reader(file);
        const located = await smallestLevel(io);
        if (!located) throw new Error("not a TIFF this preview reads");
        const got = await pixels(io, located.tiff, located.level);
        return { ...got, layout: located.layout, channels: located.channels, offsets: [...new Set(located.offsets)].sort((a, b) => a - b),
                 reads: io.reads() };
    }

    global.PlexoraFirstView = Object.freeze({
        begin, beginUrl, done, prefetch, offsetsFor, step, handOff, resume,
        active: () => Boolean(current),
        // For tests (tests/js/first_view_probe.mjs).
        _read: read,
        _plane: plane,
    });

    // The local app's viewer page takes up the first view its home page
    // handed over (handOff), loaded deferred from templates/index.html.
    try {
        if (doc && typeof doc.getElementById === "function" && doc.getElementById("viewer_sidebar")) resume();
    } catch (error) {
        // No first view: the viewer's own loading is what is on screen.
    }
})(typeof window !== "undefined" ? window : globalThis);
