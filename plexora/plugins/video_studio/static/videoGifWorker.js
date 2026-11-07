/**
 * videoGifWorker.js - an animated GIF written in the browser, off the main thread.
 *
 * The website's GIF (videoEncoders.js `GifEncoder`): the desktop app writes
 * its GIFs with FFmpeg, which a web page cannot run, and the page's Python
 * (Pillow) must hold every frame, so it is capped. This writes frame by frame
 * and keeps nothing but the file, so a GIF is as long and as large as the
 * desktop's.
 *
 * Each frame gets its own 256-colour palette (median cut over a 5-bit-per-
 * channel histogram; FFmpeg's palettegen `stats_mode=single`), its pixels
 * mapped to it through an 8 x 8 ordered (Bayer) dither as the desktop's
 * `paletteuse=dither=bayer` does, and LZW-coded as GIF89a asks. Loops forever;
 * frame delays in hundredths of a second, rounded so they add up to the
 * video's length.
 *
 * Messages in:  {type: "start", settings: {width, height, fps}}
 *               | {type: "frame", rgba: ArrayBuffer, index} | {type: "finish"} | {type: "cancel"}
 * Messages out: {type: "started"} | {type: "ack", index} | {type: "done", blob, bytes}
 *               | {type: "cancelled"} | {type: "error", message}
 *
 * Classic worker script; Plexora's own code, no third-party part.
 */
"use strict";

let state = null;

// -- the file ------------------------------------------------------------------------

class Bytes {
    constructor() {
        this.parts = [];
        this.buffer = new Uint8Array(1 << 16);
        this.length = 0;
        this.total = 0;
    }

    byte(value) {
        if (this.length === this.buffer.length) this.flush();
        this.buffer[this.length++] = value;
    }

    word(value) {
        this.byte(value & 255);
        this.byte((value >> 8) & 255);
    }

    bytes(values) {
        for (let i = 0; i < values.length; i += 1) this.byte(values[i]);
    }

    flush() {
        if (!this.length) return;
        this.parts.push(this.buffer.slice(0, this.length));
        this.total += this.length;
        this.length = 0;
    }

    blob() {
        this.flush();
        return new Blob(this.parts, { type: "image/gif" });
    }
}

const ascii = (text) => Array.from(text, (c) => c.charCodeAt(0));

function header(out, width, height) {
    out.bytes(ascii("GIF89a"));
    out.word(width);
    out.word(height);
    out.byte(0x70);  // no global colour table; 8 bits of colour resolution
    out.byte(0);
    out.byte(0);
    // NETSCAPE2.0: loop forever.
    out.bytes([0x21, 0xff, 0x0b, ...ascii("NETSCAPE2.0"), 0x03, 0x01, 0x00, 0x00, 0x00]);
}

// -- one frame's palette: median cut ---------------------------------------------------

const BINS = 1 << 15;

/** Up to 256 colours for `rgba`: [palette (Uint8Array 768), count]. */
function medianCut(rgba, counts, sums) {
    counts.fill(0);
    sums.fill(0);
    for (let i = 0; i < rgba.length; i += 4) {
        const r = rgba[i], g = rgba[i + 1], b = rgba[i + 2];
        const bin = ((r >> 3) << 10) | ((g >> 3) << 5) | (b >> 3);
        counts[bin] += 1;
        sums[bin * 3] += r;
        sums[bin * 3 + 1] += g;
        sums[bin * 3 + 2] += b;
    }
    const used = [];
    for (let bin = 0; bin < BINS; bin += 1) if (counts[bin]) used.push(bin);
    const channel = (bin, c) => (c === 0 ? bin >> 10 : c === 1 ? (bin >> 5) & 31 : bin & 31);
    const describe = (bins) => {
        let pixels = 0;
        const lo = [31, 31, 31], hi = [0, 0, 0];
        for (const bin of bins) {
            pixels += counts[bin];
            for (let c = 0; c < 3; c += 1) {
                const v = channel(bin, c);
                if (v < lo[c]) lo[c] = v;
                if (v > hi[c]) hi[c] = v;
            }
        }
        const ranges = [hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]];
        const axis = ranges.indexOf(Math.max(...ranges));
        return { bins, pixels, axis, range: ranges[axis] };
    };
    const boxes = [describe(used)];
    while (boxes.length < 256) {
        // The box worth splitting most: many pixels over a wide range.
        let best = -1;
        let score = 0;
        for (let i = 0; i < boxes.length; i += 1) {
            const box = boxes[i];
            if (box.bins.length < 2 || box.range === 0) continue;
            const s = box.pixels * box.range;
            if (s > score) { score = s; best = i; }
        }
        if (best < 0) break;
        const box = boxes[best];
        const axis = box.axis;
        box.bins.sort((a, b) => channel(a, axis) - channel(b, axis));
        // Split at the pixel-weighted median, never leaving a side empty.
        let seen = 0;
        let cut = 1;
        for (let i = 0; i < box.bins.length - 1; i += 1) {
            seen += counts[box.bins[i]];
            cut = i + 1;
            if (seen * 2 >= box.pixels) break;
        }
        boxes.splice(best, 1, describe(box.bins.slice(0, cut)), describe(box.bins.slice(cut)));
    }
    const palette = new Uint8Array(768);
    boxes.forEach((box, i) => {
        let r = 0, g = 0, b = 0;
        for (const bin of box.bins) {
            r += sums[bin * 3];
            g += sums[bin * 3 + 1];
            b += sums[bin * 3 + 2];
        }
        palette[i * 3] = Math.round(r / box.pixels);
        palette[i * 3 + 1] = Math.round(g / box.pixels);
        palette[i * 3 + 2] = Math.round(b / box.pixels);
    });
    return [palette, boxes.length];
}

// -- mapping through an ordered dither ---------------------------------------------------

//: The 8 x 8 Bayer matrix, as offsets of -4..+3 levels: a half-step of the
//: 5-bit bins either way, FFmpeg's bayer_scale=4 strength.
const BAYER = (() => {
    const out = new Int8Array(64);
    for (let y = 0; y < 8; y += 1) {
        for (let x = 0; x < 8; x += 1) {
            let v = 0;
            const q = x ^ y;
            for (let bit = 0; bit < 3; bit += 1) {
                v |= ((q >> bit) & 1) << (5 - 2 * bit);
                v |= ((y >> bit) & 1) << (4 - 2 * bit);
            }
            // Bit-reversed interleave: the classic recursive Bayer order, 0..63
            // (row 0: 0 32 8 40 2 34 10 42), as 8 offsets of one level each.
            out[y * 8 + x] = Math.floor(v / 8) - 4;
        }
    }
    return out;
})();

function nearest(palette, size, r, g, b) {
    let best = 0;
    let distance = Infinity;
    for (let i = 0; i < size; i += 1) {
        const dr = palette[i * 3] - r, dg = palette[i * 3 + 1] - g, db = palette[i * 3 + 2] - b;
        const d = dr * dr * 2 + dg * dg * 4 + db * db * 3;
        if (d < distance) { distance = d; best = i; }
    }
    return best;
}

function mapFrame(rgba, width, height, palette, size, lookup, indices) {
    lookup.fill(-1);
    for (let y = 0, p = 0; y < height; y += 1) {
        for (let x = 0; x < width; x += 1, p += 1) {
            const d = BAYER[((y & 7) << 3) | (x & 7)];
            const o = p * 4;
            const r = Math.max(0, Math.min(255, rgba[o] + d));
            const g = Math.max(0, Math.min(255, rgba[o + 1] + d));
            const b = Math.max(0, Math.min(255, rgba[o + 2] + d));
            const bin = ((r >> 3) << 10) | ((g >> 3) << 5) | (b >> 3);
            let index = lookup[bin];
            if (index < 0) {
                // The bin's own colour: its middle.
                index = nearest(palette, size, ((r >> 3) << 3) | 4, ((g >> 3) << 3) | 4, ((b >> 3) << 3) | 4);
                lookup[bin] = index;
            }
            indices[p] = index;
        }
    }
}

// -- LZW, as GIF89a codes it -------------------------------------------------------------------

function lzw(out, indices, minCode) {
    out.byte(minCode);
    const clearCode = 1 << minCode;
    const endCode = clearCode + 1;
    let nextCode = endCode + 1;
    let codeSize = minCode + 1;
    let table = new Map();
    const block = new Uint8Array(255);
    let blockLength = 0;
    let accumulator = 0;
    let bits = 0;
    const emitByte = (value) => {
        block[blockLength++] = value;
        if (blockLength === 255) {
            out.byte(255);
            out.bytes(block);
            blockLength = 0;
        }
    };
    const emit = (code) => {
        accumulator |= code << bits;
        bits += codeSize;
        while (bits >= 8) {
            emitByte(accumulator & 255);
            accumulator >>>= 8;
            bits -= 8;
        }
    };
    emit(clearCode);
    let prefix = indices[0];
    for (let i = 1; i < indices.length; i += 1) {
        const k = indices[i];
        const key = (prefix << 8) | k;
        const found = table.get(key);
        if (found !== undefined) {
            prefix = found;
            continue;
        }
        emit(prefix);
        if (nextCode === 4096) {
            emit(clearCode);
            nextCode = endCode + 1;
            codeSize = minCode + 1;
            table = new Map();
        } else {
            if (nextCode >= (1 << codeSize)) codeSize += 1;
            table.set(key, nextCode);
            nextCode += 1;
        }
        prefix = k;
    }
    emit(prefix);
    emit(endCode);
    if (bits > 0) emitByte(accumulator & 255);
    if (blockLength) {
        out.byte(blockLength);
        out.bytes(block.subarray(0, blockLength));
    }
    out.byte(0);
}

// -- the worker ------------------------------------------------------------------------

function start(settings) {
    const { width, height, fps } = settings;
    if (!(width > 0 && height > 0 && width < 65536 && height < 65536)) throw new Error("a GIF is at most 65535 px a side");
    state = {
        width, height, fps: Math.max(1, fps), frames: 0, out: new Bytes(),
        counts: new Uint32Array(BINS), sums: new Float64Array(BINS * 3), lookup: new Int16Array(BINS),
        indices: new Uint8Array(width * height),
    };
    header(state.out, width, height);
    self.postMessage({ type: "started" });
}

function frame(message) {
    const s = state;
    const rgba = new Uint8Array(message.rgba);
    if (rgba.length !== s.width * s.height * 4) throw new Error("a frame of the wrong size");
    const [palette, size] = medianCut(rgba, s.counts, s.sums);
    mapFrame(rgba, s.width, s.height, palette, size, s.lookup, s.indices);
    // Hundredths of a second, rounded so the delays add up to the length.
    const k = s.frames;
    const delay = Math.round(((k + 1) * 100) / s.fps) - Math.round((k * 100) / s.fps);
    const out = s.out;
    out.bytes([0x21, 0xf9, 0x04, 0x04]);  // graphic control: leave the frame in place
    out.word(Math.max(1, delay));
    out.bytes([0x00, 0x00]);
    out.byte(0x2c);
    out.word(0);
    out.word(0);
    out.word(s.width);
    out.word(s.height);
    out.byte(0x87);  // a local colour table of 256 entries
    out.bytes(palette);
    lzw(out, s.indices, 8);
    s.frames += 1;
    self.postMessage({ type: "ack", index: message.index });
}

function finish() {
    state.out.byte(0x3b);
    const blob = state.out.blob();
    state = null;
    self.postMessage({ type: "done", blob, bytes: blob.size });
}

if (typeof self !== "undefined") self.onmessage = (event) => {
    const message = event.data || {};
    try {
        if (message.type === "start") start(message.settings);
        else if (message.type === "frame") frame(message);
        else if (message.type === "finish") finish();
        else if (message.type === "cancel") {
            state = null;
            self.postMessage({ type: "cancelled" });
        }
    } catch (error) {
        self.postMessage({ type: "error", message: String(error?.message || error) });
    }
};

// For tests under node: the pieces, without a worker around them.
if (typeof module !== "undefined" && module.exports) {
    module.exports = { medianCut, mapFrame, lzw, Bytes, header, BAYER };
}
