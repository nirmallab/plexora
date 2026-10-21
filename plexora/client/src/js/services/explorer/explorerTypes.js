/**
 * explorerTypes.js -- what the file explorer knows about a name without
 * asking the server, and the small drawing helpers its other modules share.
 *
 * Nothing here reads a file or decides what the server will accept. The
 * tables say which rows a field can take (greying the rest), what word goes
 * in the Type column and which glyph goes beside a name. The server is the
 * authority on what a directory IS: a row it marks with `store` is a dataset
 * whatever it is called, and the `.zarr` naming rule below is only the
 * fallback for a node too old to say.
 *
 * Loaded before the other explorer modules (base.html), which read it through
 * `window.PlexoraExplorerTypes`.
 */
window.PlexoraExplorerTypes = (function () {
    "use strict";

    //: What each legacy filter accepts. Kept in step with native_dialog.py's
    //: _TK_FILTERS -- the two describe the same choices, one for each kind of
    //: picker. `fileLocation.filterFor` asks `accepts` with these keys too.
    const EXTENSIONS = {
        // ".zarr"/".ome.zarr" are here because the Image field takes an
        // OME-Zarr store, and this table is the client's one statement of what
        // each filter accepts. They change nothing about files: a store is a
        // directory, and what makes one pickable is `datasets` (normalizeAccept).
        // ".dcm" is a file: one instance of a DICOM slide selects the whole
        // slide, whose other instances the server gathers from the metadata.
        image: [".tif", ".tiff", ".ome.tif", ".ome.tiff", ".svs", ".ndpi",
                ".scn", ".bif", ".qptiff", ".dcm", ".png", ".jpg", ".jpeg",
                ".zarr", ".ome.zarr", ".mrxs", ".ims"],
        csv: [".csv"],
        h5ad: [".h5ad"],
        data: [".csv", ".tsv", ".txt", ".parquet", ".h5ad"],
        channels: [".csv", ".tsv", ".txt", ".xlsx", ".xlsm"],
        any: null,
    };

    //: How the footer explains a greyed file, per filter. A list of seventeen
    //: suffixes is not an explanation.
    const FILTER_WORDS = {
        image: "an image (.ome.tiff, .svs, .ndpi, .png, …)",
        csv: "a .csv table",
        h5ad: "an .h5ad file",
        data: "a table (.csv, .tsv, .parquet or .h5ad)",
        channels: "a channel list (.csv, .tsv, .txt or Excel)",
    };

    //: Which datasets a legacy "any" field selects in one click, by filter.
    //: The rest of the folders open, as every folder does. `true` is all of
    //: them: Import Sample's Browse is where a Xenium run is the whole point.
    const DATASET_FILTERS = {
        image: ["zarr", "spatialdata", "dicom"],
        data: ["zarr", "spatialdata"],
        any: true,
    };

    //: The folders that are a thing rather than a place, by name. Only the
    //: fallback now: /list_dir marks stores itself (`entry.store`).
    const STORE_SUFFIXES = [".zarr", ".ome.zarr"];

    const DATASET_LABELS = {
        zarr: "Zarr store",
        spatialdata: "SpatialData",
        xenium: "Xenium run",
        visium: "Visium run",
        visium_hd: "Visium HD run",
        dicom: "DICOM folder",
    };

    const DATASET_ICONS = {
        zarr: "fa-cubes",
        spatialdata: "fa-layer-group",
        xenium: "fa-dna",
        visium: "fa-dna",
        visium_hd: "fa-dna",
        dicom: "fa-images",
    };

    //: What the Type column says. Decides nothing -- it only puts a word next
    //: to a filename. Longest suffix first, so `.ome.tif` beats `.tif`.
    const TYPE_LABELS = [
        [".ome.tiff", "OME-TIFF"], [".ome.tif", "OME-TIFF"],
        [".qptiff", "QPTIFF"], [".tiff", "TIFF"], [".tif", "TIFF"],
        [".svs", "Aperio SVS"], [".ndpi", "Hamamatsu NDPI"], [".scn", "Leica SCN"],
        [".mrxs", "MIRAX slide"], [".bif", "Ventana BIF"],
        [".dcm", "DICOM"], [".ims", "Imaris"],
        [".png", "PNG"], [".jpeg", "JPEG"], [".jpg", "JPEG"],
        [".h5ad", "AnnData"], [".zarr", "Zarr store"],
        [".csv", "CSV"], [".tsv", "TSV"], [".txt", "Text"],
        [".parquet", "Parquet"],
        [".xlsx", "Excel"], [".xlsm", "Excel"],
    ];

    //: The glyph beside a file, by suffix. Every name here exists in Font
    //: Awesome 7 Free; a dead one draws nothing and says nothing.
    const FILE_ICONS = [
        [[".ome.tiff", ".ome.tif", ".tiff", ".tif", ".svs", ".ndpi", ".scn",
          ".bif", ".qptiff", ".mrxs", ".ims", ".png", ".jpg", ".jpeg", ".dcm"],
         "fa-file-image"],
        [[".csv", ".tsv"], "fa-file-csv"],
        [[".xlsx", ".xlsm", ".xls"], "fa-file-excel"],
        [[".txt", ".md", ".log"], "fa-file-lines"],
        [[".parquet"], "fa-table"],
        [[".h5ad", ".h5", ".hdf5", ".loom"], "fa-database"],
        [[".zip", ".gz", ".tgz", ".bz2", ".xz", ".tar", ".7z"], "fa-file-zipper"],
        [[".json", ".py", ".sh", ".yaml", ".yml", ".toml", ".r", ".ipynb"],
         "fa-file-code"],
    ];

    const collator = (typeof Intl !== "undefined" && Intl.Collator)
        ? new Intl.Collator(undefined, { numeric: true, sensitivity: "base" })
        : { compare: (a, b) => (a < b ? -1 : a > b ? 1 : 0) };

    function lower(name) {
        return String(name || "").toLowerCase();
    }

    function accepts(name, filter) {
        const allowed = EXTENSIONS[filter];
        if (!allowed) return true;
        const lowered = lower(name);
        return allowed.some((suffix) => lowered.endsWith(suffix));
    }

    /** The dataset a directory row is, `{type, label}`, or null. */
    function storeOf(entry) {
        if (!entry || !entry.is_dir) return null;
        if (entry.store && entry.store.type) {
            return {
                type: entry.store.type,
                label: entry.store.label || DATASET_LABELS[entry.store.type] || "Dataset",
            };
        }
        const lowered = lower(entry.name);
        return STORE_SUFFIXES.some((suffix) => lowered.endsWith(suffix))
            ? { type: "zarr", label: DATASET_LABELS.zarr } : null;
    }

    function isStore(entry) {
        return storeOf(entry) !== null;
    }

    /**
     * What a caller asked for, in one shape whichever way it asked.
     *
     * New callers say `accept: {files, directories, datasets}` and
     * `select: "one" | "many"`; the eleven older ones say `mode` and `filter`
     * and `multiple`, which map as they always behaved:
     *
     *   file       files only (filter -> extensions), folders open
     *   directory  the open folder is the answer, files are greyed
     *   any        files, the open folder, and datasets in one click
     *
     * `files` is null (every file), false (none) or a list of suffixes;
     * `datasets` is false, true (every kind) or a list of dataset types.
     */
    function normalizeAccept({ accept = null, select = null, mode = "file",
                               filter = "any", multiple = false } = {}) {
        let files;
        let directories;
        let datasets;
        let words = "";
        if (accept && typeof accept === "object") {
            files = accept.files === undefined ? false
                : accept.files === true ? null
                : Array.isArray(accept.files) ? accept.files.map(lower)
                : accept.files ? null : false;
            directories = Boolean(accept.directories);
            datasets = Array.isArray(accept.datasets) ? accept.datasets.slice()
                : Boolean(accept.datasets);
            if (Array.isArray(files)) words = suffixWords(files);
        } else if (mode === "directory") {
            files = false;
            directories = true;
            datasets = false;
        } else {
            files = EXTENSIONS[filter] || null;
            directories = mode === "any";
            datasets = mode === "any"
                ? (DATASET_FILTERS[filter] !== undefined ? DATASET_FILTERS[filter] : ["zarr"])
                : false;
            words = FILTER_WORDS[filter] || "";
        }
        const many = files !== false
            && (select === "many" || (select === null && Boolean(multiple) && mode === "file"));
        let kind = "any";
        if (files === false && directories && !datasets) kind = "directory";
        else if (!directories && !datasets) kind = "file";
        else if (files === false && !directories) kind = "dataset";
        return { files, directories, datasets, many, kind, words };
    }

    function suffixWords(suffixes) {
        if (!suffixes.length) return "";
        if (suffixes.length === 1) return `a ${suffixes[0]} file`;
        if (suffixes.length <= 4) {
            return `${suffixes.slice(0, -1).join(", ")} or ${suffixes[suffixes.length - 1]} files`;
        }
        return `${suffixes.slice(0, 4).join(", ")}, … files`;
    }

    function datasetAllowed(store, accept) {
        if (!store || !accept.datasets) return false;
        if (accept.datasets === true) return true;
        return accept.datasets.indexOf(store.type) >= 0;
    }

    /**
     * What a click on this row may do, and if nothing, why not.
     *
     * `role` is "file" (selectable), "dataset" (a folder selected in one
     * click, "›" to go in), "folder" (a place: a click goes there) or null.
     * `why` is the sentence the footer shows for a greyed row, so "my file is
     * greyed" never has to be guessed at.
     */
    function usable(entry, accept) {
        if (!entry) return { ok: false, role: null, why: "" };
        if (entry.kind === "broken") {
            return { ok: false, role: null,
                     why: "This link points to something that is no longer there." };
        }
        if (entry.kind === "denied") {
            return { ok: false, role: null,
                     why: "You do not have permission to open this folder." };
        }
        if (entry.is_dir) {
            const store = storeOf(entry);
            if (datasetAllowed(store, accept)) return { ok: true, role: "dataset", why: "" };
            return { ok: true, role: "folder", why: "" };
        }
        if (accept.files === false) {
            return { ok: false, role: null,
                     why: accept.kind === "directory"
                         ? "This asks for a folder: open it, then Use Folder."
                         : "This asks for a folder or dataset, not a file." };
        }
        if (accept.files === null) return { ok: true, role: "file", why: "" };
        const lowered = lower(entry.name);
        if (accept.files.some((suffix) => lowered.endsWith(suffix))) {
            return { ok: true, role: "file", why: "" };
        }
        return { ok: false, role: null,
                 why: accept.words ? `This field takes ${accept.words}.`
                     : "This field does not take this kind of file." };
    }

    /** The word in the Type column. Display only. */
    function typeLabel(entry) {
        const store = storeOf(entry);
        if (store) return store.label;
        const lowered = lower(entry.name);
        if (entry.kind === "broken") return "Broken link";
        if (entry.is_dir) return "Folder";
        const known = TYPE_LABELS.find(([suffix]) => lowered.endsWith(suffix));
        if (known) return known[1];
        const dot = lowered.lastIndexOf(".");
        return dot > 0 ? lowered.slice(dot + 1).toUpperCase() : "File";
    }

    /** The Font Awesome classes for a row's glyph. */
    function iconFor(entry) {
        if (entry.kind === "denied") return "fas fa-lock";
        if (entry.kind === "broken") return "fas fa-link-slash";
        const store = storeOf(entry);
        if (store) return `fas ${DATASET_ICONS[store.type] || "fa-cubes"}`;
        if (entry.is_dir) return "fas fa-folder";
        const lowered = lower(entry.name);
        const found = FILE_ICONS.find(([suffixes]) =>
            suffixes.some((suffix) => lowered.endsWith(suffix)));
        return `fas ${found ? found[1] : "fa-file"}`;
    }

    /** Bytes as something a person reads at a glance. */
    function readableSize(bytes) {
        if (bytes === null || bytes === undefined || bytes === "") return "";
        const units = ["B", "KB", "MB", "GB", "TB", "PB"];
        let value = Number(bytes);
        if (!Number.isFinite(value)) return "";
        let unit = 0;
        while (value >= 1024 && unit < units.length - 1) {
            value /= 1024;
            unit += 1;
        }
        return `${unit === 0 ? value : value.toFixed(1)} ${units[unit]}`;
    }

    const dayFormat = makeFormat({ day: "numeric", month: "short", year: "numeric" });
    const timeFormat = makeFormat({ hour: "2-digit", minute: "2-digit" });

    function makeFormat(options) {
        try {
            return new Intl.DateTimeFormat(undefined, options);
        } catch (error) {
            return null;
        }
    }

    function startOfDay(date) {
        return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
    }

    /**
     * An mtime (epoch seconds) as a column reads it: "Today 14:02",
     * "Yesterday 09:13", "12 Mar 2026". Empty for an unknown time.
     */
    function readableDate(mtime, now = Date.now()) {
        if (mtime === null || mtime === undefined || !Number.isFinite(Number(mtime))) return "";
        const when = new Date(Number(mtime) * 1000);
        const today = startOfDay(new Date(now));
        const day = startOfDay(when);
        const time = timeFormat ? timeFormat.format(when) : when.toTimeString().slice(0, 5);
        if (day === today) return `Today ${time}`;
        if (day === today - 86400000) return `Yesterday ${time}`;
        return dayFormat ? dayFormat.format(when) : when.toDateString();
    }

    /** The same time with the clock, for the footer. */
    function fullDate(mtime) {
        if (mtime === null || mtime === undefined || !Number.isFinite(Number(mtime))) return "";
        const when = new Date(Number(mtime) * 1000);
        const day = dayFormat ? dayFormat.format(when) : when.toDateString();
        const time = timeFormat ? timeFormat.format(when) : when.toTimeString().slice(0, 5);
        return `${day} ${time}`;
    }

    /**
     * The last segment of a path, for a label. Display only: whatever is
     * clicked navigates to the whole path, which came from the server.
     */
    function shortName(path) {
        const parts = String(path).split(/[\\/]/).filter(Boolean);
        return parts.length ? parts[parts.length - 1] : String(path);
    }

    function count(n, one, many) {
        const shown = Number(n).toLocaleString ? Number(n).toLocaleString() : String(n);
        return `${shown} ${n === 1 ? one : many}`;
    }

    // -- drawing helpers -------------------------------------------------------

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function button(className, text, title) {
        const node = el("button", className, text);
        node.setAttribute("type", "button");
        if (title) {
            node.setAttribute("title", title);
            if (!text) node.setAttribute("aria-label", title);
        }
        return node;
    }

    //: Rendered glyphs by class, cloned for every later row. Font Awesome
    //: swaps a `<span class="fas …">` for an <svg> a frame after it lands, so
    //: a virtualized list that redraws its rows on every scroll would flicker
    //: icon-less rows; a ready-made <svg> draws in the same frame.
    const svgCache = new Map();

    function renderedSvg(classes) {
        if (svgCache.has(classes)) return svgCache.get(classes);
        let svg = null;
        const fa = window.FontAwesome;
        const parts = String(classes).split(/\s+/);
        const prefix = parts.find((part) => /^fa[srb]$/.test(part)) || "fas";
        const name = (parts.find((part) => /^fa-/.test(part)) || "").slice(3);
        try {
            if (name && fa && typeof fa.icon === "function"
                    && typeof fa.findIconDefinition === "function") {
                const definition = fa.findIconDefinition({ prefix, iconName: name });
                const rendered = definition ? fa.icon(definition) : null;
                svg = (rendered && rendered.node && rendered.node[0]) || null;
            }
        } catch (error) {
            svg = null;
        }
        svgCache.set(classes, svg);
        return svg;
    }

    /** A decorative glyph: an <svg> when Font Awesome can draw one now. */
    function icon(classes) {
        const svg = renderedSvg(classes);
        if (svg && svg.cloneNode) {
            const copy = svg.cloneNode(true);
            copy.setAttribute("aria-hidden", "true");
            copy.setAttribute("focusable", "false");
            return copy;
        }
        const span = el("span", classes);
        span.setAttribute("aria-hidden", "true");
        return span;
    }

    /**
     * Change the glyph inside `holder`. By replacing it: Font Awesome's swap
     * is one-way, and an <svg>'s className cannot be reassigned.
     */
    function setIcon(holder, classes) {
        if (!holder) return;
        if (holder.getAttribute && holder.getAttribute("data-icon-classes") === classes) return;
        holder.setAttribute("data-icon-classes", classes);
        holder.replaceChildren(icon(classes));
    }

    /** A holder span with its glyph, for setIcon to change later. */
    function glyph(classes, className = "plx-explorer-glyph") {
        const holder = el("span", className);
        holder.setAttribute("aria-hidden", "true");
        setIcon(holder, classes);
        return holder;
    }

    return {
        EXTENSIONS,
        STORE_SUFFIXES,
        DATASET_LABELS,
        collator,
        accepts,
        storeOf,
        isStore,
        normalizeAccept,
        usable,
        typeLabel,
        iconFor,
        readableSize,
        readableDate,
        fullDate,
        shortName,
        count,
        el,
        button,
        icon,
        setIcon,
        glyph,
    };
})();
