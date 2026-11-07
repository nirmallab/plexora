/**
 * PlexoraSlides -- whole-slide images on the website, read by this page with
 * openslide-js (node/slideReader.js; plan L2).
 *
 * The formats only OpenSlide opens (MRXS, Sakura) and the TIFF-based slides
 * Python's tifffile decodes one tile at a time in the worker (NDPI, SVS) are
 * read here instead, for Python and for the viewer alike, so the two never
 * disagree:
 *
 * - Python asks (`PlexoraPython.answers`): "slide" {path} -> the slide's
 *   levels and properties, or {error} saying why not (an MRXS picked without
 *   its folder); "slide_region" {path, level, x, y, w, h} -> {width, height,
 *   data} straight RGBA (providers/browser.py `PageSlide`, which
 *   brightfield.py reads as it reads OpenSlide on the desktop);
 * - the browser node (services/browserNode.js) asks `tile(slide, level, tx,
 *   ty, tw, th)` for a brightfield tile (`/generated/data/<p>/rgb/...`) and
 *   gets the RGB PNG `read_tile` would make, or null where Python resamples
 *   (Python then answers it, through the same reader).
 *
 * The files come from the picks (services/pickedFiles.js): an MRXS is its
 * .mrxs and the folder of the same name beside it, mounted together.
 *
 * Classic script, `window.PlexoraSlides`; needs PlexoraPython and PlexoraPicks.
 */
(function (global) {
    "use strict";

    const MODULE = "/client/src/js/node/slideReader.js";
    //: Decode workers: each is a whole OpenSlide instance (tens of MB).
    const WORKERS = Math.min(4, Math.max(2, Math.floor((global.navigator.hardwareConcurrency || 4) / 4)));

    let reader = null;
    let module = null;

    async function load() {
        if (!module) module = import(MODULE);
        const loaded = await module;
        if (!reader) reader = loaded.createSlideReader({ workers: WORKERS });
        return { reader, lib: loaded };
    }

    const name = (path) => String(path).split("/").pop();
    const dirOf = (path) => String(path).slice(0, String(path).lastIndexOf("/"));

    /**
     * What openslide-js opens for the picked slide at `path`: the File, or
     * for an MRXS the .mrxs and every file under the folder named after it,
     * as [{path, file}] relative to the folder holding the .mrxs.
     */
    function sourceFor(path) {
        const picks = global.PlexoraPicks;
        const file = picks ? picks.fileFor(path) : null;
        if (!file) throw new Error("this page does not hold that file now: pick it again");
        if (!/\.mrxs$/i.test(path)) return file;
        const stem = name(path).replace(/\.mrxs$/i, "");
        const prefix = `${dirOf(path)}/${stem}/`;
        const inside = (picks.filesUnder ? picks.filesUnder(prefix) : [])
            .map(({ path: p, file: f }) => ({ path: `${stem}/${p.slice(prefix.length)}`, file: f }));
        if (!inside.some((entry) => /\/slidedat\.ini$/i.test(entry.path))) {
            throw new Error(`an MRXS slide keeps its pixels in the folder "${stem}" beside `
                            + `${name(path)}: pick the .mrxs again and choose that folder when asked`);
        }
        return [{ path: name(path), file }, ...inside];
    }

    async function open(path) {
        const { reader: r } = await load();
        return r.open(path, sourceFor(path));
    }

    //: Python's "slide" question: the slide's facts, or why not.
    async function answerSlide({ path }) {
        try {
            return { value: await open(path) };
        } catch (error) {
            const message = String(error && error.message || error);
            if (/\.svslide$/i.test(path) && /signature mismatch|RuntimeError|unreachable/i.test(message)) {
                // openslide-js 1.0.0-beta.5 stops inside OpenSlide on a Sakura
                // slide native OpenSlide opens (L2.md): say so, not the trap.
                return { value: { error: "Sakura slides do not open on the website yet (its OpenSlide build "
                                         + "stops on them); open this one in the Plexora desktop app" } };
            }
            return { value: { error: message.replace(/^OpenSlideError:\s*/, "").split("\n")[0].slice(0, 400) } };
        }
    }

    //: Python's "slide_region" question: straight RGBA of one region.
    async function answerRegion({ path, level, x, y, w, h }) {
        try {
            const { reader: r } = await load();
            if (!r.has(path)) await open(path);
            const found = await r.region(path, { level, x, y, w, h });
            return { value: found, transfer: [found.data.buffer] };
        } catch (error) {
            return { value: { error: String(error && error.message || error).slice(0, 400) } };
        }
    }

    /**
     * A brightfield tile of `slide` (node_spec's `slide`: path, levels) as
     * `encode_tile_array` makes it for `q=hd` -- the RGB PNG -- or null where
     * Python resamples the tile's rectangle or the level is a derived one.
     */
    async function tile(slide, level, tx, ty, tw, th) {
        const spec = slide.levels[level];
        if (!spec) return null;
        const { reader: r, lib } = await load();
        const window = lib.tileWindow(spec, tx, ty, tw, th);
        if (!window) return null;
        if (!r.has(slide.path)) await open(slide.path);
        const found = await r.region(slide.path, window);
        const rgb = lib.overWhite(found.data, found.width * found.height);
        return global.PlexoraNode.png(found.width, found.height, 8, 2, rgb);
    }

    if (global.PlexoraPython && global.PlexoraPython.answers) {
        global.PlexoraPython.answers("slide", answerSlide);
        global.PlexoraPython.answers("slide_region", answerRegion);
    }

    global.PlexoraSlides = Object.freeze({
        tile, open, sourceFor,
        forget: (path) => (reader ? reader.close(path) : null),
    });
})(window);
