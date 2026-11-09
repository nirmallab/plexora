/**
 * FigureNodePixels - Quick Edit's pixels, read by the page itself on the website.
 *
 * On the website the whole app runs in the page, and every request is answered
 * by its one Python, in turn. Quick Edit asks for one region per visible
 * channel on every framing change, several times a second while the view is
 * dragged -- and each of those was a Python request reading the picked file
 * through tifffile, queued behind whatever else the page was asking for. The
 * page already reads that same file itself, in workers, for the viewer's tiles
 * (core's services/browserNode.js, `PlexoraNode`). So when it can, Quick Edit
 * asks the page instead, and Python is not involved at all.
 *
 * This is `server/pixels.read_region`, transcribed, so the two answers agree:
 *
 *   - the same level, `render.choose_view_level` (the NEAREST level to the
 *     screen, short of it by at most VIEW_DETAIL);
 *   - the same box, floored and ceiled to that level's pixels and clipped to
 *     the image, as `SourceImage.read` does;
 *   - the same resample, Pillow's LANCZOS on float32 (horizontal pass, then
 *     vertical, each output pixel a normalised window of the truncated sinc),
 *     clipped and truncated to uint16 as numpy's `astype` does.
 *
 * Pixels are the viewer's own HD tiles (`PlexoraNode.decoded`): the u16
 * samples the tile route would send, 8-bit sources widened as Python widens
 * them. Exactly the same numbers as Python's for a box the output size; after
 * a resample within one level of them (the platform's `sin` may differ from
 * C's in its last bit).
 *
 * Anything this does not read the way Python does -- not the website, a
 * datasource the viewer does not have open (asking the node about one would
 * make the page's Python LOAD it and evict the user's session), a z-stack's
 * plane or projection, a level the file does not hold, a tile the page will not
 * read -- is null, and the caller asks the server as before.
 */
class FigureNodePixels {

    /** `server/utils/source_image.VIEW_DETAIL`. */
    static get VIEW_DETAIL() { return 0.7071; }

    /** `server/utils/source_image.MAX_SOURCE_PIXELS`: past this the server
     *  refuses the region, and so does this (it asks the server, which says
     *  why). */
    static get MAX_SOURCE_PIXELS() { return 120000000; }

    /** Whether the page can be asked about this datasource at all. */
    static available(datasource, loadedDatasource) {
        const node = globalThis.PlexoraNode;
        return Boolean(globalThis.PlexoraBackend && globalThis.PlexoraBackend.kind === "browser"
            && node && typeof node.decoded === "function" && typeof node.project === "function"
            && datasource && datasource === loadedDatasource);
    }

    /**
     * One channel of one region, as `readPixels` answers it: `{ok, data
     * (Uint16Array, row-major), width, height, box}` -- or null for the
     * server to answer.
     *
     * @param {string} datasource the project the source is.
     * @param {string} key the channel's stable key (its tile route segment).
     * @param {object} params `{x, y, w, h, out_w, out_h, view, signal}`, as
     *        `FigureBuilderApi.readPixels` takes them.
     */
    static async read(datasource, key, params) {
        if (params.view) return null;
        const index = /_(\d+)$/.exec(String(key || ""));
        if (!index) return null;
        const node = globalThis.PlexoraNode;
        let entry;
        try {
            entry = await node.project(datasource);
        } catch (error) {
            return null;
        }
        const spec = entry && entry.spec;
        const image = entry && entry.image;
        if (!spec || spec.kind === "slide" || !image || !image.pool || !image.geometry
                || !Array.isArray(spec.levels) || !spec.levels.length || !Array.isArray(spec.tile)) {
            return null;
        }
        if (Number(index[1]) >= spec.levels[0][0]) return null;

        const outW = Math.round(params.out_w);
        const outH = Math.round(params.out_h);
        if (!(outW >= 1) || !(outH >= 1)) return null;
        const level = FigureNodePixels.chooseViewLevel(spec.levels.length, params.w, outW);
        if (level >= image.geometry.levels.length) return null;
        const [, height, width] = spec.levels[level];
        const divisor = 2 ** level;
        const x0 = Math.max(0, Math.min(Math.floor(params.x / divisor), width));
        const y0 = Math.max(0, Math.min(Math.floor(params.y / divisor), height));
        const x1 = Math.max(x0, Math.min(Math.ceil((params.x + params.w) / divisor), width));
        const y1 = Math.max(y0, Math.min(Math.ceil((params.y + params.h) / divisor), height));

        let plane;
        let planeW;
        let planeH;
        if (x1 <= x0 || y1 <= y0) {
            plane = new Float32Array(1);
            planeW = 1;
            planeH = 1;
        } else {
            if ((x1 - x0) * (y1 - y0) > FigureNodePixels.MAX_SOURCE_PIXELS) return null;
            planeW = x1 - x0;
            planeH = y1 - y0;
            plane = await FigureNodePixels.stitch(node, datasource, key, level, spec.tile,
                                                  x0, y0, x1, y1);
            if (!plane) return null;
        }
        if (params.signal && params.signal.aborted) return { ok: false, data: null, aborted: true };

        const resized = (planeW === outW && planeH === outH)
            ? plane : FigureNodePixels.resample(plane, planeW, planeH, outW, outH);
        const data = new Uint16Array(outW * outH);
        for (let i = 0; i < data.length; i += 1) {
            const value = resized[i];
            data[i] = value <= 0 ? 0 : (value >= 65535 ? 65535 : Math.trunc(value));
        }
        return { ok: true, data, width: outW, height: outH,
                 box: [x0 * divisor, y0 * divisor, x1 * divisor, y1 * divisor] };
    }

    /** `render.choose_view_level`: the largest level whose width over the
     *  region still meets the target times VIEW_DETAIL. */
    static chooseViewLevel(levels, viewportWidth, targetPixels) {
        const target = targetPixels * FigureNodePixels.VIEW_DETAIL;
        let best = 0;
        for (let level = 0; level < Math.max(1, levels); level += 1) {
            if (viewportWidth / (2 ** level) >= target) best = level;
            else break;
        }
        return best;
    }

    /** The level's pixels in [x0, x1) x [y0, y1), from the node's tiles, as
     *  float32; null when any tile is not the page's to read. */
    static async stitch(node, datasource, key, level, tile, x0, y0, x1, y1) {
        const [tw, th] = tile;
        if (!(tw > 0) || !(th > 0)) return null;
        const w = x1 - x0;
        const plane = new Float32Array(w * (y1 - y0));
        const asks = [];
        for (let ty = Math.floor(y0 / th); ty <= Math.floor((y1 - 1) / th); ty += 1) {
            for (let tx = Math.floor(x0 / tw); tx <= Math.floor((x1 - 1) / tw); tx += 1) {
                const url = `/generated/data/${encodeURIComponent(datasource)}/${encodeURIComponent(key)}`
                    + `/${level}/${tx}_${ty}.png?q=hd`;
                asks.push(Promise.resolve(node.decoded(url)).catch(() => null)
                    .then((got) => ({ got, left: tx * tw, top: ty * th })));
            }
        }
        for (const { got, left, top } of await Promise.all(asks)) {
            if (!got || got.format !== "u16" || !got.array) return null;
            const bytes = got.array instanceof Uint8Array ? got.array
                : new Uint8Array(got.array.buffer || got.array, got.array.byteOffset || 0);
            const fromX = Math.max(x0, left);
            const toX = Math.min(x1, left + got.width);
            const fromY = Math.max(y0, top);
            const toY = Math.min(y1, top + got.height);
            for (let y = fromY; y < toY; y += 1) {
                let at = ((y - top) * got.width + (fromX - left)) * 2;
                let to = (y - y0) * w + (fromX - x0);
                // Two bytes a sample, big-endian: the HD PNG's order.
                for (let x = fromX; x < toX; x += 1, at += 2, to += 1) {
                    plane[to] = (bytes[at] << 8) | bytes[at + 1];
                }
            }
        }
        return plane;
    }

    /** Pillow's LANCZOS window, `lanczos_filter` in libImaging/Resample.c. */
    static lanczos(x) {
        if (-3 <= x && x < 3) {
            const sinc = (v) => {
                if (v === 0) return 1;
                const p = v * Math.PI;
                return Math.sin(p) / p;
            };
            return sinc(x) * sinc(x / 3);
        }
        return 0;
    }

    /** `precompute_coeffs`: per output pixel, the first input pixel, the count,
     *  and the normalised weights. */
    static coefficients(inSize, outSize) {
        const scale = inSize / outSize;
        const filterscale = Math.max(1, scale);
        const support = 3 * filterscale;
        const ksize = Math.ceil(support) * 2 + 1;
        const bounds = new Int32Array(outSize * 2);
        const weights = new Float64Array(outSize * ksize);
        const ss = 1 / filterscale;
        for (let xx = 0; xx < outSize; xx += 1) {
            const center = (xx + 0.5) * scale;
            let xmin = Math.trunc(center - support + 0.5);
            if (xmin < 0) xmin = 0;
            let xmax = Math.trunc(center + support + 0.5);
            if (xmax > inSize) xmax = inSize;
            xmax -= xmin;
            let ww = 0;
            for (let x = 0; x < xmax; x += 1) {
                const w = FigureNodePixels.lanczos((x + xmin - center + 0.5) * ss);
                weights[xx * ksize + x] = w;
                ww += w;
            }
            if (ww !== 0) {
                for (let x = 0; x < xmax; x += 1) weights[xx * ksize + x] /= ww;
            }
            bounds[xx * 2] = xmin;
            bounds[xx * 2 + 1] = xmax;
        }
        return { bounds, weights, ksize };
    }

    /** Pillow's `ImagingResample` on a float32 image: horizontally where the
     *  width changes, then vertically where the height does. */
    static resample(plane, inW, inH, outW, outH) {
        let current = plane;
        let width = inW;
        if (outW !== inW) {
            const { bounds, weights, ksize } = FigureNodePixels.coefficients(inW, outW);
            const next = new Float32Array(outW * inH);
            for (let y = 0; y < inH; y += 1) {
                const row = y * inW;
                for (let xx = 0; xx < outW; xx += 1) {
                    const xmin = bounds[xx * 2];
                    const count = bounds[xx * 2 + 1];
                    const k = xx * ksize;
                    let sum = 0;
                    for (let x = 0; x < count; x += 1) sum += current[row + xmin + x] * weights[k + x];
                    next[y * outW + xx] = sum;
                }
            }
            current = next;
            width = outW;
        }
        if (outH !== inH) {
            const { bounds, weights, ksize } = FigureNodePixels.coefficients(inH, outH);
            const next = new Float32Array(width * outH);
            for (let yy = 0; yy < outH; yy += 1) {
                const ymin = bounds[yy * 2];
                const count = bounds[yy * 2 + 1];
                const k = yy * ksize;
                for (let x = 0; x < width; x += 1) {
                    let sum = 0;
                    for (let y = 0; y < count; y += 1) sum += current[(ymin + y) * width + x] * weights[k + y];
                    next[yy * width + x] = sum;
                }
            }
            current = next;
        }
        return current;
    }
}
