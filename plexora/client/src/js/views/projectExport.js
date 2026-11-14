/**
 * projectExport.js -- Export: one project, a selection, or a dataset, as one
 * `.plexora` file (server/models/bundles.py; docs/internal/PROJECT_BUNDLES.md).
 *
 * The dialog is a list of what can go into the file, each with its size, and
 * one button:
 *
 *   Analysis and settings            always (the project's own folder)
 *   Cell tables made in Plexora      on: they exist nowhere else
 *   Bookmarks and figures            on
 *   Images, masks and source tables  OFF: referenced by name, size and
 *                                    fingerprint, and found again where the
 *                                    other computer has them
 *     Smaller images                 off, offered once images go: each 16-bit
 *                                    fluorescence image about 85% smaller,
 *                                    every pixel within its own noise
 *                                    (server/utils/approximate_image.py)
 *   Ready-to-open files              off: a converted table, a tiled image
 *
 * Images are off because they are usually most of the bytes (460 MB of
 * analysis beside a 66.7 GB slide) and usually somewhere both computers can
 * see. The total is an upper bound: the file is written with deflate, and a
 * CSV shrinks a lot while a JPEG-tiled slide does not shrink at all. With
 * smaller images it is "About": those count as the server's estimate. The
 * website never offers them: its page copies members byte for byte, and
 * coding an image would hold its one Python thread for the whole export.
 *
 * In the desktop app the Save dialog is asked BEFORE the export starts
 * (`PlexoraDesktop.pickSavePath`), so the server writes the file once,
 * straight to where it goes -- never staged and copied, which for a
 * multi-gigabyte file is minutes and twice the disk. A browser tab has no
 * such dialog: the server writes the file under its own staging folder and
 * the page downloads it when it is ready.
 *
 * Closing the dialog while the file is being written does not stop it; a
 * notice says when it is done. Cancel stops it, and the half-written file
 * goes.
 *
 * ---------------------------------------------------------------------------
 * THE TRANSPORT: `window.PlexoraBundleTransport`
 *
 * Every request the export and import dialogs make goes through this one
 * object, so the website can move the bytes in a worker of its own instead of
 * through server jobs (its Python has a 4 GiB heap; PROJECT_BUNDLES.md, "The
 * website"). The server implementation below is the default. A replacement
 * assigns the global, or overrides some of its methods, before a dialog
 * opens; the dialogs read it on every call. Everything returns a Promise
 * except `available` and `canReveal`:
 *
 *   available()                         whether the dialogs can run here at all
 *   estimate({ids, dataset, include})   GET /projects/export/estimate's answer
 *   chooseDestination({name})           {dest} to write to (`dest: null` = hand
 *                                       the file over when ready), or null
 *                                       when the user cancelled
 *   startExport({ids, dataset, include, dest})   a job status
 *   status(kind, job)                   a job status; kind "export" | "import"
 *   cancel(kind, job)
 *   deliver(status)                     the finished file to the user:
 *                                       {path} when it was written where they
 *                                       chose, {saved: true, name} when the
 *                                       browser's Save dialog took it (no
 *                                       path to show), {downloaded: true}
 *                                       otherwise
 *   canReveal(), reveal(path)           "Show in folder"
 *   pickBundle()                        {path} | {file: File} | null
 *   preview(source, location, onSent)   POST /projects/import/preview's answer;
 *                                       source {path} | {upload} | {file};
 *                                       onSent(fraction) while a file uploads
 *   startImport({source, choices, location})     a job status
 *   copy(name, location)                a job status, polled as "import"
 *   pickFolder()                        a folder path, or null
 *   findMissing(name, folder)           {found, missing}
 *   locations()                         GET /settings/locations's answer
 *
 * A job status is `{job, kind, status: pending|ready|failed|cancelled,
 * progress, stage, stage_label, error, done, total, result, file?}`, the shape
 * of `bundles.job_status`.
 *
 * `window.PlexoraBundleKit` holds what both dialogs draw with (sizes, the
 * dialog's shell, the job poller); projectImport.js is loaded after this file.
 *
 * Served as a classic script from base.html. The pure parts are on
 * `PlexoraProjectExport._internal` for tests/js/project_bundle_probe.mjs.
 * ---------------------------------------------------------------------------
 */
(function () {
    "use strict";

    // -- small helpers, shared with projectImport.js ----------------------------

    function esc(value) {
        return String(value ?? "").replace(/[&<>"']/g, (c) => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
        }[c]));
    }

    function plural(n, one, many) {
        return `${n} ${n === 1 ? one : (many || one + "s")}`;
    }

    /**
     * A size as Finder writes it: powers of 1000, whole kilobytes, and one
     * decimal below 100 of anything larger (2.6 MB, 66.7 GB, 460 MB).
     */
    function formatBytes(bytes) {
        if (bytes === null || bytes === undefined || bytes === "") return "";
        const n = Number(bytes);
        if (!Number.isFinite(n) || n < 0) return "";
        if (n < 1000) return n === 1 ? "1 byte" : `${Math.round(n)} bytes`;
        const units = ["KB", "MB", "GB", "TB", "PB"];
        let value = n / 1000;
        let unit = 0;
        while (unit < units.length - 1 && value >= (unit === 0 ? 999.5 : 999.95)) {
            value /= 1000;
            unit += 1;
        }
        const text = unit === 0 || value >= 100 ? String(Math.round(value)) : value.toFixed(1);
        return `${text.replace(/\.0$/, "")} ${units[unit]}`;
    }

    function basename(path) {
        const parts = String(path || "").replace(/[\\/]+$/, "").split(/[\\/]/);
        return parts[parts.length - 1] || "";
    }

    function url(path) {
        return typeof plexoraUrl === "function" ? plexoraUrl(path) : `/${String(path).replace(/^\/+/, "")}`;
    }

    function isWebsite() {
        return Boolean(document.documentElement?.classList?.contains("is-website"));
    }

    async function asJson(response, fallback) {
        const answer = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(answer.error || fallback || `The server answered ${response.status}.`);
        return answer;
    }

    function getJson(path, fallback) {
        return fetch(url(path)).then((response) => asJson(response, fallback));
    }

    function postJson(path, body, fallback, method = "POST") {
        return fetch(url(path), {
            method,
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(body || {}),
        }).then((response) => asJson(response, fallback));
    }

    function query(params) {
        const search = new URLSearchParams();
        Object.entries(params).forEach(([key, value]) => {
            if (value === undefined || value === null || value === "") return;
            search.set(key, Array.isArray(value) ? value.join(",") : String(value));
        });
        const text = search.toString();
        return text ? `?${text}` : "";
    }

    /** `{tables: true, images: false}` as the routes take it: the parts on. */
    function includeParam(include) {
        return Object.entries(include || {}).filter(([, on]) => on).map(([key]) => key).join(",") || "none";
    }

    /**
     * A `.plexora` file chosen in a browser, sent with its upload progress.
     * XMLHttpRequest rather than fetch: fetch says nothing until the whole
     * body is sent, and a 2 GB file takes long enough that the dialog has to.
     */
    function upload(path, file, fields, onSent) {
        return new Promise((resolve, reject) => {
            const request = new XMLHttpRequest();
            request.open("POST", url(path + query(fields || {})));
            request.responseType = "json";
            request.upload.addEventListener("progress", (event) => {
                if (event.lengthComputable && onSent) onSent(event.loaded / event.total);
            });
            request.addEventListener("load", () => {
                const answer = request.response || {};
                if (request.status >= 200 && request.status < 300) resolve(answer);
                else reject(new Error(answer.error || `The server answered ${request.status}.`));
            });
            request.addEventListener("error", () => reject(new Error(
                "The file could not be sent to Plexora.")));
            const form = new FormData();
            form.append("file", file, file.name);
            request.send(form);
        });
    }

    /** A browser's own file picker for one `.plexora`, or null. */
    function pickFileInBrowser() {
        return new Promise((resolve) => {
            const input = document.createElement("input");
            input.type = "file";
            input.accept = ".plexora";
            input.style.display = "none";
            let settled = false;
            const finish = (value) => {
                if (settled) return;
                settled = true;
                input.remove();
                resolve(value);
            };
            input.addEventListener("change", () => finish(input.files && input.files[0]
                ? {file: input.files[0]} : null));
            input.addEventListener("cancel", () => finish(null));
            document.body.appendChild(input);
            input.click();
        });
    }

    // -- the default transport: this server's routes ---------------------------

    function createServerTransport() {
        const desktop = () => window.PlexoraDesktop || null;
        return {
            kind: "server",
            /** The website's page answers these routes in a Python with a
             *  4 GiB heap, which is no place for a multi-gigabyte file: there,
             *  the dialogs wait for the website's own transport. */
            available: () => !isWebsite(),
            estimate: ({ids, dataset, include} = {}) => getJson(
                "projects/export/estimate" + query({ids, dataset,
                                                    include: include ? includeParam(include) : null}),
                "The sizes could not be read."),
            async chooseDestination({name} = {}) {
                const app = desktop();
                if (app && typeof app.pickSavePath === "function") {
                    const path = await app.pickSavePath({name, title: "Export project"});
                    return path ? {dest: path} : null;
                }
                // A browser, or an app too old to ask first: the file is
                // fetched from the server once it is written (the app's own
                // download handler then asks where it goes).
                return {dest: null};
            },
            startExport: ({ids, dataset, include, dest} = {}) => postJson("projects/export",
                {ids, dataset: dataset || null, include, dest: dest || null},
                "The export could not start."),
            status: (kind, job) => getJson(`projects/${kind}/${encodeURIComponent(job)}`,
                                           "Plexora lost track of that job."),
            cancel: (kind, job) => postJson(`projects/${kind}/${encodeURIComponent(job)}/cancel`, {},
                                            "That job could not be stopped."),
            async deliver(status) {
                const file = (status && status.file) || {};
                if (file.path) return {path: file.path};
                if (!file.url) throw new Error("The finished file is not there.");
                const link = document.createElement("a");
                link.href = url(file.url);
                link.download = file.name || "project.plexora";
                link.style.display = "none";
                document.body.appendChild(link);
                link.click();
                link.remove();
                return {downloaded: true};
            },
            canReveal: () => Boolean(desktop() && typeof desktop().revealPath === "function"),
            reveal: (path) => desktop().revealPath(path),
            async pickBundle() {
                const app = desktop();
                if (app && typeof app.pickPaths === "function") {
                    const paths = await app.pickPaths({mode: "file", filter: "plexora",
                                                       title: "Import a Plexora project"});
                    return paths && paths.length ? {path: paths[0]} : null;
                }
                return pickFileInBrowser();
            },
            preview(source, location, onSent) {
                if (source && source.file) {
                    return upload("projects/import/preview", source.file, {location}, onSent);
                }
                return postJson("projects/import/preview",
                    {path: source?.path || null, upload: source?.upload || null, location: location || null},
                    "That file could not be read.");
            },
            startImport: ({source, choices, location} = {}) => postJson("projects/import",
                {path: source?.path || null, upload: source?.upload || null,
                 choices: choices || {}, location: location || null},
                "The import could not start."),
            copy: (name, location) => postJson(`projects/${encodeURIComponent(name)}/copy`,
                {location: location || null}, "The copy could not start."),
            async pickFolder() {
                const app = desktop();
                if (app && typeof app.pickPaths === "function") {
                    const paths = await app.pickPaths({mode: "directory",
                                                       title: "Choose the folder that holds the images"});
                    return paths && paths.length ? paths[0] : null;
                }
                // A browser tab of this server: the server's own dialog, on
                // the machine the folder has to be on.
                if (typeof openSystemDialog === "function") {
                    return openSystemDialog({kind: "directory"});
                }
                return null;
            },
            findMissing: (name, folder) => postJson(`projects/${encodeURIComponent(name)}/find_missing`,
                {folder}, "That folder could not be searched."),
            locations: () => getJson("settings/locations", "Your folders could not be listed."),
        };
    }

    window.PlexoraBundleTransport = window.PlexoraBundleTransport || createServerTransport();

    function transport() {
        return window.PlexoraBundleTransport;
    }

    // -- polling a job ---------------------------------------------------------

    const POLL_MS = 450;
    const TERMINAL = new Set(["ready", "failed", "cancelled"]);

    /**
     * Ask after a job until it ends. `onStatus(status)` on every answer;
     * resolves the last one. Two failed polls in a row are a server that
     * went away, not a job that did.
     */
    function watch(kind, first, onStatus) {
        let stopped = false;
        let timer = null;
        const done = new Promise((resolve, reject) => {
            let misses = 0;
            const step = async (status) => {
                if (stopped) return;
                onStatus?.(status);
                if (TERMINAL.has(status.status)) {
                    resolve(status);
                    return;
                }
                timer = window.setTimeout(async () => {
                    if (stopped) return;
                    try {
                        const next = await transport().status(kind, status.job);
                        misses = 0;
                        step(next);
                    } catch (error) {
                        misses += 1;
                        if (misses >= 3) reject(error);
                        else step(status);
                    }
                }, POLL_MS);
            };
            step(first);
        });
        return {
            done,
            stop() {
                stopped = true;
                window.clearTimeout(timer);
            },
        };
    }

    /** The detail line under a job's bar: what it is doing, and how far. */
    function progressDetail(status) {
        const label = status.stage_label || "Starting";
        if (status.total > 0) {
            return `${label} · ${formatBytes(status.done)} of ${formatBytes(status.total)}`;
        }
        return label;
    }

    // -- the dialog's shell ------------------------------------------------------

    function portal(verb, node) {
        if (typeof PopoverPortal !== "undefined") {
            PopoverPortal[verb](node);
            return;
        }
        if (verb === "attach") document.body.appendChild(node);
        else node.remove();
    }

    /**
     * A `.plx-dialog .plx-import .plx-bundle` with a title, a subtitle, a body
     * that scrolls, a pinned strip under it that does not (where an import
     * goes stays in view however many projects there are), a status line and
     * a foot. The X in the head is the only way out; Esc goes through
     * `onClose` too.
     */
    function shell({extraClass, onClose}) {
        const node = document.createElement("dialog");
        node.className = `plx-dialog plx-import plx-bundle ${extraClass || ""}`.trim();
        node.innerHTML = `
            <div class="plx-import-head">
                <div class="plx-import-head-text">
                    <h2 class="plx-dialog-title" data-role="title"></h2>
                    <p class="plx-import-subtitle" data-role="subtitle"></p>
                </div>
                <div class="plx-import-head-actions">
                    <button class="plx-picker-close" type="button" data-role="close"
                            title="Close" aria-label="Close">
                        <span class="fas fa-xmark" aria-hidden="true"></span>
                    </button>
                </div>
            </div>
            <div class="plx-import-body plx-bundle-body" data-role="body"></div>
            <div class="plx-bundle-pinned" data-role="pinned"></div>
            <p class="plx-import-status" data-role="status" role="status" aria-live="polite"></p>
            <div class="plx-import-foot plx-bundle-foot" data-role="foot"></div>`;
        node.querySelector('[data-role="close"]').addEventListener("click", () => onClose());
        node.addEventListener("cancel", (event) => {
            event.preventDefault();
            onClose();
        });
        portal("attach", node);
        return {
            node,
            part: (role) => node.querySelector(`[data-role="${role}"]`),
            setStatus(message, isError) {
                const status = node.querySelector('[data-role="status"]');
                status.textContent = message || "";
                status.classList.toggle("is-error", Boolean(message) && Boolean(isError));
            },
            remove() {
                try {
                    node.close();
                } catch (error) { /* already closed */ }
                portal("detach", node);
            },
        };
    }

    /**
     * The running job's panel: jobProgress.js's bar and stage rail, inside the
     * dialog body. Null when that component is not on the page (a test).
     */
    function progressPanel(mount, {title, stages}) {
        if (!window.PlexoraJobProgress) return null;
        const panel = window.PlexoraJobProgress.create({title, detail: "Starting", stages, mount});
        panel.element.classList.add("plx-bundle-progress");
        return panel;
    }

    function showProgress(panel, status) {
        if (!panel) return;
        const percent = status.status === "pending" && !status.progress && !status.total
            ? null : Number(status.progress || 0);
        panel.setProgress(percent, progressDetail(status), status.stage || null);
    }

    function toast(options) {
        window.PlexoraToast?.show?.(options);
    }

    window.PlexoraBundleKit = {
        esc, plural, formatBytes, basename, url, transport, watch, shell, progressPanel,
        showProgress, progressDetail, toast, isWebsite,
    };

    // ===========================================================================
    // The export dialog
    // ===========================================================================

    const EXPORT_STAGES = [
        {key: "snapshot", label: "Taking a snapshot"},
        {key: "pack", label: "Packing"},
        {key: "verify", label: "Checking the file"},
    ];

    //: What each part of the file is called, in the order the dialog lists
    //: them. `parts` names the estimate rows (and `include` keys) a line
    //: stands for: bookmarks and figures are two parts and one choice.
    const ROWS = [
        {key: "state", parts: ["state"], title: "Analysis and settings",
         note: "Channels, gates, regions, QC and the project's own settings. Always included.",
         locked: true},
        {key: "tables", parts: ["tables"], title: "Cell tables made in Plexora",
         note: "Edited copies of cell tables. They exist only in this project."},
        {key: "extras", parts: ["captures", "figures"], title: "Bookmarks and figures",
         note: "Bookmarks of these samples, and figures made only from them."},
        {key: "images", parts: ["images"], title: "Images, masks and source tables",
         note: "Leave this off when the other person can open the same files, for example on a shared drive.",
         list: true},
        {key: "derived", parts: ["derived"], title: "Ready-to-open files",
         note: "Saves rebuilding them on the other computer.", list: true},
    ];

    //: Where a referenced file is, as the expanded list says it.
    const WHERE = {
        here: {label: "This computer", icon: "laptop", tone: "here"},
        shared: {label: "Shared drive", icon: "network-wired", tone: "shared"},
        node: {label: "Data node", icon: "cloud", tone: "node"},
        remote: {label: "Remote", icon: "globe", tone: "node"},
        missing: {label: "Missing", icon: "triangle-exclamation", tone: "missing"},
    };

    //: Under the images once they go: each 16-bit fluorescence image within
    //: its own noise (server/utils/approximate_image.py).
    const SMALLER = {title: "Smaller images",
                     note: "About 85% smaller, every pixel within its own noise. Masks and tables stay exact."};

    /** The parts a fresh dialog starts with: the server's defaults, which
     *  are tables, bookmarks and figures on, images, derived files and
     *  smaller images off. */
    function defaultInclude(estimate) {
        const given = (estimate && estimate.include) || {};
        return {
            tables: given.tables !== undefined ? Boolean(given.tables) : true,
            captures: given.captures !== undefined ? Boolean(given.captures) : true,
            figures: given.figures !== undefined ? Boolean(given.figures) : true,
            images: Boolean(given.images),
            derived: Boolean(given.derived),
            approximate: Boolean(given.approximate),
        };
    }

    function rowOf(estimate, part) {
        return ((estimate && estimate.rows) || {})[part] || {size: 0, count: 0};
    }

    /**
     * The row "Smaller images" is offered under: Images (else Ready-to-open
     * files) once it is ticked and holds an image that can go smaller -- or
     * null. Never on the website, whose Python cannot make them.
     */
    function smallerRow(estimate, include) {
        const offer = estimate && estimate.approximate;
        if (!offer || !offer.available || isWebsite()) return null;
        for (const part of ["images", "derived"]) {
            if (!(include && include[part])) continue;
            const items = rowOf(estimate, part).items || [];
            if (items.some((item) => item.approximate && item.can_include)) return part;
        }
        return null;
    }

    /** Whether the ticked images go smaller. */
    function goesSmaller(estimate, include) {
        return Boolean(include && include.approximate && smallerRow(estimate, include));
    }

    /** What a part comes to as it would go: images going smaller count as
     *  the server's estimate of them. */
    function partSize(estimate, part, include) {
        const row = rowOf(estimate, part);
        if (goesSmaller(estimate, include) && row.approximate_size != null) return Number(row.approximate_size || 0);
        return Number(row.size || 0);
    }

    /**
     * The lines the dialog draws for an estimate, given what is ticked:
     * `[{key, title, note, size, count, on, locked, disabled, items, hint}]`.
     * A line with nothing in it is left out (Analysis and settings excepted),
     * so a project with no edited table is not asked about one.
     */
    function rowsFor(estimate, include) {
        const out = [];
        const smallerUnder = smallerRow(estimate, include);
        const smaller = goesSmaller(estimate, include);
        for (const spec of ROWS) {
            const parts = spec.parts.map((part) => rowOf(estimate, part));
            const size = spec.parts.reduce((sum, part) => sum + partSize(estimate, part, include), 0);
            const count = parts.reduce((sum, row) => sum + Number(row.count || 0), 0);
            const items = spec.list ? (parts[0].items || []) : [];
            if (!spec.locked && !count && !items.length) continue;
            const includable = items.filter((item) => item.can_include);
            const disabled = Boolean(spec.locked) || (spec.list && !includable.length);
            let note = spec.note;
            if (spec.list && !includable.length) note = stayNote(items);
            out.push({
                key: spec.key, parts: spec.parts, title: spec.title, note, size, count, items,
                locked: Boolean(spec.locked), disabled,
                on: spec.locked ? true : (!disabled && spec.parts.every((part) => include[part])),
                hint: spec.key === "derived" ? derivedNote(estimate, include) : "",
                smaller: spec.key === smallerUnder ? {on: smaller} : null,
                // Its files say what they come to, "≈" where they go smaller.
                approximate: smaller,
            });
        }
        return out;
    }

    /** Why a list of files cannot go into the file: where they are. */
    function stayNote(items) {
        const nodes = Array.from(new Set(items.filter((item) => item.where === "node")
            .map((item) => item.node).filter(Boolean)));
        if (nodes.length && items.every((item) => item.where === "node" || item.where === "remote")) {
            return `These stay on ${nodes.join(", ")} and open when the other computer connects to it.`;
        }
        if (items.every((item) => item.where === "missing")) {
            return "Plexora cannot find these on this computer, so they cannot go into the file.";
        }
        return "None of these is on this computer, so they stay where they are.";
    }

    /**
     * The warning under Ready-to-open files when it is off and some of them
     * are on this computer alone: left out, they travel as references, and
     * nothing on the other computer makes them again by itself.
     */
    function derivedNote(estimate, include) {
        if (include && include.derived) return "";
        const items = rowOf(estimate, "derived").items || [];
        const alone = items.filter((item) => item.where === "here").length;
        if (!alone) return "";
        return alone === 1
            ? "One of these is only on this computer. The other computer does not rebuild it by itself."
            : `${alone} of these are only on this computer. The other computer does not rebuild them by themselves.`;
    }

    /** What the ticked parts add up to: an upper bound, before compression
     *  (images going smaller count as the server's estimate of them). */
    function totalFor(estimate, include) {
        let total = Number(rowOf(estimate, "state").size || 0);
        for (const part of ["tables", "captures", "figures", "images", "derived"]) {
            if (include[part]) total += partSize(estimate, part, include);
        }
        return total;
    }

    /** "Export lsp11385_2", "Export 3 projects", "Export dataset NLU". */
    function titleFor({names, dataset, datasetName, estimate}) {
        if (dataset) return `Export dataset ${datasetName || dataset}`;
        const projects = (estimate && estimate.projects) || [];
        const count = projects.length || (names || []).length;
        if (count === 1) {
            const one = projects[0];
            return `Export ${(one && (one.title || one.name)) || names[0]}`;
        }
        return `Export ${plural(count, "project")}`;
    }

    /** The file's proposed name, as the server would name it. */
    function suggestedName({names, dataset, datasetName, estimate}) {
        const projects = (estimate && estimate.projects) || [];
        let base;
        if (dataset) base = datasetName || dataset;
        else if ((projects.length || (names || []).length) === 1) base = (projects[0] && projects[0].name) || names[0];
        else base = `plexora-projects-${new Date().toISOString().slice(0, 10)}`;
        const safe = String(base).replace(/[\\/:*?"<>|\u0000-\u001f]/g, "_").trim().replace(/^\.+|\.+$/g, "");
        return `${safe || "project"}.plexora`;
    }

    function fileKind(item) {
        const role = String(item.role || "").toLowerCase();
        const name = String(item.name || "").toLowerCase();
        if (/segment|mask|label/.test(role)) return {icon: "shapes", word: "Mask"};
        if (/\.(csv|tsv|txt|parquet|h5ad|feather|arrow|xlsx)$/.test(name) || /dataset|table/.test(role)) {
            return {icon: "table", word: "Table"};
        }
        if (item.kind === "folder") return {icon: "folder", word: "Folder"};
        return {icon: "image", word: "Image"};
    }

    function whereChip(item) {
        const where = WHERE[item.where] || WHERE.missing;
        let label = where.label;
        if (item.where === "node" && item.node) label = `${item.node} · stays there`;
        else if (item.where === "remote") label = "Remote · stays there";
        return `<span class="plx-bundle-where is-${where.tone}"><span class="fas fa-${where.icon}"
                    aria-hidden="true"></span>${esc(label)}</span>`;
    }

    let current = null;

    /**
     * Open the export dialog.
     *
     * @param names project names (or ids)
     * @param dataset a dataset's id: every project in it, with its membership
     * @param datasetName what to call the dataset in the title
     */
    function open(options) {
        options = options || {};
        if (current) close();
        const names = (options.names || options.ids || []).filter(Boolean);
        const ui = {
            names, dataset: options.dataset || null, datasetName: options.datasetName || null,
            estimate: null, include: null, expanded: new Set(), phase: "loading",
            job: null, watcher: null, status: null, background: false, delivered: null,
        };
        ui.shell = shell({extraClass: "plx-bundle-export", onClose: close});
        current = ui;
        ui.shell.node.showModal();
        ui.shell.part("body").addEventListener("change", (event) => onChange(ui, event));
        ui.shell.part("body").addEventListener("click", (event) => onBodyClick(ui, event));
        ui.shell.part("foot").addEventListener("click", (event) => onFootClick(ui, event));
        void load(ui);
        return ui.shell.node;
    }

    function close() {
        const ui = current;
        if (!ui) return;
        current = null;
        if (ui.phase === "running" && ui.watcher) {
            // Closing is not cancelling: the file is still being written.
            // Say so once it is done, wherever the user is by then.
            ui.background = true;
        } else {
            ui.watcher?.stop();
        }
        ui.shell.remove();
    }

    async function load(ui) {
        setPhase(ui, "loading");
        renderLoading(ui);
        try {
            ui.estimate = await transport().estimate({ids: ui.names, dataset: ui.dataset});
        } catch (error) {
            if (current !== ui) return;
            renderFailed(ui, error.message || String(error), "load");
            return;
        }
        if (current !== ui) return;
        ui.include = ui.include || defaultInclude(ui.estimate);
        renderChoose(ui);
    }

    function setPhase(ui, phase) {
        ui.phase = phase;
        ui.shell.node.dataset.phase = phase === "running" ? "importing" : phase;
    }

    function head(ui, subtitle) {
        ui.shell.part("title").textContent = titleFor({...ui, estimate: ui.estimate});
        ui.shell.part("subtitle").textContent = subtitle || "";
    }

    function renderLoading(ui) {
        head(ui, "Working out what goes into the file…");
        ui.shell.part("body").innerHTML = `<div class="plx-bundle-rows is-loading" aria-hidden="true">
            ${[0, 1, 2].map(() => `<div class="plx-bundle-row plx-bundle-skeleton">
                <span class="plx-bundle-skel plx-bundle-skel-box"></span>
                <span class="plx-bundle-skel-lines"><span class="plx-bundle-skel"></span>
                    <span class="plx-bundle-skel is-short"></span></span>
                <span class="plx-bundle-skel is-size"></span></div>`).join("")}
        </div>`;
        ui.shell.part("foot").innerHTML = `<span class="plx-bundle-total"></span>
            <button class="plx-button plx-button-primary" type="button" disabled>Export…</button>`;
        ui.shell.setStatus("");
    }

    function subtitleFor(ui) {
        const projects = (ui.estimate && ui.estimate.projects) || [];
        const datasets = (ui.estimate && ui.estimate.datasets) || [];
        if (ui.dataset) {
            return `${plural(projects.length, "sample")} in one .plexora file, with the dataset they make up.`;
        }
        if (projects.length > 1) {
            return `${plural(projects.length, "sample")} in one .plexora file`
                + (datasets.length ? `, with ${datasets.length === 1 ? "their dataset" : "their datasets"}.` : ".");
        }
        return "One .plexora file, to open in Plexora on another computer.";
    }

    function rowMarkup(row, ui) {
        const many = ((ui.estimate && ui.estimate.projects) || []).length > 1;
        const expandable = row.items.length > 0;
        const open = ui.expanded.has(row.key);
        const id = `plx-bundle-row-${row.key}`;
        const sizeText = row.disabled && !row.locked ? "" : formatBytes(row.size);
        const files = expandable ? `
            <ul class="plx-bundle-files" id="${id}-files" ${open ? "" : "hidden"}>
                ${row.items.map((item) => {
                    const kind = fileKind(item);
                    const sub = [kind.word, many ? item.project : null].filter(Boolean).join(" · ");
                    return `<li class="plx-bundle-file${item.can_include ? "" : " is-staying"}"
                                title="${esc(item.path || item.name || "")}">
                        <span class="plx-bundle-file-icon fas fa-${kind.icon}" aria-hidden="true"></span>
                        <span class="plx-bundle-file-text">
                            <span class="plx-bundle-file-name">${esc(item.name || "Unnamed file")}</span>
                            <span class="plx-bundle-file-sub">${esc(sub)}</span>
                        </span>
                        ${whereChip(item)}
                        <span class="plx-bundle-file-size">${esc(fileSize(item, row.approximate))}</span>
                    </li>`;
                }).join("")}
            </ul>` : "";
        const toggle = expandable ? `<button type="button" class="plx-bundle-expand" data-expand="${row.key}"
                aria-expanded="${open ? "true" : "false"}" aria-controls="${id}-files">
                ${esc(plural(row.items.length, "file"))}
                <span class="fas fa-chevron-down" aria-hidden="true"></span></button>` : "";
        const smaller = row.smaller ? `
            <label class="plx-bundle-option" for="plx-bundle-approximate">
                <input type="checkbox" class="plx-bundle-tick" id="plx-bundle-approximate" data-include="approximate"
                       ${row.smaller.on ? "checked" : ""}>
                <span class="plx-bundle-row-text">
                    <span class="plx-bundle-option-title">${esc(SMALLER.title)}</span>
                    <span class="plx-bundle-row-note">${esc(SMALLER.note)}</span>
                </span>
            </label>` : "";
        return `<div class="plx-bundle-row${row.on ? " is-on" : ""}${row.disabled ? " is-disabled" : ""}${
                    row.locked ? " is-locked" : ""}" data-row="${row.key}">
            <label class="plx-bundle-row-main" for="${id}">
                <input type="checkbox" class="plx-bundle-tick" id="${id}" data-include="${row.key}"
                       ${row.on ? "checked" : ""} ${row.disabled ? "disabled" : ""}>
                <span class="plx-bundle-row-text">
                    <span class="plx-bundle-row-title">${esc(row.title)}</span>
                    <span class="plx-bundle-row-note">${esc(row.note)}</span>
                    ${row.hint ? `<span class="plx-bundle-row-hint"><span class="fas fa-circle-info"
                        aria-hidden="true"></span>${esc(row.hint)}</span>` : ""}
                </span>
            </label>
            <span class="plx-bundle-row-side">
                <span class="plx-bundle-size">${esc(sizeText)}</span>
                ${toggle}
            </span>
            ${smaller}
            ${files}
        </div>`;
    }

    /** A listed file's size as it would go: "≈ 2.1 GB" when it goes smaller. */
    function fileSize(item, smaller) {
        if (!item.can_include || item.size == null) return "";
        if (smaller && item.approximate && item.approximate_size != null) return `≈ ${formatBytes(item.approximate_size)}`;
        return formatBytes(item.size);
    }

    function renderChoose(ui) {
        setPhase(ui, "choose");
        head(ui, subtitleFor(ui));
        const rows = rowsFor(ui.estimate, ui.include);
        ui.shell.part("body").innerHTML = `<div class="plx-bundle-rows" role="group"
                aria-label="What goes into the file">${rows.map((row) => rowMarkup(row, ui)).join("")}</div>`;
        paintFoot(ui);
    }

    function paintFoot(ui) {
        const total = totalFor(ui.estimate, ui.include);
        // Smaller images are an estimate, not a bound.
        const label = goesSmaller(ui.estimate, ui.include) ? "About" : "Up to";
        ui.shell.part("foot").innerHTML = `<span class="plx-bundle-total"
                title="Before compression. Tables and uncompressed images usually shrink a lot.">
                <span class="plx-bundle-total-label">${label}</span>
                <span class="plx-bundle-total-size">${esc(formatBytes(total))}</span></span>
            <button class="plx-button plx-button-primary" type="button" data-act="export">Export…</button>`;
    }

    function onChange(ui, event) {
        const input = event.target.closest?.("[data-include]");
        if (!input || ui.phase !== "choose") return;
        const key = input.dataset.include;
        if (key === "approximate") {
            ui.include.approximate = input.checked;
        } else {
            const spec = ROWS.find((row) => row.key === key);
            if (!spec || spec.locked) return;
            spec.parts.forEach((part) => { ui.include[part] = input.checked; });
        }
        // Redrawn whole: the derived note, the smaller-images choice and the
        // sizes all follow the tick.
        const scroll = ui.shell.part("body").scrollTop;
        renderChoose(ui);
        ui.shell.part("body").scrollTop = scroll;
        ui.shell.node.querySelector(`[data-include="${key}"]`)?.focus();
    }

    function onBodyClick(ui, event) {
        const toggle = event.target.closest?.("[data-expand]");
        if (toggle) {
            event.preventDefault();
            const key = toggle.dataset.expand;
            if (ui.expanded.has(key)) ui.expanded.delete(key);
            else ui.expanded.add(key);
            const list = ui.shell.node.querySelector(`#plx-bundle-row-${key}-files`);
            const open = ui.expanded.has(key);
            if (list) list.hidden = !open;
            toggle.setAttribute("aria-expanded", open ? "true" : "false");
            return;
        }
        const reveal = event.target.closest?.("[data-act='reveal']");
        if (reveal) void revealFile(ui);
    }

    function onFootClick(ui, event) {
        const button = event.target.closest?.("[data-act]");
        if (!button || button.disabled) return;
        const act = button.dataset.act;
        if (act === "export") void start(ui);
        else if (act === "cancel") void cancelJob(ui, button);
        else if (act === "reveal") void revealFile(ui);
        else if (act === "again") renderChoose(ui);
        else if (act === "retry") void load(ui);
        else if (act === "download") void deliver(ui);
    }

    async function start(ui) {
        const button = ui.shell.part("foot").querySelector("[data-act='export']");
        if (button) button.disabled = true;
        ui.shell.setStatus("");
        let destination;
        try {
            destination = await transport().chooseDestination({name: suggestedName({...ui, estimate: ui.estimate})});
        } catch (error) {
            if (current !== ui) return;
            ui.shell.setStatus(error.message || String(error), true);
            if (button) button.disabled = false;
            return;
        }
        if (current !== ui) return;
        if (!destination) {
            // The Save dialog was cancelled: nothing has started.
            if (button) button.disabled = false;
            return;
        }
        let first;
        try {
            first = await transport().startExport({ids: ui.names, dataset: ui.dataset,
                                                   include: ui.include, dest: destination.dest});
        } catch (error) {
            if (current !== ui) return;
            ui.shell.setStatus(error.message || String(error), true);
            if (button) button.disabled = false;
            return;
        }
        if (current !== ui) return;
        run(ui, first, destination.dest);
    }

    function run(ui, first, dest) {
        setPhase(ui, "running");
        ui.job = first.job;
        const name = (first.file && first.file.name) || suggestedName({...ui, estimate: ui.estimate});
        // The panel names the file; the line above it says where it goes.
        const folder = dest ? String(dest).replace(/[\\/][^\\/]*$/, "") : "";
        head(ui, folder ? `Saving to ${folder}` : "It downloads when it is ready.");
        const body = ui.shell.part("body");
        body.innerHTML = "";
        ui.panel = progressPanel(body, {title: name, stages: EXPORT_STAGES});
        ui.shell.part("foot").innerHTML = `<span class="plx-bundle-foot-note">Closing this keeps the export going.</span>
            <button class="plx-button" type="button" data-act="cancel">Cancel</button>`;
        ui.watcher = watch("export", first, (status) => {
            ui.status = status;
            if (current === ui) showProgress(ui.panel, status);
        });
        ui.watcher.done.then((status) => finished(ui, status), (error) => {
            if (current === ui) renderFailed(ui, error.message || String(error), "again");
            else if (ui.background) toast({title: "Export stopped", note: error.message || String(error),
                                           tone: "warning"});
        });
    }

    async function cancelJob(ui, button) {
        if (!ui.job) return;
        button.disabled = true;
        button.textContent = "Stopping…";
        try {
            await transport().cancel("export", ui.job);
        } catch (error) {
            button.disabled = false;
            button.textContent = "Cancel";
            ui.shell.setStatus(error.message || String(error), true);
        }
        // The poller sees `cancelled` and ends; `finished` puts the choices back.
    }

    async function finished(ui, status) {
        ui.status = status;
        if (status.status === "cancelled") {
            if (current !== ui) return;
            renderChoose(ui);
            ui.shell.setStatus("Export stopped. Nothing was saved.");
            return;
        }
        if (status.status === "failed") {
            if (current === ui) renderFailed(ui, status.error || "The export did not finish.", "again");
            else toast({title: "Export failed", note: status.error || "", tone: "warning"});
            return;
        }
        let delivered = null;
        try {
            delivered = await transport().deliver(status);
        } catch (error) {
            if (current === ui) renderFailed(ui, error.message || String(error), "download");
            return;
        }
        ui.delivered = delivered;
        const file = status.file || {};
        const size = file.size || (status.result && status.result.size);
        if (current === ui) {
            renderDone(ui, file, size, delivered);
            return;
        }
        // Finished behind a closed dialog: the notice is the only place left
        // to say so, and to reach the file from.
        const actions = delivered && delivered.path && transport().canReveal()
            ? [{label: revealLabel(), onSelect: () => { void transport().reveal(delivered.path); }}] : [];
        toast({title: `Saved ${file.name || "the project"}${size ? ` (${formatBytes(size)})` : ""}`,
               note: delivered && delivered.path ? delivered.path : "", actions, timeout: 12000});
        window.PlexoraDesktop?.notifyIfAway?.({title: "Export saved", body: file.name || ""});
    }

    function revealLabel() {
        return window.PlexoraDesktop && window.PlexoraDesktop.platform === "macos" ? "Show in Finder" : "Show in folder";
    }

    function renderDone(ui, file, size, delivered) {
        setPhase(ui, "done");
        head(ui, "");
        const hasPath = Boolean(delivered && delivered.path);
        const saved = hasPath || Boolean(delivered && delivered.saved);
        // The folder, under the name the line above already gives.
        const where = hasPath ? String(delivered.path).replace(/[\\/][^\\/]*$/, "") || delivered.path : "";
        ui.shell.part("body").innerHTML = `<div class="plx-bundle-done">
            <span class="plx-bundle-done-mark" aria-hidden="true"><span class="fas fa-check"></span></span>
            <div class="plx-bundle-done-text">
                <p class="plx-bundle-done-title">${saved ? "Saved" : "Downloaded"}
                    <strong>${esc(file.name || (delivered && delivered.name) || "the project")}</strong>${size
                    ? ` <span class="plx-bundle-done-size">(${esc(formatBytes(size))})</span>` : ""}</p>
                ${hasPath ? `<p class="plx-bundle-done-path" title="${esc(delivered.path)}">${esc(where)}</p>`
                        : saved ? "" : `<p class="plx-bundle-done-path is-prose">Your browser keeps it in its downloads.</p>`}
                ${(((ui.status && ui.status.result) || {}).left_out || []).length
                    ? `<p class="plx-bundle-done-note">${esc(plural(ui.status.result.left_out.length, "file"))}
                        could not be read and ${ui.status.result.left_out.length === 1 ? "was" : "were"} left out.</p>`
                    : ""}
            </div>
        </div>`;
        const foot = hasPath && transport().canReveal()
            ? `<button class="plx-button plx-button-primary" type="button" data-act="reveal">${revealLabel()}</button>`
            : (!saved ? `<button class="plx-button" type="button" data-act="download">Download again</button>` : "");
        ui.shell.part("foot").innerHTML = foot;
        ui.shell.setStatus("");
    }

    async function revealFile(ui) {
        const path = ui.delivered && ui.delivered.path;
        if (!path) return;
        try {
            await transport().reveal(path);
        } catch (error) {
            ui.shell.setStatus(error.message || String(error), true);
        }
    }

    async function deliver(ui) {
        if (!ui.status) return;
        try {
            await transport().deliver(ui.status);
        } catch (error) {
            ui.shell.setStatus(error.message || String(error), true);
        }
    }

    function renderFailed(ui, message, act) {
        setPhase(ui, "failed");
        ui.shell.part("body").innerHTML = `<div class="plx-bundle-failed">
            <span class="plx-bundle-failed-mark fas fa-circle-exclamation" aria-hidden="true"></span>
            <p>${esc(message)}</p>
        </div>`;
        const label = act === "download" ? "Download again" : "Try again";
        ui.shell.part("foot").innerHTML = `<button class="plx-button plx-button-primary" type="button"
            data-act="${act === "load" ? "retry" : act}">${label}</button>`;
    }

    // -- where Export is offered -------------------------------------------------

    /** The navbar's File > Export Project…, for the project on screen. The
     *  row is a link to the old one-step download, so a page without this
     *  file -- or the website, until its transport is in -- still has one. */
    document.addEventListener("click", (event) => {
        const row = event.target.closest?.("#nav_export_project");
        if (!row || row.classList.contains("disabled") || row.disabled) return;
        if (!transport().available()) return;
        const project = window.flaskVariables && window.flaskVariables.datasource;
        if (!project) return;
        event.preventDefault();
        open({names: [project]});
    });

    window.PlexoraProjectExport = {
        open, close,
        available: () => Boolean(transport() && transport().available()),
        _internal: {formatBytes, defaultInclude, rowsFor, totalFor, derivedNote, titleFor, suggestedName,
                    stayNote, fileKind, includeParam, createServerTransport, progressDetail, smallerRow,
                    fileSize},
    };
})();
