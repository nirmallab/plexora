/**
 * The first view of an image, at once: the moment a file is given to the
 * home page, its first channel's smallest pyramid level is read by the page
 * itself and shown where the viewer will be, while the import (Python's
 * inspect and registration, the viewer's page, the project's load) goes on
 * behind it. When the viewer has drawn the same image -- or the person reaches
 * for it, or the import needs a question answered -- the preview fades.
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
            return { tiff, level: best, offsets, layout: "subifds" };
        }
        // A pyramid in the main chain, or no pyramid at all.
        const planes = planesOf(await values(io, tiff, first, 270));
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
            return { tiff, level: best, offsets, layout };
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
        return { tiff, level: best, offsets, layout };
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

    /** A File's first view: {canvas, width, height, ms, reads, layout} (throws when it cannot). */
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
                 full: { width: 0, height: 0 } };
    }

    // -- on screen ---------------------------------------------------------------

    let current = null;
    let watch = 0;
    //: The longest a first view stays up waiting for the viewer, and the
    //: longest a loaded page of the app with no viewer in it is covered.
    const MAX_SHOWN_MS = 180000;
    const NO_VIEWER_MS = 15000;

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
  display:flex;align-items:center;justify-content:center;background:#05070a;transition:opacity .28s ease}
.plexora-first-view.is-leaving{opacity:0}
.plexora-first-view canvas,.plexora-first-view img{max-width:calc(100% - 48px);max-height:calc(100% - 48px);
  object-fit:contain;image-rendering:auto;opacity:0;transition:opacity .18s ease}
.plexora-first-view.is-shown canvas,.plexora-first-view.is-shown img{opacity:1}
.plexora-first-view-chip{position:absolute;left:16px;bottom:14px;font:500 12px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif;
  color:#c9d3e0;background:rgba(14,18,25,.82);border:1px solid rgba(255,255,255,.08);border-radius:999px;padding:5px 11px}`;
        doc.head.appendChild(css);
    }

    function overlay(name) {
        style();
        const box = doc.createElement("div");
        box.className = "plexora-first-view";
        box.setAttribute("aria-hidden", "true");
        const chip = doc.createElement("div");
        chip.className = "plexora-first-view-chip";
        chip.textContent = name ? `Opening ${name}…` : "Opening…";
        box.appendChild(chip);
        doc.body.appendChild(box);
        return { box, chip };
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
                state.box.style.left = `${rect.left}px`;
                state.box.style.top = `${rect.top}px`;
                state.box.style.right = "auto";
                state.box.style.bottom = "auto";
                state.box.style.width = `${rect.width}px`;
                state.box.style.height = `${rect.height}px`;
                const frame = doc.getElementById("plexora-app");
                const win = frame ? frame.contentWindow : global;
                if (win !== reachWin) {
                    unreach();
                    reachWin = win;
                    unreach = onReach(() => done("reached"));
                }
            }
            if (viewerDrawn()) { done("drawn"); return; }
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

    function show(state, element) {
        element.setAttribute("alt", "");
        state.box.insertBefore(element, state.chip);
        // Two frames: drawn, then faded in.
        global.requestAnimationFrame(() => global.requestAnimationFrame(() => state.box.classList.add("is-shown")));
        state.shownAt = performance.now();
        global.__plexoraFirstView = { ...(global.__plexoraFirstView || {}), shownMs: Math.round(state.shownAt - state.started) };
    }

    function start(name) {
        done("replaced");
        const state = { ...overlay(name), started: performance.now(), name };
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
        const state = start(name || file.name);
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
            show(state, preview.canvas);
        }).catch((error) => {
            if (current !== state) return;
            global.__plexoraFirstView = { ...(global.__plexoraFirstView || {}), failed: String(error && error.message || error) };
            // Nothing to show: the import's own progress is what is on screen.
            done("unreadable");
        });
        return state;
    }

    /** A preview image Python made from a path (the local app's home page). */
    function beginUrl({ url, name = "" } = {}) {
        if (!url || !doc || !doc.body) return null;
        const state = start(name);
        const img = new Image();
        img.decoding = "async";
        img.onload = () => {
            if (current !== state) return;
            if (!img.naturalWidth) { done("unreadable"); return; }
            show(state, img);
        };
        img.onerror = () => { if (current === state) done("unreadable"); };
        img.src = url;
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
        return { ...got, layout: located.layout, offsets: [...new Set(located.offsets)].sort((a, b) => a - b),
                 reads: io.reads() };
    }

    global.PlexoraFirstView = Object.freeze({
        begin, beginUrl, done, prefetch, offsetsFor,
        active: () => Boolean(current),
        // For tests (tests/js/first_view_probe.mjs).
        _read: read,
        _plane: plane,
    });
})(typeof window !== "undefined" ? window : globalThis);
